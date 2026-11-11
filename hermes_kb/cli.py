import argparse
import os
import time

from .watcher import KnowledgeWatcher
from .engine import KBEngine
from .gate import gated_search


def _fmt_ts(ts):
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "-"


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def cmd_status(limit_paths=5):
    """打印知识库状态。

    2026-11-01 重写：原实现在 LanceDB→SQLite 迁移（2026-10-20）后失效
    —— 它调 engine.db.list_tables() / table.count_rows() / list_indices()，
    SQLite 引擎一个都没有，且被 try/except 吞成一行提示，静默腐烂。
    2026-10-21 整段删除。本次按 engine_sqlite 的真 API 重写：

      · 表统计直接查 SQLite（chunks / file_records / kb_meta）
      · 引擎层只读属性：kb_path / db_dir
      · 不做任何写入，只读打开也用引擎自身的 _conn()

    注意：不要复活 engine.db.* 那套调用。
    """
    engine = KBEngine()
    kb_path = engine.kb_path

    print("Hermes Knowledge Base (SQLite)")
    print("=" * 60)

    if not os.path.exists(kb_path):
        print(f"状态     : 未初始化（库文件不存在）")
        print(f"库路径   : {kb_path}")
        return 1

    size = os.path.getsize(kb_path)
    # WAL 未 checkpoint 的部分也是真实占用，一并算进去
    wal = kb_path + "-wal"
    if os.path.exists(wal):
        size += os.path.getsize(wal)

    print(f"状态     : OK")
    print(f"库路径   : {kb_path}")
    print(f"库大小   : {_human(size)}")

    with engine._conn() as conn:
        conn.row_factory = None
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0]
        toks = conn.execute("SELECT COALESCE(SUM(token_estimate),0) FROM chunks").fetchone()[0]
        last_upd = conn.execute("SELECT MAX(updated_at) FROM chunks").fetchone()[0]
        print(f"切片数   : {chunks:,}")
        print(f"文件数   : {files:,}")
        print(f"估算token: {toks:,}")
        print(f"最近更新 : {_fmt_ts(last_upd)}")

        # kb_meta：嵌入模型/维度/FTS 切词口径版本
        print("-" * 60)
        print("元数据:")
        try:
            for k, v, _ts in conn.execute(
                "SELECT key, value, updated_at FROM kb_meta ORDER BY key"
            ):
                print(f"  {k:<18} = {v}")
        except Exception as e:
            print(f"  (kb_meta 读取失败: {e})")

        # 平均每文件切片数，异常低往往是切分或同步出了问题
        if files:
            print("-" * 60)
            print(f"平均切片/文件: {chunks / files:.1f}")

        # 抽几个最近更新的文件，方便肉眼确认同步面
        if limit_paths > 0:
            rows = conn.execute(
                """SELECT file_path, chunk_count, updated_at
                   FROM file_records ORDER BY updated_at DESC LIMIT ?""",
                (limit_paths,),
            ).fetchall()
            if rows:
                print("-" * 60)
                print(f"最近同步的 {len(rows)} 个文件:")
                for fp, cc, ts in rows:
                    print(f"  [{cc:>3} 块] {_fmt_ts(ts)}  {fp}")

    print("=" * 60)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Hermes Knowledge Base CLI (SQLite)")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # sync
    subparsers.add_parser("sync", help="Run a full scan and sync all files")

    # search
    search_p = subparsers.add_parser("search", help="Search the knowledge base")
    search_p.add_argument("query", type=str, help="Search query")
    search_p.add_argument("--limit", type=int, default=0,
                          help="最多显示几条（默认 0 = 不限制，"
                               "返回全部通过 min_rerank_score 闸门的块）")

    # watch
    subparsers.add_parser("watch", help="Run real-time file watcher daemon")

    # status
    status_p = subparsers.add_parser("status", help="Show knowledge base status (read-only)")
    status_p.add_argument("--paths", type=int, default=5,
                          help="How many recently synced files to list (0 = none)")

    args = parser.parse_args()

    if args.command == "sync":
        watcher = KnowledgeWatcher()
        watcher.full_scan()
    elif args.command == "watch":
        from .watcher import run_watcher_daemon
        run_watcher_daemon()
    elif args.command == "search":
        engine = KBEngine()
        retrieval_cfg = engine.config["retrieval"]

        # 与注入链路同口径：闸门只有 gate.gated_search 一处（插件也调它），排序键在
        # engine 内部按 retrieval.rank_by 决定。CLI 不自己过滤、不自己排序。
        gated = gated_search(engine, args.query)

        # --limit 默认 0 = 不限制（返回全部过闸门的块）；
        # 显式给正整数才截断，供"我只想瞄一眼"的场景用。
        if args.limit and args.limit > 0:
            shown = gated.kept[: args.limit]
            truncated_note = f"（仅显示前 {len(shown)} 条）"
        else:
            shown = gated.kept
            truncated_note = ""

        print(f"\n🔍 Search Query: {args.query}")
        print(f"📊 口径: rank_by={retrieval_cfg['rank_by']} "
              f"候选={gated.candidates}/{retrieval_cfg['candidate_limit']} "
              f"rerank>={gated.min_score} 通过={len(gated.kept)} 丢弃={gated.below}"
              + (f" 无重排分={gated.unscored}" if gated.unscored else ""))
        print(f"🎯 Results: {len(shown)}{truncated_note}\n" + "=" * 60)
        for idx, r in enumerate(shown, 1):
            print(f"[{idx}] rrf={r.get('rrf_score', 0):.6f} | "
                  f"rerank={r.get('rerank_score', 0):.4f} | {r['heading_path']}")
            print(f"    File: {r['file_path']}")
            print("    Content:")
            # 原文全量，不截断
            print(r["content"])
            print("-" * 60)
    elif args.command == "status":
        return cmd_status(limit_paths=args.paths)
    else:
        parser.print_help()


if __name__ == "__main__":
    raise SystemExit(main() or 0)
