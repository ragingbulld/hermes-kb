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

from .engine_sqlite import KBEngine  # noqa: F401

__all__ = ["KBEngine"]
