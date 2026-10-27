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
    MENU_BUDGET,
    MENU_MAX_CARDS,
    PROTOCOL_PROMPT,
    _HDR_RE,
    _USER_QUERY_MAX_CARDS,
    _content_text,
    _kb_query_is_usable,
    _short,
    _user_query_text,
    logger,
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
        # ---------------- 全部状态：本轮印过的卡 + 去重键 ----------------
        # 本轮清单里**已经印过**的卡（card_key）。不存正文：正文一律由 read_file 取，
        # 插件不再持有任何一块原文 —— 存了也只服务一个已删掉的展开工具。
        # 只增不改：序号 = 下标 + 1，所以已发出去的 #N 永不重排。
        self._shown: List[Tuple[str, str]] = []
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

    def _locate(
        self, file_path: str, content: str
    ) -> Optional[Tuple[int, int, int, int]]:
        """``(块起行, 块行数, 节起行, 节行数)`` —— 都是 `read_file` 的现成参数。

        **块起行**：拿块正文前 80 字去原文 `find`。库里存的就是原文切片，实测 10/10 命中
        且起行连续（1/32/46/78/103/126/144/152/170/175）；退一步用该块第一行前 40 字。
        两种都不中就直接返回 None —— 宁可少给，也不给会让模型读错地方的假行号。

        **节起行**：块是从节中间切下去的（一个 1,500 字的块常常只是某节的第 1 片），
        所以从块头往上找最近的 `#…` 标题行，再往下找到第一个「级别不高于它」的标题，
        中间就是这个标题管辖的整节。全库 29% 的块 heading_path 有 3 层以上，
        「命中段 18 行」和「该节 96 行」是两个不同的东西，都告诉模型它才有得选。
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
        lines = disk.split("\n")
        sec_start, sec_lines = start, seg_lines
        for i in range(min(start - 1, len(lines) - 1), -1, -1):
            m = _HDR_RE.match(lines[i])
            if not m:
                continue
            lvl = len(m.group(1))
            end = len(lines)
            for j in range(i + 1, len(lines)):
                m2 = _HDR_RE.match(lines[j])
                if m2 and len(m2.group(1)) <= lvl:
                    end = j
                    break
            sec_start, sec_lines = i + 1, end - i
            break
        # 块会跨节：chunker 是按字数切的，不看标题，实测有 31 行的块落在 7 行的
        # 「## 避坑铁律」下面，直接印「该节 591~597」会让模型**读少一半内容**。
        # 这种情况只给命中段 —— 宁可少给一个语义单元，不给一个会截断的假坐标。
        if sec_start and (start + seg_lines - 1) > (sec_start + sec_lines - 1):
            sec_start, sec_lines = 0, 0
        return (start, seg_lines, sec_start, sec_lines)

    def _build_menu(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过闸门的块 → 一屏标题菜单，每行自带 `read_file` 直接可用的行号。

        跳过条件只有一个：**这张卡本轮清单里已经印过了**（判据是卡，所以同一张卡的
        另一块也一起挡掉）。正文要不要读、读哪几行，由模型拿行号自己定。
        """
        menu: List[Dict[str, Any]] = []
        seen = set()
        for chunk in chunks:
            key = self._card_key(chunk)
            if key in seen:
                continue
            seen.add(key)
            fp = str(chunk.get("file_path") or "")
            content = str(chunk.get("content") or "")
            total, blocks = self._card_size(fp)
            geo = self._locate(fp, content)
            _chain = self._card_chain(chunk)
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
                "chain": _chain,
                # 每段自己的字符数，与 "total" 同一口径（都是 len 字符数）
                "chain_spans": self._chain_spans(fp, _chain),
                # 体量：整卡字数 / 块数 / 整卡行数 —— 决定「一次读完」还是「只读命中段」
                "total": total,
                "blocks": blocks,
                # total_lines 是**这张卡**的行数（一文件多卡时不含别的卡），
                # card_start 是它在文件里的起始行，渲染层拼成「行 a~b」。
                "total_lines": _cl,
                "card_start": _cs,
                # 命中段在原文里的 (起行, 行数)。定位不到就是 None，那行不印。
                "line_start": geo[0] if geo else 0,
                "seg_lines": geo[1] if geo else 0,
                # 命中块所属的整节（可能比块大得多：块 1,500 字上限，节想多长就多长）
                "sec_start": geo[2] if geo else 0,
                "sec_lines": geo[3] if geo else 0,
                # 绝对路径：read_file 的唯一入口
                "path": fp,
            })
        return menu

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

    def _chain_spans(self, file_path: str, chain: List[str]) -> List[Tuple[int, int, int]]:
        """命中链每一级在原文里的 ``(字符数, 起行, 行数)``。

        **每一级都给完整行范围**，不只是最末一级。理由：模型完全可能想读整级
        （「Telemetry 这节我全要」），只在最末一级给行号等于逼它为了读父级自己
        去推算 —— 而父级的起止它没有。

        字符数口径与「整卡 N 字」完全一致（都是 `len()`）。

        模型看到「Telemetry · 1,880 字」和「整卡 48,258 字」是同一把尺子，才能判断
        「这一级就够了」还是「得往下钻两层」。只给整卡总数是失效口径：整卡 48k 字、
        命中那节 96 字，两个数差 500 倍，不给节大小等于让模型猜。

        算法：扫磁盘原文的标题行建树，按链的顺序**逐级收窄搜索范围**（第 i 段只在第
        i-1 段的子树里找），所以同名小节不会串到别的分支去。任一级比对上就跳过那一段
        （返回 0，渲染层就不标），绝不给猜出来的数。
        """
        if not file_path or not chain:
            return []
        disk = self._disk(file_path)[0]
        if not disk:
            return []
        lines = disk.split("\n")
        heads = []
        for i, ln in enumerate(lines):
            m = _HDR_RE.match(ln)
            if m:
                heads.append((i, len(m.group(1)), self._clean_heading(m.group(2))))
        spans: List[Tuple[int, int, int]] = []
        lo, hi = 0, len(lines)
        for want in chain:
            want = self._clean_heading(want)

            def _find(lo_: int, hi_: int) -> int:
                for idx, (i, _lvl, txt) in enumerate(heads):
                    if i < lo_ or i >= hi_:
                        continue
                    if txt == want or txt.startswith(want) or want.startswith(txt):
                        return idx
                return -1

            hit = _find(lo, hi)
            if hit < 0:
                # 子树内找不到就退回全文再找一次。收窄是为了消解同名歧义（同一张卡里三个
                # 「修法与验收」），但 heading_path 是入库时拍的快照：磁盘被改过、或上一级
                # 匹配到了别处的同名标题，子树就会整个落空。实测这类漏标占 2.6%，全在
                # 「父级匹配偏移」这一种成因上 —— 退一步能救回来，且不会把同名节串错，
                # 因为同名歧义早在父级收窄那一步就消掉了。
                hit = _find(0, len(lines))
            if hit < 0:
                spans.append((0, 0, 0))
                continue
            i, lvl, _txt = heads[hit]
            end = len(lines)
            for j in range(i + 1, len(lines)):
                m2 = _HDR_RE.match(lines[j])
                if m2 and len(m2.group(1)) <= lvl:
                    end = j
                    break
            spans.append((sum(len(x) + 1 for x in lines[i:end]), i + 1, end - i))
            lo, hi = i + 1, end
        return spans

    @staticmethod
    def _render_menu(
        menu: List[Dict[str, Any]], start: int = 1, more: int = 0
    ) -> str:
        """标题清单：序号 + 卡标题 + 命中小节 + **可直接喂给 `read_file` 的行号**。

        刻意不印 gate 分（全挤在 0.99xx，纯噪音）、不印 chunk_id（4 位前缀 36.42% 碰撞）。

        每行给的是 `read_file` 的现成参数，不是给人看的描述：
        每级都带自己的 `行 a~b`，读整级还是只读命中段，由调用方按任务定。
        """

        def _chain_lines(chain: List[str], spans: List[Tuple[int, int, int]], tail: str = "") -> str:
            """标题链画成树：一行一层，逐级缩进，每层尾巴标该节字数。

            挤成 `A > B > C` 一行时，模型要自己在长句里数层级边界；实测卡越长越数不清
            （「6. Repoint a public Minecraft address only af…」这种直接看不出它在第几层）。
            一行一层就不用数了，代价是每层多一个换行。

            每层的 `· N 字` 和整卡行用的是同一把尺子（都是 len 字符数）—— 模型拿两个数
            比一下就知道该读到哪一层，不用先点进去再后悔。
            """
            if not chain:
                return ""

            def _tag(i: int) -> str:
                """`· N 字 · 行 a~b（n 行）` —— 和卡标题行同一个写法、同一把尺子。

                字数回答「值不值得读」，行范围回答「怎么读」；两个都得给。只给字数，
                想读整级的人没有 offset/limit；只给行号，判断不了这级有多大。
                """
                if i >= len(spans):
                    return ""
                sz, a, n = spans[i]
                out = f" · {sz:,} 字" if sz else ""
                if n:
                    out += f" · 行 {a}~{a + n - 1}（{n} 行）"
                return out

            # 不印「命中链」三个字：每张卡下面缩进的第一层就是命中处的标题路径，
            # 没有第二种可能，标签是纯噪音。
            #
            # 连线必须真的连上：卡标题下面是**两个**孩子（命中链、路径），所以首段用
            # `├`，垂线 `│` 一路贯到最末一级，路径行用 `└` 收口 ——
            #     ├ Functionality · 21,850 字
            #     │ └ Telemetry · 13,291 字
            #     │   └ Configuring Sensors · 8,205 字
            #     └ 路径：…
            # 早先每段都写 `└ ` 且不带垂线，视觉上是一串互不相干的悬浮行，
            # 路径那行跟上面那串断开了。
            out = []
            for depth, seg in enumerate(chain):
                if depth == 0:
                    mark = "   ├ "
                else:
                    mark = "   │" + " " * (2 * depth - 1) + "└ "
                # 每行只放这一级自己的字数和范围；命中段另起一行挂在最末一级下面，
                # 免得同一行挤两个范围（「行 241~254」和「命中段 241~250」挨着看很像笔误）。
                out.append(f"{mark}{seg}{_tag(depth)}")
            if tail:
                depth = len(chain)
                out.append("   │" + " " * (2 * depth - 1) + "└ " + tail)
            return "\n" + "\n".join(out)

        def _seg_note(m: Dict[str, Any]) -> str:
            """最末一级额外补一笔「命中段」——只在它比整级窄时才需要。

            每一级的 `行 a~b` 已经给了整级范围，所以命中段**不必**再占一个字段；
            只有当命中落点比整级小（块只是这节的第一片）时，才值得告诉模型
            「你其实只想看这一小段」。窄了就标，重合就闭嘴。
            """
            spans = m.get("chain_spans") or []
            ls = int(m.get("line_start") or 0)
            sl = int(m.get("seg_lines") or 0)
            if not spans or not ls or not sl:
                return ""
            _sz, a, n = spans[-1]
            if n and a == ls and n == sl:
                return ""
            # 块会跨节（chunker 按字数切、不看标题），此时「命中段」可能比这一级还大。
            # 那种情况标出来是自相矛盾的（14 行的节里塞一个 36 行的段），而且会把
            # 模型引到下一节的内容上 —— 只标完全落在这一级内部的命中段。
            if n and not (a <= ls and ls + sl - 1 <= a + n - 1):
                return ""
            return f"命中段 行 {ls}~{ls + sl - 1}（{sl} 行）"

        def _row(i: int, m: Dict[str, Any]) -> str:
            # 大标题行 = 这张卡的身份 + 整卡体量。体量决定「整篇读还是只读一段」，
            # 是卡的属性，就该跟卡标题在一起。
            out = f"#{start + i - 1} {m['title']}"
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
            chain = m.get("chain") or []
            note = _seg_note(m)
            out += _chain_lines(chain, m.get("chain_spans") or [], note)
            if not chain:
                # 没有标题链（heading_path 不足两级）：直接给命中段和它所在的节。
                # 同样「一行一个范围」：节一行、命中段再缩一层。
                # 节**就是**这张卡本身时（一文件多卡的第二张卡必然如此：卡 = 一个 H1 节）
                # 不重复印 —— 卡标题行已经写着同一个范围了。早先按「ss==1」判，
                # 多卡文件里会多出一行没有名字的「行 34~74（41 行）」，看着像标题丢了。
                ls = int(m.get("line_start") or 0)
                sl = int(m.get("seg_lines") or 0)
                ss = int(m.get("sec_start") or 0)
                sn = int(m.get("sec_lines") or 0)
                whole = bool(ss and sn and tl and ss == cs and sn == tl)
                if ss and sn and not whole:
                    out += f"\n   ├ 行 {ss}~{ss + sn - 1}（{sn} 行）"
                    if ls and sl and (ss, sn) != (ls, sl):
                        out += f"\n   │ └ 命中段 行 {ls}~{ls + sl - 1}（{sl} 行）"
                elif ls and sl:
                    out += f"\n   ├ 命中段 行 {ls}~{ls + sl - 1}（{sl} 行）"
            if m.get("path"):
                out += f"\n   └ 路径：{m['path']}"
            return out

        lines = "\n".join(_row(i, m) for i, m in enumerate(menu, 1))
        found = len(menu) + more
        head = (
            f"知识库检索到 {found} 个相关知识块，这里列最相关的 {len(menu)} 个。"
            if more
            else f"知识库检索到 {found} 个相关知识块。"
        )
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
        """统一检索入口：engine.search + min_rerank_score 闸门（唯一真源）。

        注入路径（retrieve_chunk）与主动查库（search_knowledge）必须共用本函数。
        曾因两条路径各写一份过滤逻辑，显式工具不套闸门、把 rerank=0.00004 的
        噪音原样返回给模型。排序键与阈值只要分叉一次就会静默漂移。
        """
        if not query or len(query.strip()) < 3:
            return []
        try:
            retrieval_cfg = self.engine.config.get("retrieval", {})
            candidate_limit = max(
                1,
                int(
                    candidate_limit
                    if candidate_limit is not None
                    else retrieval_cfg.get("candidate_limit", 25)
                ),
            )
            if top_k is None:
                top_k = int(retrieval_cfg.get("rerank_top_n", 25))
            top_k = max(1, int(top_k))
            # 只保留分数阈值：min_rerank_score。RRF 分数阈值已按用户要求移除，
            # 语义是「没有重排分数的切片一律不进」——reranker 超时/失败产生的
            # rrf_fallback 条目没有 rerank_score，会被当作无效直接丢弃。
            min_score = float(retrieval_cfg.get("min_rerank_score", 0.85))
            results = self.engine.search(
                query,
                limit=candidate_limit,
                rerank_top_n=top_k,
            )
            valid_results = []
            _rej_rerank = 0
            for result in results:
                score = result.get("rerank_score")
                if score is None:
                    _rej_rerank += 1
                    continue
                try:
                    if float(score) >= min_score:
                        valid_results.append(result)
                    else:
                        _rej_rerank += 1
                except (TypeError, ValueError):
                    _rej_rerank += 1
                    continue

            # 断崖截断已按用户要求移除：不再按 top1 分差砍掉后续切片。

            # [留痕] 单行检索摘要：事后可直接回答「阈值卡掉多少张」
            logger.info(
                "[KBAutoInject] %s: cand=%d kept=%d | rerank<%.2f dropped=%d | q=%s",
                tag,
                len(results or []),
                len(valid_results),
                min_score,
                _rej_rerank,
                _short(query, 100),
            )
            return valid_results
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

        ``top_k`` 是喂给 reranker 的候选数；``max_cards`` 是这一屏**印几张**。
        两者不是一回事：注入路径不截断（一条标题 ~130 字，便宜），
        只有 search_knowledge 的 ``limit`` 才需要按张数封顶。

        序号只增不变：``self._shown`` 记本轮印过的卡，新卡往 `#N+1` 追加，
        已给出的号永不重排（曾每批重排 → 模型照记忆点序号指错，见
        20261023_225015_ea5c533c 第 3、4 轮）。
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
        _known = set(self._shown)
        _fresh_cards = [it for it in _screen if it["key"] not in _known]
        _cap = max_cards or MENU_MAX_CARDS
        _pick, _used = [], 0
        for _it in _fresh_cards[:_cap]:
            # 估这一张会占多少字：标题行 + 命中链 + 行号/路径两行（约 90 字固定开销）
            _chain = _it.get("chain") or []
            _cost = (len(_it.get("title") or "") + sum(len(x) for x in _chain)
                     + 90 + 14 * len(_chain))
            if _pick and _used + _cost > MENU_BUDGET:
                break
            _pick.append(_it)
            _used += _cost
        _more = max(0, len(_fresh_cards) - len(_pick))
        _fresh_cards = _pick
        if not _fresh_cards:
            logger.info(
                "[KBAutoInject] inline MENU no-new (已印过 %d 张) q=%s",
                len(self._shown),
                _short(_q, 70),
            )
            return ""
        _start = len(self._shown) + 1
        self._shown.extend(it["key"] for it in _fresh_cards)
        logger.info(
            "[KBAutoInject] inline MENU EMIT(#%d..#%d of %d) q=%s",
            _start,
            len(self._shown),
            len(self._shown),
            _short(_q, 70),
        )
        return self._render_menu(_fresh_cards, start=_start, more=_more)

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
        self._shown = []
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
                _user_menu = self._inline_menu(_uq, max_cards=_USER_QUERY_MAX_CARDS)
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
