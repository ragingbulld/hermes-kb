"""`KBAutoInjectPlugin`：清单池、检索、注入钩子的主体。

从原 1502 行单文件拆出的最大一块。这里只有**一个类**，职责按方法切好：
`_locate` / `_chain_spans` 是「在磁盘上把块定位到行」，`_build_menu` / `_render_menu`
是「印清单」，`_search_validated` / `_inline_menu` 是「检索 + 过闸」，
`handle_pre_llm_call` 是唯一注入点。常量与工具函数在 `.plugin_common`，
schema/handler 在 `.registration`。
"""

import bisect
import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from itertools import islice
from typing import Any, Dict, List, Optional, Tuple

from .async_pool import KBAsyncPool
from .plugin_common import (
    PROTOCOL_PROMPT,
    _content_text,
    _kb_query_is_usable,
    _short,
    _user_query_text,
    logger,
    scan_heads,
)

# 检索**一律异步**，出口只有一条：检索跑完把清单攒进待投递槽（`_on_job_done`），
# 下一次模型请求前由 `llm_request` 中间件贴到最后一条消息尾部（`handle_llm_request`）。
# 中间件改的是核心 `_safe_copy` 出来的请求副本，**只在这一次调用里生效、不落 state.db** ——
# 这是 owner 2026-11-24 定案「不进上下文历史」的唯一正确落点。
# 曾经有过的通道：直写会话历史（进历史、下一轮反复重放）、`transform_tool_result`
# （要等下一次工具返回）、轮首有界等待（把首字拖慢 2.5s）。都删了，别再长回来。
#
# **没有去重账**（2026-11-24 晚 owner 定案）：不记已印过的卡、不扫历史比对、不按词查重。
# 检索到几块就印几块，重复就重复 —— 少一份账本就少一处会漂的状态。


# heading_path 的层级怎么拆，规则只有一份：`hermes_kb/heading.py`。
# 这里不复制实现 —— 但也不能模块级导入 hermes_kb：它的 __init__ 会拉
# KBEngine（numpy/sqlite 一堆重库），实测把插件加载从 0.05s 拖到 7s。
# 所以首次用到时延迟导入一次，之后走缓存。
_split_heading_path = None
_split_ready = False


def _path_parts(heading_path) -> List[str]:
    """``heading_path`` → 层级数组。实现在 ``hermes_kb/heading.py``。"""
    global _split_heading_path, _split_ready
    if not _split_ready:
        try:
            from hermes_kb.heading import split_heading_path as _f
            _split_heading_path = _f
        except Exception:                          # 极端兜底：宁可不切，不切错
            logger.warning(
                "[KBAutoInject] hermes_kb.heading 不可用，heading_path 按单层处理"
            )
            _split_heading_path = None
        _split_ready = True
    hp = str(heading_path or "")
    if _split_heading_path is None:
        return [hp] if hp else []
    return _split_heading_path(hp)


_MISS = object()


class _TTL:
    """极简 TTL 缓存：命中即返回，条目超上限整表清掉。

    为什么不用 LRU：键是「路径 + 读取区间」，一个会话撑死几十个，清空一次
    的代价只是重查几毫秒 —— 为这个维护链表不划算。

    命中判断用 ``_MISS`` 而不是 ``None``：``None`` 在这里是**合法值**
    （「这个路径不在索引里」），拿返回值判命中会把「查过了、没有」误当成
    「没查过」，每次都重新打库。
    """

    __slots__ = ("_d", "ttl", "cap")

    def __init__(self, ttl: float = 60.0, cap: int = 512) -> None:
        self._d: Dict[Any, Tuple[float, Any]] = {}
        self.ttl = ttl
        self.cap = cap

    def get(self, key) -> Any:
        hit = self._d.get(key)
        if hit is not None and time.time() - hit[0] < self.ttl:
            return hit[1]
        return _MISS

    def put(self, key, val) -> None:
        if len(self._d) > self.cap:
            self._d.clear()
        self._d[key] = (time.time(), val)


