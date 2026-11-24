"""`kb_query` 门禁：给所有工具 schema 注入字段，dispatch 前校验 + 剥离。

为什么要蹲这么深：provider 层**不校验** `required`（只认 properties），所以在 schema
里加个必填字段并不会让任何一方报错 —— 只能自己在 `registry.dispatch` 上拦。
`tool_call` 那条延迟桥还要 `unwrap` 递归校验，否则桥里塞的 args 一路裸奔。
"""


import copy

from .plugin_common import (
    KB_FIELD,
    KB_QUERY_DESCRIPTION,
    _EXEMPT_TOOLS,
    _NO_KB_FIELD,
    _STRIP_KEYS,
    _kb_query_is_usable,
    _kb_targets,
    _resolve_original,
    _short,
    in_sandbox_rpc,
    logger,
)
from .plugin_class import plugin_instance
from .registration import search_knowledge_handler

def _install_schema_guard() -> None:
    """给所有工具 schema 注入顶层 kb_query，并在 dispatch 前校验 + 剥离。

    规则只剩三条：
      * kb_query 必填 —— 每一次调用都要写，省略拦下
      * 值必须能用 —— 空串 / 字面引号 / 单字符 / 弃权写法（none、null、无…）拦下
      * 当场检索 —— 清单贴回这条工具结果，不等下一轮

    `_NO_KB_FIELD` 里的工具整类不看它（沉淀三件套 / search_knowledge）。

    为什么用 dispatch 层校验而不是只标 required：provider 不校验
    function.parameters.required，模型不写照样能跑。真正能拦住的是这层。

    两条安全线（上次 10-15 自锁事故的教训）：
      1. `tool_call` 延迟桥会 unwrap 后递归回 handle_function_call →
         registry.dispatch，所以内层真实工具的参数才是被校验的对象；
         模型必须在 calls[].arguments 里也写 kb_query。
      2. `_EXEMPT_TOOLS` 豁免后台收尾线程的沉淀工具。它们的 prompt 里
         可能没有协议提示词，硬拦只会把知识库沉淀静默吞掉。
    """
    try:
        from tools.registry import ToolRegistry
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KBAutoInject] ToolRegistry import failed: %s", exc)
        return

    # 反复安装不能套娃，也不能因为「装过了」就跳过 —— 否则热重载换了新代码，
    # 已装好的旧闭包仍指向上一个模块对象，schema/prompt 改动全部静默失效。
    # 做法：原方法只记一次到类属性上，之后每次都拿原方法重新包一层。
    if not hasattr(ToolRegistry, "_kb_guard_orig_get_definitions"):
        ToolRegistry._kb_guard_orig_get_definitions = ToolRegistry.get_definitions
    if not hasattr(ToolRegistry, "_kb_guard_orig_dispatch"):
        ToolRegistry._kb_guard_orig_dispatch = ToolRegistry.dispatch
    ToolRegistry._kb_doing_guard_installed = True

    original_get_definitions = _resolve_original(
        ToolRegistry.get_definitions,
        "get_definitions",
        ToolRegistry._kb_guard_orig_get_definitions,
    )

    def patched_get_definitions(self, *args, **kwargs):
        tool_defs = original_get_definitions(self, *args, **kwargs)
        try:
            for tool_def in tool_defs:
                if not isinstance(tool_def, dict):
                    continue
                function = tool_def.get("function")
                if not isinstance(function, dict):
                    continue
                params = function.get("parameters")
                if not isinstance(params, dict):
                    params = {"type": "object", "properties": {}}
                else:
                    # registry._definition() 用 {**entry.schema, ...} 浅拷贝，
                    # function["parameters"] 与 entry.schema["parameters"] 是同一个 dict。
                    # v0.23.2 起 tools/arg_validation 拿 entry.schema 的 required 校验入参，
                    # 原地 append kb_query 会污染注册表 —— 变成「schema 要求 kb_query、
                    # dispatch 又把它 pop 掉」的自相矛盾，所有内置工具一调即死。
                    # 深拷贝后只改返回给模型的这份，注册表保持原样。
                    params = copy.deepcopy(params)
                function["parameters"] = params
                properties = params.get("properties")
                if not isinstance(properties, dict):
                    properties = {}
                if (function.get("name") or "") in _NO_KB_FIELD:
                    # 这些工具不带 kb_query：把它们 schema 里残留的元参数清掉就走。
                    params["properties"] = {
                        k: v for k, v in properties.items() if k not in _STRIP_KEYS
                    }
                    continue
                new_properties = {
                    KB_FIELD: {
                        "type": "string",
                        "description": KB_QUERY_DESCRIPTION,
                    },
                }
                new_properties.update(
                    {k: v for k, v in properties.items() if k not in _STRIP_KEYS}
                )
                params["properties"] = new_properties

                # kb_query 是硬必填，所以标进 required —— 让模型在 schema 层就看到它，
                # 而不是只靠 dispatch 拦（provider 不校验 required，但模型会照着填）。
                required = params.get("required")
                if not isinstance(required, list):
                    required = []
                required = [r for r in required if r not in _STRIP_KEYS]
                if KB_FIELD not in required:
                    required.append(KB_FIELD)
                params["required"] = required
        except Exception as exc:  # noqa: BLE001
            logger.warning("[KBAutoInject] kb_query schema injection failed: %s", exc)
        return tool_defs

    ToolRegistry.get_definitions = patched_get_definitions

    original_dispatch = _resolve_original(
        ToolRegistry.dispatch, "dispatch", ToolRegistry._kb_guard_orig_dispatch
    )

    def patched_dispatch(self, name, args, *pos_args, **kwargs):
        """kb_query 门禁 + 当场检索出清单。

        三道关，全在 dispatch 这一层（provider 不校验 schema.required，模型不写照样能跑）：
          1. 必填 —— 每一次调用都要有，省略拦下；弃权写法（none/null/无）同样拦
          2. 值合法 —— 空串 / 字面引号 / 单字符 / none·null·无 这类弃权写法拦下
          3. 当场检索 —— 清单贴回**这条**工具结果，不等下一轮

        `_NO_KB_FIELD` 里的工具整类不看 kb_query：三个沉淀工具（后台收尾线程
        没有协议）、`search_knowledge`
        （它自己的 query 就是检索词）。

        桥形态 {"calls":[{name, arguments}]} 要逐条看内层 —— 外层自己没有 kb_query，
        只看外层会把整条桥误判成漏写。
        """
        if name == "search_knowledge":
            return search_knowledge_handler(args if isinstance(args, dict) else {})

        if isinstance(args, dict) and in_sandbox_rpc():
            # execute_code 的 RPC 子调用：整段跳过（理由见 plugin_common.sandbox_rpc_scope）。
            # 只保留剥离 —— 不剥，底层工具会收到它不认识的 kb_query。
            for key in _STRIP_KEYS:
                args.pop(key, None)
            return original_dispatch(self, name, args, *pos_args, **kwargs)

        if isinstance(args, dict):
            from tools.registry import tool_error

            _kb_query = ""
            for _tname, _targs in (_kb_targets(args) or [(name, args)]):
                _eff = _tname or name
                if _eff in _NO_KB_FIELD:
                    continue
                _raw = _targs.get(KB_FIELD, None) if isinstance(_targs, dict) else None
                if _raw is None:
                    # 整条桥的最外层写了也算（老形态：kb_query 写在外层）
                    _raw = args.get(KB_FIELD, None) if _eff != name else None
                if _raw is None:
                    logger.info(
                        "[KBAutoInject] kb_query missing, blocked tool=%s", _eff
                    )
                    return tool_error(
                        f"[kb_query 必填] 工具 `{_eff}` 没写 `kb_query`。\n"
                        f"{KB_QUERY_DESCRIPTION}\n"
                        "经 tool_call 桥调用时写在 calls[].arguments 内层。",
                        error_type="kb_query_missing",
                        tool=_eff,
                    )
                if _kb_query_is_usable(_raw):
                    # 一条调用只检索一次：桥里多个内层调用共用第一个可用的词。
                    # 每个词都检索 = 一屏叠一屏，实测把上下文灌满却没多捞到卡。
                    _kb_query = _kb_query or str(_raw).strip()
                    continue
                logger.info(
                    "[KBAutoInject] kb_query unusable (raw=%r), blocked tool=%s",
                    _short(str(_raw), 40),
                    _eff,
                )
                return tool_error(
                    f"[kb_query 不可用] 工具 `{_eff}` 的 `kb_query` 必须填真实检索词"
                    f"（现在收到 {_raw!r}）。\n"
                    f"{KB_QUERY_DESCRIPTION}",
                    error_type="kb_query_unusable",
                    tool=_eff,
                )

            # kb_query 只有一个去向：丢进后台池。工具照常执行，清单一律由
            # `handle_llm_request` 在下一次模型请求的**尾部**注入 —— 与用户原话那条
            # 完全同一条管道、同一个池、同一套去重账。
            #
            # 这里**不做任何分档**。早先按「只读 / 危险」分两路：危险工具当场同步查
            # 并把清单贴在工具结果里。代价是两套去重账互不可见 —— 同一个词可能查两遍，
            # 也可能两边都以为对方查过，结果谁都没查。
            #
            # 全是豁免工具（search_knowledge / 沉淀三件套）就整条不检索：
            # 展开自己就是「去库里取」，再检索一次只会多贴一屏没人要的清单；
            # 沉淀工具写卡时也不必顺带搜一遍库。
            if _kb_query:
                try:
                    plugin_instance.kb_queue(
                        _kb_query, str(kwargs.get("session_id") or "")
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[KBAutoInject] kb_queue failed (%s): %s", name, exc
                    )
            # 剥离：元参数只给本插件取词，底层工具不认识它
            for key in _STRIP_KEYS:
                args.pop(key, None)
        return original_dispatch(self, name, args, *pos_args, **kwargs)

    ToolRegistry.dispatch = patched_dispatch
    logger.info(
        "[KBAutoInject] kb_query schema + dispatch gate installed [v2-single-exit] "
        "(fail-open exempts: %s).",
        ", ".join(sorted(_EXEMPT_TOOLS)),
    )
