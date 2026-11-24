"""知识库引擎入口（SQLite 单文件后端）。

历史
----
2026-10-20 之前用 LanceDB。检索质量没问题，垮的是索引维护层：
`create_fts_index(replace=True)` 没有追加 API，任何写入都要全表重建
（61k 行实测 46s/次），配上 watcher 里 2 秒 debounce 的刷新线程，
42.9 小时里累计烧掉 40.04 小时单核 CPU。

2026-10-20 迁到 SQLite 单文件：chunks 表存正文 + float32 向量 BLOB，
FTS5 存 bigram 展开正文，向量走内存矩阵暴力余弦（61k 行 15.7ms）。
写入即索引。

同日稍后按 owner 指令彻底移除 LanceDB：数据目录、engine_lancedb.py、
迁移脚本、插件里的 lancedb / pyarrow / pylance 依赖声明全部删除，
不再保留 `HERMES_KB_BACKEND=lancedb` 回退通道。回滚点为同日的
旧的迁移备份目录与向量缓存目录。

**不使用 ANN**：6 万行暴力扫描已足够快。未来涨到百万级再评估 sqlite-vec。
"""

import threading

from .engine_sqlite import AbortSlot, KBEngine  # noqa: F401

__all__ = ["AbortSlot", "KBEngine", "get_engine"]

# 进程级唯一实例。向量矩阵是进程内常驻资源（6.7 万行 x 1024 维 ≈ 276 MB），
# 每构造一个实例就要全量读一遍 SQLite（实测 0.9~2.1 s）。
#
# 为什么单例必须放在这一层
# ------------------------
# 插件在每轮 turn 都会被重新加载，插件实例上的 self._engine 跟着归零 ——
# 单例放在插件里只活一轮，等于没有缓存。本模块挂在 sys.path 上、不参与插件
# reload，模块级变量才能跨轮活下来。
#
# 外部进程（watcher daemon）的写入由 KBEngine 内部的库状态探针
# （_vectors_stale，比 chunk 行数 + MAX(updated_at)）发现，这里不用管。

_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine() -> KBEngine:
    """取进程内唯一的 KBEngine，首次调用时才真正建实例。"""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = KBEngine()
    return _ENGINE
