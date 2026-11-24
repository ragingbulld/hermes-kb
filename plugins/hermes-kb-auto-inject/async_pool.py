"""kb_query 检索的后台执行池。

把链路里唯一慢的那一段（embedding + rerank 的网络往返，2~8s，偶发挂到超时）
挪出主线程，其余全部留在原地。分工是刻意切开的，别混：

    后台线程：retrieve(query) → on_done 印刷 + 塞进待投递槽
    主线程  ：pre_llm_call 只提交；llm_request 中间件只取走贴进请求副本

插件那边没有任何跨任务账本：**不去重、不编号、不扫历史**（2026-11-24 晚定案）。
池子的产物不是「裸 chunks 等人取」，而是**当场排进待投递槽的清单**；槽由
``_pending_lock`` 保护，取走就清空，所以不存在「两份清单抢同一张卡」的问题。

并发度：默认 8 = 常见一批 tool call 内等价「全并行」。这不是性能旋钮而是
保险丝 —— ai.gitee.com 的 rerank 实测会静默挂起（连上不回状态码），并发越高
失败率越高，而失败会降级成 kept=0（等于没知识）。上限保留，别调到几十。

排队规则只有一条：**池位不够，最旧的让位。**

``submit`` 时若占位任务已满 ``max_workers``，掐掉 ``seq`` 最小的那条 ——
排队的直接取消，在跑的关掉它正在读的 socket。不分轮次：一轮只提交 1~2 条，
本来排不满 8 个位；真要凑满，必然已经跨了好几轮，那时「最旧」几乎总是上一轮
留下的，本来就该走。

**没有轮次概念**（2026-11-24 删干净）：结果跑完就投递，不需要「这轮还活着吗」
这种判据，也就不需要代数、不需要取件口、不需要过期。

「掐断正在跑的」不是杀线程（杀不死），而是关它正在读的那个 socket ——
每个任务带一个 ``AbortSlot``，engine 收到后把 ``conn.sock`` 登记进去，
``cancel()`` 一调，阻塞中的 ``getresponse()`` 立刻抛，线程当场空出来。

生命周期：跑完就投递 —— ``retrieve`` 一返回，池子当场调 ``on_done(session_id, query, chunks)``，
由插件把清单写进会话历史，然后把这条任务从池里删掉。**不存在「等人来取」的中间态**，
因此也没有 TTL、没有代数、没有取件口（2026-11-24 owner 定案：检索完成即注入历史）。
池子现在只负责两件事：别让同一个词重复查（inflight），池位满时掐最旧的（``_make_room_locked``）。
"""

from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from .plugin_common import _short, logger


def _new_abort():
    """造一个中断句柄（engine 侧的 ``AbortSlot``）。

    懒导入：这个池没有知识库引擎也要能用（探针、离线测试）。拿不到就退化成
    ``None``，任务照样跑，只是不能被掐断。
    """
    try:
        from hermes_kb.engine import AbortSlot

        return AbortSlot()
    except Exception:                        # noqa: BLE001 — 引擎缺席不是错误
        return None


