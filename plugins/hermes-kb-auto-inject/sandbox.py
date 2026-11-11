"""execute_code 沙箱侧的两处补齐：门禁作用域标记 + stub 形参容错。

为什么单独一个模块：这两件事都作用在 core 的 execute_code 链路上（既不是
`ToolRegistry`，也不是注入链）。混进 `guard.py` 会让那份「schema 注入 +
dispatch 校验」读起来更绕；放这里，`guard.py` 只需要一句 `in_sandbox_rpc()`。

背景：`hermes_tools` 的 RPC stub 把脚本里的 `terminal(...)` 原样转回宿主进程，
最终仍然落到 `ToolRegistry.dispatch` —— 也就是本插件的 kb_query 门禁。但这两类
调用不是一回事：模型直接发起的那条是决策，值得拿知识库拦一次；脚本内部的子调用
只是实现细节，模型只写了外层那一条 `execute_code`（它已经带过 kb_query）。

而且内层这条路原本根本走不通：`_TOOL_STUBS`（tools/code_execution_tool.py:122）
是硬编码签名表，六个包装器一个都不收 `kb_query` —— 传了 `TypeError`，不传被门禁
拦，两条都是死路。就算放行并把标题清单拼回返回值，`_call()` 还要 `json.loads`
它，脚本照样解析失败。
"""

from .plugin_common import logger, sandbox_rpc_scope

# 原方法 / 原签名表各只记一次，挂在 core 模块属性上。插件热重载换的是插件自己的
# 模块对象，core 模块对象不变，所以记下来的仍是真·原方法，重装不会套娃。
_RPC_ORIG_ATTR = "_kb_guard_orig_handle_rpc_request"
_STUB_ORIG_ATTR = "_kb_guard_orig_tool_stubs"

_KB_PARAM = "kb_query: str = None"


def _accept_kb_query(sig: str) -> str:
    """给 stub 签名补一个可选形参（排在最后，不动原有默认值）。"""
    return f"{sig}, {_KB_PARAM}"


def _forward_kb_query(args_expr: str) -> str:
    """在 stub 的 args 字面量里补一个 `kb_query` 键。

    值可能是 None —— dispatch 侧的 `_STRIP_KEYS` 会把它剥掉，底层工具看不到。
    """
    body = args_expr.strip()
    inner = body[1:-1].strip() if body.startswith("{") and body.endswith("}") else ""
    return '{"kb_query": kb_query' + (", " + inner if inner else "") + "}"


def install_sandbox_rpc_scope() -> None:
    """RPC 处理期间打上门禁作用域标记，dispatch 见到它就整段跳过门禁。

    打的是 `code_execution_rpc._handle_rpc_request` —— UDS 与 file 两种 transport
    都从这一个入口进 dispatch，一处覆盖本地沙箱与远端沙箱（含 per-call 与 session
    kernel 两条路径）。

    必须用 threading.local 而不是 ContextVar：session kernel 的 `CellAuthority`
    会 `ctx.run()` 到 cell 启动时复制的那个上下文里，派发期间设的 ContextVar
    到不了那一层，而 `ctx.run()` 不换线程。

    幂等按「每次都从记下的原方法重新包一层」实现，不是「装过就跳过」：热重载
    换的是插件自己的模块对象，跳过会让上一版的闭包（含旧逻辑）继续在线上跑。
    """
    try:
        from tools import code_execution_rpc
    except Exception as exc:  # noqa: BLE001 - core 换模块也不该让插件装载失败
        logger.warning("[KBAutoInject] code_execution_rpc import failed: %s", exc)
        return
    original = getattr(code_execution_rpc, _RPC_ORIG_ATTR, None)
    if original is None:
        original = code_execution_rpc._handle_rpc_request
        setattr(code_execution_rpc, _RPC_ORIG_ATTR, original)

    def _marked_handle_rpc_request(*args, **kwargs):
        with sandbox_rpc_scope():
            return original(*args, **kwargs)

    _marked_handle_rpc_request._kb_guard_wrapper = True
    code_execution_rpc._handle_rpc_request = _marked_handle_rpc_request


def install_stub_kb_query_tolerance() -> None:
    """让**之后新建**的 kernel 收下 `kb_query`（容错，不是门禁通道）。

    内层门禁已经整段跳过（见 `install_sandbox_rpc_scope`），所以这里收下它只是
    为了让「照协议写 kb_query 的模型」不撞 `TypeError`：模型完全可能在脚本里照抄
    `terminal("...", kb_query="...")`。值原样转发进 args，dispatch 侧剥离顺手清掉。

    只影响新 kernel：stub 源码是 kernel 启动时整份生成落盘的，已经在跑的会话
    kernel 保留旧 stub —— 而旧 stub 缺的正是这个可选形参，不传就没影响，
    所以不存在「必须重开会话才生效」这回事。
    """
    try:
        from tools import code_execution_tool
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KBAutoInject] code_execution_tool import failed: %s", exc)
        return
    stubs = getattr(code_execution_tool, "_TOOL_STUBS", None)
    if not isinstance(stubs, dict):
        logger.warning(
            "[KBAutoInject] _TOOL_STUBS not found; sandbox stubs stay kb_query-free"
        )
        return
    original = getattr(code_execution_tool, _STUB_ORIG_ATTR, None)
    if original is None:
        original = dict(stubs)
        setattr(code_execution_tool, _STUB_ORIG_ATTR, original)
    for name, (sig, doc, args_expr) in original.items():
        stubs[name] = (_accept_kb_query(sig), doc, _forward_kb_query(args_expr))
