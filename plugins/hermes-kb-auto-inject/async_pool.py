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

排队规则只有一条：**池位不够，最旧的让位。**

``submit`` 时若占位任务已满 ``max_workers``，掐掉 ``seq`` 最小的那条 ——
排队的直接取消，在跑的关掉它正在读的 socket。不分轮次：一轮只提交 1~2 条，
本来排不满 8 个位；真要凑满，必然已经跨了好几轮，那时「最旧」几乎总是上一轮
留下的，本来就该走。

轮次不参与淘汰，只管**新鲜度**：

* ``begin_round`` 只把代数 +1，不动池子
* ``take_done`` 只返回当前轮的，更早的顺手清掉
* ``_run`` 取到线程先对自己的代数，过期的一次请求都不发

「掐断正在跑的」不是杀线程（杀不死），而是关它正在读的那个 socket ——
每个任务带一个 ``AbortSlot``，engine 收到后把 ``conn.sock`` 登记进去，
``cancel()`` 一调，阻塞中的 ``getresponse()`` 立刻抛，线程当场空出来。

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
    """后台检索池：``submit`` 立刻返回，结果由主线程在下次 LLM 请求前取走。"""

    def __init__(
        self,
        retrieve: Callable[[str], List[Dict[str, Any]]],
        max_workers: int = 8,
        ttl: float = 180.0,
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
        # 唯一的并发判据：同时在跑 + 同时在排 不超过这个数。
        # 不再有第二个「积压上限」—— 两个数管同一件事，迟早互相打架。
        self._max_workers = max_workers
        self._seq = itertools.count(1)
        # session_id -> 当前轮次。由 begin_round 递增，submit 时随任务记下。
        self._gen: Dict[str, int] = {}
        # 这行是换码的硬判据：reload 之后活进程里应该恰好出现一条，
        # `evict=oldest-when-full` 是改后的版本才有，旧版打的是别的形状。
        logger.info(
            "[KBAutoInject] async pool ready: max_workers=%d ttl=%.0fs evict=oldest-when-full "
            "retry=unbounded",
            max_workers, ttl,
        )

    # ---------------- 主线程调用 ----------------

    def begin_round(self, session_id: str) -> int:
        """标记该会话进入新一轮。**只加一个数，不动池子**，返回新代数。

        池子的清理归两处，判据不重叠：

        * ``_make_room_locked`` —— 容量不够时掐最旧的（唯一动手的地方）
        * ``take_done`` —— 只返回当前轮的，更早的顺手清掉

        轮次**不参与淘汰**：上一轮的任务在跑就跑着，结果取不到也没关系 ——
        下一轮的 query 会重新提交，池位不够时它自然被掐。engine 侧的重试次数
        不设上限，靠的正是这一点。

        ``_run`` 取到线程时会拿自己的代数对一下，过期的一次请求都不发。
        """
        sid = session_id or "default"
        with self._lock:
            self._expire_locked()
            gen = self._gen.get(sid, 0) + 1
            self._gen[sid] = gen
        return gen

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
        """占着池位的任务数（在跑 + 在排）。已完成的只等取走，不算。"""
        return sum(1 for j in self._jobs.values() if j["state"] == "running")

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
            self._expire_locked()
            if key in self._jobs:
                return "inflight"
            seq = next(self._seq)
            gen = self._gen.get(key[0], 0)
            abort = _new_abort()
            self._jobs[key] = {
                "state": "running",
                "chunks": None,
                "ts": time.monotonic(),
                "seq": seq,
                "gen": gen,
                "future": None,
                "abort": abort,
            }
            # 只清上一代还堵在队列里的；同一代的一个都不动。
            self._make_room_locked(keep=key)
        try:
            fut = self._exec.submit(self._run, key, q, seq, gen, abort)
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
                sid = session_id or "default"
                cur = self._gen.get(sid, 0)
                # 更早轮次的结果已经没人要了：不返回，顺手清掉别躺着。
                for k in [
                    k for k, j in self._jobs.items()
                    if k[0] == sid and j.get("gen", 0) < cur
                ]:
                    del self._jobs[k]
                ready = sorted(
                    (
                        (k, j)
                        for k, j in self._jobs.items()
                        if k[0] == sid
                        and j.get("gen", 0) == cur
                        and j["state"] == "done"
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

    def _run(
        self,
        key: Tuple[str, str],
        query: str,
        seq: int,
        gen: int,
        abort: Any = None,
    ) -> None:
        """跑一次检索。取到线程的第一件事是**问自己这轮还活着吗**。

        代数已经翻篇的任务当场退，一次网络请求都不发。有了这一句，池子就不
        需要「上限」——过期的会自己死，活着的才占线程，堆多少都不怕。
        """
        with self._lock:
            job = self._jobs.get(key)
            if job is None or job["seq"] != seq:
                return                      # 已被摘走，没有可写的目标
            stale = self._gen.get(key[0], 0) != gen
            if stale:
                del self._jobs[key]
        if stale:
            logger.info(
                "[KBAutoInject] async skip q=%s (stale round, never sent)",
                _short(query, 60),
            )
            return

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
            if job is not None and job["seq"] == seq:
                job.update(state="done", chunks=chunks, ts=time.monotonic())
                keep = True
            else:
                keep = False
        if not keep:
            logger.info(
                "[KBAutoInject] async discard q=%s chunks=%d (evicted, nobody waits)",
                _short(query, 60), len(chunks or []),
            )
            return
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
