import os
import sys
import time
import hashlib
import sqlite3
import logging
import threading
import traceback
import contextlib
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)

from .config import get_config
from .chunker import MarkdownChunker
from .engine import KBEngine

class StateDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS file_records (
                    file_path TEXT PRIMARY KEY,
                    file_hash TEXT,
                    mtime REAL,
                    chunk_count INTEGER,
                    updated_at REAL
                )
            """)

    def get_record(self, file_path: str):
        cur = self.conn.cursor()
        cur.execute("SELECT file_hash, mtime, chunk_count FROM file_records WHERE file_path = ?", (file_path,))
        return cur.fetchone()

    def set_record(self, file_path: str, file_hash: str, mtime: float, chunk_count: int):
        with self.conn:
            self.conn.execute("""
                INSERT INTO file_records (file_path, file_hash, mtime, chunk_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    file_hash = excluded.file_hash,
                    mtime = excluded.mtime,
                    chunk_count = excluded.chunk_count,
                    updated_at = excluded.updated_at
            """, (file_path, file_hash, mtime, chunk_count, time.time()))

    def delete_record(self, file_path: str):
        with self.conn:
            self.conn.execute("DELETE FROM file_records WHERE file_path = ?", (file_path,))

    def get_all_indexed_paths(self) -> set:
        cur = self.conn.cursor()
        cur.execute("SELECT file_path FROM file_records")
        return set(row[0] for row in cur.fetchall())

class KnowledgeWatcher:
    def __init__(self):
        self.config = get_config()
        # 同步状态已并入 kb.sqlite3 的 file_records 表，和切片写入同事务。
        # 原 StateDB 指向独立的 sync_state.sqlite，跨库无法原子。
        self._file_locks = {}
        self._locks_guard = threading.Lock()
        self.chunker = MarkdownChunker(
            min_chars=self.config["chunker"]["min_chunk_chars"],
            max_chars=self.config["chunker"]["max_chunk_chars"],
            target_chars=self.config["chunker"]["target_chunk_chars"]
        )
        self.engine = KBEngine()
        self.watch_dirs = self.config["watch_dirs"]
        # SQLite 后端：写入即索引，不存在独立的「索引刷新」阶段。
        # 历史实现这里跑着 _fts_refresh_worker —— 任何文件变动静默 2 秒后就
        # create_fts_index(replace=True) 重建整张表（61k 行实测 46s/次，
        # 累计吃掉 40 小时单核）。FTS5 的 INSERT 本身就是增量索引，此线程已删除。

    def calculate_file_hash(self, file_path: str) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    CONVERT_EXTS = (".docx", ".pdf", ".pptx", ".xlsx", ".xls", ".epub")

    def _is_excluded_path(self, file_path: str) -> bool:
        """Apply the same source policy to scans and filesystem events."""
        path = os.path.normpath(file_path)
        components = [part for part in path.split(os.sep) if part]
        filters = self.config.get("source_filters", {})
        excluded_paths = {
            os.path.normpath(os.path.expanduser(p))
            for p in filters.get("exclude_paths", [])
        }
        if path in excluded_paths:
            return True
        # ---- 路径前缀排除 ----
        # 为什么需要独立字段：`exclude_paths` 是全路径精确匹配，排不掉整棵子树；
        # `exclude_dirs` 按「目录名」匹配，会把两个根下所有同名目录一起排掉
        # （例如只想跳过 /srv/hermes-kb-inbox/reflections，却连
        #  其他被软链或重命名的监控目录也一起失效）。
        # 双根 reflections 是同一批卡的 md5 镜像（写入侧硬性要求双根落盘），
        # 索引只需建一份，否则注入时前 N 块会浪费在同一张卡的两份副本上。
        for prefix in filters.get("exclude_path_prefixes", []):
            pfx = os.path.normpath(os.path.expanduser(prefix))
            if path == pfx or path.startswith(pfx + os.sep):
                return True
        if any(part in filters.get("exclude_dirs", []) for part in components):
            return True
        if any(
            any(marker in part for marker in filters.get("exclude_component_substrings", []))
            for part in components
        ):
            return True
        if any(
            any(part.startswith(prefix) for prefix in filters.get("exclude_file_prefixes", []))
            for part in components
        ):
            return True
        basename = os.path.basename(path)
        if any(basename.endswith(suffix) for suffix in filters.get("exclude_file_suffixes", [])):
            return True
        return False

    def _is_supported_path(self, file_path: str) -> bool:
        return file_path.endswith((".md", ".markdown", ".txt") + self.CONVERT_EXTS)

    def _extract_text(self, file_path: str) -> str:
        """按格式提取文本：纯文本直读，Office/PDF 用 MarkItDown 转 Markdown。"""
        ext = os.path.splitext(file_path)[1].lower()
        if ext in self.CONVERT_EXTS:
            try:
                from markitdown import MarkItDown
                md = MarkItDown()
                result = md.convert(file_path)
                content = getattr(result, "text_content", None) or ""
                if not content:
                    raise ValueError("MarkItDown returned empty content")
                return content
            except Exception as e:
                print(f"[KBWatcher] MarkItDown convert failed for {file_path}: {e}")
                return ""
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    @contextlib.contextmanager
    def _file_lock(self, file_path: str):
        """同一文件的同步串行化。

        full_scan 和 inotify 事件可能同时碰一个文件：扫描线程正在读大文件，
        用户保存触发了另一个事件。没有锁时两个线程会各删一次、各写一次，
        后写的那份覆盖前面的，chunk 可能一半新一半旧。
        """
        with self._locks_guard:
            lk = self._file_locks.get(file_path)
            if lk is None:
                lk = threading.Lock()
                self._file_locks[file_path] = lk
        with lk:
            yield

    def _prepare_sync(self, file_path: str):
        """读文件 + 切片 + 算 hash，**不写库**。

        返回 None 表示未变更（按 file_hash 跳过）。
        返回 {"empty": True} 表示解析不出正文，调用方负责清索引。
        拆出这一步是为了让 full_scan 能先批量收集、再一次性 embedding。
        """
        mtime = os.path.getmtime(file_path)
        file_hash = self.calculate_file_hash(file_path)
        rec = self.engine.get_file_record(file_path)
        if rec and rec[0] == file_hash:
            return None
        content = self._extract_text(file_path)
        if not content:
            return {"file_path": file_path, "file_hash": file_hash,
                    "mtime": mtime, "empty": True}
        chunks = self.chunker.split_markdown_into_sections(content, file_path)
        return {"file_path": file_path, "chunks": chunks,
                "file_hash": file_hash, "mtime": mtime}

    def _purge_empty(self, item: dict):
        """文件解析不出正文：清掉旧切片并记状态。"""
        purged = self.engine.delete_by_file_path(item["file_path"])
        self.engine.set_file_record(
            item["file_path"], item["file_hash"], item["mtime"], 0
        )
        print(
            f"[KBWatcher] Empty content, purged old index "
            f"({'yes' if purged else 'nothing'}): {item['file_path']}"
        )

    def sync_file(self, file_path: str):
        if self._is_excluded_path(file_path) or not self._is_supported_path(file_path):
            return
        if not os.path.exists(file_path):
            return

        with self._file_lock(file_path):
            try:
                item = self._prepare_sync(file_path)
                if item is None:
                    return
                print(f"[KBWatcher] Processing: {file_path}")
                if item.get("empty"):
                    self._purge_empty(item)
                    return
                # 原子替换：embedding 先在事务外算好（失败则旧数据原样保留），
                # 再单事务删旧 + 写新 + 更新 file_records。
                result = self.engine.replace_file_chunks(
                    file_path, item["chunks"], item["file_hash"], item["mtime"]
                )
                print(
                    f"[KBWatcher] Indexed {result['inserted']} chunks "
                    f"(replaced {result['deleted']}) for: {file_path}"
                )
            except Exception as e:
                print(f"[KBWatcher] Error syncing {file_path}: {e}")
                traceback.print_exc()

    def remove_file(self, file_path: str):
        with self._file_lock(file_path):
            try:
                print(f"[KBWatcher] Removing index for: {file_path}")
                self.engine.delete_by_file_path(file_path)
                self.engine.delete_file_record(file_path)
            except Exception as e:
                print(f"[KBWatcher] Error removing {file_path}: {e}")

    # 每批的攒批上限：既限块数也限字符量。
    # 只按块数会踩 token 上限的坑——128 块 × 1500 字 ≈ 19 万字符，
    # 按 batch_size=12 切组后每组约 1.8 万字符 ≈ 4~5 K token，
    # 再叠加长切片会撞上游的请求体上限。9 万字符是实测的安全值。
    _SCAN_CHUNK_BATCH = 128
    _SCAN_CHAR_BUDGET = 90000

    def full_scan(self):
        """全量扫描：先并行收集变更，再按切片数分批 embedding，最后逐文件原子写。

        为什么不再逐文件同步：`_prepare_sync` 只在内存里读文件+切片（快），
        真正的瓶颈是 embedding 网络调用。以前每个文件单独调一次，
        并发度被文件边界卡死在一个文件的 1~2 个请求里。
        """
        print("[KBWatcher] Starting full scan...")
        current_disk_files = set()
        pending = []

        for w_dir in self.watch_dirs:
            if not os.path.exists(w_dir):
                os.makedirs(w_dir, exist_ok=True)
            for root, dirs, files in os.walk(w_dir):
                # Prune excluded trees before descending into them.
                dirs[:] = [d for d in dirs if not self._is_excluded_path(os.path.join(root, d))]
                for f in files:
                    full_p = os.path.join(root, f)
                    if self._is_excluded_path(full_p):
                        continue
                    if not self._is_supported_path(full_p):
                        continue
                    current_disk_files.add(full_p)
                    try:
                        item = self._prepare_sync(full_p)
                    except Exception as e:
                        print(f"[KBWatcher] Error preparing {full_p}: {e}")
                        continue
                    if item:
                        pending.append(item)

        print(
            f"[KBWatcher] Scan collected {len(current_disk_files)} files, "
            f"{len(pending)} need reindexing"
        )

        # ---- 分批写库 ----
        scan_t0 = time.time()
        i = 0
        batch_no = 0
        files_done = 0
        chunks_done = 0
        errors = 0
        while i < len(pending):
            batch, nchunks, nchars = [], 0, 0
            while i < len(pending):
                if nchunks >= self._SCAN_CHUNK_BATCH:
                    break
                if nchars >= self._SCAN_CHAR_BUDGET:
                    break
                it = pending[i]
                if it.get("empty"):
                    self._purge_empty(it)   # 空文件不进 embedding
                    i += 1
                    continue
                batch.append(it)
                nchunks += len(it["chunks"])
                nchars += sum(len(c.get("content", "") or "") for c in it["chunks"])
                i += 1
            if not batch:
                continue
            batch_no += 1
            bt0 = time.time()
            try:
                results = self.engine.replace_files_batch(batch)
            except Exception as e:
                # embedding 整批失败：这批文件全部保留旧数据，下次扫描重试
                print(
                    f"[KBWatcher] Batch #{batch_no} FAILED "
                    f"({len(batch)} files, {nchunks} chunks kept old data): {e}"
                )
                errors += len(batch)
                continue
            ok = sum(1 for r in results if r.get("ok"))
            errors += len(results) - ok
            files_done += ok
            chunks_done += nchunks
            print(
                f"[KBWatcher] Batch #{batch_no}: {ok}/{len(results)} files, "
                f"{nchunks} chunks, {nchars} chars, {time.time()-bt0:.1f}s "
                f"(progress {i}/{len(pending)})"
            )

        # 清理在磁盘上已被删除的文件索引
        indexed_files = self.engine.get_all_indexed_paths()
        deleted_files = indexed_files - current_disk_files
        for df in deleted_files:
            self.remove_file(df)

        elapsed = time.time() - scan_t0
        msg = (
            f"[KBWatcher] Full scan complete. Monitored: {len(current_disk_files)} files, "
            f"reindexed: {files_done} files / {chunks_done} chunks in {batch_no} batches, "
            f"errors: {errors}, pruned: {len(deleted_files)}, "
            f"index time: {elapsed:.1f}s"
            + (f" ({chunks_done/elapsed:.1f} chunks/s)" if elapsed > 0 and chunks_done else "")
        )
        # logger 在本服务里没配 handler，走 print 才进 journalctl
        print(msg)
        logger.info(msg)

class FileEventHandler(FileSystemEventHandler):
    def __init__(self, watcher: KnowledgeWatcher):
        self.watcher = watcher

    def on_created(self, event):
        if not event.is_directory:
            time.sleep(0.1)
            self.watcher.sync_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            time.sleep(0.1)
            self.watcher.sync_file(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.watcher.remove_file(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.watcher.remove_file(event.src_path)
            time.sleep(0.1)
            self.watcher.sync_file(event.dest_path)

def run_watcher_daemon():
    watcher = KnowledgeWatcher()
    watcher.full_scan()

    observer = Observer()
    handler = FileEventHandler(watcher)

    for w_dir in watcher.watch_dirs:
        os.makedirs(w_dir, exist_ok=True)
        observer.schedule(handler, w_dir, recursive=True)
        print(f"[KBWatcher] Watching directory: {w_dir}")

    observer.start()

    # 启动定期存储自动压实与垃圾回收线程 (每 6 小时检查并清理 7 天前过期事务快照)
    def _periodic_optimize_worker():
        while True:
            time.sleep(6 * 3600)
            try:
                logger.info("[HermesKB Watcher] Running periodic SQLite maintenance & cleanup...")
                stats = watcher.engine.optimize_table(older_than_days=7)
                logger.info(f"[KBWatcher] Periodic optimize stats: {stats}")
            except Exception as e:
                logger.warning(f"[KBWatcher] Periodic optimize error: {e}")

    optimize_thread = threading.Thread(target=_periodic_optimize_worker, daemon=True)
    optimize_thread.start()
    print("[KBWatcher] Periodic storage compaction worker started (interval: 6h)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    run_watcher_daemon()
