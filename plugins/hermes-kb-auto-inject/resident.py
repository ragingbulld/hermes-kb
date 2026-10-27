"""核心工具常驻：保证 `search_knowledge` 真出现在模型的工具表里。

2026-09-29 事故：插件加载成功、日志正常、注入闸门也对，但注入全哑 —— 根因是
`search_knowledge` 进了 registry 却没进「本轮 enabled 的 toolset」，`_select_tool_names`
永远筛不到它。这里在加载期把它钉进核心集合，并且**绝不 import `tool_search`**：
首次 import 要建 BM25 目录，10s 加载门禁会直接判插件超时。
"""

import sys
from typing import Any, List

from .plugin_common import logger

_CORE_TOOLS = ("search_knowledge",)


def _ensure_core_resident() -> None:
    """让 KB 的两个工具**天生常驻**：只改判据来源，不补任何工具表。

    Hermes 判可延迟只看一处 —— `tools.tool_search.is_deferrable_tool_name()`：
    名字不在 `_core_tool_names()`（= `toolsets._HERMES_CORE_TOOLS`）、且 toolset 不是
    直表面 → 可延迟 → `assemble_tool_defs()` 把它换成 `tool_search` 桥。

    所以唯一的正解是让它进 `_HERMES_CORE_TOOLS`。进去之后 assembly 根本不碰它，
    既不用在 `get_definitions` 里补 schema，也不用在 assembly 之后捞回来 —— 这两层
    「补救」曾经同时存在，还各自带过一个 bug（`ToolRegistry` 未定义、幂等标志挡住
    热重载），删掉就是删掉整类问题。

    另外把名字补进各 toolset 的 `tools`：`_bundle()` 在 toolsets 导入那一刻就把
    `_HERMES_CORE_TOOLS` 复制走了，不逐个补的话，靠 `resolve_toolset()` 选出来的会话
    （gateway 各平台全是这条路）拿不到这两个工具。

    `_core_tool_names()` 是运行时重新 import 读的，所以热重载即生效、不用重启网关。
    装完当场验两条：判据必须翻成 False、构建出来的工具表里必须真有它们。
    """
    # 历史包袱：早前版本在 `tools.tool_search` 上挂过一个「assembly 之后再把工具捞回来」
    # 的闭包。那层已被常驻判据取代，但热重载不会自动摘掉（标志存在被 patch 的模块里），
    # 留着会把真实状态盖住 —— 这里显式还原成原生函数。
    try:
        from tools import tool_search as _ts

        _orig = getattr(_ts, "_kb_keep_orig", None)
        if _orig is not None and _ts.assemble_tool_defs is not _orig:
            _ts.assemble_tool_defs = _orig
            logger.info("[KBAutoInject] 已摘除历史 assembly 补丁，回归原生 Tool Search")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KBAutoInject] 摘除历史 assembly 补丁失败：%s", exc)

    try:
        import toolsets
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KBAutoInject] toolsets 不可用，KB 工具无法常驻：%s", exc)
        return

    core = getattr(toolsets, "_HERMES_CORE_TOOLS", None)
    if isinstance(core, list):
        core.extend(n for n in _CORE_TOOLS if n not in core)
    # 绝不能补进**平台门控** toolset（带 `platforms` 的，如 desktop-only 的
    # `onboarding` / `catalog`）：`session_platform_tool_drops()` 会把这类 toolset 的
    # 全部工具算成「本平台拿不到」，于是 qqbot_native 会话的
    # `agent_tool_drops()` 里凭空多出 KB 工具名，
    # `_drop_session_tools()` 在 `_publish_tool_snapshot` 里把它们从 new_names 里删掉 ——
    # 注册成功、判据 False、registry 出口有 schema，模型侧却永远没有。
    # 实测（旧版）：drops={open_knowledge, search_knowledge, manage_catalog, ...}，探针恒 n=23。
    for info in (getattr(toolsets, "TOOLSETS", None) or {}).values():
        if not isinstance(info, dict):
            continue
        tools = info.get("tools")
        if not isinstance(tools, list):
            continue
        if "platforms" in info:
            # 老版本已经把 KB 工具补进了门控 toolset（进程内的 TOOLSETS 是可变字典，
            # 热重载不会自动回滚）——这里主动剔除，否则 drops 里的污染会一直留着。
            stale = [n for n in _CORE_TOOLS if n in tools]
            if stale:
                tools[:] = [n for n in tools if n not in _CORE_TOOLS]
                logger.warning("[KBAutoInject] 已从平台门控 toolset 剔除误补的 KB 工具：%s", stale)
            continue
        tools.extend(n for n in _CORE_TOOLS if n not in tools)

    # 自检一：判据。这条不过，后面补什么都是白费 —— 所以它用 error。
    # 只在 `tools.tool_search` **已经加载**时核对判据 —— 绝不在这里 import 它：
    # 首次 import 要建 BM25 延迟目录，插件因此被判 load timeout，
    # 后果是工具注册被忽略 + pre_llm_call 钩子整条静默失效（比补丁失效更隐蔽）。
    still: List[str] = []
    _tsm_live = sys.modules.get("tools.tool_search")
    if _tsm_live is not None and hasattr(_tsm_live, "is_deferrable_tool_name"):
        try:
            still = [n for n in _CORE_TOOLS if _tsm_live.is_deferrable_tool_name(n)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[KBAutoInject] 常驻自检跳过（判据不可用）：%s", exc)
    if still:
        logger.error("[KBAutoInject] KB 工具仍被判为可延迟，装配时会再被剔除：%s", still)

    # 自检二：纯数据核对 —— 名字真进了核心名单、也真进了 toolset bundle。
    # 刻意不碰 model_tools / _select_tool_names：那条链会重入插件加载，
    # 在测试进程里二次注册、把已装好的 dispatch 门禁换到别的实例上。
    try:
        core = list(getattr(toolsets, "_HERMES_CORE_TOOLS", []) or [])
        bundle = list(((toolsets.TOOLSETS.get("memory") or {}).get("tools")) or [])
        lost = [n for n in _CORE_TOOLS if n not in core or n not in bundle]
        if lost:
            logger.error("[KBAutoInject] 常驻自检失败，%s 没写进核心名单/bundle", lost)
        elif not still:
            logger.info(
                "[KBAutoInject] KB 工具已常驻：%s（is_deferrable=False，核心名单 %d 项）",
                " + ".join(_CORE_TOOLS),
                len(core),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KBAutoInject] 常驻自检跳过：%s", exc)

    # 另一层 memo：`toolsets._resolve_toolset_memo` 的 key 含 registry generation，
    # 注释写「stale-generation entries are never hit again」—— 靠 generation 淘汰。
    # 而我们只改 toolset 定义数据、不注册新工具，generation 不动 ⇒ 旧 key 永久命中。
    # 一并清掉。
    try:
        _tsm2 = sys.modules.get("toolsets")
        _memo = getattr(_tsm2, "_resolve_toolset_memo", None)
        if isinstance(_memo, dict) and _memo:
            _memo.clear()
            logger.info("[KBAutoInject] toolset 解析 memo 已清")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[KBAutoInject] 清 toolset memo 跳过：%s", exc)

    # 判据改了，但长驻网关进程里 get_tool_definitions 的 memo 是按
    # (scope, 参数, registry generation, config 指纹…) 存的 —— 只是改写 toolset 数据
    # 不会让旧 key 失效。这里顺手清掉，让「常驻变更」当轮就生效。
    # 注意只清已加载的模块，绝不 import model_tools（那会重入插件加载）。
    _mt = sys.modules.get("model_tools")
    if _mt is not None and hasattr(_mt, "_clear_tool_defs_cache"):
        _mt._clear_tool_defs_cache()
        logger.info("[KBAutoInject] tool_defs 缓存已清，常驻变更本轮生效")
