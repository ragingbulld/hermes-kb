"""hermes-kb-auto-inject · 薄入口。

只做三件事：把各模块拼起来、定义 `register(ctx)`、注册完做一次出口自检。
真正的活分在四处（单向依赖，无环）：

    plugin_common.py   常量 / 正则 / 纯函数 / 协议提示词（不依赖任何实例）
    plugin_class.py    KBAutoInjectPlugin 类 + 单实例 plugin_instance
    registration.py    search_knowledge 的 schema 与 handler
    guard.py           kb_query 门禁（schema 注入 + dispatch 校验剥离）
    resident.py        核心工具常驻（防「加载成功但注入全哑」）

拆之前的形态是 1502 行单文件，上面这些职责全堆在 `__init__.py` 里。
设计骨架没动：清单化投递、单池归一、轮作用域去重、fail-open 一个不少。
"""

import hashlib
import sys
from typing import Any

from .guard import _install_schema_guard
from .plugin_class import KBAutoInjectPlugin, plugin_instance
from .plugin_common import logger
from .registration import SEARCH_KB_SCHEMA, search_knowledge_handler
from .resident import _ensure_core_resident

__all__ = [
    "register",
    "KBAutoInjectPlugin",
    "plugin_instance",
    "search_knowledge_handler",
    "SEARCH_KB_SCHEMA",
]


def register(ctx: Any) -> None:
    """注册三样东西：pre_llm_call 钩子（唯一注入点，块直接落 api_content）、
    search_knowledge 工具、以及 kb_query 门禁补丁。

    不再注册 llm_request 中间件 —— core 在 hermes_cli/middleware.py 用 _safe_copy
    隔离 request，那条路上的任何修改按设计都不可能持久化。
    """
    _install_schema_guard()
    _ensure_core_resident()
    # toolset 归属必须落在会话实际 enabled 的集合里。`memory` 在 gateway 的冻结
    # launch policy（`session_policy.toolsets`）里不一定存在 —— 那样工具虽然注册成功、
    # 判据也不可延迟，却永远进不了 `_select_tool_names`，模型侧就没有。
    # `file` 的证据：gateway 的会话 pin 里 read_file/patch/write_file/search_files 全在，
    # 说明 `file` 一定被 enable。
    ctx.register_hook("pre_llm_call", plugin_instance.handle_pre_llm_call)
    
    # 注册主动查库工具 search_knowledge
    ctx.register_tool(
        name="search_knowledge",
        toolset="file",
        schema=SEARCH_KB_SCHEMA,
        handler=search_knowledge_handler,
        check_fn=lambda: True,
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
        "hermes-kb-auto-inject code md5=%s (%s 行, %s 个模块) registered — kb_query 门禁 + 标题清单池 "
        "+ pre_llm_call 协议注入；检索入口两条：用户原话（每轮一次）+ kb_query",
        _md5, _lines, len([p for p in _pl.Path(__file__).parent.glob("*.py")]),
    )
