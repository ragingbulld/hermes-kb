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

from .plugin_common import (
    PROTOCOL_PROMPT,
    _content_text,
    _kb_query_is_usable,
    _short,
    _user_query_text,
    logger,
    scan_heads,
)


class KBAutoInjectPlugin:
    """Execution-state-driven knowledge base retriever and API-bound injector."""

    def __init__(self):
        # 类型注解用 Any：KBEngine 已改为惰性导入，运行时不再求值该符号
        self._engine: Optional[Any] = None
        self._engine_lock = threading.Lock()
        self._turn_lock = threading.RLock()
        # 协议段「每 turn 只发一次」的记账 —— 本插件唯一保留的 turn 状态。
        self._turns: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._max_turn_states = 128
        # ---------------- 全部状态：本轮印过的卡 + 已印落点 ----------------
        # card_key -> {"no": 序号, "hits": 已印过的落点集合}。不存正文：正文一律由
        # read_file 取，插件不再持有任何一块原文 —— 存了也只服务一个已删掉的展开工具。
        # 键一旦入册序号就定死（同一张卡永远同一个 #N），已发出去的号永不重排。
        # 落点按 (完整标题链, 起行, 行数) 记账 —— 去重的尺度是落点，不是整张卡。
        self._shown: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # 同一轮里同一个检索词只检索一次：模型一批里两个调用写同一个 kb_query，
        # 就会检索两遍、贴两份清单（实测 20261023_225015_ea5c533c）。
        self._served_query = ""
        # 没有「已注入」账本、也没有跨轮状态：池子每 turn 清空，
        # 上面三个字段就是全部。热重载清零也不影响（从空态自洽起步）。

    @property
    def engine(self) -> Any:
        if self._engine is None:
            with self._engine_lock:
                if self._engine is None:
                    # 局部导入：把 6~7s 的重库加载成本推迟到首次真实检索，
                    # 避免撞 Hermes 插件 10s 加载门禁导致钩子被 ignored。
                    from hermes_kb.engine import KBEngine

                    self._engine = KBEngine()
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

    # ============ 一步，全在当场：kb_query → 标题清单（带行号）============
    #
    # 每一次工具调用都带 kb_query → 清单当场贴回这条工具结果（序号只增不变）。
    # 清单里每行都写着 `read_file` 现成的路径 + 命中段行号，要正文模型自己去读。
    # 插件到此为止，不再持有原文、也没有第二个工具。

    @staticmethod
    def _card_title(chunk: Dict[str, Any]) -> str:
        """卡片标题 = ``heading_path`` 的第一段。

        不能用 ``chunk["title"]``：那一列是**最深层小节名**（实测全库 25.35% 的块
        ``title`` ≤8 字，是「症状」「避坑铁律」「根因」「LEGAL」这种东西），拿它当
        卡片标题等于没给。``heading_path`` 全库 0 条缺失，第一段就是卡片 H1。
        """
        hp = str(chunk.get("heading_path") or "").strip()
        h1 = re.split(r"\s+>\s+", hp)[0] if hp else ""
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
                str(chunk.get("heading_path") or "").split(" > ")[0]
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

    @classmethod
    def _card_chain(cls, chunk: Dict[str, Any]) -> List[str]:
        """菜单第二行：这张卡里命中的那一段，在标题树里的**完整路径**。

        只印最深一级看不出上下文（同一张卡里有好几个「修法与验收」），只印尾部两级也会
        砍掉中间层。实测"完整链"根本不贵：去掉 H1 之后全长**中位 44 字、P90 108 字**，
        超过 120 字的只有 6.7%，所以没有理由为省几十字牺牲可理解性 —— 全印。

        H1 不重复印（它已经在 `#N` 标题行上）。`(Part 1)` 是切分母出来的，剥掉，
        否则同一节看着像三节。通用小节名（「根本原因与核心设计」）在完整链里是结构
        信息，**保留**，不再像只印一级时那样过滤掉。

        **一个字都不省。** 早先设过 200 字上限、超了中间打 `…`，那是把「判断这段值不值得
        点」的关键层砍掉喂给模型 —— 省略掉的正好是它最需要看的那部分。真要控预算，
        就少印几张卡（抬头会写「另有 N 张没印」），不是把每张卡切一半。

        返回**段列表**，不自己拼字符串：渲染层要把它画成逐级缩进的树（owner：挤在
        一行看不出结构，`A > B > C` 得写成一行一层）。
        """
        parts = [p.strip() for p in str(chunk.get("heading_path") or "").split(" > ") if p.strip()]
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

    def _render_menu(self, menu: List[Dict[str, Any]]) -> str:
        """标题清单：序号 + 卡标题 + 命中小节 + **可直接喂给 `read_file` 的行号**。

        刻意不印 gate 分（全挤在 0.99xx，纯噪音）、不印 chunk_id（4 位前缀 36.42% 碰撞）。

        每行给的是 `read_file` 的现成参数，不是给人看的描述：
        每级都带自己的 `行 a~b`，读整级还是只读命中段，由调用方按任务定。

        改成实例方法（原 `@staticmethod`）：画树时要查磁盘标题行算每节的行范围，
        那需要 `_chain_spans` / `_outline`。原先只有 `_chain_spans` 被外部算好
        传进来，所以还能是静态的；现在树的形状由命中集合决定，行范围得在里面现算。

        序号不在这里发：`#N` 是**跨屏地址**，由 `_inline_menu` 记账时定死
        （同一张卡永远同一个号），渲染层只负责照 `m["no"]` 印。
        """
        _chain_spans = self._chain_spans

        def _hit_tree(m: Dict[str, Any], tail: bool = True) -> str:
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

            ``tail=False`` 省掉末尾的 `路径` 行：老卡的追加块，路径在上一条消息里
            已经给过，再抄一遍纯属重复。
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

            def walk(node: Dict[str, Any], prefix: str, segs: List[str],
                     tail: str = "") -> None:
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
                if tail:
                    # 路径行跟树的孩子同级，必须**参与** last 判定：早先它被硬编码成
                    # `└ 路径`，于是根层既有一个 `└` 收尾的分支、又有一个 `└ 路径`，
                    # 一棵树长出两个收口，垂线视觉上是断的。永远排最后。
                    items.append((10 ** 10, "tail", tail))
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

            _tail = str(m.get("path") or "")
            walk(root, "   ", [], _tail if tail else "")
            return ("\n" + "\n".join(out)) if out else ""

        def _row(i: int, m: Dict[str, Any]) -> str:
            # 序号由记账层（_inline_menu）给，同一张卡永远同一个号；直接拿未编号的
            # 菜单来渲染时（离线验证脚本）就地按顺序编，不留 `#0` 这种假地址。
            no = m.get("no") or i
            if m.get("extra"):
                # 老卡的新落点：卡标题那一行上一条消息里已经印过，不重印（省字），
                # 只补这一批还没给过的命中段；路径同上，也不再抄一遍。
                return (f"#{no} ↑ 追加 {len(m['hits'])} 处命中"
                        + _hit_tree(m, tail=False))
            # 大标题行 = 这张卡的身份 + 整卡体量。体量决定「整篇读还是只读一段」，
            # 是卡的属性，就该跟卡标题在一起。
            out = f"#{no} {m['title']}"
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

        lines = "\n".join(_row(i, m) for i, m in enumerate(menu, 1))
        # 抬头报**卡数**：追加块只是老卡的新落点，不是新卡，不该把数字算进去。
        head = f"知识库检索到 {len({m['key'] for m in menu})} 个相关知识块。"
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
                self.engine, query, candidate_limit=candidate_limit, top_n=top_k
            )
            # [留痕] 单行检索摘要：事后可直接回答「阈值卡掉多少张」
            logger.info(
                "[KBAutoInject] %s: cand=%d kept=%d | rerank<%.2f dropped=%d | q=%s",
                tag,
                gated.candidates,
                len(gated.kept),
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
    ) -> List[Dict[str, Any]]:
        """注入路径的检索入口。参数解析与闸门全在 `_search_validated`，这里只贴标签。

        （曾把 candidate_limit/top_k 的解析逻辑在本函数里抄了一份，与
          `_search_validated` 逐字重复 —— 两处一旦分叉就会静默漂移，已删。）
        """
        return self._search_validated(
            query, top_k=top_k, candidate_limit=candidate_limit, tag="retrieve summary"
        )

    def _inline_menu(
        self, query: str, top_k: Optional[int] = None, max_cards: Optional[int] = None
    ) -> str:
        """检索词 → 一屏**标题清单**（含 `read_file` 现成的行号）。两条入口共用本函数。

        入口一：dispatch 层的 kb_query（写在任意工具调用里）。
        入口二：search_knowledge 工具自己的 query。

        ``top_k`` 是喂给 reranker 的候选数；``max_cards`` 只在**调用方自己点名**时才设
        （`search_knowledge` 的 `limit`），注入路径一律不设 —— 过闸的全印。

        只认一道闸：`min_rerank_score`。过了就是合格块，合格块必须出现在清单里。
        早先这里还有两道我自加的闸（`MENU_MAX_CARDS=6` 卡数、`MENU_BUDGET=4000` 字数），
        实测把 19 张过闸卡砍到 6 张，等于让 owner 定的阈值形同虚设 —— 阈值说它合格、
        另一段代码又把它撤掉，两道判据打架。已整体删除。

        去重的尺度是**落点**，不是整张卡。卡标题只印一次（省字），但同一张卡后来
        又命中了别的段落，那些段落必须补印 —— 早先按整卡拦，实测 5 次工具调用里
        53 处（占 58%）首次命中的落点被连坐吞掉，模型连那些段落存在都不知道。

        序号只增不变：同一张卡永远同一个 `#N`，新卡往 `#N+1` 追加，已给出的号永不
        重排（曾每批重排 → 模型照记忆点序号指错，见 20261023_225015_ea5c533c）。
        """
        _q = (query or "").strip()
        if not _kb_query_is_usable(_q):
            return ""
        if _q == self._served_query:
            # 同一个词这一轮已经检索过，清单也已经贴在前面那条结果里了 ——
            # 再检索一遍只是白烧 rerank，再贴一遍只是把同样的字打进上下文。
            logger.info("[KBAutoInject] inline MENU DUP skipped q=%s", _short(_q, 60))
            return ""
        self._served_query = _q
        try:
            # top_k 为 None 时 _search_validated 会自己读 retrieval.rerank_top_n ——
            # 这里不再抄一份 `doing_top_k`（那是旧 doing 协议时代的键名，
            # 跟 rerank_top_n 实盘同值 25，两份真源迟早分叉）。
            _chunks = self.retrieve_chunk(_q, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[KBAutoInject] inline retrieve failed: %s", exc)
            _chunks = []
        _screen = self._build_menu(_chunks) if _chunks else []
        # 逐卡裁到只留还没印过的落点：新卡全收，老卡只收增量。
        _batch: List[Dict[str, Any]] = []
        for it in _screen:
            _rec = self._shown.get(it["key"])
            _old = _rec["hits"] if _rec else set()
            _fresh = [h for h in (it.get("hits") or [])
                      if (tuple(h.get("chain") or []), int(h.get("a") or 0),
                          int(h.get("n") or 0)) not in _old]
            if _rec and not _fresh:
                continue          # 老卡、零新增落点 —— 真重复，跳过
            if max_cards is not None and not _rec and len(
                [x for x in _batch if not x.get("extra")]
            ) >= max_cards:
                continue          # 调用方点名的卡数上限，只约束**新卡**
            _entry = dict(it, hits=_fresh, extra=bool(_rec))
            if _rec:
                _entry["no"] = _rec["no"]
            _batch.append(_entry)
        if not _batch:
            logger.info(
                "[KBAutoInject] inline MENU no-new (已印过 %d 张) q=%s",
                len(self._shown),
                _short(_q, 70),
            )
            return ""
        _new_cards = 0
        for it in _batch:
            _rec = self._shown.get(it["key"])
            if _rec is None:
                _new_cards += 1
                it["no"] = len(self._shown) + 1
                _rec = self._shown[it["key"]] = {"no": it["no"], "hits": set()}
            _rec["hits"].update(
                (tuple(h.get("chain") or []), int(h.get("a") or 0), int(h.get("n") or 0))
                for h in it["hits"]
            )
        logger.info(
            "[KBAutoInject] inline MENU EMIT(+%d 卡 / 累计 %d 卡 / %d 段) q=%s",
            _new_cards,
            len(self._shown),
            sum(len(x["hits"]) for x in _batch),
            _short(_q, 70),
        )
        return self._render_menu(_batch)

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
        # 新一轮：清空「本轮印过的卡」。清单由 dispatch 层当场填，
        # 这里不许再按「上一轮的声明」重放任何东西。
        self._shown = {}
        self._served_query = ""
        context_parts: List[str] = []
        protocol = self._protocol_context(turn_key)
        if protocol:
            context_parts.append(protocol)
        # 第二步：用户原话也参与检索。清单与 kb_query 路径共用同一个池、同一套 #N，
        # 所以这里印过的卡，后面工具调用里的 kb_query 不会再印一遍。
        # 代价：每轮开头一次同步检索（实测 2.5~3.3s）+ 一屏最多 3 张清单。
        _uq = self._user_query(kwargs)
        if _uq:
            try:
                _user_menu = self._inline_menu(_uq)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[KBAutoInject] user-query menu failed (%s): %s",
                    type(exc).__name__,
                    exc,
                )
                _user_menu = ""
            if _user_menu:
                context_parts.append(_user_menu)
        else:
            logger.info(
                "[KBAutoInject] user-query skipped（原话太短或全是填充词）raw=%s",
                _short(str(kwargs.get("user_message") or ""), 40),
            )
        return {"context": "\n\n".join(context_parts)} if context_parts else None


# 单实例：guard / registration 都要引用它，所以放在类所在模块的底部（谁 import
# 谁拿到同一个对象，不会出现两个池）。
plugin_instance = KBAutoInjectPlugin()
