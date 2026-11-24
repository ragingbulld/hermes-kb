"""kb_query 检索的后台执行池。

把链路里唯一慢的那一段（embedding + rerank 的网络往返，2~8s，偶发挂到超时）
挪出主线程，其余全部留在原地。分工是刻意切开的，别混：

    后台线程：retrieve(query)          纯网络，不碰任何插件状态
    主线程  ：_menu_from_chunks(...)   印刷、#N 编号、落点去重，全在 self._shown 上

反过来做（后台线程直接印清单）会让 ``_shown`` 变成多线程写，而
``it["no"] = len(self._shown) + 1`` 是读-改-写，并发下会出重号。所以池子里
只放 ``retrieve``，池子的产物是**裸 chunks**，不是渲染好的文本。

并发度：默认 8 = 常见一批 tool call 内等价「全并行」。这不是性能旋钮而是
保险丝 —— ai.gitee.com 的 rerank 实测会静默挂起（连上不回状态码），并发越高
失败率越高，而失败会降级成 kept=0（等于没知识）。上限保留，别调到几十。

生命周期：结果只活 ``ttl`` 秒（默认 180），取走即删。没取走的过期满即丢
（发出去的清单已经没意义了，留着只会串到后面的请求里）。
"""

from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Tuple

from .plugin_common import _short, logger


class KBAsyncPool:
    """后台检索池：``submit`` 立刻返回，结果由主线程在下次 LLM 请求前取走。"""

    def __init__(
        self,
        retrieve: Callable[[str], List[Dict[str, Any]]],
        max_workers: int = 8,
        ttl: float = 180.0,
        max_pending: int = 32,
    ) -> None:
        self._retrieve = retrieve
        self._exec = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kb-async"
        )
        # 只护 _jobs 这一份 dict；插件状态（_shown/_served_query）后台线程根本不碰。
        self._lock = threading.Lock()
        # (session_id, query) -> {state, chunks, ts, seq}
        self._jobs: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._ttl = ttl
        self._max_pending = max_pending
        self._seq = itertools.count(1)

    # ---------------- 主线程调用 ----------------

    def submit(self, session_id: str, query: str) -> str:
        """把一次检索丢到后台，返回三态字符串：

        ``"ok"``       已提交
        ``"inflight"`` 同一个词已在池里（在跑 / 已完成待取）—— 别重复提交
        ``"full"``     积压过多，调用方应降级同步
        ``"empty"``    检索词为空

        区分 ``inflight`` 和 ``full`` 是必须的：前者表示「这个词已经有人负责」，
        同步再查一遍就是白烧一次 rerank；后者表示「没人管这个词」，只有这种情况
        才该退回同步。
        """
        q = (query or "").strip()
        if not q:
            return "empty"
        key = (session_id or "default", q)
        with self._lock:
            self._expire_locked()
            if key in self._jobs:
                return "inflight"
            inflight = sum(1 for j in self._jobs.values() if j["state"] == "running")
            if inflight >= self._max_pending:
                logger.warning(
                    "[KBAutoInject] async pool full (%d running), skip q=%s",
                    self._max_pending, _short(q, 60),
                )
                return "full"
            self._jobs[key] = {
                "state": "running",
                "chunks": None,
                "ts": time.monotonic(),
                "seq": next(self._seq),
            }
        try:
            self._exec.submit(self._run, key, q)
        except RuntimeError as exc:          # executor 已 shutdown（进程收尾）
            with self._lock:
                self._jobs.pop(key, None)
            logger.warning("[KBAutoInject] async submit failed q=%s: %s", _short(q, 60), exc)
            return "full"
        logger.info("[KBAutoInject] async submit q=%s", _short(q, 70))
        return "ok"

    def take_done(
        self, session_id: str, wait: float = 0.0, limit: int = 4
    ) -> List[Tuple[str, List[Dict[str, Any]]]]:
        """取走本会话已完成、还没注入的检索结果（按提交顺序）。

        ``wait`` 默认 0 = 完全不阻塞。要「提高命中率而不明显拖慢」可以调到
        0.2~0.3，但那已经是同步的雏形了 —— 异步路径默认不这么干。
        失败的任务（chunks 为 None）在这里直接丢掉，不返回给调用方印刷。
        """
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            with self._lock:
                self._expire_locked()
                ready = sorted(
                    (
                        (k, j)
                        for k, j in self._jobs.items()
                        if k[0] == (session_id or "default") and j["state"] == "done"
                    ),
                    key=lambda kv: kv[1]["seq"],
                )[:limit]
                if ready:
                    out: List[Tuple[str, List[Dict[str, Any]]]] = []
                    for (sid, q), job in ready:
                        del self._jobs[(sid, q)]
                        if job["chunks"] is not None:
                            out.append((q, job["chunks"]))
                    return out
            if time.monotonic() >= deadline:
                return []
            time.sleep(0.02)

    def pending(self, session_id: str) -> int:
        """本会话还在跑的检索数（只用于留痕，不影响是否取）。"""
        with self._lock:
            return sum(
                1
                for k, j in self._jobs.items()
                if k[0] == (session_id or "default") and j["state"] == "running"
            )

    def shutdown(self) -> None:
        self._exec.shutdown(wait=False, cancel_futures=True)

    # ---------------- 后台线程 ----------------

    def _run(self, key: Tuple[str, str], query: str) -> None:
        chunks: Any = None
        started = time.monotonic()
        try:
            chunks = self._retrieve(query)
        except Exception as exc:             # noqa: BLE001 — 后台线程必须自己兜住
            logger.warning(
                "[KBAutoInject] async retrieve failed q=%s: %s", _short(query, 60), exc
            )
        elapsed_ms = (time.monotonic() - started) * 1000
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                job.update(state="done", chunks=chunks, ts=time.monotonic())
        logger.info(
            "[KBAutoInject] async done q=%s chunks=%d in %.0fms",
            _short(query, 60), len(chunks or []), elapsed_ms,
        )

    def _expire_locked(self) -> None:
        """丢弃过期任务。调用方必须已持有 ``self._lock``。"""
        now = time.monotonic()
        stale = [k for k, j in self._jobs.items() if now - j["ts"] > self._ttl]
        for k in stale:
            del self._jobs[k]
        if stale:
            logger.info("[KBAutoInject] async pool expired %d stale job(s)", len(stale))
