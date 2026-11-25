"""`KBAutoInjectPlugin`：清单池、检索、注入钩子的主体。

从原 1502 行单文件拆出的最大一块。这里只有**一个类**，职责按方法切好：
`_locate` / `_chain_spans` 是「在磁盘上把块定位到行」，`_build_menu` / `_render_menu`
是「印清单」，`_search_validated` / `_inline_menu` 是「检索 + 过闸」，
`handle_pre_llm_call` 是唯一注入点。常量与工具函数在 `.plugin_common`，
schema/handler 在 `.registration`。
"""

import hashlib
import logging
import os
import re
import sqlite3
import sys
import threading
from collections import OrderedDict
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


class KBAutoInjectPlugin:
    """Execution-state-driven knowledge base retriever and API-bound injector."""

    # 单次请求最多合并几批清单（一个检索任务 = 一批）。
    # 只限**批**，不限批内的卡：阈值仍是唯一质量闸（injection-gate-rules §7.5）。
    # 为什么是 2：早批对应的工具调用已经跑完，下文生成只缺最近动作的坐标；
    # 实测一轮峰值 3 批 / 8.5k 字符，2 批足够覆盖「用户原话 + 当前动作」。
    # **超出的是排队，不是丢弃** —— 剩下的留在槽里等下一个请求，不设字符预算。
    _MAX_MERGED_MENUS = 2
    # 待投递槽的**容量硬上限**（槽里最多压几批，不含本次投出去的）。
    # 只有一个作用：防无界积压。每轮投 2 批、实测新进 1.47 批/轮，
    # 常态槽深度 ≤ 2，摸不到这条线；真摸到了就是连续多轮进 3 批以上，
    # 那时旧批已经和当前动作脱节，丢掉比拖着更有价值。
    # **不看积压深度改投递顺序** —— 永远只投提示词里最新的 2 批。
    _MAX_PENDING_SLOTS = 6

    def __init__(self):
        # 类型注解用 Any：KBEngine 已改为惰性导入，运行时不再求值该符号
        self._engine: Optional[Any] = None
        self._engine_lock = threading.Lock()
        self._turn_lock = threading.RLock()
        # 协议段「每 turn 只发一次」的记账 —— 本插件唯一保留的 turn 状态。
        self._turns: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._max_turn_states = 128
        # ---------------- 全部状态：一次待投递的清单 ----------------
        # sid -> [(提交序号, 检索词, 清单文本), ...]。检索跑完由后台线程
        # `_on_job_done` 追加，下一次模型请求前由 `handle_llm_request` 取走。
        # 带检索词是为了出口能回查它在**当前提示词**里的位置 —— 服务端排新的
        # 依据是「提示词里最靠后」，既不认完成时间，也不认提交序号（并行段乱序）。
        # 不记「印过哪张卡」、不扫历史比对、不按词查重 —— 去重整套删掉了（owner 2026-11-24 定案）。
        self._pending: Dict[str, List[Tuple[int, str, str]]] = {}
        self._pending_lock = threading.Lock()
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

    # 「这张卡一共多少字」的进程内缓存。一屏 6 张卡 = 6 次聚合查询（实测 1 ms），
    # 缓存只是防长会话里反复问同一张；热重载会清空，无所谓。
    _size_cache: Dict[str, Tuple[int, int]] = {}
    # 整卡行数 / 命中块行号：read_file 的 offset/limit 是按行的，这两个缓存让
    # 一屏清单只读一次文件（同一个 file_path 的多块共用）。
    _disk_cache: Dict[str, Tuple[str, int]] = {}
    # 文件的标题骨架（标题行 + 每节起止行），按文件缓存。所有行号推导都查它。
    _outline_cache: Dict[str, Dict[str, Any]] = {}
    # (文件, 链) → 行范围。画树时同一节点会被问很多次，不缓存就是重复全扫。
    _span_cache: Dict[Tuple[str, Tuple[str, ...]], Tuple[Tuple[int, int, int], ...]] = {}

    def _card_size(self, file_path: str) -> Tuple[int, int]:
        """这张卡在库里一共多少字、切成几块。

        是**入库字数**（块之间有重叠），比磁盘原文略大；但库是自足的，文件被移走也不影响。
        查不到就回 (0, 0)，菜单那行不印 —— 宁可少一行，不印「0 字」误导。
        """
        if not file_path:
            return (0, 0)
        hit = self._size_cache.get(file_path)
        if hit:
            return hit
        val: Tuple[int, int] = (0, 0)
        try:
            db_dir = str(getattr(self.engine, "db_dir", "") or "")
            db = os.path.join(db_dir, "kb.sqlite3")
            if db_dir and os.path.exists(db):
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    row = con.execute(
                        "SELECT SUM(LENGTH(content)), COUNT(*) FROM chunks WHERE file_path=?",
                        (file_path,),
                    ).fetchone()
                    val = (int(row[0] or 0), int(row[1] or 0))
                finally:
                    con.close()
        except Exception:
            val = (0, 0)
        self._size_cache[file_path] = val
        return val

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
        # 每个标题的节结束行：往下第一个级别不高于它的标题；没有就是文件尾。
        end: Dict[int, int] = {}
        for k, (i, lvl, _t) in enumerate(heads):
            stop = len(lines)
            for j, lvl2, _t2 in heads[k + 1:]:
                if lvl2 <= lvl:
                    stop = j
                    break
            end[i] = stop
        out = {"lines": len(lines), "heads": heads, "end": end,
               "level": {i: lvl for i, lvl, _t in heads}}
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
            total, blocks = self._card_size(fp)
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
                _sz, _cs, _cl = _sp[0]
                if _cs:
                    total = _sz or total
            if not _cs:
                _cs, _cl = 1, self._file_lines(fp)
            menu.append({
                "key": key,
                "cid": str(chunk.get("chunk_id") or ""),
                "title": self._card_title(chunk),
                # 这张卡的全部命中落点（含首块）：[{chain, a, n}, ...]。
                # chain 是完整标题链（不含 H1），渲染层按它合并成树。
                "hits": [hit] if hit else [],
                # 体量：整卡字数 / 块数 / 整卡行数 —— 决定「一次读完」还是「只读命中段」
                "total": total,
                "blocks": blocks,
                # total_lines 是**这张卡**的行数（一文件多卡时不含别的卡），
                # card_start 是它在文件里的起始行，渲染层拼成「行 a~b」。
                "total_lines": _cl,
                "card_start": _cs,
                # 绝对路径：read_file 的唯一入口
                "path": fp,
            })
        return menu

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
        lines = self._disk(file_path)[0].split("\n") if out["lines"] else []
        heads = out["heads"]
        spans: List[Tuple[int, int, int]] = []
        lo, hi = 0, len(lines)

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
            exact, loose = -1, -1
            for idx, (i, _lvl, txt) in enumerate(heads):
                if i < lo_ or i >= hi_:
                    continue
                if txt == want_:
                    return idx
                if loose < 0 and (txt.startswith(want_) or want_.startswith(txt)):
                    loose = idx
            return exact if exact >= 0 else loose

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
                lo, hi = 0, len(lines)
                continue
            i, _lvl, _txt = heads[hit]
            end = out["end"][i]        # 骨架里已算好：下一个同级或更高级的标题
            spans.append((sum(len(x) + 1 for x in lines[i:end]), i + 1, end - i))
            lo, hi = i + 1, end
        self._span_cache[_ck] = tuple(spans)
        return spans

    def _render_menu(self, menu: List[Dict[str, Any]], query: str = "") -> str:
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
                        sz, a, n = _span(ksegs)
                        tag = f" · {sz:,} 字" if sz else ""
                        if n:
                            tag += f" · 行 {a}~{a + n - 1}（{n} 行）"
                        out.append(f"{_mark(prefix, last)}{k['seg']}{tag}")
                        walk(k, child_prefix, ksegs)
                    elif kind == "leaf":
                        a, n = val
                        out.append(f"{_mark(prefix, last)}"
                                   f"命中段 行 {a}~{a + n - 1}（{n} 行）")
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
            total = int(m.get("total") or 0)
            tl = int(m.get("total_lines") or 0)
            cs = int(m.get("card_start") or 1) or 1
            vol = []
            if total:
                vol.append(f"{total:,} 字")
            if tl:
                # 和每一级同款：整卡就是「行 a~b（n 行）」，不搞特殊措辞。
                # 起点未必是 1 —— 一个文件里可以有好几张卡，第二张从自己的 H1 起算。
                # 只印数，不加「别整读」这类命令，怎么读是模型自己的判断。
                vol.append(f"行 {cs}~{cs + tl - 1}（{tl} 行）")
            if vol:
                out += " · " + " · ".join(vol)
            _hits = m.get("hits") or []
            if len(_hits) > 1:
                # 这张卡命中多处时点一句，模型一眼知道「不止一处对题」，不会读完
                # 第一段就收工。只命中一处不必说 —— 那是默认形态，说了是噪音。
                out += f" · 命中 {len(_hits)} 处"
            out += _hit_tree(m)
            # 路径行由 `_hit_tree` 统一收口（它要参与树的孩子排序才能只有一个 `└`）。
            # 没有命中落点时（定位全失败）树是空的，这里兜底补一行，否则这卡没有入口。
            if m.get("path") and not (m.get("hits") or []):
                out += f"\n   └ 路径：{m['path']}"
            return out

        lines = "\n".join(_row(m) for m in menu)
        # 抬头报**卡数**：每行一张卡（老卡有新落点时重印的仍是同一张卡，不会重复计数）。
        # 检索词印在自己这一屏的抬头里：多词合并时每屏各带各的词，模型能对上
        # 「这批卡是按什么捞的」。不另传一份词表 —— 那会多出一个判据、多一种形态。
        _q = (query or "").strip()
        head = f"知识库检索到 {len({m['key'] for m in menu})} 个相关知识块"
        if _q:
            head += f"（检索词：{_q if len(_q) <= 40 else _q[:40] + '…'}）"
        head += "。"
        return (
            "<knowledge-base-titles>\n"
            f"{head}\n"
            "读法：照抄行号 —— `read_file(路径, offset=起行, limit=行数)`。"
            "**卡标题下逐级缩进就是命中处的小节路径，每行末尾的「行 a~b」是该级的完整范围，想读整级就抄它**；"
            "最末一级下面若另起一行标「命中段」，说明命中只是那一级的一部分。\n"
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
        return self._render_menu(_menu, _q)

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
        _menu = self._menu_from_chunks(query, chunks)
        if not _menu:
            return
        with self._pending_lock:
            self._pending.setdefault(_sid, []).append((seq, query, _menu))
        logger.info(
            "[KBAutoInject] pending MENU chars=%d q=%s sid=%s",
            len(_menu), _short(query, 60), _sid[:26],
        )

    def handle_llm_request(
        self, request: Any = None, session_id: str = "", **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        """每次模型调用前，把攒下的清单贴到最后一条消息尾部。**不进历史**。

        核心（`hermes_cli/middleware.py: apply_llm_request_middleware`）把请求
        `_safe_copy` 之后交进来，改的是副本，替换 `api_kwargs` 只对这一次调用
        生效，`state.db` 里一个字都不落 —— 这正是「不进上下文历史」。
        契约：改了就返回 `{"request": <dict>}`，没改必须返回 `None`。
        """
        if not isinstance(request, dict):
            return None
        _sid = session_id or "default"
        with self._pending_lock:
            _slot = self._pending.get(_sid)
            if not _slot:
                return None
            # 按「检索词在提示词里的位置」排，位置相同再按提交序号兜底。
            # 不按完成时间、也不单独按提交序号：并行段里两者都会错位。
            _slot.sort(key=lambda item: (self._prompt_rank(request, item[1]), item[0]))
            # 容量闸：只在超过硬上限时从**最旧**一端删，防无界积压。
            # 这里不参与「投谁」的决策 —— 投递顺序永远只看新鲜度。
            _dropped = 0
            if len(_slot) > self._MAX_PENDING_SLOTS:
                _dropped = len(_slot) - self._MAX_PENDING_SLOTS
                del _slot[:_dropped]
            # 永远投提示词里最新的 N 批，与积压深度无关。
            _take = _slot[-self._MAX_MERGED_MENUS:]
            del _slot[-self._MAX_MERGED_MENUS:]
            _queued = len(_slot)
            if not _slot:
                self._pending.pop(_sid, None)
        _block = "\n\n".join(text for _, _, text in _take)
        if not _append_to_last_message(request, _block):
            # 没拼进去就放回槽，下次还有机会 —— 注入失败不等于这批数据作废。
            with self._pending_lock:
                self._pending.setdefault(_sid, []).extend(_take)
            return None
        logger.info(
            "[KBAutoInject] llm_request INJECT batches=%d chars=%d queued=%d dropped=%d sid=%s",
            len(_take), len(_block), _queued, _dropped, _sid[:26],
        )
        return {
            "request": request,
            "name": "hermes-kb-auto-inject",
            "reason": "kb titles tail inject",
        }

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