class KBAsyncPool:
    """后台检索池：``submit`` 立刻返回，检索一跑完就交给 ``on_done``，池子不留成品。"""

    def __init__(
        self,
        retrieve: Callable[[str], List[Dict[str, Any]]],
        on_done: Optional[Callable[[str, str, List[Dict[str, Any]]], None]] = None,
        max_workers: int = 8,
    ) -> None:
        self._retrieve = retrieve
        # 唯一的投递口：跑完当场交出去。回调在后台线程里执行，插件那边自己上锁。
        self._on_done = on_done
        self._exec = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kb-async"
        )
        # 只护 _jobs 这一份 dict；插件那边的待投递槽有自己的锁。
        self._lock = threading.Lock()
        # (session_id, query) -> {seq, future, abort}
        self._jobs: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # 唯一的并发判据：同时在跑 + 同时在排 不超过这个数。
        # 不再有第二个「积压上限」—— 两个数管同一件事，迟早互相打架。
        self._max_workers = max_workers
        self._seq = itertools.count(1)
        # 这行是换码的硬判据：reload 之后活进程里应该恰好出现一条，
        # `evict=oldest-when-full` 是改后的版本才有，旧版打的是别的形状。
        logger.info(
            "[KBAutoInject] async pool ready: max_workers=%d evict=oldest-when-full "
            "retry=unbounded on_done=%s",
            max_workers, "yes" if on_done is not None else "NO",
        )

    # ---------------- 主线程调用 ----------------

    def _kill_locked(self, key: Tuple[str, str], reason: str) -> None:
        """让一条任务走人：排队的取消，在跑的掐 socket。调用方须已持锁。"""
        job = self._jobs.get(key)
        if job is None:
            return
        fut = job.get("future")
        if fut is not None and fut.cancel():
            del self._jobs[key]
            logger.info(
                "[KBAutoInject] async drop queued q=%s (%s)",
                _short(key[1], 60), reason,
            )
            return
        abort = job.get("abort")
        del self._jobs[key]
        killed = bool(abort is not None and abort.cancel())
        logger.info(
            "[KBAutoInject] async abort running q=%s socket=%s (%s)",
            _short(key[1], 60), "killed" if killed else "not-bound", reason,
        )

    def _pending_locked(self) -> int:
        """占着池位的任务数。``_jobs`` 只收在跑和在排的，投完即删，所以就是字典长度。"""
        return len(self._jobs)

    def _make_room_locked(self, keep: Tuple[str, str]) -> None:
        """池位不够时让最旧的让位。调用方须已持锁。

        不分代，只看 ``seq``。看着粗暴，其实很安全：一轮只提交 1~2 条，
        要凑满 ``max_workers`` 条必然是跨了四轮以上 —— 那时「最旧」一定是更早
        那一轮留下来的，本来就该走。反过来，如果某一轮真提了十几条把它撑满，
        这里掐掉的是最早的（通常是用户原话），代价明摆着，但比「新任务排队等
        十几秒」划算。
        """
        while self._pending_locked() > self._max_workers:
            oldest = min(
                (k for k in self._jobs if k != keep),
                key=lambda kk: self._jobs[kk]["seq"],
                default=None,
            )
            if oldest is None:
                return
            self._kill_locked(oldest, "pool full")

    def submit(self, session_id: str, query: str) -> str:
        """把一次检索丢到后台，返回三态字符串：

        ``"ok"``       已提交（**新任务永远能进**，挡住它的只会是 executor 已关）
        ``"inflight"`` 同一个词已在池里（在跑 / 已完成待取）—— 别重复提交
        ``"full"``     池子已 shutdown（进程收尾），不是积压
        ``"empty"``    检索词为空

        区分 ``inflight`` 和 ``full`` 是必须的：前者表示「这个词已经有人负责」，
        同步再查一遍就是白烧一次 rerank；后者表示「这次真的没跑」，只有这种情况
        才该退回同步。
        """
        q = (query or "").strip()
        if not q:
            return "empty"
        key = (session_id or "default", q)
        with self._lock:
            if key in self._jobs:
                return "inflight"
            seq = next(self._seq)
            abort = _new_abort()
            self._jobs[key] = {
                "seq": seq,
                "future": None,
                "abort": abort,
            }
            self._make_room_locked(keep=key)
        try:
            fut = self._exec.submit(self._run, key, q, seq, abort)
        except RuntimeError as exc:          # executor 已 shutdown（进程收尾）
            with self._lock:
                self._jobs.pop(key, None)
            logger.warning("[KBAutoInject] async submit failed q=%s: %s", _short(q, 60), exc)
            return "full"
        with self._lock:
            job = self._jobs.get(key)
            if job is not None and job["seq"] == seq:
                job["future"] = fut
        logger.info("[KBAutoInject] async submit q=%s", _short(q, 70))
        return "ok"

    def shutdown(self) -> None:
        self._exec.shutdown(wait=False, cancel_futures=True)

    # ---------------- 后台线程 ----------------

    def _run(
        self,
        key: Tuple[str, str],
        query: str,
        seq: int,
        abort: Any = None,
    ) -> None:
        """跑一次检索，跑完**当场投递**（``on_done``），然后把这条任务从池里删掉。

        没有「等人来取」的中间态，所以没有过期、没有代数、没有取件口 —— 结果
        一出来就进历史，这正是「检索完成即注入」的全部实现。被淘汰（池位不够）
        的任务在 ``_kill_locked`` 里已经摘走，这里对不上 ``seq`` 就直接收工。
        """
        with self._lock:
            job = self._jobs.get(key)
            if job is None or job["seq"] != seq:
                return                      # 已被摘走，没有可投递的目标

        chunks: Any = None
        started = time.monotonic()
        try:
            chunks = self._retrieve(query, abort=abort)
        except Exception as exc:             # noqa: BLE001 — 后台线程必须自己兜住
            logger.warning(
                "[KBAutoInject] async retrieve failed q=%s: %s", _short(query, 60), exc
            )
        elapsed_ms = (time.monotonic() - started) * 1000
        with self._lock:
            job = self._jobs.get(key)
            # 只认自己那一次：被淘汰后同一个词又被重提时，这里是**另一个** job，
            # 旧线程跑完不能把结果塞进新任务（否则新任务拿到的是旧词的结果）。
            keep = job is not None and job["seq"] == seq
            if keep:
                del self._jobs[key]         # 投完就删：池子不留成品
        if not keep:
            logger.info(
                "[KBAutoInject] async discard q=%s chunks=%d (evicted, pool full)",
                _short(query, 60), len(chunks or []),
            )
            return
        logger.info(
            "[KBAutoInject] async done q=%s chunks=%d in %.0fms",
            _short(query, 60), len(chunks or []), elapsed_ms,
        )
        if chunks and self._on_done is not None:
            try:
                self._on_done(key[0], query, chunks)
            except Exception as exc:         # noqa: BLE001 — 投递失败不能拖垮线程池
                logger.warning(
                    "[KBAutoInject] on_done failed q=%s: %s", _short(query, 60), exc
                )

