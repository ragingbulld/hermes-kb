"""hermes-kb-auto-inject · 薄入口。

只做三件事：把各模块拼起来、定义 `register(ctx)`、注册完做一次出口自检。
真正的活分在四处（单向依赖，无环）：

    plugin_common.py   常量 / 正则 / 纯函数 / 协议提示词（不依赖任何实例）
    plugin_class.py    KBAutoInjectPlugin 类 + 单实例 plugin_instance
    registration.py    search_knowledge 的 schema 与 handler
    guard.py           kb_query 门禁（schema 注入 + dispatch 校验剥离）
    resident.py        核心工具常驻（防「加载成功但注入全哑」）

拆之前的形态是 1502 行单文件，上面这些职责全堆在 `__init__.py` 里。
设计骨架没动：清单化投递、单池归一、fail-open 一个不少；去重账整套删了。
"""

import hashlib
import sys
from typing import Any

from .guard import _install_schema_guard
from .plugin_class import KBAutoInjectPlugin, plugin_instance
from .plugin_common import logger
from .registration import SEARCH_KB_SCHEMA, search_knowledge_handler
from .resident import _ensure_core_resident
from .sandbox import install_sandbox_rpc_scope, install_stub_kb_query_tolerance

__all__ = [
    "register_inject",
    "KBAutoInjectPlugin",
    "plugin_instance",
    "search_knowledge_handler",
    "SEARCH_KB_SCHEMA",
]

# KB 工具图标只在写侧（`write.py`）登记一份真源，它同时管写/改/删三个工具。
# 合并进同一个包之前这行是 `from hermes_plugins.hermes_kb_write import TOOL_EMOJI`
# 加一层 ImportError 兜底 —— 拆两个插件时只能软引用，现在同包直接取。
_KB_TOOL_EMOJI_FALLBACK = {"search_knowledge": "📇"}


def _kb_tool_emoji(tool_name: str) -> str:
    """Return the KB-family glyph for *tool_name* from the shared table, else the fallback."""
    from .write import TOOL_EMOJI

    return TOOL_EMOJI.get(tool_name) or _KB_TOOL_EMOJI_FALLBACK.get(tool_name, "")


def register_inject(ctx: Any) -> None:
    """注册三样东西：注入中间件、search_knowledge 工具、kb_query 门禁补丁。

    **出口只有一个，时机是「下一次模型请求之前」**：检索结果一返回，池子回调
    ``plugin_instance._on_job_done``，把标题清单排进待投递槽；中间件
    ``handle_llm_request`` 在每次请求前取空槽，贴到最后一条消息尾部。

    改的是 ``_safe_copy`` 出来的**请求副本**，``state.db`` 一个字都不落 ——
    「当次就能看见、而且不进历史」这两件同时成立，靠的就是这一层。

    ``pre_llm_call`` 只剩一件事：把用户原话丢进池子（纯问答轮也要有检索）。
    它**不返回清单**，所以不拖慢首字；也不维护任何去重账 —— 账本整套删了。
    """
    _install_schema_guard()
    # 沙箱侧两件事（必须在门禁装好之后：它们只打标记 / 放宽签名，不放宽判据）：
    #   · RPC 处理期间打作用域标记 —— 脚本内 `hermes_tools.*` 子调用从此不再被拦
    #   · 给 stub 签名补可选 kb_query —— 模型照协议写也不撞 TypeError
    install_sandbox_rpc_scope()
    install_stub_kb_query_tolerance()
    _ensure_core_resident()
    # toolset 归属必须落在会话实际 enabled 的集合里。`memory` 在 gateway 的冻结
    # launch policy（`session_policy.toolsets`）里不一定存在 —— 那样工具虽然注册成功、
    # 判据也不可延迟，却永远进不了 `_select_tool_names`，模型侧就没有。
    # `file` 的证据：gateway 的会话 pin 里 read_file/patch/write_file/search_files 全在，
    # 说明 `file` 一定被 enable。
    # 入口一：每 turn 提交用户原话。出口：llm_request 中间件贴进请求副本。
    ctx.register_hook("pre_llm_call", plugin_instance.handle_pre_llm_call)
    ctx.register_middleware("llm_request", plugin_instance.handle_llm_request)

    # 读正文时补一行「这张卡什么时候写的」。只在 `read_file` 命中**索引内文件**
    # 时动手（判据是 `file_records` 里有没有这个路径），别的工具与普通文件原样放行。
    # 单独包一层：老版本 Hermes 没有这个钩子时注册会抛，不能把整个插件拖垮 ——
    # 插件加载失败等于注入静默归零（2026-09-30 事故）。
    try:
        ctx.register_hook(
            "transform_tool_result", plugin_instance.handle_transform_tool_result
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KBAutoInject] transform_tool_result 注册失败：%s", exc)
    
    # 注册主动查库工具 search_knowledge
    ctx.register_tool(
        name="search_knowledge",
        toolset="file",
        schema=SEARCH_KB_SCHEMA,
        handler=search_knowledge_handler,
        check_fn=lambda: True,
        emoji=_kb_tool_emoji("search_knowledge"),
    )

    # 自检三：registry 出口实测。注册完当场把名字喂回 `get_definitions`，
    # 看它到底吐不吐 schema —— 这一步在加载期跑，日志立刻可读，不用等下一轮对话。
    # 「registry 里有」「判据 False」「出口不给」三者同时成立时，缺口就在这一段。
    try:
        _rg = sys.modules.get("tools.registry")
        _r = getattr(_rg, "registry", None)
        if _r is not None:
            _probe_names = ["search_knowledge"]
            _d = _r.get_definitions(_probe_names) or []
            _got = [x.get("function", {}).get("name") for x in _d if isinstance(x, dict)]
            _missing = [n for n in _probe_names if n not in _got]
            logger.info(
                "[KBAutoInject] registry 出口自检: 请求=%s 返回=%s 缺=%s scope=%s",
                _probe_names, _got, _missing,
                getattr(_r, "current_scope_key", lambda: "?")() if hasattr(_r, "current_scope_key") else "?",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KBAutoInject] registry 出口自检失败：%s", exc)
    # md5 覆盖整包（拆成多文件后，只算 `__init__.py` 会恒等于同一份薄入口，
    # 线上到底跑的哪一版就再也看不出来了）。
    try:
        import pathlib as _pl
        _pkg = _pl.Path(__file__).parent
        _blob = b"".join(
            p.read_bytes() for p in sorted(_pkg.glob("*.py")) if not p.name.endswith(".bak")
        )
        _md5 = hashlib.md5(_blob).hexdigest()[:12]
        _lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in sorted(_pkg.glob("*.py")))
    except Exception:
        _md5, _lines = "?", 0
    # 注意 `code md5=` 这个字面：plugin_health_watchdog.py 的「已装载」判据
    # 就是 grep 它 + 插件名 + 时间戳（热重载不改进程启动时间，只能靠这条留痕翻案）。
    # 曾写成 `registered: md5=`，与判据对不上，改动明明已装载却天天报「需 reload」。
    logger.info(
        "hermes-kb/inject code md5=%s (%s 行, %s 个模块) registered — kb_query 门禁 + 标题清单池 "
        "+ 单注入出口（llm_request 中间件贴请求副本尾部，不落 state.db、不等工具、不压请求）；"
        "检索入口两条：用户原话（每轮一次）+ 每次工具调用的 kb_query，同一池；**无去重账**",
        _md5, _lines, len([p for p in _pl.Path(__file__).parent.glob("*.py")]),
    )