class KBAutoInjectPlugin:
    """Execution-state-driven knowledge base retriever and API-bound injector."""

    # 待投递槽**没有批次闸**：攒了几批就一次投几批。
    # 早先这里是三道闸纠缠 ——「每次只投最新 2 批、其余排队、超 6 批削最旧」。
    # 实测常态 1.47 批/请求，闸从没起过作用；只在连续高并发的少数窗口里
    # 把**检索成本已经付掉**的清单悄悄丢掉（dropped 记账只留一个数字，看不出丢了什么）。
    # 既然全投，槽在每次请求后必空，积压上限与 dropped 记账一起失去意义。
    # owner 2026-11-26：「合并注入限制删干净优雅点」。
    #
    # 阶梯投递（owner 2026-11-28 定）：**阶段点是「间隔」，而且间隔按倍数延长**。
    # 第一次命中就投；投完清零，再攒 3 次投；再清零，攒 10 次投；之后 30、100、300…
    # 命中序号上是 1、4、14、44、144、444…（间距 ≈×3，越往后越稀）。
    # owner 原话：「第一次命中 1 了然后清理计数器继续下一个 3，到了又清空再到 10，
    #   相当于阶段点其实是间隔」+「还是倍数不要你那个几何」。
    # 注意：**这里存的是间隔，不是命中序号** —— 上一版把同一串数当序号用
    # （`n in (1,3,10)`）是错的，那等于「第 1/3/10 次命中」，不是「隔 1/3/10 次」。
    _HIT_GAPS = (1, 3, 10, 30, 100, 300)

    # `_ladder` 里的哨兵：本轮这张卡**已经被真读过正文** → 本轮不再投。
    # 用同一个 dict 的另一个值域表达，不新开一张「已读账」（owner: 优雅点）。
    _READ = -1

    def __init__(self):
        # 类型注解用 Any：KBEngine 已改为惰性导入，运行时不再求值该符号
        self._engine: Optional[Any] = None
        self._engine_lock = threading.Lock()
        self._turn_lock = threading.RLock()
        # 协议段「每 turn 只发一次」的记账。`_ladder`（阶梯投递的命中计数）
        # 与它同形、同一个 turn_key 口径，两处共用 `_max_turn_states` 做 LRU 裁剪。
        self._turns: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._max_turn_states = 128
        # ---------------- 全部状态：一次待投递的清单 ----------------
        # sid -> [(提交序号, 检索词, 清单文本), ...]。检索跑完由后台线程
        # `_on_job_done` 追加，下一次模型请求前由 `handle_llm_request` 取走。
        # 带检索词是为了出口能回查它在**当前提示词**里的位置 —— 服务端排新的
        # 依据是「提示词里最靠后」，既不认完成时间，也不认提交序号（并行段乱序）。
        # 不记「印过哪张卡」、不扫历史比对、不按词查重 —— 去重整套删掉了（owner 2026-11-24 定案）。
        # sid -> [(提交序号, 检索词, 清单), ...]。清单是**结构化菜单**（`_build_menu`
        # 的产物，一张卡一条），不是渲染好的字符串 —— 出口要把多批合成一屏，只有
        # 拿到结构才能把同一张卡的命中落点并成一棵树。渲染统一在出口那一次做。
        self._pending: Dict[str, List[Tuple[int, str, List[Dict[str, Any]]]]] = {}
        self._pending_lock = threading.Lock()
        # 阶梯记账：turn_key -> {卡key: 记录}。**与 `_turns` 同形、同一个 turn_key
        # 口径** —— 换 turn 天然就是一张新表，不需要第二个轮次管理器。
        # 记录 = [命中次数, 绝对路径, 起行, 行数, 区间是否精确]：
        #   · 前一项定节奏（阶梯），后四项定「读的是不是这张卡」。
        #   两件事共用同一条记录，所以已读判定是纯内存比较，不扫历史、不查骨架。
        # 记的是「命中几次」而不是「发过没有」：没有能力永久压制一张卡，
        # 第 2 次静默，第 4 次、第 14 次照样再送。
        # 命中次数为 `_READ`(-1) = 本轮已读过正文，不再投；换 turn 就作废。
        self._ladder: "OrderedDict[str, Dict[str, list]]" = OrderedDict()
        # kb_query 的后台检索池：只跑 retrieve（慢的网络段）。**跑完当场回调**
        # `_on_job_done` 把清单塞进待投递槽 —— 池子里没有「等人来取」的成品。
        self._async_pool = KBAsyncPool(
            self.retrieve_chunk, on_done=self._on_job_done
        )

    @property
    def engine(self) -> Any:
        if self._engine is None:
            with self._engine_lock:
                if self._engine is None:
                    # 局部导入：把 6~7s 的重库加载成本推迟到首次真实检索，
                    # 避免撞 Hermes 插件 10s 加载门禁导致钩子被 ignored。
                    # get_engine() 是进程级单例（hermes_kb/engine.py）：
                    # 插件每轮 reload 会把 self._engine 归零，但模块级实例不受影响，
                    # 向量库因此只加载一次，而不是每轮 1~2 s 重载 6.7 万行。
                    from hermes_kb.engine import get_engine

                    self._engine = get_engine()
        return self._engine

    def _turn_key(self, kwargs: Dict[str, Any], messages: Any = None) -> str:
        session_id = str(kwargs.get("session_id") or "default")
        turn_id = str(kwargs.get("turn_id") or "")
        if turn_id:
            return f"{session_id}:{turn_id}"
        text = str(kwargs.get("user_message") or "")
        if not text and isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    text = _content_text(message.get("content"))
                    if text:
                        break
        digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"{session_id}:content:{digest}"

    def _user_query(self, kwargs: Dict[str, Any]) -> str:
        """本轮用户原话 → 检索词。取不到或没信息量就返回空串（那就不检索）。

        `user_message` 是网关给的现成本轮输入；拿不到时才回退到 messages 列表里
        最后一条 user（本地会话/测试进程走的是后一条）。
        """
        raw = str(kwargs.get("user_message") or "")
        if not raw:
            messages = kwargs.get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("role") == "user":
                        raw = _content_text(message.get("content"))
                        if raw:
                            break
        return _user_query_text(raw)

    def _state(self, turn_key: str) -> Dict[str, Any]:
        """只服务「协议段每 turn 只发一次」。"""
        with self._turn_lock:
            state = self._turns.get(turn_key)
            if state is None:
                state = {"protocol_added": False}
                self._turns[turn_key] = state
            self._turns.move_to_end(turn_key)
            while len(self._turns) > self._max_turn_states:
                self._turns.popitem(last=False)
            return state

    # ============ 两步：kb_query → 后台池 → 标题清单（带行号）============
    #
    # 每一次工具调用都带 kb_query → 检索丢进后台池；清单渲染好排进待投递槽，
    # 下一次模型请求由 `llm_request` 中间件贴进请求副本尾部（改副本，不落 state.db）。
    # 清单里每行都写着 `read_file` 现成的路径 + 命中段行号，要正文模型自己去读。
    # 插件到此为止，不再持有原文、也没有第二个工具。

    @staticmethod
    def _card_title(chunk: Dict[str, Any]) -> str:
        """卡片标题 = ``heading_path`` 的第一段。

        不能用 ``chunk["title"]``：那一列是**最深层小节名**（实测全库 25.35% 的块
        ``title`` ≤8 字，是「症状」「避坑铁律」「根因」「LEGAL」这种东西），拿它当
        卡片标题等于没给。``heading_path`` 全库 0 条缺失，第一段就是卡片 H1。
        """
        # 首选 chunk 自带的一级标题：切分时原样带出，零猜测。
        # 老数据没有这一列，才回退到从 heading_path 反推 —— 反推在 H1 自身
        # 含 " > " 时必错（「…（64000 > 32768）」会被割成「…（64000」，
        # 而「…用 " > " 拼层级…」这种引号场景连括号平衡也救不回来）。
        h1 = str(chunk.get("h1") or "").strip()
        if not h1:
            _parts = _path_parts(chunk.get("heading_path"))
            h1 = _parts[0] if _parts else ""
        # 长卡切段时 H1 会被写成 "…(Part 1)/(Part 2)"，剥掉，否则同一张卡看着像三张
        h1 = re.sub(r"(?:\s*\(Part \d+\))+$", "", h1).strip()
        if len(h1) <= 8:
            # 这段其实是小节名（原文没有 `# ` 一级标题），退回文件名 —— 比「次生影响」强
            name = os.path.basename(str(chunk.get("file_path") or ""))
            name = re.sub(r"\.md$", "", name)
            h1 = re.sub(r"_[0-9a-f]{6,8}$", "", name).replace("_", " ").strip()
        return h1 or "(无标题)"

    @classmethod
    def _card_key(cls, chunk: Dict[str, Any]) -> Tuple[str, str]:
        """「一卡一行」的键。

        不能只用 ``file_path``：实测 22.5% 的反思卡文件里写了 2~3 个不同的 ``# `` 一级
        标题（一个 .md 装了几张不同主题的卡），只按文件去重会把另外两个主题整行吃掉。
        """
        return (str(chunk.get("file_path") or ""), cls._card_title(chunk))

    # 整卡行数 / 命中块行号：read_file 的 offset/limit 是按行的，这两个缓存让
    # 一屏清单只读一次文件（同一个 file_path 的多块共用）。
    _disk_cache: Dict[str, Tuple[str, int]] = {}
    # 文件的标题骨架（标题行 + 每节起止行），按文件缓存。所有行号推导都查它。
    _outline_cache: Dict[str, Dict[str, Any]] = {}
    # (文件, 链) → 行范围。画树时同一节点会被问很多次，不缓存就是重复全扫。
    _span_cache: Dict[Tuple[str, Tuple[str, ...]], Tuple[Tuple[int, int, int], ...]] = {}
    # 读正文时补的时间摘要：(path, offset, limit) → (检查时刻, 文本)，60s TTL。
    # 键必须带上读取区间 —— 同一个文件读不同段落，命中的节不同，时间也不同。
    # (路径, offset, limit) → 文案；路径 → {heading_path: (last_changed, source)}。
    # 都 60 秒过期：一轮对话里反复读同一张卡不必次次打库，又不能缓存太久，
    # 否则刚补完这张卡、回头读还是旧时间。
    _time_cache = _TTL()
    _times_cache = _TTL()
    _rec_cache = _TTL()
    # 正文头部的时间行：「- **记录时间 / 生效日期**：2026-11-28 11:40:49 (UTC+8)」。
    # 只认这三个词 —— 正文里别处出现的「时间」多数在讲别的事，抓了就是误报。
    _TIME_PAT = re.compile(
        r"(记录时间|生效日期|更新时间)\s*[:：]?\s*[^\d\n]{0,8}"
        r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?"
    )

    def _chunk_times(self, path: str):
        """``{heading_path: (last_changed, time_source)}``；不在索引里回 ``None``。

        判据只有一条：`chunk_times` 里有没有这个路径。有，就是知识块；没有，
        就是普通文件 —— 目录、后缀、名字一概不看，所以同一个源目录里混着
        代码和卡片也不会误判。

        时间按**每一节自己的内容哈希**推进（引擎侧 `_CHUNK_TIME_UPSERT`）：
        一节没变，它那一行就一步不动。所以「三个月没动的卡、上周刚补了一个
        坑」读老段落时不会误显示成「刚改过」—— 这是文件级时间做不到的。
        """
        if not path:
            return None
        hit = self._times_cache.get(path)
        if hit is not _MISS:
            return hit
        rows = None
        try:
            db = os.path.join(
                str(getattr(self.engine, "db_dir", "") or ""), "kb.sqlite3"
            )
            if os.path.exists(db):
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    got = con.execute(
                        "SELECT heading_path, last_changed, time_source "
                        "FROM chunk_times WHERE file_path=?",
                        (path,),
                    ).fetchall()
                finally:
                    con.close()
                # 一行都没有 = 旧库还没播种过 → 当作不在库，宁可不印
                rows = (
                    {(r[0] or ""): (r[1], r[2] or "") for r in got} if got else None
                )
        except Exception:  # noqa: BLE001 —— 查不到就当不是知识块，宁可少印一行
            rows = None
        self._times_cache.put(path, rows)
        return rows

    def _heading_chain(self, heading_path: str) -> List[str]:
        """`heading_path` → 标题链。

        老数据用 `" > "` 拼、新数据用 ``\\x1f``，混在同一个库里（实测 51286 条
        老格式）。拆分**只认 `heading.py` 那一份实现** —— 它会对老数据做括号
        平衡回补，自己 `split(" > ")` 会把「（64000 > 32768）」这类标题切碎。

        必须走 `_path_parts`，不能直接读 `_split_heading_path`：后者是延迟导入的
        结果，`_path_parts` 才负责触发那次导入。早先这里自己判断了一遍全局变量，
        于是**首次调用时它还是 None**，整条链被当成单层返回 —— 老格式的卡全靠
        `_find` 的宽松前缀匹配挂到父节上，行范围永远是父节范围，`(Part N)` 拆出
        来的多片也从来归不到同一节。
        """
        return _path_parts(heading_path) if heading_path else []

    @staticmethod
    def _ago(ts: float) -> str:
        """``1670000000`` → ``3 天前``。相对时间才是「时效感」的来源，绝对日期只给坐标。"""
        delta = max(0.0, time.time() - float(ts))
        if delta < 3600:
            return "不到 1 小时前"
        if delta < 86400:
            return f"{int(delta // 3600)} 小时前"
        days = int(delta // 86400)
        if days < 60:
            return f"{days} 天前"
        return f"{days // 30} 个月前"

    def _card_time_text(
        self, path: str, offset: Any = None, limit: Any = None
    ) -> str:
        """按**读到的行范围**算这张卡的时间：``记于 … · 修订 …``。

        两路来源各管一件事：
        - 「记于」＝正文里作者手写的记录时间，语义权威，不受系统重写影响；
        - 「修订」＝**这一节自己的内容哈希**上次真正变化的时刻。

        为什么按行范围挑而不是整卡一个值：一个文件里每节各有各的
        `last_changed`。读整卡时取最新；只读命中段时只看相交的那几节 ——
        这样「三个月没动的卡、上周刚补了一个坑」读老段落时不会误显示成「刚改过」。
        """
        cache_key = (path, offset, limit)
        hit = self._time_cache.get(cache_key)
        if hit is not _MISS:
            return hit
        text = " · ".join(
            x
            for x in (self._rec_stamp(path), self._rev_text(path, offset, limit))
            if x
        )
        self._time_cache.put(cache_key, text)
        return text

    def _rec_db(self, path: str) -> Optional[Tuple[float, str]]:
        """``file_records`` 里这文件的 ``(first_seen, time_source)``。"""
        hit = self._rec_cache.get(path)
        if hit is not _MISS:
            return hit
        row = None
        try:
            db = os.path.join(
                str(getattr(self.engine, "db_dir", "") or ""), "kb.sqlite3"
            )
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                r = con.execute(
                    "SELECT first_seen, time_source FROM file_records "
                    "WHERE file_path=?",
                    (path,),
                ).fetchone()
            finally:
                con.close()
            if r and r[0]:
                row = (float(r[0]), str(r[1] or ""))
        except Exception:  # noqa: BLE001 —— 查不到就当没有
            row = None
        self._rec_cache.put(path, row)
        return row

    def _rec_stamp(self, path: str) -> str:
        """「记于 …」：这张卡是什么时候写的。

        **表优先**。`file_records.first_seen` 由引擎记，不依赖正文文字 —— 卡头
        那行「记录时间」因此可以从写卡模板里去掉，老卡留着也不影响显示。
        只认 `card`（从老卡正文迁过来的作者时间）和 `live`（首次入库时刻），
        两者都可信；`backfill` 是从 mtime 灌的，不可信，跳过。

        兜底才扫正文：迁移之前的老卡，那一行还在。
        """
        row = self._rec_db(path)
        if row:
            ts, src = row
            if src in ("card", "live"):
                return "记于 " + time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(ts)
                ) + f"（{self._ago(ts)}）"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                # 用 islice 而不是 `[next(fh) for _ in range(40)]`：后者对
                # **不足 40 行**的文件会抛 StopIteration，整个读取被判失败 ——
                # 实测 2475 个短卡（26%）的「记于」因此从来没显示过。
                head = list(islice(fh, 40))
        except Exception:  # noqa: BLE001
            return ""
        for line in head:
            m = self._TIME_PAT.search(line)
            if not m:
                continue
            _key, y, mo, d, hh, mm = m.groups()
            try:
                ts = datetime.datetime(
                    int(y), int(mo), int(d), int(hh or 0), int(mm or 0)
                ).timestamp()
            except Exception:  # noqa: BLE001 —— 日期非法就当没写
                continue
            stamp = f"{y}-{mo}-{d}" + (f" {hh}:{mm}" if hh else "")
            return f"记于 {stamp}（{self._ago(ts)}）"
        return ""

    def _intersect(self, path: str, times, lo: int, hi: int):
        """把 ``[lo, hi]`` 与各节行范围求交 → ``{行范围: (last_changed, source)}``。

        按**行范围**收桶而不是按 heading_path：老数据把「(Part 2)」拼在标题里，
        同一节的三片成了三个不同的 heading_path、行范围却完全一样 —— 按 key
        数算会虚报「读到 3 节」，其实是同一节。

        再剔除被别的命中范围**完全包住**的父节：读「避坑铁律」时 H1 那节
        （范围 = 全文）必然也命中，但它只是外框，不是读到的内容。不剔的话
        永远至少报 2 节，时间也总被 H1 拉走。
        """
        out = self._outline(path)
        by_title, end_map = out["by_title"], out["end"]
        buckets = {}
        for hp, val in times.items():
            chain = self._heading_chain(hp)
            if not chain:
                continue
            # 预筛：末级标题在文件里的**所有**出现位置都落在 [lo, hi] 之外，
            # 这一节必然不相交，不必为它跑逐级收窄。6643 节的卡靠这一步把
            # 1.6s 降到几十 ms —— 末级范围是收窄后的超集，所以这个判断只会
            # 提前排除「本来就不相交」的节，不会误杀相交的。
            cands = by_title.get(self._clean_heading(chain[-1]))
            if cands and not any(
                not (hi < i + 1 or lo > end_map[i]) for i, _ix in cands
            ):
                continue
            start = n = 0
            # 取**最后一个算得出范围**的层级：末级对不上时 `_chain_spans` 给
            # (0,0,0)，退回上一级仍有意义的范围，比整卡瞎猜准。
            spans = self._chain_spans(path, chain)
            for _c, _s, _n in reversed(spans):
                if _s and _n:
                    start, n = _s, _n
                    break
            if start and n:
                if hi < start or lo > start + n - 1:
                    continue
                key = (start, n)
            else:
                # 算不出范围（标题被改过 / 分隔符不认识）→ 当作整卡命中。
                # 宁可多算，也不让这节的时间凭空消失；合并成一桶，不计数。
                key = (0, 0)
            prev = buckets.get(key)
            if prev is None or (val[0] or 0) > (prev[0] or 0):
                buckets[key] = val

        items = list(buckets.items())
        kept = [
            (sp, v)
            for i, (sp, v) in enumerate(items)
            if not sp[0]
            or not any(
                sp2[0]
                and sp2[0] >= sp[0]
                and sp2[0] + sp2[1] <= sp[0] + sp[1]
                and sp2[1] < sp[1]
                for j, (sp2, _v) in enumerate(items)
                if j != i
            )
        ]
        return dict(kept or items)

    def _rev_text(self, path: str, offset: Any = None, limit: Any = None) -> str:
        """「修订 …」：读到的节里，最新的那片内容变化时刻。

        整卡（没传 offset/limit）走快路径：所有节都算读到，直接取最大值，
        不必为每节算一遍行范围 —— 一张 60 节的卡能省 60 次 `_chain_spans`。
        """
        times = self._chunk_times(path)
        if not times:
            return ""

        if offset is None and limit is None:
            picked = list(times.values())
            # 节数只用于文案，按分隔符判断深度即可 —— 逐条 split_heading_path
            # 本身就是分钟级的开销（6643 节实测）。
            nested = sum(1 for hp in times if "\x1f" in hp or " > " in hp)
            n_sections = nested if nested else len(times)
        else:
            lo, hi = 1, 10 ** 9
            try:
                if offset is not None:
                    lo = max(1, int(offset))
                if limit is not None:
                    hi = lo + max(1, int(limit)) - 1
            except Exception:  # noqa: BLE001 —— 参数不像数就当整卡
                lo, hi = 1, 10 ** 9
            buckets = self._intersect(path, times, lo, hi)
            picked = list(buckets.values())
            # (0,0) 是「算不出范围」的兜底桶，不参与计数。
            n_sections = sum(1 for k in buckets if k[0])

        best = max((c for c, _s in picked if c), default=0.0)
        if not best:
            return ""
        src = next((s for c, s in picked if c == best), "")
        rev = "修订 " + time.strftime(
            "%m-%d %H:%M", time.localtime(float(best))
        ) + f"（{self._ago(float(best))}）"
        if n_sections > 1:
            rev = f"读到 {n_sections} 节，最新" + rev
        if (src or "") == "backfill":
            rev += "，待确认"
        return rev

    def _disk(self, file_path: str) -> Tuple[str, int]:
        """读磁盘原文 → ``(全文, 行数)``，同一屏多块共用一次读。

        只缓存 ≤4 MB 的卡：清单一次最多印 10 张，正常卡（中位 3,290 字）全缓存也就
        几十 KB；库里还有 247,575 字、21.7 MB 那种「一本书」级脏数据，不该常驻内存。
        """
        hit = self._disk_cache.get(file_path)
        if hit is not None:
            return hit
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            self._disk_cache[file_path] = ("", 0)
            return ("", 0)
        info = (text, text.count("\n") + 1 if text else 0)
        if len(text) <= 4 * 1024 * 1024:
            self._disk_cache[file_path] = info
        return info

    def _file_lines(self, file_path: str) -> int:
        """整卡共几行（`read_file` 的 limit 按行算，字数换算不出行号）。"""
        return self._disk(file_path)[1] if file_path else 0

    def _outline(self, file_path: str) -> Dict[str, Any]:
        """文件的**标题骨架**，一处算好，三处共用。``{"lines": n, "heads": [...], "end": {}}``

        - ``heads``：围栏外的标题行 ``[(行下标, 级别, 已清洗文本)]``，按行序。
        - ``end``：每个标题的**节结束行下标**（不含）—— 下一个「级别不高于它」的标题，
          没有就是文件尾。一次算好，`_locate` / `_chain_spans` 都查它，不再各自往下扫。

        为什么要收敛成一个：这三件事（找块所在的节、算链上每级的范围、找卡的 H1）
        本来都要「扫标题行 + 找下一个同级或更高级的标题」，早先是三份各写一遍的实现，
        于是围栏感知这种修复要改三处、必然漏一处。现在只有一份。

        围栏感知是硬要求，不是优化：文档里的 shell / python 示例常写成

            ```bash
            # Batch #1 FAILED: embedding failed for 2/2 texts
            ```

        那行 `# xxx` 是注释，早先被 `_HDR_RE` 当 H1 认了，后果不是少标一格 —— 而是
        **把上一节的结束行算错**（父节本该到文件尾，被这条假 H1 截断），于是它下面
        所有子节的 `行 a~b` 全落在父范围之外，模型照抄行号读到不相干内容。
        实测 `framework-packaging-and-fresh-install.md` 第 70 行就是这么把整棵树带偏的。
        """
        cached = self._outline_cache.get(file_path)
        if cached is not None:
            return cached
        disk = self._disk(file_path)[0]
        lines = disk.split("\n") if disk else []
        heads = scan_heads(lines)
        n = len(lines)

        # 每个标题的节结束行：单调栈，O(标题数)。早先是「每个标题都往后
        # 扫一遍、找第一个级别不高于它的」的双层循环 —— 1846 个标题要 340 万
        # 次比较，一本小说光这里就 119ms；而且 `heads[k+1:]` 每轮都在复制切片。
        end: Dict[int, int] = {}
        stack: List[Tuple[int, int]] = []          # [(级别, 行下标)]
        for i, lvl, _t in heads:
            while stack and stack[-1][0] >= lvl:
                _lv, _li = stack.pop()
                end[_li] = i
            stack.append((lvl, i))
        for _lv, _li in stack:
            end[_li] = n

        # 每节字符数一次算好。**这一条是性能的关键**：`_chain_spans` 早先每次
        # 调用都 `self._disk(...).split("\n")` 一遍全文，7.3MB 文本 split 一次
        # 79.8ms —— `_span_cache` 是按「链」缓存的，6643 个不同的链全 miss，
        # 于是同一份文本被 split 了 6643 次，实测把一次读取拖到 504 秒。
        pre = [0] * (n + 1)
        for k, x in enumerate(lines):
            pre[k + 1] = pre[k] + len(x) + 1
        chars = {i: pre[e] - pre[i] for i, e in end.items()}

        # 标题文本 → [(行下标, heads 下标)]，按行序。`_chain_spans` 每级都要
        # 「在 [lo, hi) 里找这个标题」，逐条扫 1846 个标题再乘 6643 节是千万级；
        # 有这张表就是一次二分。
        by_title: Dict[str, List[Tuple[int, int]]] = {}
        for idx, (i, _lvl, txt) in enumerate(heads):
            by_title.setdefault(txt, []).append((i, idx))

        out = {"lines": n, "heads": heads, "end": end, "chars": chars,
               "by_title": by_title, "level": {i: lvl for i, lvl, _t in heads}}
        self._outline_cache[file_path] = out
        return out

    def _locate(
        self, file_path: str, content: str
    ) -> Optional[Tuple[int, int, int, int]]:
        """``(块起行, 块行数, 节起行, 节行数)`` —— 都是 `read_file` 的现成参数。

        **块起行**：拿块正文前 80 字去原文 `find`。库里存的就是原文切片，实测 10/10 命中
        且起行连续（1/32/46/78/103/126/144/152/170/175）；退一步用该块第一行前 40 字。
        两种都不中就直接返回 None —— 宁可少给，也不给会让模型读错地方的假行号。

        **节起行**：块是从节中间切下去的（一个 1,500 字的块常常只是某节的第 1 片），
        所以从块头往上找最近的标题行，它的节范围就是骨架里算好的那段。全库 29% 的块
        heading_path 有 3 层以上，「命中段 18 行」和「该节 96 行」是两个不同的东西。
        """
        if not file_path or not content:
            return None
        disk = self._disk(file_path)[0]
        if not disk:
            return None
        body = content.strip()
        seg_lines = body.count("\n") + 1
        start = 0
        for probe in (body[:80], body.split("\n")[0].strip()[:40]):
            if not probe:
                continue
            pos = disk.find(probe)
            if pos >= 0:
                start = disk[:pos].count("\n") + 1
                break
        if not start:
            return None
        out = self._outline(file_path)
        sec_start, sec_lines = start, seg_lines
        # 块头之上（含块头那行）最后一个标题，就是块所在的节。
        for i, _lvl, _t in reversed(out["heads"]):
            if i + 1 <= start:
                sec_start = i + 1
                sec_lines = out["end"][i] - i
                break
        # 块会跨节：chunker 是按字数切的，不看标题，实测有 31 行的块落在 7 行的
        # 「## 避坑铁律」下面，直接印「该节 591~597」会让模型**读少一半内容**。
        # 这种情况只给命中段 —— 宁可少给一个语义单元，不给一个会截断的假坐标。
        if sec_start and (start + seg_lines - 1) > (sec_start + sec_lines - 1):
            sec_start, sec_lines = 0, 0
        return (start, seg_lines, sec_start, sec_lines)

    def _build_menu(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过闸门的块 → 一屏标题菜单，每行自带 `read_file` 直接可用的行号。

        **一卡一行，落点一处不少**：同一张卡命中多块时全收进 `hits`（每块带自己的
        **完整标题链** + 命中段行范围），渲染层把重复的链前缀合并成一棵树 ——
        同一小节下命中四片，就只印一次小节名、下面挂四个命中段。

        早先这里直接 `continue` 掉后续块，实测一张卡能命中 12 块，11 处落点凭空消失。
        """
        menu: List[Dict[str, Any]] = []
        seen: Dict[Tuple[str, str], int] = {}
        for chunk in chunks:
            key = self._card_key(chunk)
            fp = str(chunk.get("file_path") or "")
            hit = self._chunk_hit(chunk)
            if key in seen:
                # 同一张卡的另一块：落点挂到已印的那一行上，不丢。
                if hit:
                    _hits = menu[seen[key]]["hits"]
                    if hit not in _hits:
                        _hits.append(hit)
                continue
            seen[key] = len(menu)
            content = str(chunk.get("content") or "")
            geo = self._locate(fp, content)
            # 卡标题行印的「行 a~b」必须是**这张卡自己**的范围，不是整个文件。
            # 一个 .md 里可以塞好几张卡（实测 441 份文件装 1,048 张），第二张卡从
            # 自己的 H1 那一行才算开始；照文件总行数印「行 1~N」，模型抄下去就会
            # 把上一张卡的内容一起读进来。heading_path 第一段就是这张卡的 H1。
            _h1 = self._clean_heading(
                (_path_parts(chunk.get("heading_path")) or [""])[0]
            )
            _cs, _cl = 0, 0
            if _h1:
                _sp = self._chain_spans(fp, [_h1]) or [(0, 0, 0)]
                _, _cs, _cl = _sp[0]
            # 区间是不是**这张卡自己**的：只有 H1 真定位到了才算。定位失败会退化成
            # 整文件区间，而一文件多卡时那等于「读了任意一行 → 同文件所有卡都算读过」
            # —— 静默漏投，方向不安全。记下精确性，已读判定只认精确区间。
            _span_exact = bool(_cs)
            if not _cs:
                _cs, _cl = 1, self._file_lines(fp)
            menu.append({
                "key": key,
                "cid": str(chunk.get("chunk_id") or ""),
                "title": self._card_title(chunk),
                # 这张卡的全部命中落点（含首块）：[{chain, a, n}, ...]。
                # chain 是完整标题链（不含 H1），渲染层按它合并成树。
                "hits": [hit] if hit else [],
                # 体量只留行数：字数是行数的派生量，卡片行已经不印了，也不必再查库。
                # total_lines 是**这张卡**的行数（一文件多卡时不含别的卡），
                # card_start 是它在文件里的起始行，渲染层拼成「行 a~b」。
                "total_lines": _cl,
                "card_start": _cs,
                # False = 上面那两个数是整文件兜底，不是这张卡自己的范围。
                "span_exact": _span_exact,
                # 绝对路径：read_file 的唯一入口
                "path": fp,
                # 原始首段（未归一）。兜底时要区分「假 H1」和「文件名回退」：
                # 后者本来就没有可定位的标题行，改了会跟文件里第一张真卡撞 key。
                "_h1": _h1,
            })
        # 标题兜底：切分器不认代码围栏，会把代码块里的 `# 注释` 写成 `chunk.h1`
        # （实测某块 `h1` 就是 ``reindexed: 0 files / 0 chunks`` —— 文件第 6983 行
        # 一个 bash 块里的一行）。这种标题在 `_outline` 的骨架里定位不到，而骨架
        # **只认围栏外的标题**，所以「定位不到」本身就是干净的判据：退回文件首个
        # H1，那张「卡」于是并回它本来属于的那张卡。
        for m in menu:
            fp0 = str(m.get("path") or "")
            _t = str(m.get("title") or "")
            _raw = str(m.get("_h1") or "")
            # 判「这张卡的起点是不是真的本文件一级标题」：
            # - 级别 1 → 就是主卡，不动（一文件多卡靠这条保住）；
            # - 级别 ≥2 → 切分器拿 `## 小节` 当了卡起点，它属于文件首个 H1 那张卡；
            # - 级别 0 且首段 >8 字 → 代码块里的 `# 注释`（假 H1），同上去；
            # - 级别 0 且首段 ≤8 字 → `_card_title` 退回文件名，本来没有标题行，别动
            #   （实测 `SKILL.md` 里「SKILL」被兜底成文件首个 H1，把两张卡合成了一张）。
            if not _raw or _t != _raw:
                continue
            _lvl = self._title_level(fp0, _t)
            if _lvl == 1 or (_lvl == 0 and len(_raw) <= 8):
                continue
            _first = next(
                (t for _i, _l, t in (self._outline(fp0).get("heads") or []) if _l == 1),
                "",
            )
            if _first:
                m["title"] = _first
                m["key"] = (fp0, _first)
        return self._merge_menus(menu)

    def _title_pos(self, fp: str, title: str) -> int:
        """这个标题在原文里作为标题行的起始行；定位不到返回 0。"""
        if not fp or not title:
            return 0
        try:
            sp = self._chain_spans(fp, [self._clean_heading(title)])
        except Exception:  # noqa: BLE001 —— 定位失败不该让整屏渲染炸掉
            return 0
        return int(sp[0][1]) if sp else 0

    def _title_level(self, fp: str, title: str) -> int:
        """这个标题在骨架里的级别（1 = 一级标题）；定位不到返回 0。

        用来区分三种「卡标题」：真 H1（1）、切分器拿 `## 小节` 当卡起点（≥2）、
        代码块里的 `# 注释`（0，骨架围栏感知，认不到）。
        """
        if not fp or not title:
            return 0
        want = self._clean_heading(title)
        for _i, lvl, txt in (self._outline(fp).get("heads") or []):
            if txt == want:
                return int(lvl)
        return 0

    def _merge_menus(self, menus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """多批清单 → 一屏：同一张卡只留一行，命中落点取并集。

        `_build_menu` 的去重只在**单批内**有效 —— 批与批之间互相看不见。批数闸
        删掉之后一次可能投十几批，同一张卡在好几批里各命中一次的概率反而更高；
        不合并的话那张卡会在屏上出现两三次，每棵树只带自己那一批的落点，模型
        会当成不同的卡分别去读。

        判同卡三条，从严到松：
        1. `key`（路径 + 卡标题）相同；
        2. 路径相同、两个标题互相包含（老数据缺 `h1` 列时标题由 `heading_path`
           反推，同一张卡的不同块可能推出长短两种写法）；
        3. 路径相同、`card_start` 相同 —— 一条兜底：切分器**不认代码围栏**，
           会把代码块里的 `# 注释` 当成 H1（实测某块 `h1` 列就是
           ``reindexed: 0 files / 0 chunks``，那是文件第 6983 行 bash 块里的一行）。
           这种「卡」没有自己的范围，整块都落在主卡里，只能是主卡的一部分。

        标题取「在原文里更靠前」的那个：上面那种假 H1 的行号在文件后半段，
        不该抢走主卡的名字。
        """
        out: List[Dict[str, Any]] = []
        for m in menus:
            fp = str(m.get("path") or "")
            title = str(m.get("title") or "")
            _cs = int(m.get("card_start") or 0)
            idx = -1
            for i, base in enumerate(out):
                if base.get("key") == m.get("key"):
                    idx = i
                    break
                if not fp or str(base.get("path") or "") != fp:
                    continue
                bt = str(base.get("title") or "")
                _bp2 = self._title_pos(fp, bt)
                _mp2 = self._title_pos(fp, title)
                # 标题互相包含：两条都**能定位到标题行**时才认。否则
                # 「SKILL.md」和「SKILL」这种前缀关系会把两张不同的卡合掉。
                if bt and title and _bp2 and _mp2 and (bt in title or title in bt):
                    idx = i
                    break
                # 同坐标兜底：只在「一边定位得到、一边定位不到」时启用。
                # 定位不到的那半是切分器写坏的假 H1（代码块里的 `# 注释`），
                # 它的 `card_start` 只能回退成 1，本身没有可信坐标；两条都
                # 定位不到就说明是另一个问题，宁可分开也别混成一棵树。
                if _cs and int(base.get("card_start") or 0) == _cs and bool(_bp2) != bool(_mp2):
                    idx = i
                    break
            if idx < 0:
                out.append(m)
                continue
            base = out[idx]
            _bp, _mp = self._title_pos(fp, str(base.get("title") or "")), self._title_pos(fp, title)
            if _mp and (not _bp or _mp < _bp):
                base["title"] = title
            _have = {(int(h.get("a") or 0), int(h.get("n") or 0))
                     for h in base.get("hits") or []}
            for h in m.get("hits") or []:
                _k = (int(h.get("a") or 0), int(h.get("n") or 0))
                if _k not in _have:
                    base["hits"].append(h)
                    _have.add(_k)
        return out

    def _chunk_hit(self, chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """一个命中块的落点：``{chain, a, n}`` —— 完整标题链 + 命中段行范围。

        **必须带完整链**（不含 H1）：渲染层就是靠比对各块链的公共前缀来合并成树的。
        早先只留末级小节名，于是「一、三屏 > 多级标题」和「九、上线后 > 多级标题」
        这两处不同分支的同名小节糊成一行，看着像重复。

        定位不到行号就返回 None —— 不印没有坐标的条目，那种行模型抄不了。
        """
        geo = self._locate(str(chunk.get("file_path") or ""),
                           str(chunk.get("content") or ""))
        if not geo or not geo[0] or not geo[1]:
            return None
        return {
            "chain": self._card_chain(chunk),
            "a": geo[0],
            "n": geo[1],
        }

    @staticmethod
    def _chain_parts(chunk: Dict[str, Any]) -> List[str]:
        """``heading_path`` → 层级数组，**H1 用 chunk 自带的，不再拿 " > " 去切它**。

        单层卡的 ``heading_path`` 就是 H1 本身（没有分隔符）；标题里一旦含
        " > "（例如「…用 " > " 拼层级…」），``_path_parts`` 会把它割成好几段，
        凭空长出假小节。H1 已知时按前缀整段剥掉，剩下的才交给分隔符去拆。
        """
        hp = str(chunk.get("heading_path") or "")
        h1 = str(chunk.get("h1") or "").strip()
        if h1 and hp.startswith(h1):
            rest = hp[len(h1):].lstrip("\x1f").strip()
            if rest.startswith(">"):
                rest = rest[1:].strip()
            return [h1] + (_path_parts(rest) if rest else [])
        return _path_parts(hp)

    @classmethod
    def _card_chain(cls, chunk: Dict[str, Any]) -> List[str]:
        """菜单第二行：这张卡里命中的那一段，在标题树里的**完整路径**。

        只印最深一级看不出上下文（同一张卡里有好几个「修法与验收」），只印尾部两级也会
        砍掉中间层。实测"完整链"根本不贵：去掉 H1 之后全长**中位 44 字、P90 108 字**，
        超过 120 字的只有 6.7%，所以没有理由为省几十字牺牲可理解性 —— 全印。

        H1 不重复印（它已经在卡片首行的标题里）。`(Part 1)` 是切分母出来的，剥掉，
        否则同一节看着像三节。通用小节名（「根本原因与核心设计」）在完整链里是结构
        信息，**保留**，不再像只印一级时那样过滤掉。

        **一个字都不省。** 早先设过 200 字上限、超了中间打 `…`，那是把「判断这段值不值得
        点」的关键层砍掉喂给模型 —— 省略掉的正好是它最需要看的那部分。真要控预算，
        就少印几张卡（抬头会写「另有 N 张没印」），不是把每张卡切一半。

        返回**段列表**，不自己拼字符串：渲染层要把它画成逐级缩进的树（owner：挤在
        一行看不出结构，`A > B > C` 得写成一行一层）。
        """
        parts = cls._chain_parts(chunk)
        if len(parts) < 2:
            return []
        segs = []
        for part in parts[1:]:
            seg = cls._clean_heading(part)
            if seg:
                segs.append(seg)
        return segs

    @staticmethod
    def _rng(a: int, n: int) -> str:
        """行范围。单行印一个数；多行印 ``a~b``，行数由 `b - a + 1` 算得出来，不重复写。"""
        return f"行 {a}" if int(n) <= 1 else f"行 {a}~{int(a) + int(n) - 1}"

    @staticmethod
    def _clean_heading(text: str) -> str:
        """标题文本去装饰，标题链和磁盘原文两边都过一遍才能比对上。

        `(Part 1)` 是切分母出来的（同一节看着像三节）；`{#custom-block-states}` 是文档站
        锚点，落库的 `heading_path` 原样留着；`**` / 反引号是 markdown 残留。全剥。
        """
        seg = re.sub(r"(?:\s*\(Part \d+\))+$", "", str(text or "")).strip()
        seg = re.sub(r"\{#[-\w]+\}", "", seg)
        return seg.replace("**", "").replace("`", "").strip()

    def _heads(self, file_path: str) -> List[Tuple[int, int, str]]:
        """磁盘上的标题行 ``[(行下标, 级别, 已清洗文本)]`` —— 直接取骨架，不自己扫。"""
        return self._outline(file_path)["heads"]

    def _chain_spans(self, file_path: str, chain: List[str]) -> List[Tuple[int, int, int]]:
        """命中链每一级在原文里的 ``(字符数, 起行, 行数)``。

        **每一级都给完整行范围**，不只是最末一级。理由：模型完全可能想读整级
        （「Telemetry 这节我全要」），只在最末一级给行号等于逼它为了读父级自己
        去推算 —— 而父级的起止它没有。

        字符数口径与「整卡 N 字」完全一致（都是 `len()`）。

        模型看到「Telemetry · 1,880 字」和「整卡 48,258 字」是同一把尺子，才能判断
        「这一级就够了」还是「得往下钻两层」。只给整卡总数是失效口径：整卡 48k 字、
        命中那节 96 字，两个数差 500 倍，不给节大小等于让模型猜。

        算法：在骨架里按链的顺序**逐级收窄搜索范围**（第 i 段只在第 i-1 段的子树里找），
        所以同名小节不会串到别的分支去。任一级对不上就跳过那一段（返回 0，渲染层
        不标），绝不给猜出来的数。
        """
        if not file_path or not chain:
            return []
        # 记忆化：画树时每个节点都要算一次行范围，同一 (文件, 链) 会反复问。
        # 实测一张 5000 行卡画完要 1.8 秒 —— 注入钩子跑在每轮请求路径上，不能留。
        _ck = (file_path, tuple(chain))
        _cached = self._span_cache.get(_ck)
        if _cached is not None:
            return list(_cached)
        out = self._outline(file_path)
        heads = out["heads"]
        n = out["lines"]
        spans: List[Tuple[int, int, int]] = []
        lo, hi = 0, n

        def _find(want_: str, lo_: int, hi_: int) -> int:
            """在 ``[lo_, hi_)`` 行内找这个标题，**返回最贴近的那个**。

            匹配口径（从紧到松）：
            1. 完全相等 —— 优先；
            2. 互相 `startswith` —— 只用于吸收 markdown 残留（反引号、`**`、锚点已被
               `_clean_heading` 剥掉后仍有的尾差），但**必须**取最长公共的那个，不能
               见着第一个就返回。

            早先只有第 2 条且「见第一个就返回」：同一张卡里「改法」和「改法：一卡一行」
            是两个不同小节，找「改法：一卡一行」时先撞上兄弟节点「改法」就返回了，
            于是行范围指到别的小节（实测把 6734~6777 标成 5277~5293）。
            """
            lst = out["by_title"].get(want_)
            if lst:
                k = bisect.bisect_left(lst, (lo_, -1))
                if k < len(lst) and lst[k][0] < hi_:
                    return lst[k][1]
            loose = -1
            for idx, (i, _lvl, txt) in enumerate(heads):
                if i < lo_ or i >= hi_:
                    continue
                if loose < 0 and (txt.startswith(want_) or want_.startswith(txt)):
                    loose = idx
            return loose

        for want in chain:
            want = self._clean_heading(want)
            hit = _find(want, lo, hi)
            if hit < 0:
                # **不回退全文**。早先这里 `_find(0, len(lines))` 兜底，理由是
                # 「heading_path 是入库快照，父级偏移会让子树整个落空」（实测 2.6%）。
                # 但回退的代价是**给出别的分支的行号** —— 画成树之后，一个孙子节点
                # 的行范围能落到它祖先范围之外（实测 6734~6777 挂在 6706~6722 下面），
                # 模型照着抄就是读到不相干的内容。
                # 宁可这一段标 (0,0,0) 不印行号，也不给一个会让人读错的坐标。
                spans.append((0, 0, 0))
                lo, hi = 0, n
                continue
            i, _lvl, _txt = heads[hit]
            end = out["end"][i]        # 骨架里已算好：下一个同级或更高级的标题
            spans.append((out["chars"][i], i + 1, end - i))
            lo, hi = i + 1, end
        self._span_cache[_ck] = tuple(spans)
        return spans

    def _render_menu(self, menu: List[Dict[str, Any]]) -> str:
        """标题清单：卡标题 + 命中小节 + **可直接喂给 `read_file` 的行号**。

        刻意不印 gate 分（全挤在 0.99xx，纯噪音）、不印 chunk_id（4 位前缀 36.42% 碰撞）。

        每行给的是 `read_file` 的现成参数，不是给人看的描述：
        每级都带自己的 `行 a~b`，读整级还是只读命中段，由调用方按任务定。

        改成实例方法（原 `@staticmethod`）：画树时要查磁盘标题行算每节的行范围，
        那需要 `_chain_spans` / `_outline`。原先只有 `_chain_spans` 被外部算好
        传进来，所以还能是静态的；现在树的形状由命中集合决定，行范围得在里面现算。

        **不印序号**：`#N` 是去重账本的产物（同一张卡永远同一个号），账本删了，
        序号也就没有意义 —— 认卡靠标题和路径。
        """
        _chain_spans = self._chain_spans

        def _hit_tree(m: Dict[str, Any]) -> str:
            """把这张卡的全部命中落点合并成**一棵**树。

            为什么要合并：同一小节常被切出好几片（一张卡 12 块里 4 块同属
            「多级标题」那一节）。早先每块一行，于是同一个小节名连印四遍、各带一个
            行范围，看着像四条互不相干的命中，实际是同一节的四个片段 —— 既浪费
            预算，也让模型以为要分四次去读。

            合并规则：按各块**完整标题链**的公共前缀建树，同一个节点只印一次；
            命中段作为叶子挂在它自己那一节下面。跨分支的同名小节（「一、三屏 >
            多级标题」与「九、上线后 > 多级标题」）链前缀不同，各在各的分支里，
            不会被糊成一行。

            每一级仍带自己的 `· N 字 · 行 a~b（n 行）` —— 那个范围是**整节**的，
            和叶子上的「命中段」是两个不同粒度，都要给。

            路径行一律印：一棵树只有一个入口，就是它自己的绝对路径。早先给老卡的
            「追加块」开过 `tail=False` 省这一行（理由：路径上一条消息里给过），
            但省掉之后那一行只剩 `#N ↑ 追加 M 处命中`，**卡标题和路径双双缺席**，
            模型拿不到「这是什么卡、文件在哪」这两个判断入口，等于有坐标没对象。
            追加形态已整体删除，这里不再有第二个分支。
            """
            hits = [h for h in (m.get("hits") or [])
                    if int(h.get("a") or 0) and int(h.get("n") or 0)]
            if not hits:
                return ""
            fp = str(m.get("path") or "")
            # 建树：children 保持首次出现的顺序（检索序），leaves 按行号排序（文档序）。
            root: Dict[str, Any] = {"seg": "", "children": {}, "order": [], "leaves": []}
            for h in hits:
                node = root
                for seg in (h.get("chain") or []):
                    if seg not in node["children"]:
                        node["children"][seg] = {
                            "seg": seg, "children": {}, "order": [], "leaves": []}
                        node["order"].append(seg)
                    node = node["children"][seg]
                key = (int(h["a"]), int(h["n"]))
                if key not in node["leaves"]:
                    node["leaves"].append(key)

            def _span(segs: List[str]) -> Tuple[int, int, int]:
                """这一级在原文里的 ``(字数, 起行, 行数)``；比对上就是 (0, 0, 0)。"""
                if not segs:
                    return (0, 0, 0)
                sp = _chain_spans(fp, segs)
                return sp[-1] if sp else (0, 0, 0)

            def _mark(prefix: str, last: bool) -> str:
                """`prefix` 是祖先层的垂线串，`last` 决定本层用 `└` 还是 `├`。"""
                return prefix + ("└ " if last else "├ ")

            out: List[str] = []

            # 路径行永远印（见 docstring：一棵树只有一个入口）。做成闭包变量而不是
            # `walk` 的形参 —— 早先它是形参、只有根层传值，等于给「不印路径」留了个
            # 只有追加块在用的暗门。
            _tail = str(m.get("path") or "")

            def walk(node: Dict[str, Any], prefix: str, segs: List[str]) -> None:
                """深度优先画这一层。孩子与叶子**按原文行号排序**（文档顺序）。

                早先孩子按「首次出现」排（= 检索序），于是同一张卡的树长成
                「六、十、一、四、七、二、九」这种乱序 —— 读者没法判断自己在文档的
                哪个位置，也没法顺着一路读下去。树本来就该反映原文结构。
                """
                leaves = sorted(node["leaves"])
                # 这一节整级就是一个命中段时（块正好切满整节）不重复印一遍叶子。
                _nsz, na, nn = _span(segs) if segs else (0, 0, 0)
                if len(leaves) == 1 and nn and leaves[0] == (na, nn):
                    leaves = []
                # (排序键, 类型, 内容)：孩子键=该节起行，叶子键=命中段起行。
                # 定位不到行号的给孩子一个极大键，排在最后但不丢。
                items: List[Tuple[int, str, Any]] = []
                for s in node["order"]:
                    _sz2, ka, _kn = _span(segs + [s])
                    items.append((ka or 10 ** 9, "kid", node["children"][s]))
                for l in leaves:
                    items.append((l[0], "leaf", l))
                if _tail and not segs:
                    # 只在**根层**印路径行（`segs` 为空即根）：递归时的 children 各自
                    # 不印，否则一棵树每层都挂一条路径。
                    # 路径行跟树的孩子同级，必须**参与** last 判定：早先它被硬编码成
                    # `└ 路径`，于是根层既有一个 `└` 收尾的分支、又有一个 `└ 路径`，
                    # 一棵树长出两个收口，垂线视觉上是断的。永远排最后。
                    items.append((10 ** 10, "tail", _tail))
                items.sort(key=lambda x: x[0])
                for idx, (_key, kind, val) in enumerate(items):
                    last = idx == len(items) - 1
                    # 本层垂线：最后一个孩子后面不再画 `│`，否则会拖出一条断线。
                    child_prefix = prefix + ("  " if last else "│ ")
                    if kind == "kid":
                        k = val
                        ksegs = segs + [k["seg"]]
                        _chars, a, n = _span(ksegs)
                        # 不印字数：判断「整节读还是只读命中段」看行范围就够，
                        # 字数是它的派生量，两个数说同一件事只占地方。
                        tag = f" · {self._rng(a, n)}" if n else ""
                        out.append(f"{_mark(prefix, last)}{k['seg']}{tag}")
                        walk(k, child_prefix, ksegs)
                    elif kind == "leaf":
                        a, n = val
                        out.append(f"{_mark(prefix, last)}"
                                   f"命中段 {self._rng(a, n)}")
                    else:
                        out.append(f"{_mark(prefix, last)}路径：{val}")

            walk(root, "   ", [])
            return ("\n" + "\n".join(out)) if out else ""

        def _row(m: Dict[str, Any]) -> str:
            # 卡标题行 = 这张卡的身份 + 整卡体量。体量决定「整篇读还是只读一段」，
            # 是卡的属性，就该跟卡标题在一起。
            # 不再印 `#N`：序号本来就是去重账本的产物（同一张卡永远同一个号），
            # 账本删了，序号也就没有意义 —— 认卡靠标题和路径。
            out = m["title"]
            tl = int(m.get("total_lines") or 0)
            cs = int(m.get("card_start") or 1) or 1
            # 只印行范围。起点未必是 1 —— 一个文件里可以有好几张卡，第二张从自己的
            # H1 起算。不印字数、不加「别整读」这类命令，怎么读是模型自己的判断。
            if tl:
                out += f" · {self._rng(cs, tl)}"
            # 不印「命中 N 处」：树里几个「命中段」就是几处，同一件事说两遍只占地方。
            out += _hit_tree(m)
            # 路径行由 `_hit_tree` 统一收口（它要参与树的孩子排序才能只有一个 `└`）。
            # 没有命中落点时（定位全失败）树是空的，这里兜底补一行，否则这卡没有入口。
            if m.get("path") and not (m.get("hits") or []):
                out += f"\n   └ 路径：{m['path']}"
            return out

        lines = "\n".join(_row(m) for m in menu)
        # 抬头只报**卡数**。不印检索词：一屏可能由十几批拼成，各带各的词，
        # 印出来是一串重复的噪声；词本身在每条检索日志里都有 `q=…`，要查查日志。
        head = f"知识库检索到 {len({m['key'] for m in menu})} 个相关知识块。"
        return (
            "<knowledge-base-titles>\n"
            f"{head}\n"
            "读法：`read_file(路径, offset=起行, limit=止行-起行+1)`；缩进是小节路径，"
            "标「命中段」的那行只是该节的一部分。\n"
            f"{lines}\n"
            "</knowledge-base-titles>"
        )

    def _protocol_context(self, turn_key: str) -> str:
        with self._turn_lock:
            state = self._state(turn_key)
            if state["protocol_added"]:
                return ""
            state["protocol_added"] = True
        return PROTOCOL_PROMPT

    def _search_validated(
        self,
        query: str,
        top_k: Optional[int] = None,
        candidate_limit: Optional[int] = None,
        tag: str = "retrieve summary",
        abort: Any = None,
    ) -> List[Dict[str, Any]]:
        """统一检索入口：注入路径（retrieve_chunk）与主动查库（search_knowledge）共用。

        闸门本身在 `hermes_kb.gate.gated_search`（CLI 也调它），这里只负责留痕。
        曾因两条路径各写一份过滤逻辑，显式工具不套闸门、把 rerank=0.00004 的
        噪音原样返回给模型。排序键与阈值只要分叉一次就会静默漂移。
        """
        if not query or len(query.strip()) < 3:
            return []
        try:
            # 局部导入：理由同 `engine` 属性，hermes_kb 的重库加载推迟到首次真实检索。
            from hermes_kb.gate import gated_search

            gated = gated_search(
                self.engine, query, candidate_limit=candidate_limit, top_n=top_k,
                abort=abort,
            )
            # [留痕] 单行检索摘要：事后可直接回答「阈值卡掉多少张」
            logger.info(
                "[KBAutoInject] %s: cand=%d kept=%d%s | rerank<%.2f dropped=%d | q=%s",
                tag,
                gated.candidates,
                len(gated.kept),
                f" (fallback {gated.fallback})" if gated.fallback else "",
                gated.min_score,
                gated.dropped,
                _short(query, 100),
            )
            return gated.kept
        except Exception as exc:
            logger.warning(
                "[KBAutoInject] %s error for %r: %s", tag, query[:30], exc
            )
            return []

    def retrieve_chunk(
        self,
        query: str,
        top_k: Optional[int] = None,
        candidate_limit: Optional[int] = None,
        abort: Any = None,
    ) -> List[Dict[str, Any]]:
        """注入路径的检索入口。参数解析与闸门全在 `_search_validated`，这里只贴标签。

        （曾把 candidate_limit/top_k 的解析逻辑在本函数里抄了一份，与
          `_search_validated` 逐字重复 —— 两处一旦分叉就会静默漂移，已删。）
        """
        return self._search_validated(
            query, top_k=top_k, candidate_limit=candidate_limit,
            tag="retrieve summary", abort=abort,
        )

    def _inline_menu(
        self, query: str, top_k: Optional[int] = None, max_cards: Optional[int] = None
    ) -> str:
        """检索词 → 检索 → 一屏标题清单。**同步、当场返回**。

        唯一的调用方是 `search_knowledge` 工具 —— 它本身就要把结果交回模型。
        注入路径（用户原话、所有工具的 kb_query）都不走这里：一律 `kb_queue`
        → 后台池 → 跑完由 `_on_job_done` 攒进待投递槽，等下一次请求贴进副本。

        ``top_k`` 是喂给 reranker 的候选数；``max_cards`` 只在**调用方自己点名**时才设
        （`search_knowledge` 的 `limit`），注入路径一律不设 —— 过闸的全印。

        印刷部分全在 `_menu_from_chunks`：同步与异步两条路径共用它，渲染只有一份实现。
        """
        _q = (query or "").strip()
        if not _kb_query_is_usable(_q):
            return ""
        try:
            # top_k 为 None 时 _search_validated 会自己读 retrieval.rerank_top_n ——
            # 这里不再抄一份 `doing_top_k`（那是旧 doing 协议时代的键名，
            # 跟 rerank_top_n 实盘同值 25，两份真源迟早分叉）。
            _chunks = self.retrieve_chunk(_q, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[KBAutoInject] inline retrieve failed: %s", exc)
            _chunks = []
        return self._menu_from_chunks(_q, _chunks, max_cards=max_cards)

    def _menu_from_chunks(
        self, query: str, chunks: Any, max_cards: Optional[int] = None
    ) -> str:
        """已检索好的 chunks → 一屏**标题清单**（含 `read_file` 现成的行号）。

        与 `_inline_menu` 的唯一差别：检索那一步由调用方先做完。因此本函数**不碰网络**，
        只做本地计算 —— 后台线程的产物在这里印刷，主线程只负责把它贴进请求副本。

        **不去重、不记账、不编号**（2026-11-24 晚定案）。检索到几块就印几块：
        同一批重复贴出来只是多几行字，而一套「印过没有」的账本会漂、会在压缩后
        失效、会和历史里的残行对不上 —— 那才是真麻烦。

        只认一道闸：`min_rerank_score`。过了就是合格块，合格块必须出现在清单里。
        """
        _q = (query or "").strip()
        _menu = self._build_menu(chunks) if chunks else []
        if max_cards is not None:
            _menu = _menu[:max_cards]
        if not _menu:
            return ""
        logger.info(
            "[KBAutoInject] MENU EMIT(%d 卡 / %d 段) q=%s",
            len(_menu),
            sum(len(m.get("hits") or []) for m in _menu),
            _short(_q, 70),
        )
        return self._render_menu(_menu)

    # ============ 唯一的提交口 ============
    #
    # 触发点有两个：用户原话（pre_llm_call）、工具参数里的 kb_query（dispatch）。
    # 两者都只调 `kb_queue`，不看返回值、不判断同步异步 —— 一旦调用方开始看返回值，
    # 就会重新长出第二条注入路径，那正是这一轮拆掉的东西。
    #
    # 后台线程只跑 retrieve 和印刷（`_menu_from_chunks`），产物一律进待投递槽；
    # `_pending` 由 `_pending_lock` 保护，除此之外没有任何共享账本。

    def kb_queue(self, query: str, session_id: str = "") -> None:
        """把检索丢进后台池。**不返回任何东西**。

        为什么返回 None 而不是状态：调用方一旦能看见状态，就会写出
        「池里没位置就同步查一次」的降级分支 —— 那个分支就是第三条注入路径。
        池子现在不会因为积压而满：超额时让最老的排队者让位（见 async_pool）。
        返回 ``"full"`` 只剩 executor 已 shutdown 这一种情况，那时记一行 warning。

        **不判重**：同一个词在同一轮里被两个工具调用都写上，就检索两次、贴两屏。
        少一次去重账本，换来的是「永远不会有账本和历史对不上」。
        """
        _q = (query or "").strip()
        if not _kb_query_is_usable(_q):
            return
        action = self._async_pool.submit(session_id or "default", _q)
        if action in ("ok", "inflight"):
            return
        logger.warning(
            "[KBAutoInject] kb_queue DROP (%s) sid=%s q=%s",
            action, (session_id or "default")[:26], _short(_q, 60),
        )

    # ============ 唯一出口：检索跑完攒起来，下次请求贴进副本 ============

    def _prompt_rank(self, request: Dict[str, Any], query: str) -> int:
        """这条检索词在**当前提示词**里的位置，越大越新。

        为什么不按提交序号（``seq``）排：Hermes 的只读工具批次是**并行**
        执行的，并行段里 ``next(seq)`` 的先后取决于线程调度，既不等于
        ``tool_calls`` 数组顺序，也不等于提示词顺序。完成时间更不行 ——
        跑得快的旧批会冒充新的。
        所以直接从请求里从后往前找它最后一次出现的位置。

        找不到（上一轮留下、已被压缩）返回 -1，按旧批处理。
        """
        needle = query[:40]
        if not needle:
            return -1
        msgs = request.get("messages") or request.get("input") or []
        for back, msg in enumerate(reversed(msgs[-40:])):
            if not isinstance(msg, dict):
                continue
            for tc in reversed(msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                args = (tc.get("function") or {}).get("arguments")
                if isinstance(args, str) and needle in args:
                    return 1_000_000 - back
            if msg.get("role") == "user":
                text = _content_text(msg.get("content"))
                if text and needle in text:
                    return 1_000_000 - back
        return -1

    def _on_job_done(
        self,
        session_id: str,
        query: str,
        chunks: List[Dict[str, Any]],
        seq: int,
    ) -> None:
        """检索完成的这一刻：清单进待投递槽。后台线程调用。

        不做判断、不查历史、不比对 —— 印出来就排队等着，下一次模型请求
        由 `handle_llm_request` 取走贴进请求副本（不进 state.db）。

        ``seq`` 是池子的**提交**序号（不是完成序）：出口排序时的兜底键。
        **必填，不给默认值** —— 漏传时若默认成 0，这批会被排到最前当成
        「最旧的」直接丢掉，而且静默无日志。宁可当场 TypeError。
        真正的「新旧」由 ``handle_llm_request`` 用检索词在提示词里的位置判定。
        """
        _sid = session_id or "default"
        # 槽里存**结构化菜单**而不是渲染好的字符串：同一张卡可能在两批里各命中一次，
        # 只有拿到结构才能在出口合成一棵树。渲染推迟到出口那一处（见 `_merge_menus`）。
        _menu = self._build_menu(chunks) if chunks else []
        if not _menu:
            return
        with self._pending_lock:
            self._pending.setdefault(_sid, []).append((seq, query, _menu))
        logger.info(
            "[KBAutoInject] pending MENU cards=%d q=%s sid=%s",
            len(_menu), _short(query, 60), _sid[:26],
        )

    def handle_transform_tool_result(
        self, tool_name: str = "", args: Any = None, result: Any = None, **kwargs: Any
    ) -> Optional[str]:
        """读正文时补一行「这张卡什么时候写的」（`transform_tool_result` 钩子）。

        清单里不印时间（清单要干净），但 AI 常常**只读命中段**、跳过卡头那行
        「记录时间」—— 于是它对「这条知识是三个月前的」毫无感觉。这里在真正的
        正文读取出口补上：只认 `read_file` + **库内文件**，别的工具/路径一律
        返回 None（None = 原样放行）。

        加的是返回 JSON 里的一个新字段 `kb_card`，**`content` 一个字不碰**：
        content 每行带 `行号|` 前缀，往里插一行会让后面所有行号错位。
        """
        # 先记「这张卡本轮读过了」—— 与下面挂时间戳是两件事、互不依赖：
        # 时间戳拿不到（小文件没坐标）也不该影响已读记账。
        self._mark_read(tool_name, args, **kwargs)
        if tool_name != "read_file" or not isinstance(result, str):
            return None
        # 工具自己失败了（文件不存在 / 权限 / 目录），读到的不可能是正文。
        # `status` 缺失时按成功处理 —— 宁多加一行，不因缺字段就不加。
        if str(kwargs.get("status") or "ok").strip().lower() != "ok":
            return None
        try:
            raw = str((args or {}).get("path") or "")
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        path = os.path.abspath(os.path.expanduser(raw))
        try:
            if self._chunk_times(path) is None:
                return None
            info = self._card_time_text(
                path, (args or {}).get("offset"), (args or {}).get("limit")
            )
            if not info:
                return None
            obj = json.loads(result)
            if not isinstance(obj, dict) or "kb_card" in obj:
                return None
            obj["kb_card"] = info
            return json.dumps(obj, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 —— 补时间失败不该让读文件失败
            logger.debug("[KBAutoInject] kb_card skip %s: %s", raw, exc)
            return None

    def handle_llm_request(
        self, request: Any = None, session_id: str = "", **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """每次模型调用前，把攒下的清单**按阶梯**贴到最后一条消息尾部。**不进历史**。

        阶梯（**间隔口径**）：第一次命中就投；投完清零，再攒 3 次投；再清零，攒 10 次
        投；之后 30、100、300… —— 命中序号上是 1、4、14、44、144、444…。其余次数只
        推进计数、不贴，既压掉「同一张卡连着贴两遍」，也保证每张卡在本轮里始终有回来的
        节奏。

        **本轮已经读过正文的卡直接不投**：标记不在这层做 —— `transform_tool_result`
        钩子在读取发生的那一刻就把那张卡记成 `_READ`（`_mark_read`）。这里只认一个
        事实，不扫历史、不问模型。换 turn 就是新表，标记自动作废。

        核心（`hermes_cli/middleware.py: apply_llm_request_middleware`）把请求
        `_safe_copy` 之后交进来，改的是副本，替换 `api_kwargs` 只对这一次调用
        生效，`state.db` 里一个字都不落 —— 这正是「不进上下文历史」。
        契约：改了就返回 `{"request": <dict>}`，没改必须返回 `None`。
        """
        if not isinstance(request, dict):
            return None
        _sid = session_id or "default"
        # `_turn_key` 内部是从 kwargs 取 session_id 的（`handle_pre_llm_call` 那边
        # 它本来就在 kwargs 里）。这里 session_id 是独立形参，不并进去会退化成
        # `default:<turn_id>`，多会话共用同一个前缀。
        _tk = self._turn_key({**kwargs, "session_id": _sid}, request.get("messages"))
        with self._pending_lock:
            _slot = self._pending.get(_sid)
            if not _slot:
                return None
            # 计数必须按**提交序号**推进：一次请求可能同时取到好几批，若照渲染
            # 顺序走，「第 3 次命中」就会错位。渲染顺序在下面另排一次。
            _slot.sort(key=lambda item: item[0])
            _take = list(_slot)
            _slot.clear()
            self._pending.pop(_sid, None)

        with self._turn_lock:
            # 深拷贝：值是可变记录（含命中次数），浅拷贝会让「贴失败不记账」变成空话
            # —— 失败回退的批会把已加过的次数留在共享对象上。
            _hits = {k: list(v) for k, v in (self._ladder.get(_tk) or {}).items()}

        _emit: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        _skipped = 0
        for _, _q, _menu in _take:
            # `_build_menu` 的产物已经是一张卡一条，单批内不必再合并。
            for _item in _menu:
                _key = str(_item.get("key") or _item.get("title") or "")
                if not _key:
                    continue
                # 记录 = [命中次数, 路径, 起行, 行数, 区间是否精确]。
                # 本轮第一次见到这张卡时把判定材料一并存下 —— `transform_tool_result`
                # 那边标记已读就纯内存比区间，不必回头查文件骨架、更不必扫历史。
                rec = _hits.get(_key)
                if rec is None:
                    rec = _hits[_key] = [
                        0,
                        os.path.abspath(
                            os.path.expanduser(str(_item.get("path") or ""))
                        ),
                        int(_item.get("card_start") or 1),
                        int(_item.get("total_lines") or 1),
                        bool(_item.get("span_exact", True)),
                    ]
                if rec[0] == self._READ:
                    # 本轮读过正文了，这一屏不再给它位置。
                    _skipped += 1
                    continue
                rec[0] += 1
                if self._is_hit_point(rec[0]):
                    # 同一请求里两批都踩到阶梯点 → 只渲染一次，取最后一次的落点。
                    _emit[_key] = (_q, _item)

        if not _emit:
            # 全是静默档：只推进计数，这次请求一个字都不贴。
            self._commit_ladder(_tk, _hits)
            if _skipped:
                logger.info(
                    "[KBAutoInject] llm_request READ-SKIP skip=%d sid=%s",
                    _skipped, _sid[:26],
                )
            return None

        _items = list(_emit.values())
        _items.sort(key=lambda pair: self._prompt_rank(request, pair[0]))
        _block = self._render_menu([m for _, m in _items])
        if not _append_to_last_message(request, _block):
            # 没拼进去就放回槽，下次还有机会 —— 注入失败不等于这批数据作废。
            with self._pending_lock:
                self._pending.setdefault(_sid, []).extend(_take)
            return None
        self._commit_ladder(_tk, _hits)
        logger.info(
            "[KBAutoInject] llm_request INJECT batches=%d emit=%d read_skip=%d chars=%d sid=%s",
            len(_take), len(_items), _skipped, len(_block), _sid[:26],
        )
        return {
            "request": request,
            "name": "hermes-kb-auto-inject",
            "reason": "kb titles tail inject (ladder)",
        }

    def _mark_read(self, tool_name: str, args: Any = None, **kwargs: Any) -> None:
        """读取发生的那一刻就记账：这张卡本轮读过了。

        不扫历史、不问模型 —— `transform_tool_result` 是工具返回的必经之路，
        在这里记的是既成事实。`_ladder` 里每条记录自带这张卡的行区间
        `[命中次数, 路径, 起行, 行数, 区间是否精确]`，所以判定是纯内存 O(本轮卡数)。

        `turn_key` 由钩子自带的 `session_id` + `turn_id` 拼，与
        `handle_llm_request` 的 `_turn_key` 同格式。**缺 turn_id 时直接返回**：
        勾子没有 messages，算不出 content-hash 兜底键，硬拼出来的 key 两边对不上，
        记了也是白记 —— 那种情况下已读跳过不生效，但绝不误标。
        """
        if tool_name != "read_file" or not isinstance(args, dict):
            return
        if str(kwargs.get("status") or "ok").strip().lower() != "ok":
            return
        sid = str(kwargs.get("session_id") or "")
        tid = str(kwargs.get("turn_id") or "")
        if not sid or not tid:
            logger.debug("[KBAutoInject] read-mark skip: no turn_id (%s)", tool_name)
            return
        # 键必须由 `_turn_key` 生成，不要在这里手拼 `f"{sid}:{tid}"`：
        # 手拼等于把「两边格式一致」变成隐式契约 —— `_turn_key` 哪天改格式，
        # 这里不会跟着改，已读跳过会静默失效且没有任何日志报错。
        tk = self._turn_key({"session_id": sid, "turn_id": tid})
        raw = str(args.get("path") or "").strip()
        if not raw:
            return
        try:
            start = args.get("offset")
            start = int(start) if start not in (None, "") else 1
            size = args.get("limit")
            size = int(size) if size not in (None, "") else 2000
        except (TypeError, ValueError):
            start, size = 1, 2000
        if start < 1 or size < 1:
            return
        path = os.path.abspath(os.path.expanduser(raw))
        lo, hi = start, start + size - 1

        marked = 0
        with self._turn_lock:
            hits = self._ladder.get(tk)
            if not hits:
                return
            for rec in hits.values():
                # rec = [命中次数, 路径, 起行, 行数, 区间是否精确]
                if rec[0] == self._READ:
                    continue
                if not rec[4] or rec[1] != path:
                    continue
                card_lo = rec[2]
                card_hi = card_lo + max(1, rec[3]) - 1
                if lo <= card_hi and hi >= card_lo:
                    rec[0] = self._READ
                    marked += 1
        if marked:
            # 低频（只有真读卡才触发），而且这是「钩子这条链活着」的唯一线上证据。
            logger.info(
                "[KBAutoInject] read-mark %d card(s) %s", marked, raw,
            )

    def _is_hit_point(self, n: int) -> bool:
        """本轮第 *n* 次命中该不该投递。

        `_HIT_GAPS` 是**间隔**：1、3、10、30、100、300…，累加成投递点
        1、4、14、44、144、444…。走完全部间隔后按最后一个间隔重复，
        所以再长的 turn 也不会把某张卡彻底压死。
        """
        prev = 0
        for gap in self._HIT_GAPS:
            prev += gap
            if n == prev:
                return True
        return n > prev and (n - prev) % self._HIT_GAPS[-1] == 0

    def _commit_ladder(self, turn_key: str, hits: Dict[str, list]) -> None:
        """写回本轮记录（只在贴成功之后调）。换 turn 是新 key，旧表交给 LRU 裁掉。

        `_READ` 是终态：本次请求的快照可能比钩子晚一拍，合并时不让它把「已读过」
        覆盖回「没读过」—— 否则同一张卡会被重复贴一遍。
        """
        with self._turn_lock:
            cur = self._ladder.get(turn_key)
            if cur is None:
                self._ladder[turn_key] = hits
            else:
                for _k, _rec in hits.items():
                    if (cur.get(_k) or [0])[0] == self._READ:
                        continue
                    cur[_k] = _rec
            self._ladder.move_to_end(turn_key)
            while len(self._ladder) > self._max_turn_states:
                self._ladder.popitem(last=False)

    def handle_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:

        # 取证残留已降级（2026-10-25 拆分）：这里原本每轮解析 registry / tool_search
        # 判据、留一行 INFO 记「这轮模型看得见哪些工具」。那是插件「静默变哑」取证期的
        # 产物，事故查完就该收 —— 正常运行一行不跑，DEBUG 打开才探。
        if logger.isEnabledFor(logging.DEBUG):
            _names = getattr(sys.modules.get("model_tools"), "_last_resolved_tool_names", None) or []
            logger.debug(
                "[KBAutoInject] 工具表: n=%d search=%s session=%s",
                len(_names), "search_knowledge" in _names,
                str(kwargs.get("session_id"))[:26],
            )
        turn_key = self._turn_key(kwargs)
        _sid = str(kwargs.get("session_id") or "default")
        context_parts: List[str] = []
        protocol = self._protocol_context(turn_key)
        if protocol:
            context_parts.append(protocol)
        # **这里只提交，不等**。检索跑完由 `_on_job_done` 塞进待投递槽，
        # 下一次请求由 `handle_llm_request` 贴进副本（上一次请求的请求体早已发出去，
        # 压着它等只是「同步回答」，owner 不要那个）。
        # 用户原话与工具里的 kb_query 走**同一个提交口**、同一个池。
        _uq = self._user_query(kwargs)
        if _uq:
            self.kb_queue(_uq, _sid)
        else:
            logger.info(
                "[KBAutoInject] user-query skipped（原话太短或全是填充词）raw=%s",
                _short(str(kwargs.get("user_message") or ""), 40),
            )
        return {"context": "\n\n".join(context_parts)} if context_parts else None




def _append_to_last_message(request: Dict[str, Any], block: str) -> bool:
    """把 *block* 拼到请求里最后一条消息的尾部。改的是**请求副本**，返回是否改成功。

    两条 wire 都要认：chat 用 ``messages``，Responses 用 ``input``。
    最后一条的 content 三种形态都处理 —— 字符串拼在后面、块列表追加一个 text 块、
    None 直接赋值。Anthropic 的 ``tool_result`` 是 user turn 里的块：**往该 turn
    顶层追加兄弟 text 块**，绝不写进 ``tool_result.content`` 里（写进去模型看不见）。
    """
    for key in ("messages", "input"):
        msgs = request.get(key)
        if not isinstance(msgs, list) or not msgs:
            continue
        last = msgs[-1]
        if isinstance(last, str):
            msgs[-1] = last + "\n\n" + block
            return True
        if not isinstance(last, dict):
            continue
        content = last.get("content")
        if content is None:
            last["content"] = block
        elif isinstance(content, str):
            last["content"] = content + "\n\n" + block
        elif isinstance(content, list):
            _btype = "input_text" if key == "input" else "text"
            content.append({"type": _btype, "text": block})
        else:
            return False
        return True
    return False


plugin_instance = KBAutoInjectPlugin()
