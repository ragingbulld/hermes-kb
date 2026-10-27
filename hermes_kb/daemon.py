import os
import sys
import time
import signal
import threading

# 把本包的**父目录**插到 sys.path，这样无论仓库克隆到哪里都能 import。
# 部署时把 engine/ 目录改名成 hermes_kb/ 放进包搜索路径即可（见 INSTALL.md）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hermes_kb.watcher import KnowledgeWatcher, FileEventHandler
from watchdog.observers import Observer

running = True

def signal_handler(signum, frame):
    global running
    print(f"[KB Daemon] Received signal {signum}, stopping...")
    running = False

def main():
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    print("[KB Daemon] Initializing knowledge base watcher...")
    watcher = KnowledgeWatcher()
    
    # 启动时执行一次全量增量同步
    print("[KB Daemon] Running initial scan...")
    watcher.full_scan()

    observer = Observer()
    handler = FileEventHandler(watcher)

    for w_dir in watcher.watch_dirs:
        os.makedirs(w_dir, exist_ok=True)
        observer.schedule(handler, w_dir, recursive=True)
        print(f"[KB Daemon] Watching directory: {w_dir}")

    observer.start()
    # SQLite 后端写入即索引，无需 FTS 刷新线程。
    # 维护线程仍保留：清死文件切片 + PRAGMA optimize。

    # 启动定期存储自动压实与垃圾回收线程 (每 6 小时检查并清理 1 天前过期事务快照与孤儿索引)
    def _periodic_optimize_worker():
        while running:
            time.sleep(6 * 3600)
            if not running:
                break
            try:
                print("[HermesKB Daemon] Running periodic SQLite maintenance & cleanup...")
                stats = watcher.engine.optimize_table(older_than_days=1)
                print(f"[KB Daemon] Periodic optimize stats: {stats}")
            except Exception as e:
                print(f"[KB Daemon] Periodic optimize error: {e}")

    optimize_thread = threading.Thread(target=_periodic_optimize_worker, daemon=True)
    optimize_thread.start()
    print("[KB Daemon] Periodic storage compaction worker started (interval: 6h).")
    print("[KB Daemon] Real-time file observer started.")

    while running:
        time.sleep(1)

    observer.stop()
    observer.join()
    print("[KB Daemon] Shutdown complete.")

if __name__ == "__main__":
    main()
