import argparse
from .watcher import KnowledgeWatcher
from .engine import KBEngine

def main():
    parser = argparse.ArgumentParser(description="Hermes Knowledge Base CLI (SQLite)")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # sync
    subparsers.add_parser("sync", help="Run a full scan and sync all files")

    # search
    search_p = subparsers.add_parser("search", help="Search the knowledge base")
    search_p.add_argument("query", type=str, help="Search query")
    search_p.add_argument("--limit", type=int, default=3, help="Number of results")

    # watch
    subparsers.add_parser("watch", help="Run real-time file watcher daemon")

    args = parser.parse_args()

    if args.command == "sync":
        watcher = KnowledgeWatcher()
        watcher.full_scan()
    elif args.command == "watch":
        from .watcher import run_watcher_daemon
        run_watcher_daemon()
    elif args.command == "search":
        engine = KBEngine()
        results = engine.search(args.query, limit=args.limit * 2, rerank_top_n=args.limit)
        print(f"\n🔍 Search Query: {args.query}")
        print(f"🎯 Results Found: {len(results)}\n" + "="*60)
        for idx, r in enumerate(results, 1):
            score = r.get("rerank_score", 0.0)
            print(f"[{idx}] Score: {score:.4f} | Path: {r['heading_path']}")
            print(f"    File: {r['file_path']}")
            print(f"    Content Preview:\n{r['content'][:300]}...")
            print("-" * 60)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
