"""hermes-kb · 一个加载单元，两条互不牵连的注册链。

合并前是两个目录：

    hermes-kb-auto-inject   读侧 —— pre_llm_call / llm_request / transform_tool_result
                            + search_knowledge，跑在**每轮每个 API 请求**上
    hermes-kb-write         写侧 —— save_reflection / edit_reflection_card /
                            delete_reflection_card + 常驻规则段 + 三层注册期 patch

这里只合并「加载单元」，不合并「失效域」：两段各自 `try/except`，任何一段抛异常
都只留一条 error 日志，另一段照常注册。合并前一个插件加载失败只死一半，合并后
如果共用一段注册代码，就会变成一个语法错打死整个知识库 —— 那才是真倒退。

**失败语义刻意保持不一致**（铁律，别顺手统一）：

  · 读侧**失败即抛** —— 这轮注入崩了必须看得见，静默降级等于模型拿着不存在的
    知识继续编。
  · 写侧 **fail-open** —— 查重拿不到引擎就放行；把「查重降级」静默改成
    「写不了卡」是行为变更，不是重构。

两处 `sys.path` 自举也**刻意不合并**（`inject.py` 与 `write.py` 各有一份）：
它们自举的是同一份 `hermes_kb`，但触发时机不同、失败语义不同，合成一处就等于
让写侧继承读侧的抛错策略。
"""

import hashlib
import logging
import pathlib
from typing import Any, List

logger = logging.getLogger(__name__)

__all__ = ["register"]


def _fingerprint() -> tuple:
    """整包 md5 + 行数 + 模块数。

    只算 `__init__.py` 会恒等于同一份薄入口，线上到底跑的哪一版就看不出来；
    只算单个子模块则漏掉另一半。整包口径才对得上「一个插件」这个事实。
    """
    try:
        pkg = pathlib.Path(__file__).parent
        mods = sorted(p for p in pkg.glob("*.py") if not p.name.endswith(".bak"))
        blob = b"".join(p.read_bytes() for p in mods)
        lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in mods)
        return hashlib.md5(blob).hexdigest()[:12], lines, len(mods)
    except Exception:  # noqa: BLE001 —— 留痕失败绝不影响注册
        return "?", 0, 0


def register(ctx: Any) -> None:
    """注册读侧、写侧各一次；互不牵连。"""
    done: List[str] = []
    failed: List[str] = []

    # ── 读侧：注入中间件 + search_knowledge + kb_query 门禁 + 常驻核心工具 ──
    try:
        from .inject import register_inject

        register_inject(ctx)
        done.append("inject")
    except Exception as exc:  # noqa: BLE001
        failed.append("inject")
        logger.error(
            "[hermes-kb] 读侧注册失败，**注入与 search_knowledge 不可用**：%s",
            exc, exc_info=True,
        )

    # ── 写侧：写/改/删三条工具 + 常驻规则段 + toolsets/fork/notify 补丁 ──
    try:
        from .write import register_write

        register_write(ctx)
        done.append("write")
    except Exception as exc:  # noqa: BLE001
        failed.append("write")
        logger.error(
            "[hermes-kb] 写侧注册失败，**save/edit/delete 不可用**：%s",
            exc, exc_info=True,
        )

    _md5, _lines, _mods = _fingerprint()
    # `code md5=` 这个字面是 plugin_health_watchdog 的「改动是否已装载」判据，
    # 形态跟合并前两个插件各自那一行保持一致，别改格式。
    logger.info(
        "hermes-kb code md5=%s (%s 行, %s 个模块) registered — 成功=%s 失败=%s",
        _md5, _lines, _mods, done or "无", failed or "无",
    )
