"""SQLite 单文件知识库引擎。

移植来源：本目录 engine.py 的 LanceDB 版本（已备份为 engine_lancedb.py）。

设计要点（2026-10-20 重写）
--------------------------------------------------------------------------
1. **单文件存储**：一个 `kb.sqlite3` 同时装正文、元数据和向量（float32 BLOB）。
   LanceDB 那套把 FTS/ANN 索引放在表内、每次刷新要全表重算，
   `create_fts_index(replace=True)` 追加 1 行也要 7.66s（实测），
   60k 行规模下这等于每两小时烧掉几十小时 CPU。
2. **不用向量索引**：实测 61711 行 × 1024 维暴力余弦只要 12.8ms，
   比维护 ANN 索引划算得多，且永远不会出现「索引落后 N 行」。
3. **FTS5 真增量**：INSERT 即索引，实测 500 行 7~25ms。
   切词按字符类型分流：汉字走二字滑窗，ASCII 整词交给 unicode61。
   整串一律 bigram 会让英文碎成字母对，IDF 塌到 0.31、七成文档全命中；
   而 trigram 又对 2 字中文全失明（实测 '路由' 0 条）。依据见 _fts_tokens。
4. **对外接口与 LanceDB 版完全一致**，调用方（watcher/cli/注入插件）零改动。
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time

import numpy as np
import requests

from .config import get_config

logger = logging.getLogger(__name__)


class _VectorStore:
    """增量向量存储：预分配 ndarray + tombstone，写入摊销 O(1)。

    为什么不直接 np.vstack
    ----------------------
    vstack 每次都拷贝整个矩阵。实测追加 100 行：6 万行规模 27.95ms、
    12 万行 57.35ms，严格线性。改成预分配后 0.016~0.023ms 且**与规模无关**
    （1353x @6万 / 3563x @12万）。
    删除同理：np.isin + 布尔掩码拷贝 6 万行 43.02ms，改 tombstone 后 0.08ms（545x）。

    为什么 tombstone 不是直接删
    ---------------------------
    真删要移动后面所有行（O(N) 拷贝）。tombstone 只翻一个 bool，查询时多取
    4 倍候选再过 alive 掩码过滤。实测 1000 条 tombstone 对查询影响 < 1ms。
    攒够了由 compact() 一次性收拾（6 万行删 1.5 万后压实 51ms）。
    """

    def __init__(self, dim):
        self.dim = dim
        self._lock = threading.RLock()
        self._ids = np.empty(0, dtype=np.int64)
        self._mat = np.empty((0, dim), dtype=np.float32)
        self._alive = np.empty(0, dtype=bool)
        self._size = 0   # 已占用的槽位数
        self._tomb = 0   # 其中已删除（tombstone）的数量
        self.loaded = False

    def load(self, ids, vecs):
        """整体载入并 L2 归一化，容量预留 25% 给后续 append。"""
        with self._lock:
            n = len(ids)
            cap = max(1024, int(n * 1.25) + 1)
            self._ids = np.empty(cap, dtype=np.int64)
            self._mat = np.empty((cap, self.dim), dtype=np.float32)
            self._alive = np.zeros(cap, dtype=bool)
            if n:
                arr = np.asarray(vecs, dtype=np.float32).reshape(n, self.dim)
                norms = np.linalg.norm(arr, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                self._ids[:n] = np.asarray(ids, dtype=np.int64)
                self._mat[:n] = (arr / norms).astype(np.float32)
                self._alive[:n] = True
            self._size = n
            self._tomb = 0
            self.loaded = True
            return n

    def _grow(self, need):
        cap = self._ids.shape[0]
        if self._size + need <= cap:
            return
        newcap = cap or 1024
        while newcap < self._size + need:
            newcap *= 2
        ni = np.empty(newcap, dtype=np.int64)
        ni[: self._size] = self._ids[: self._size]
        nm = np.empty((newcap, self.dim), dtype=np.float32)
        nm[: self._size] = self._mat[: self._size]
        na = np.zeros(newcap, dtype=bool)
        na[: self._size] = self._alive[: self._size]
        self._ids, self._mat, self._alive = ni, nm, na
        logger.info("[KBEngine] vector store grown to capacity %d", newcap)

    def append(self, ids, vecs):
        n = len(ids)
        if not n:
            return
        with self._lock:
            self._grow(n)
            arr = np.asarray(vecs, dtype=np.float32).reshape(n, self.dim)
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            s = self._size
            self._ids[s:s + n] = np.asarray(ids, dtype=np.int64)
            self._mat[s:s + n] = (arr / norms).astype(np.float32)
            self._alive[s:s + n] = True
            self._size += n

    def remove(self, ids):
        ids = list(ids)
        if not ids:
            return 0
        with self._lock:
            if self._size == 0:
                return 0
            m = np.isin(self._ids[: self._size], np.asarray(ids, dtype=np.int64))
            self._alive[: self._size] &= ~m
            k = int(m.sum())
            self._tomb += k
            return k

    def search(self, qv, k):
        """返回 (ids, scores)，已按分数降序，最多 k 条（不含 tombstone）。"""
        with self._lock:
            n = self._size
            if n == 0:
                return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float32)
            scores = self._mat[:n] @ qv
            k = min(int(k), n)
            if self._tomb == 0:
                idx = np.argpartition(-scores, k - 1)[:k]
                idx = idx[np.argsort(-scores[idx])]
                return self._ids[idx], scores[idx]
            # 有 tombstone：多取候选再过 alive 掩码
            pool = min(n, max(k * 4, k + 64))
            cand = np.argpartition(-scores, pool - 1)[:pool]
            cand = cand[self._alive[cand]]
            if len(cand) < k:
                # 候选被 tombstone 吃光了，兜底对全部存活行排序
                cand = np.flatnonzero(self._alive[:n])
            cand = cand[np.argsort(-scores[cand])][:k]
            return self._ids[cand], scores[cand]

    def compact(self):
        """把 tombstone 行真正移出，返回回收的槽位数。"""
        with self._lock:
            if self._tomb == 0:
                return 0
            n = self._size
            m = self._alive[:n]
            k = int(m.sum())
            if k:
                self._ids[:k] = self._ids[:n][m]
                self._mat[:k] = self._mat[:n][m]
            self._alive[:k] = True
            self._alive[k:n] = False
            freed = n - k
            self._size = k
            self._tomb = 0
            return freed

    def stats(self):
        with self._lock:
            return {
                "size": self._size,
                "capacity": int(self._ids.shape[0]),
                "tombstones": self._tomb,
                "matrix_mb": round(self._mat.nbytes / 1024 / 1024, 1),
            }


class KBEngine:
    """知识库引擎（SQLite 后端，类名沿用历史名以兼容调用方）。"""

    # FTS 切词：汉字段走二字滑窗，ASCII 段整词保留。分开处理的原因见 _fts_tokens。
    _TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9_.\-/]+|[\u4e00-\u9fff]+")

    # 切词口径版本。改了 _fts_tokens 的切法就必须 +1，_init_db 据此重建 fts。
    _FTS_TOKENIZER_VERSION = 2

    # MATCH 表达式里 AND 组合的上限：太长会拖慢 FTS 且容易 0 命中
    _FTS_AND_MAX_TERMS = 16
    _FTS_OR_FALLBACK_COUNT = 8

    def __init__(self):
        self.config = get_config()
        self.db_dir = self.config.get("db_dir") or os.path.expanduser("~/.hermes/data/hermes_kb")
        os.makedirs(self.db_dir, exist_ok=True)
        self.kb_path = self.config.get("sqlite_path") or os.path.join(
            self.db_dir, "kb.sqlite3"
        )

        self._vec_lock = threading.Lock()
        self._dim = int(self.config.get("embedding", {}).get("dimensions", 1024) or 1024)
        # 增量向量存储（预分配 + tombstone），替代原来的 _vec_ids/_vec_mat 全量拷贝
        self._vs = _VectorStore(self._dim)

        self._init_db()
        self._ensure_cache_model()

    # ------------------------------------------------------------------ 建库
    def _conn(self):
        conn = sqlite3.connect(self.kb_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id            INTEGER PRIMARY KEY,
                    chunk_id      TEXT UNIQUE NOT NULL,
                    file_path     TEXT NOT NULL,
                    heading_path  TEXT DEFAULT '',
                    title         TEXT DEFAULT '',
                    content       TEXT NOT NULL,
                    token_estimate INTEGER DEFAULT 0,
                    vector        BLOB NOT NULL,
                    updated_at    REAL NOT NULL,
                    text_hash     TEXT
                )
                """
            )
            # 老库是 ALTER 加上来的，新库直接建在 DDL 里。
            # 只靠 ALTER 的话，重建库 / 换机器 / 切后端时
            # 这张表没有 text_hash，向量缓存整条链会崩在 "no such column"。
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(chunks)")
            }
            if "text_hash" not in existing_cols:
                conn.execute("ALTER TABLE chunks ADD COLUMN text_hash TEXT")
                logger.info("[KBEngine] migrated chunks: added text_hash column")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(file_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_text_hash ON chunks(text_hash)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kb_meta ("
                "key TEXT PRIMARY KEY, value TEXT, updated_at REAL)"
            )
            # 文件级同步状态：并进同一个库，才能和 chunks 写入同事务。
            # 原来是独立的 sync_state.sqlite，跨库无法原子——
            # 写库成功但记状态失败会重复索引，反过来会丢数据。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_records (
                    file_path   TEXT PRIMARY KEY,
                    file_hash   TEXT,
                    mtime       REAL,
                    chunk_count INTEGER,
                    updated_at  REAL
                )
                """
            )
            # FTS5：bi 列存 _fts_tokens 切好的 token 串（汉字二字滑窗 + ASCII 整词）。
            # 行号用 FTS5 自带的 rowid（= chunks.id），不再另开 id UNINDEXED 列。
            # 原因：UNINDEXED 列不建索引，`DELETE ... WHERE id = ?` 会 SCAN 整张
            # 虚表——62k 行实测 98.74ms/条；改 rowid 后 0.86ms/条（121x），
            # 批量 IN 再快 1.6x。一次 222 片重索引从 23.0s 降到 0.12s。
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                    bi,
                    tokenize='unicode61'
                )
                """
            )
            # 老库（fts5(bi, id UNINDEXED)）自动迁到 rowid 版。
            # 62k 行重建约 34s，只在首次启动发生一次。
            fts_cols = [r[1] for r in conn.execute("PRAGMA table_info(fts)")]
            if "id" in fts_cols:
                logger.info("[KBEngine] migrating fts: id UNINDEXED -> rowid ...")
                t_mig = time.time()
                conn.execute(
                    "CREATE VIRTUAL TABLE fts_rowid USING fts5(bi, tokenize='unicode61')"
                )
                conn.execute("INSERT INTO fts_rowid(rowid, bi) SELECT id, bi FROM fts")
                conn.execute("DROP TABLE fts")
                conn.execute("ALTER TABLE fts_rowid RENAME TO fts")
                conn.commit()
                logger.info(
                    "[KBEngine] fts migrated to rowid in %.2fs", time.time() - t_mig
                )
            self._migrate_fts_tokenizer(conn)
            conn.commit()

    def _migrate_fts_tokenizer(self, conn):
        """切词口径变了就重建 fts。

        fts.bi 存的是**已经切好的** token 串，切法一变，旧串就配不上新查询——
        而且不报错：MATCH 照样返回结果，只是全变成噪声。所以只能用版本号
        触发重建，探测不出来。
        """
        row = conn.execute(
            "SELECT value FROM kb_meta WHERE key='fts_tokenizer'"
        ).fetchone()
        if row and row[0] == str(self._FTS_TOKENIZER_VERSION):
            return
        t0 = time.time()
        logger.info(
            "[KBEngine] rebuilding fts with tokenizer v%d ...",
            self._FTS_TOKENIZER_VERSION,
        )
        # 先建新表、灌完再换名：中途失败旧 fts 还在，检索不会被打空。
        conn.execute("DROP TABLE IF EXISTS fts_next")
        conn.execute(
            "CREATE VIRTUAL TABLE fts_next USING fts5(bi, tokenize='unicode61')"
        )
        n, batch = 0, []
        for cid, heading, content in conn.execute(
            "SELECT id, heading_path, content FROM chunks"
        ):
            batch.append((cid, self._fts_tokens(f"{heading or ''} {content or ''}")))
            n += 1
            if len(batch) >= 500:
                conn.executemany(
                    "INSERT INTO fts_next(rowid, bi) VALUES (?,?)", batch
                )
                batch.clear()
        if batch:
            conn.executemany("INSERT INTO fts_next(rowid, bi) VALUES (?,?)", batch)
        conn.execute("DROP TABLE fts")
        conn.execute("ALTER TABLE fts_next RENAME TO fts")
        conn.execute(
            "INSERT OR REPLACE INTO kb_meta(key, value, updated_at) VALUES(?,?,?)",
            ("fts_tokenizer", str(self._FTS_TOKENIZER_VERSION), time.time()),
        )
        logger.info("[KBEngine] fts rebuilt: %d rows in %.2fs", n, time.time() - t0)

    @staticmethod
    def _fts_delete(conn, ids):
        """按 rowid 批量删 fts 行。rowid 走 FTS5 自带 B 树，O(log N)。

        旧写法 `WHERE id = ?` 里 id 是 UNINDEXED 列，每次删除 SCAN 整张虚表：
        62k 行 98.74ms/条。这里批量 IN 后 222 条只要 116ms（198x）。
        分 500 一批，避开 SQLite 变量数上限。
        """
        ids = [int(i) for i in ids if i is not None]
        if not ids:
            return 0
        for s in range(0, len(ids), 500):
            part = ids[s : s + 500]
            ph = ",".join("?" * len(part))
            conn.execute(f"DELETE FROM fts WHERE rowid IN ({ph})", part)
        return len(ids)

    # ---------------------------------------------------------- FTS 切词工具
    @classmethod
    def _fts_tokens(cls, text: str) -> str:
        """把正文切成 FTS token 串：汉字段走二字滑窗，ASCII 段整词保留。

        空白先归一化：否则 "端\\n口" 会切出 "端\\n" / "\\n口" 而丢掉 "端口"，
        导致跨行位置查不到（实测 LIKE 与 bigram 命中数不一致就是这个原因）。

        两种切法并列不是口味问题，是 IDF 决定的（全库 N=54,384 实测）：
        - 汉字必须自己切，unicode61 不切汉字（整段会变成一个 token）。二字
          滑窗够用——现代汉语双音节词占多数，实测中文 bigram 平均 IDF 3.59
          （"端口" 587 篇 1.1%，"快照" 1,074 篇 2.0%）。
        - ASCII 整词原样留下，交给 unicode61 按符号切。英文只有 26 个字母、
          二字组才 676 种，任意英文文本都覆盖绝大多数：实测英文 bigram 平均
          命中 73.7% 的库、IDF 只有 0.31，切碎等于往候选池灌噪声。
          "restic" 切成 re/es/st/ti/ic 后命中 27,356 条而真命中仅 206 条
          （133 倍差距），整词保留后回落到 206；"space-y-2" 6,415 → 22。
        """
        s = " ".join(str(text or "").split())
        if not s:
            return ""
        out = []
        for m in cls._TOKEN_RUN_RE.finditer(s):
            run = m.group(0)
            if run[0].isascii() or len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i : i + 2] for i in range(len(run) - 1))
        return " ".join(out)

    def _build_fts_expr(self, query: str):
        """构造 FTS5 MATCH 表达式：每个 token 单独加引号，token 之间 AND。

        每个 token 必须自己带引号，两个原因，都不是风格问题：

        1. **不加引号会语法报错**。`space-y-2`、`p-3` 这类专名里的 `-` 是
           FTS5 的运算符，`space-y-2` 会被读成「列 y 上的操作」，直接抛
           `OperationalError: no such column: y`，被调用方当 0 结果吞掉——
           于是所有带连字符的专名，词法这一路一直是静默失效的。
        2. **加引号才能保住专名内部的相邻性**。`"bpf_redirect_peer"` 由
           unicode61 切成 `bpf redirect peer` 并作为短语匹配，三个子词必须
           相邻；不加引号就成了三个独立词，`bpf` 和 `peer` 隔着几行也能命中。

        但引号只能包单个 token，不能包整串：token 已经是真实单词了，把
        `"a b c"` 当成一个短语等于要求这几个词在原文里**连续挨着写**，
        实测 `fts_candidate_limit candidate_limit rrf` 这样查是 0 命中，
        改成三个 token 各自加引号再 AND 就有 26 条。词与词之间只要求共现，
        顺序和距离不设限，这是 BM25 该有的语义。
        """
        terms = self._fts_tokens(query).split()
        if not terms:
            return None
        return " AND ".join(f'"{t}"' for t in terms[: self._FTS_AND_MAX_TERMS])

    def _build_fts_expr_or(self, query: str):
        terms = self._fts_tokens(query).split()
        if not terms:
            return None
        # 取靠前的 token（通常携带主要信息），OR 组合保证有召回。
        # 同样必须逐个加引号，否则 p-3 之类会抛 OperationalError。
        return " OR ".join(f'"{t}"' for t in terms[: self._FTS_OR_FALLBACK_COUNT])

    # -------------------------------------------------------------- 向量矩阵
    def _load_vectors(self):
        """从 SQLite 整体载入向量库。容量预留 25%，后续 append 不再拷贝全表。"""
        ids, vecs = [], []
        with self._conn() as conn:
            for cid, blob in conn.execute("SELECT id, vector FROM chunks"):
                ids.append(cid)
                vecs.append(blob)
        if vecs:
            mat = np.frombuffer(b"".join(vecs), dtype=np.float32).reshape(
                len(vecs), self._dim
            )
        else:
            mat = np.zeros((0, self._dim), dtype=np.float32)
        n = self._vs.load(ids, mat)
        logger.info(
            "[KBEngine] vector store loaded: %d rows x %d dim, capacity %d",
            n, self._dim, self._vs.stats()["capacity"],
        )

    def _ensure_vectors(self):
        if not self._vs.loaded:
            with self._vec_lock:
                if not self._vs.loaded:
                    t0 = time.time()
                    self._load_vectors()
                    logger.info("[KBEngine] vector load took %.2fs", time.time() - t0)
        return self._vs

    def _append_vectors(self, ids, vecs):
        if ids:
            self._vs.append(ids, vecs)

    def _remove_vectors(self, ids):
        if ids:
            self._vs.remove(ids)

    # ------------------------------------------------------- embedding / rerank
    def compute_embedding(self, text: str) -> list:
        emb_cfg = self.config.get("embedding", {})
        api_key = emb_cfg.get("api_key", "")
        base_url = emb_cfg.get("base_url", "https://api.siliconflow.cn/v1")
        model = emb_cfg.get("model", "BAAI/bge-m3")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "input": [text[:8000]],
            "encoding_format": "float",
        }

        for attempt in range(3):
            try:
                res = requests.post(
                    f"{base_url}/embeddings", headers=headers, json=payload, timeout=15
                )
                if res.status_code == 200:
                    data = res.json()
                    return data["data"][0]["embedding"]
                time.sleep(0.5)
            except Exception:
                time.sleep(0.5)
        return []

    def compute_embeddings_batch(
        self, texts: list, batch_size: int = None, max_workers: int = 8
    ) -> list:
        if not texts:
            return []
        emb_cfg = self.config.get("embedding", {})
        # batch_size 必须读配置：此前默认硬编码 32，导致每次请求发 32 条切片
        # ≈ 2.1 万 token，超出小批量上限（约 1.2 万 token）→ 全被计为大批量扣费。
        if batch_size is None:
            try:
                batch_size = int(emb_cfg.get("batch_size", 12) or 12)
            except (TypeError, ValueError):
                batch_size = 12
        batch_size = max(1, batch_size)
        api_key = emb_cfg.get("api_key", "")
        base_url = emb_cfg.get("base_url", "https://api.siliconflow.cn/v1")
        model = emb_cfg.get("model", "BAAI/bge-m3")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        from concurrent.futures import ThreadPoolExecutor

        def embed_mini_batch(batch_idx_and_items):
            batch_idx, batch_items = batch_idx_and_items
            truncated_texts = [t[:8000] for t in batch_items]
            payload = {
                "model": model,
                "input": truncated_texts,
                "encoding_format": "float",
            }
            for attempt in range(3):
                try:
                    res = requests.post(
                        f"{base_url}/embeddings",
                        headers=headers,
                        json=payload,
                        timeout=30,
                    )
                    if res.status_code == 200:
                        data = res.json()
                        data_sorted = sorted(data["data"], key=lambda x: x["index"])
                        return batch_idx, [item["embedding"] for item in data_sorted]
                    time.sleep(0.5)
                except Exception:
                    time.sleep(0.5)
            # fallback single
            return batch_idx, [self.compute_embedding(t) for t in batch_items]

        # ---- 按「条数上限 + 字符预算」双重切组 ----
        # 只按条数切不够：单请求 token 超过约 1.2 万会被 Gitee 判为大批量
        # （响应时间恒定 ~718ms、按大批量计费），再往上还会被静默截断——
        # 向量质量掉了却没有任何报错。
        # 实测拐点落在 11,512（小批量）~ 13,402（大批量）之间，
        # 而本库切片平均 996 字符，12 条 = 11,952 token 正好贴着线，
        # 批次里只要混进几条 1500 字的长切片就越界了。
        # 所以用字符预算硬控：11,000 字符留足余量。
        char_budget = 11000
        try:
            char_budget = int(emb_cfg.get("char_budget", 11000) or 11000)
        except (TypeError, ValueError):
            char_budget = 11000

        groups, cur, cur_chars = [], [], 0
        for t in texts:
            tlen = min(len(t), 8000)          # 与 truncated_texts 截断口径一致
            if cur and (len(cur) >= batch_size or cur_chars + tlen > char_budget):
                groups.append(cur)
                cur, cur_chars = [], 0
            cur.append(t)
            cur_chars += tlen
        if cur:
            groups.append(cur)

        results_map = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for g_idx, embs in executor.map(embed_mini_batch, enumerate(groups)):
                results_map[g_idx] = embs

        all_embs = []
        for g_idx, g in enumerate(groups):
            embs = results_map.get(g_idx)
            if not embs or len(embs) != len(g):
                embs = [[0.0] * self._dim] * len(g)
            all_embs.extend(embs)

        if len(groups) > 1:
            logger.debug(
                "[KBEngine] embed %d texts -> %d groups (%d workers), budget %d chars",
                len(texts), len(groups), max_workers, char_budget,
            )
        return all_embs

    def rerank_pairs(self, query: str, documents: list) -> list:
        if not documents:
            return []
        rerank_cfg = self.config.get("reranker", {})
        api_key = rerank_cfg.get("api_key", "")
        base_url = rerank_cfg.get(
            "base_url", "https://api.siliconflow.cn/v1/rerank"
        )
        model = rerank_cfg.get("model", "BAAI/bge-reranker-v2-m3")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "query": query,
            "documents": [d[:4000] for d in documents],
            "top_n": len(documents),
            "return_documents": False,
        }

        req_url = base_url if base_url.endswith("/rerank") else f"{base_url}/rerank"

        # 每次重试都要留痕：降级最终只打一句「Reranker unavailable」，
        # 分不清是连不上、超时还是 5xx —— 2026-10-25 统计 25 次降级时就是盲的。
        # 这里按 attempt 逐次记异常类型/状态码/耗时，末尾再汇总一次。
        _rt_errs = []
        for attempt in range(3):
            _t0 = time.monotonic()
            try:
                res = requests.post(req_url, headers=headers, json=payload, timeout=15)
                _dt = (time.monotonic() - _t0) * 1000
                if res.status_code == 200:
                    data = res.json()
                    results = data.get("results", [])
                    if attempt:
                        logger.info(
                            "[KBEngine] rerank recovered on attempt %d/3 in %.0fms (prev errors: %s)",
                            attempt + 1, _dt, _rt_errs,
                        )
                    return sorted(
                        results, key=lambda x: x["relevance_score"], reverse=True
                    )
                # 非 200：把状态码和响应体前 200 字记下来（大量 4xx/5xx 都死在这一行）
                _rt_errs.append(f"HTTP{res.status_code} {res.text[:200]}")
                logger.warning(
                    "[KBEngine] rerank attempt %d/3 non-200: status=%s in %.0fms body=%s",
                    attempt + 1, res.status_code, _dt, res.text[:200],
                )
                time.sleep(0.5)
            except Exception as exc:
                _dt = (time.monotonic() - _t0) * 1000
                _rt_errs.append(f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "[KBEngine] rerank attempt %d/3 raised %s after %.0fms: %s",
                    attempt + 1, type(exc).__name__, _dt, exc,
                )
                time.sleep(0.5)
        logger.warning(
            "[KBEngine] rerank gave up after 3 attempts: url=%s docs=%d timeout=15s errors=%s",
            req_url, len(documents), _rt_errs,
        )
        return []

    # -------------------------------------------------------------- 写入/删除
    def add_chunks(self, chunks: list):
        if not chunks:
            return
        vectors = self._embed_or_raise(chunks)

        now = time.time()
        rows, fts_rows, new_ids, new_vecs = [], [], [], []
        with self._conn() as conn:
            for i, c in enumerate(chunks):
                v = vectors[i]
                chunk_id = c.get("chunk_id") or c.get("id") or hashlib.md5(
                    f"{c.get('file_path','')}::{i}::{c.get('content','')[:50]}".encode(
                        "utf-8"
                    )
                ).hexdigest()
                heading = c.get("heading_path", "") or ""
                title = heading.split(" > ")[-1] if " > " in heading else heading
                content = c.get("content", "") or ""
                token_estimate = int(
                    c.get("char_count", len(content)) / 2
                )
                vec_blob = np.asarray(v, dtype=np.float32).tobytes()
                cur = conn.execute(
                    """
                    INSERT INTO chunks
                        (chunk_id, file_path, heading_path, title, content,
                         token_estimate, vector, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        content=excluded.content,
                        heading_path=excluded.heading_path,
                        title=excluded.title,
                        token_estimate=excluded.token_estimate,
                        vector=excluded.vector,
                        updated_at=excluded.updated_at
                    """,
                    (
                        chunk_id,
                        c.get("file_path", ""),
                        heading,
                        title,
                        content,
                        token_estimate,
                        vec_blob,
                        now,
                    ),
                )
                row_id = cur.lastrowid
                if row_id is None or row_id == 0:
                    fetched = conn.execute(
                        "SELECT id FROM chunks WHERE chunk_id=?", (chunk_id,)
                    ).fetchone()
                    row_id = fetched[0] if fetched else None
                if row_id is None:
                    continue
                # FTS 行：先删同 id 旧行，避免 upsert 之后残留重复
                self._fts_delete(conn, [row_id])
                fts_rows.append((row_id, self._fts_tokens(f"{heading} {content}")))
                new_ids.append(int(row_id))
                new_vecs.append(v)
            if fts_rows:
                conn.executemany("INSERT INTO fts(rowid, bi) VALUES (?,?)", fts_rows)
            conn.commit()

        # 向量矩阵增量同步（已存在的 id 先剔除，避免重复）
        if new_ids:
            self._remove_vectors(new_ids)
            self._append_vectors(new_ids, new_vecs)

    # ------------------------------------------------------------ 向量缓存
    @staticmethod
    def _chunk_text(c: dict) -> str:
        """喂给 embedding 的文本，必须和 chunker 逐字一致。

        chunker.py:137 是 `f"【路径】{heading_path}\n\n{content}"`。
        历史兜底路径写的是 `f"{heading}\n{content}"`（**没有【路径】前缀**），
        同一份内容会算出不同向量：缓存命不中，语义空间也分裂。
        """
        return c.get("vector_text") or (
            f"【路径】{c.get('heading_path', '') or ''}\n\n{c.get('content', '') or ''}"
        )

    @staticmethod
    def _text_key(text: str) -> str:
        return hashlib.md5((text or "").encode("utf-8", "ignore")).hexdigest()

    def _ensure_cache_model(self):
        """embedding 模型或维度变了就作废整个向量缓存。

        不复位的话，换模型后旧向量会被当成"命中"复用，
        两套语义空间混在一个库里，检索结果会莫名其妙地漂。
        """
        emb = self.config.get("embedding", {})
        cur = f"{emb.get('model', '')}|{self._dim}"
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kb_meta ("
                "key TEXT PRIMARY KEY, value TEXT, updated_at REAL)"
            )
            row = conn.execute(
                "SELECT value FROM kb_meta WHERE key='cache_model'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO kb_meta(key,value,updated_at) VALUES('cache_model',?,?)",
                    (cur, time.time()),
                )
            elif row[0] != cur:
                logger.warning(
                    "[KBEngine] embedding model changed %r -> %r, invalidating vector cache",
                    row[0], cur,
                )
                conn.execute("UPDATE chunks SET text_hash = NULL")
                conn.execute(
                    "UPDATE kb_meta SET value=?, updated_at=? WHERE key='cache_model'",
                    (cur, time.time()),
                )

    def _lookup_cached_vectors(self, keys):
        """按 text_hash 批量取已算好的向量。"""
        out = {}
        uniq = list({k for k in keys if k})
        if not uniq:
            return out
        with self._conn() as conn:
            for i in range(0, len(uniq), 400):
                part = uniq[i:i + 400]
                ph = ",".join("?" * len(part))
                for k, blob in conn.execute(
                    f"SELECT text_hash, vector FROM chunks WHERE text_hash IN ({ph})",
                    part,
                ):
                    if blob and len(blob) == self._dim * 4:
                        out[k] = blob
        return out

    def _embed_chunks(self, chunks):
        """带缓存的 embedding：命中复用旧向量，只对 miss 的调上游。

        返回 (vectors, hits, misses)。

        为什么值得：改一个文件的尾部时，前面所有切片的内容没变，
        以前整文件 6 片全重算；现在只算真正变了的那 1~2 片。
        双根 reflections 那种跨文件重复内容也能直接命中。
        """
        if not chunks:
            return [], []
        texts = [self._chunk_text(c) for c in chunks]
        keys = [self._text_key(t) for t in texts]
        cache = self._lookup_cached_vectors(keys)

        result = [None] * len(texts)
        miss_i, miss_t = [], []
        for i, (k, t) in enumerate(zip(keys, texts)):
            blob = cache.get(k)
            if blob is not None:
                result[i] = np.frombuffer(blob, dtype=np.float32)
            else:
                miss_i.append(i)
                miss_t.append(t)

        if miss_t:
            fresh = self._embed_texts_or_raise(miss_t)
            for i, v in zip(miss_i, fresh):
                result[i] = v
        hit_flags = [r is not None and i not in set(miss_i) for i, r in enumerate(result)]
        # 更直接的写法：先建 miss 集合再判定
        miss_set = set(miss_i)
        hit_flags = [i not in miss_set for i in range(len(texts))]
        return result, hit_flags

    def _embed_texts_or_raise(self, texts):
        """算 embedding，任何一条不合法就抛。

        以前失败会静默写 `[0.0]*dim`：归一化后仍是零，`mat @ qv` 恒为 0，
        这条切片永远不会被向量路召回，只靠 FTS 兜着，而且没人知道它坏了。
        fail loud 比静默降级好——调用方会记日志并在下次 sync 重试。
        """
        if not texts:
            return []
        vectors = self.compute_embeddings_batch(texts)

        # 校验不能写 `not v`：v 若是 numpy.ndarray，`bool(array)` 对多元素数组
        # 会抛 "truth value of an array is ambiguous"。上游现在返回 list，
        # 但任何一处换成 ndarray 就会把整条写入链炸掉。用 np.any 统一处理。
        bad = []
        for i, v in enumerate(vectors):
            if v is None:
                bad.append(i)
                continue
            try:
                if len(v) != self._dim:
                    bad.append(i)
                    continue
                if not np.any(v):
                    bad.append(i)
            except (TypeError, ValueError):
                bad.append(i)

        if bad:
            raise RuntimeError(
                f"embedding failed for {len(bad)}/{len(texts)} texts "
                f"(indexes {bad[:5]}{'...' if len(bad) > 5 else ''}); "
                f"refusing to write zero vectors"
            )
        return vectors

    def _embed_or_raise(self, chunks):
        """兼容旧调用：chunks -> vectors（走缓存）。"""
        vectors, hit_flags = self._embed_chunks(chunks)
        return vectors

    # ------------------------------------------------------------ 同步状态
    def get_file_record(self, file_path: str):
        """返回 (file_hash, mtime, chunk_count) 或 None。"""
        with self._conn() as conn:
            return conn.execute(
                "SELECT file_hash, mtime, chunk_count FROM file_records WHERE file_path = ?",
                (file_path,),
            ).fetchone()

    def set_file_record(self, file_path, file_hash, mtime, chunk_count):
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO file_records (file_path, file_hash, mtime, chunk_count, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(file_path) DO UPDATE SET
                    file_hash=excluded.file_hash, mtime=excluded.mtime,
                    chunk_count=excluded.chunk_count, updated_at=excluded.updated_at
                """,
                (file_path, file_hash, mtime, chunk_count, time.time()),
            )

    def delete_file_record(self, file_path: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM file_records WHERE file_path = ?", (file_path,))

    def get_all_indexed_paths(self) -> set:
        with self._conn() as conn:
            return {
                r[0] for r in conn.execute("SELECT file_path FROM file_records")
            }

    def _write_chunks_tx(self, file_path, chunks, vectors, file_hash=None, mtime=None):
        """把「已算好向量」的切片原子写入：单事务删旧+写新+更新 file_records。

        拆出来是为了让 batch 路径能复用：embedding 批量算一次，
        写库仍按文件逐个走事务，既拿到并发也保住原子性。
        """
        now = time.time()
        fts_rows, new_ids, new_vecs = [], [], []
        with self._conn() as conn:
            old_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM chunks WHERE file_path = ?", (file_path,)
                )
            ]
            if old_ids:
                self._fts_delete(conn, old_ids)
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))

            for i, c in enumerate(chunks):
                v = vectors[i]
                chunk_id = c.get("chunk_id") or c.get("id") or hashlib.md5(
                    f"{file_path}::{i}::{c.get('content', '')[:50]}".encode("utf-8")
                ).hexdigest()
                heading = c.get("heading_path", "") or ""
                title = heading.split(" > ")[-1] if " > " in heading else heading
                content = c.get("content", "") or ""
                token_estimate = int(c.get("char_count", len(content)) / 2)
                text_hash = self._text_key(self._chunk_text(c))
                cur = conn.execute(
                    """
                    INSERT INTO chunks
                        (chunk_id, file_path, heading_path, title, content,
                         token_estimate, vector, updated_at, text_hash)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        file_path=excluded.file_path,
                        content=excluded.content,
                        heading_path=excluded.heading_path,
                        title=excluded.title,
                        token_estimate=excluded.token_estimate,
                        vector=excluded.vector,
                        updated_at=excluded.updated_at,
                        text_hash=excluded.text_hash
                    """,
                    (
                        chunk_id, file_path, heading, title, content,
                        token_estimate, np.asarray(v, dtype=np.float32).tobytes(), now,
                        text_hash,
                    ),
                )
                row_id = cur.lastrowid
                if not row_id:
                    fetched = conn.execute(
                        "SELECT id FROM chunks WHERE chunk_id=?", (chunk_id,)
                    ).fetchone()
                    row_id = fetched[0] if fetched else None
                if row_id is None:
                    continue
                self._fts_delete(conn, [row_id])
                fts_rows.append((row_id, self._fts_tokens(f"{heading} {content}")))
                new_ids.append(int(row_id))
                new_vecs.append(v)

            if fts_rows:
                conn.executemany("INSERT INTO fts(rowid, bi) VALUES (?,?)", fts_rows)

            # 同步状态和切片写在同一事务 —— 要么全成，要么全不成。
            if file_hash is not None:
                conn.execute(
                    """
                    INSERT INTO file_records
                        (file_path, file_hash, mtime, chunk_count, updated_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        file_hash=excluded.file_hash, mtime=excluded.mtime,
                        chunk_count=excluded.chunk_count, updated_at=excluded.updated_at
                    """,
                    (file_path, file_hash, mtime or now, len(chunks), now),
                )
        # with 退出即提交；异常自动 rollback，旧数据原样保留

        self._remove_vectors(old_ids)
        if new_ids:
            self._append_vectors(new_ids, new_vecs)
        return {
            "ok": True,
            "chunks": len(chunks),
            "deleted": len(old_ids),
            "inserted": len(new_ids),
        }

    def replace_file_chunks(self, file_path: str, chunks: list, file_hash=None, mtime=None) -> dict:
        """原子替换一个源文件的全部切片：算向量 → 单事务删旧写新。

        为什么需要这个
        --------------
        以前 `delete_by_file_path()` 和 `add_chunks()` 各自提交事务，中间 add 失败
        （embedding API 挂、网络抖动）时旧数据已删掉，文件从库里整个消失，
        直到下次 full_scan 才发现。实测确认过这个丢数据窗口。

        顺序：
          1. 事务外先算 embedding（网络调用不能持锁，否则整个库的写被堵住）
          2. 任一条失败 → 直接抛，**旧数据完好无损**
          3. 单事务内 DELETE 旧 + INSERT 新 + 更新 file_records，异常自动回滚
          4. 提交成功后才动内存向量库
        """
        vectors, hit_flags = self._embed_chunks(chunks)
        result = self._write_chunks_tx(file_path, chunks, vectors, file_hash, mtime)
        result["cache_hits"] = sum(1 for h in hit_flags if h)
        result["cache_misses"] = len(hit_flags) - result["cache_hits"]
        return result

    def replace_files_batch(self, entries: list) -> list:
        """批量替换多个文件：向量一次算完，再逐文件原子写库。

        entries: [{"file_path", "chunks", "file_hash", "mtime"}, ...]

        为什么要批量
        ------------
        `compute_embeddings_batch` 内部用 ThreadPoolExecutor(max_workers=8) 并发，
        但 watcher 是**逐文件**调用的，每个文件只有 5~20 条切片、按 batch_size=12
        拆成 1~2 个请求，并发度被文件边界卡死在一个文件里。
        full_scan 时 1 万个文件就是 1 万轮「等 2 个请求」。

        攒成一批后：请求总数不变，但并发度真正跑满，耗时从
        「文件数 × 每文件轮次」降到「总切片数 / batch_size / 并发数」。

        若任一批次 embedding 失败，该批次所有文件都不写——旧数据全部保留。
        """
        if not entries:
            return []
        flat, spans = [], []
        for e in entries:
            a = len(flat)
            flat.extend(e.get("chunks") or [])
            spans.append((a, len(flat)))

        # 一次大批量，内部并发；重复内容直接复用旧向量不往上发
        vectors, hit_flags = self._embed_chunks(flat)
        hits = sum(1 for h in hit_flags if h)
        if hits:
            print(
                f"[KBEngine] vector cache: {hits}/{len(flat)} chunks reused, "
                f"{len(flat)-hits} freshly embedded"
            )

        results = []
        for e, (a, b) in zip(entries, spans):
            try:
                r = self._write_chunks_tx(
                    e["file_path"], e.get("chunks") or [], vectors[a:b],
                    e.get("file_hash"), e.get("mtime"),
                )
                r["file_path"] = e["file_path"]
                span_flags = hit_flags[a:b]
                r["cache_hits"] = sum(1 for h in span_flags if h)
                r["cache_misses"] = len(span_flags) - r["cache_hits"]
                results.append(r)
            except Exception as ex:
                logger.warning(
                    "[KBEngine] batch write failed for %s: %s", e.get("file_path"), ex
                )
                results.append({
                    "ok": False, "file_path": e.get("file_path"), "error": str(ex)
                })
        return results

    def delete_by_file_path(self, file_path: str) -> bool:
        """删除指定源文件的全部切片。

        必须把失败暴露出来：此前的 ``except Exception: pass`` 会让删除失败静默通过，
        watcher 接着 ``add_chunks`` 追加新切片，结果是同一文件在库中多份并存
        （实测曾出现 50 行同 id 重复行）。
        """
        try:
            with self._conn() as conn:
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM chunks WHERE file_path = ?", (file_path,)
                    )
                ]
                if ids:
                    self._fts_delete(conn, ids)
                    conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
                conn.commit()
            self._remove_vectors(ids)
            return True
        except Exception as e:
            logger.warning(
                "[KBEngine] delete_by_file_path failed for %s: %s", file_path, e
            )
            return False

    # ------------------------------------------------------------------ 检索
    def _row_to_item(self, row):
        cid, chunk_id, fpath, heading, title, content, tok, updated = row
        return {
            "id": str(chunk_id),
            "chunk_id": chunk_id,
            "file_path": fpath,
            "heading_path": heading,
            "title": title,
            "content": content,
            "token_estimate": tok,
            "updated_at": updated,
        }

    def search(
        self,
        query: str,
        limit: int = 30,
        rerank_top_n=None,
        use_reranker=None,
        rank_by=None,
    ) -> list:
        if not query or not query.strip():
            return []
        q_clean = query.strip()
        retrieval_cfg = self.config.get("retrieval", {})
        vector_limit = max(1, int(limit or retrieval_cfg.get("candidate_limit", 40)))
        fts_limit = max(1, int(retrieval_cfg.get("fts_candidate_limit", 20)))
        if rerank_top_n is None:
            rerank_top_n = int(retrieval_cfg.get("rerank_top_n", 6))
        rerank_top_n = max(1, int(rerank_top_n))
        rrf_k = max(1, int(retrieval_cfg.get("rrf_k", 60)))

        candidates_map = {}

        def add_ranked(items, source):
            for rank, result in enumerate(items, start=1):
                if not isinstance(result, dict):
                    continue
                cid = result.get("id") or result.get("heading_path")
                if not cid:
                    continue
                cid = str(cid)
                item = candidates_map.get(cid)
                if item is None:
                    item = result.copy()
                    item["_vector_rank"] = None
                    item["_fts_rank"] = None
                    item["rrf_score"] = 0.0
                    candidates_map[cid] = item
                item[f"_{source}_rank"] = rank
                item["rrf_score"] += 1.0 / (rrf_k + rank)

        # ---- 向量路：内存矩阵暴力余弦
        try:
            q_vec = self.compute_embedding(q_clean)
            if q_vec:
                vs = self._ensure_vectors()
                if vs.stats()["size"] > 0:
                    qv = np.asarray(q_vec, dtype=np.float32)
                    nv = np.linalg.norm(qv)
                    if nv > 0:
                        qv = qv / nv
                    hit_ids_arr, _scores = vs.search(qv, vector_limit)
                    hit_ids = [int(x) for x in hit_ids_arr]
                    with self._conn() as conn:
                        ph = ",".join("?" * len(hit_ids))
                        rows = {
                            r[0]: r
                            for r in conn.execute(
                                f"SELECT id, chunk_id, file_path, heading_path, title, content, token_estimate, updated_at FROM chunks WHERE id IN ({ph})",
                                hit_ids,
                            )
                        }
                    ordered = [
                        self._row_to_item(rows[i]) for i in hit_ids if i in rows
                    ]
                    add_ranked(ordered, "vector")
        except Exception as e:
            logger.warning("[KBEngine] Vector search error: %s", e)

        # ---- FTS 路：短语/AND，0 结果降级 OR
        # 不再预处理 q_clean：_fts_tokens 的正则本来就把 ｜|# 之类当分隔符，
        # 而旧写法连 _ / \ 也换成空格，会把 api_content、bpf_redirect_peer
        # 这类专名打散，跟索引侧的整词切法对不上（静默失配，不报错）。
        if q_clean:
            try:
                expr = self._build_fts_expr(q_clean)
                hit_ids = []
                if expr:
                    with self._conn() as conn:
                        try:
                            hit_ids = [
                                r[0]
                                for r in conn.execute(
                                    "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                                    (expr, fts_limit),
                                )
                            ]
                        except sqlite3.OperationalError:
                            hit_ids = []
                        if not hit_ids:
                            or_expr = self._build_fts_expr_or(q_clean)
                            if or_expr:
                                hit_ids = [
                                    r[0]
                                    for r in conn.execute(
                                        "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                                        (or_expr, fts_limit),
                                    )
                                ]
                if hit_ids:
                    with self._conn() as conn:
                        ph = ",".join("?" * len(hit_ids))
                        rows = {
                            r[0]: r
                            for r in conn.execute(
                                f"SELECT id, chunk_id, file_path, heading_path, title, content, token_estimate, updated_at FROM chunks WHERE id IN ({ph})",
                                hit_ids,
                            )
                        }
                    add_ranked(
                        [self._row_to_item(rows[i]) for i in hit_ids if i in rows],
                        "fts",
                    )
            except Exception as e:
                logger.debug("[KBEngine] FTS search skipped: %s", e)

        fused_candidates = sorted(
            candidates_map.values(),
            key=lambda item: float(item.get("rrf_score", 0.0)),
            reverse=True,
        )
        # 同一结果集内按内容折叠完全重复的切片（双根镜像会产生同文两份）
        seen_content = set()
        deduped_candidates = []
        for item in fused_candidates:
            content_key = " ".join(str(item.get("content", "")).split())
            if content_key and content_key in seen_content:
                continue
            if content_key:
                seen_content.add(content_key)
            deduped_candidates.append(item)
        fused_candidates = deduped_candidates
        if not fused_candidates:
            return []

        # RRF 后再交给 Cross-Encoder Reranker 精排。
        if use_reranker is None:
            use_reranker = bool(retrieval_cfg.get("use_reranker", True))
        rerank_results = []
        if use_reranker:
            # 卡重排免费线：小批量免费按候选条数(<=25)判，
            # 送入 reranker 的文档数必须 <= 该值，否则整单按大批量计费。
            _rr_cap = int(retrieval_cfg.get("reranker_max_candidates", 25) or 25)
            _rr_pool = fused_candidates[:_rr_cap] if _rr_cap > 0 else fused_candidates
            docs_to_rerank = [
                f"{c.get('heading_path', '')}\n{c.get('content', '')}" for c in _rr_pool
            ]
            rerank_results = self.rerank_pairs(q_clean, docs_to_rerank)
        else:
            logger.info(
                "[KBEngine] Reranker disabled (use_reranker=false); returning RRF order, q=%s",
                " ".join(str(q_clean).split())[:80],
            )

        try:
            _all_scores = sorted(
                (
                    float(r.get("relevance_score"))
                    for r in (rerank_results or [])
                    if isinstance(r, dict) and r.get("relevance_score") is not None
                ),
                reverse=True,
            )
        except (TypeError, ValueError):
            _all_scores = []
        logger.info(
            "[KBEngine] rerank: fused=%d reranked=%d returned=%d | top scores=%s | q=%s",
            len(fused_candidates),
            len(_all_scores),
            min(int(rerank_top_n), len(_all_scores)),
            [round(s, 4) for s in _all_scores[:10]],
            " ".join(str(q_clean).split())[:80],
        )

        final_list = []
        if rerank_results:
            # 重排分只做「去噪闸门」，不再决定名次（2026-10-21 起默认 rank_by=rrf）。
            # 依据：Qwen3-Reranker 在同主题候选池上分数饱和 —— 实测 6 个场景里
            # 25 张候选平均 12.2 张 >= 0.99，分差落到 1e-5 噪声级，排序等于随机。
            # 同批场景 A/B：重排序命中第 1 名 4/6、平均位次 1.33；RRF 序 6/6、位次 1.00。
            # 故排序键交给 RRF（梯度干净），rerank_score 只供下游做阈值过滤。
            score_by_idx = {}
            for result in rerank_results:
                try:
                    idx = int(result.get("index"))
                    score = float(result.get("relevance_score"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if 0 <= idx < len(fused_candidates):
                    score_by_idx[idx] = score
            _rank_by = str(
                rank_by if rank_by is not None else retrieval_cfg.get("rank_by", "rrf")
            ).lower()
            if score_by_idx:
                if _rank_by == "rrf":
                    ordered_idx = sorted(
                        score_by_idx.keys(),
                        key=lambda i: float(fused_candidates[i].get("rrf_score", 0.0)),
                        reverse=True,
                    )
                else:
                    ordered_idx = sorted(
                        score_by_idx.keys(),
                        key=lambda i: score_by_idx[i],
                        reverse=True,
                    )
                for idx in ordered_idx[:rerank_top_n]:
                    item = fused_candidates[idx].copy()
                    item["rerank_score"] = score_by_idx[idx]
                    item["retrieval_score"] = score_by_idx[idx]
                    item["score_source"] = "reranker"
                    final_list.append(item)
                if final_list:
                    return final_list

        # Reranker 超时、返回空数组或返回坏索引时，保留 RRF 结果。
        # 不能让缺失 rerank_score 被下游当成 0 分，从而全量丢失召回。
        for item in fused_candidates[:rerank_top_n]:
            fallback = item.copy()
            fallback["retrieval_score"] = float(fallback.get("rrf_score", 0.0))
            fallback["score_source"] = "rrf_fallback"
            final_list.append(fallback)
        if use_reranker:
            logger.warning(
                "[KBEngine] Reranker unavailable or invalid; using RRF fallback for %d result(s)",
                len(final_list),
            )
        return final_list

    # ------------------------------------------------------------------ 维护
    def reconcile_and_prune_dead_files(self) -> int:
        """检查并清理在磁盘上已不存在的孤岛源文件切片"""
        try:
            with self._conn() as conn:
                paths = [
                    r[0] for r in conn.execute("SELECT DISTINCT file_path FROM chunks")
                ]
                dead = [p for p in paths if p and not os.path.exists(p)]
                if not dead:
                    return 0
                total = 0
                for p in dead:
                    ids = [
                        r[0]
                        for r in conn.execute(
                            "SELECT id FROM chunks WHERE file_path = ?", (p,)
                        )
                    ]
                    if ids:
                        self._fts_delete(conn, ids)
                        conn.execute("DELETE FROM chunks WHERE file_path = ?", (p,))
                        conn.execute("DELETE FROM file_records WHERE file_path = ?", (p,))
                        total += len(ids)
                conn.commit()
            logger.info("[KBEngine] pruned %d chunks from dead source files", total)
            return total
        except Exception as e:
            logger.warning("[KBEngine] reconcile_and_prune_dead_files error: %s", e)
            return 0

    def _vacuum_if_fragmented(self, min_free_pages: int = 5000) -> dict:
        """freelist 攒够才 VACUUM，否则跳过。

        为什么不能无条件跑：VACUUM 要整库复制一份，
        峰值占用 = 库大小 × 2（840MB 库就要 1.7GB 临时空间），
        而且全程独占写锁，运行中会把 watcher 的写入全部堵住。

        但也不能不跑：加列、批量回填这类操作会留下大量页空洞。
        实测一次 text_hash 回填（6.2 万行 × 2 遍 UPDATE）产生 112MB 空洞，
        VACUUM 后才还给文件系统。日常增删是「删行+插行」，
        空间会被 freelist 复用，不产生长期碎片（长期观察 freelist 仅 270 页 / 1.1MB）。

        判据只看 freelist_count —— 文件大小会骗人：
        数据删掉后文件不会缩小，但空间已经在 freelist 里可复用，
        这种情况 VACUUM 只换来一次全库重写，没有任何收益。
        """
        out = {"vacuumed": False, "freelist_pages": 0, "freelist_mb": 0.0}
        try:
            with self._conn() as conn:
                ps = conn.execute("PRAGMA page_size").fetchone()[0]
                fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
            out["freelist_pages"] = fl
            out["freelist_mb"] = round(fl * ps / 1024 / 1024, 1)
            if fl < min_free_pages:
                return out

            # VACUUM 不能在事务里执行，用独立 autocommit 连接
            t0 = time.time()
            raw = sqlite3.connect(self.kb_path, timeout=600)
            try:
                raw.isolation_level = None
                raw.execute("VACUUM")
                # ⚠️ WAL 模式下 VACUUM 是把所有重写页打进 WAL 实现的，
                # 跑完 WAL 会膨胀到接近主库大小（实测 840MB 库 → WAL 846MB），
                # 不立刻 checkpoint 就等于白腾了空间：主文件小了 5MB，
                # WAL 却多了 846MB。必须截断。
                raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                raw.close()
            with self._conn() as conn:
                ps = conn.execute("PRAGMA page_size").fetchone()[0]
                after = conn.execute("PRAGMA freelist_count").fetchone()[0]
                pc = conn.execute("PRAGMA page_count").fetchone()[0]
            wal_bytes = 0
            wal_path = self.kb_path + "-wal"
            try:
                if os.path.exists(wal_path):
                    wal_bytes = os.path.getsize(wal_path)
            except OSError:
                pass
            out.update({
                "vacuumed": True,
                "reclaimed_mb": round((fl - after) * ps / 1024 / 1024, 1),
                "file_mb": round(pc * ps / 1024 / 1024, 0),
                "wal_mb_after_checkpoint": round(wal_bytes / 1024 / 1024, 1),
                "seconds": round(time.time() - t0, 1),
            })
            logger.info(
                "[KBEngine] VACUUM done: freelist %d -> %d pages, reclaimed %.1f MB "
                "in %.1fs (file %.0f MB, wal %.1f MB after truncate)",
                fl, after, out["reclaimed_mb"], out["seconds"],
                out["file_mb"], out["wal_mb_after_checkpoint"],
            )
        except Exception as ex:
            out["error"] = str(ex)
            logger.warning("[KBEngine] VACUUM skipped: %s", ex)
        return out

    def optimize_table(self, older_than_days: int = 1) -> dict:
        """SQLite 侧「维护」：清理死文件切片 + PRAGMA optimize。

        LanceDB 时代这里跑 compaction/prune/index，SQLite 不需要重建索引，
        写入即索引，所以只剩清孤儿和统计信息刷新。
        """
        stats = {
            "compacted": False,
            "indices_optimized": False,
            "cleaned": False,
            "pruned_dead_files": 0,
            "rows": 0,
            "error": None,
        }
        try:
            stats["pruned_dead_files"] = self.reconcile_and_prune_dead_files()
            # tombstone 攒够了在这里一次性收拾（6 万行删 1.5 万实测 51ms）
            if getattr(self, "_vs", None) is not None and self._vs.loaded:
                freed = self._vs.compact()
                if freed:
                    stats["vector_compacted"] = freed
                    logger.info(
                        "[KBEngine] vector store compacted, reclaimed %d slots", freed
                    )
            with self._conn() as conn:
                conn.execute("PRAGMA optimize")
                conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
                conn.commit()
                stats["rows"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            stats["indices_optimized"] = True
            # 碎片攒够才整库重写；顺便回收批量回填/加列留下的页空洞
            stats["vacuum"] = self._vacuum_if_fragmented()
            stats["cleaned"] = True
            logger.info("[KBEngine] optimize() completed: %s", stats)
        except Exception as e:
            stats["error"] = str(e)
            logger.warning("[KBEngine] optimize_table warning: %s", e)
        return stats

    def vector_stats(self) -> dict:
        """向量存储运行态（size/capacity/tombstones/矩阵占用）。"""
        return self._vs.stats()

    def count_rows(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    # ------------------------------------------------------- 兼容空实现（历史接口）
    def create_or_refresh_fts_index(self):
        """SQLite 后端无需刷新：写入即索引。保留空实现兼容旧调用方。"""
        return None

    def create_or_refresh_ann_index(self):
        """不使用 ANN 索引：6 万行暴力余弦 12.8ms，索引维护成本远大于收益。"""
        return None

    def cleanup_orphan_indices(self) -> int:
        return 0

    def cleanup_spill_files(self, max_age_seconds: int = 1800) -> int:
        return 0
