"""hermes-kb-write — 主AI 收尾反思落卡工具.

设计（与 owner 2026-10-07 定的规格一致）:
  1. `save_reflection` 注册为普通工具，主AI / background_review fork 都可调用。
  2. **写卡前硬查重**：复用 KBEngine.search，命中同题卡则拒写并返回旧卡路径，
     让主AI自己判断是「追加修正节」还是「force 重写」。查重不靠模型自觉 —— 写在
     handler 里，可外部复核。
  3. 双根落盘 + md5 一致校验（KB 规约：reflections/ 卡片双根）。
  4. 回执照内置记忆工具 tool result 形态，主AI 收到后自然转述。

为什么查重的判据是「语义检索」而不是字符串相似度:
  实测探针簇 5 张同事故卡，difflib 正文相似度最高只有 0.438（拦不住），
  而 KBEngine 语义检索召回 0.92~0.99（拦得住）。措辞不同但语义相同，
  向量检索吃意思、不吃字面。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import suppress
from typing import Any

logger = logging.getLogger("hermes_kb_write")

# ── KB 规约路径（与 hermes-kb-reflection 保持一致，双根镜像） ──────────────────
# 双根落盘目录。两个根都写同一份卡（md5 一致），索引只建一份。
# 用环境变量覆盖，不写死：KB_REFLECTIONS_INBOX_DIR / KB_REFLECTIONS_LOCAL_DIR
REFLECTIONS_INBOX_DIR = os.environ.get(
    "KB_REFLECTIONS_INBOX_DIR", os.path.expanduser("~/hermes-kb-inbox/reflections")
)
LOCAL_KB_REFLECTIONS_DIR = os.environ.get(
    "KB_REFLECTIONS_LOCAL_DIR", os.path.expanduser("~/knowledge_base/reflections")
)

# ── 查重阈值 ──────────────────────────────────────────────────────────────────
# 实测该拦的同事故卡全在 0.92 以上；0.85 留出余量，避免误杀「同为 CCH 排障
# 但主题不同」的卡。可用 KB_WRITE_DUP_THRESHOLD 覆盖。
DUP_THRESHOLD = float(os.environ.get("KB_WRITE_DUP_THRESHOLD") or "0.85")
# 送回闸门的候选条数 = rerank 免费线。Gitee rerank 单请求 1~25 篇，超过 25 整单
# 按大批量计费；engine 侧 reranker_max_candidates 也是 25，两边同时卡在这条线上。
# 实测（16 张卡自检索）：只回 6 条时准确卡 12/16 过闸门，回满 25 条时 16/16。
DUP_RERANK_TOP_N = int(os.environ.get("KB_WRITE_DUP_RERANK_TOP_N") or "25")
# dry-run: 只记录「假如拦了会拦谁」，不真拒写。用于按用户规矩先标定阈值。
DRY_RUN = (os.environ.get("KB_WRITE_DUP_DRY_RUN") or "").strip().lower() in ("1", "true", "yes", "on")
# 查重命中后**不替模型下结论**：把高分候选（标题 + 正文片段 + 分数）回给模型，
# 由模型判断「同一主题 → 不要重复沉淀 / 确为新主题 → 带 force=true 再写」。
# 置 KB_WRITE_DUP_JUDGE=0 可退回旧的硬拒写语义。
JUDGE_MODE = (os.environ.get("KB_WRITE_DUP_JUDGE") or "1").strip().lower() not in (
    "0", "false", "no", "off"
)
DUP_JUDGE_CANDIDATES = int(os.environ.get("KB_WRITE_DUP_JUDGE_CANDIDATES") or "3")
DUP_SNIPPET_CHARS = int(os.environ.get("KB_WRITE_DUP_SNIPPET_CHARS") or "400")

# ── 工具 schema ───────────────────────────────────────────────────────────────
SAVE_REFLECTION_SCHEMA = {
    "name": "save_reflection",
    "description": (
        "把本轮具备长期复用价值的技术成果沉淀成一张 Markdown 反思卡进知识库。\n"
        "【什么时候该调用】仅当本轮出现了真正的实质交付：\n"
        "  1. 排障与根治：定位并根治了系统/服务/脚本的 Bug、Crash、性能卡顿；\n"
        "  2. 工具与流水线开发：从零搭建或重构了自动化工具、CLI、复合调用流水线；\n"
        "  3. 规约与适配补丁：实装了协议适配、平台差异兜底、配置安全调优。\n"
        "【什么时候不要调用】查状态、读文件、纯问答、闲聊、无结论的中间步骤 —— "
        "直接不调用，宁可什么都不写。灌水卡会污染检索。\n"
        "【查重命中不替你下结论】写卡前会用语义检索比对库内已有卡；命中高分候选时返回 "
        "action_required=judge_duplicate + candidates（`title`=卡片 H1 / `section`=命中小节 / "
        "`snippet`=正文片段 / 分数）。先看 title 判是否同一张卡，再看 section 与 snippet 核细节："
        "同一主题且那张卡仍然成立 → 不要重复沉淀，改用 edit_reflection_card(mode=\"insert\") 追加；"
        "**那张卡本身是错的（结论被证伪 / 根因写错 / 占位空壳 / 已被更完整的卡取代）→ 不要 insert 续命，"
        "用 delete_reflection_card 删掉再写正确的新卡**；确为新主题 → 再次调用并带 force=true。\n"
        "【禁止占位】任何时候都不要用本工具写「占位 / 登记 / 待填 / 无效」这类空壳卡 —— "
        "要拿已有卡路径请用 edit_reflection_card(mode=\"find\", query=...)，不要把 save_reflection 当「占位登记」。\n"
        "【失败路径最值钱】务必写清「先试了什么、为什么不行」，这是检索时最有用的部分。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "卡片标题，须含核心技术术语与目标对象，禁用『修复了一个bug』这类空泛标题。",
            },
            "body": {
                "type": "string",
                "description": (
                    "卡片正文 Markdown。必须含四个小节：\n"
                    "- **触发场景 / 业务背景**：什么现象、什么任务引发\n"
                    "- **根本原因与核心设计**：root cause 与机制，带代码/配置证据\n"
                    "- **标准解决步骤 / CLI使用 / 关键代码**：可复现的命令与参数\n"
                    "- **避坑铁律与硬性约束**：走死的路、血泪教训\n"
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["排障SOP", "经验", "工具链路", "规约SOP"],
                "description": "卡片分类，用于生成文件名前缀。",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选标签。",
            },
            "force": {
                "type": "boolean",
                "description": "查重命中时仍强制写入。仅在你确认那是不同主题、不是重复卡时才用。",
            },
        },
        "required": ["title", "body"],
    },
}


# ── 引擎懒加载（避免插件加载期就把 lancedb 拖起来） ─────────────────────────────
_engine = None


# ── 近实时写入清单（索引延迟兜底） ─────────────────────────────────────────────
# KBWatcher 切片入库有秒级延迟，同轮连写两张同题卡会漏检；这里用进程内清单
# 对「刚写过的标题」做近实时比对。24h 过期，防内存增长。
_recent_writes: dict[str, dict] = {}
_RECENT_TTL_S = 24 * 3600


def _prune_recent() -> None:
    now = time.time()
    for k in [k for k, v in _recent_writes.items() if now - v["ts"] > _RECENT_TTL_S]:
        _recent_writes.pop(k, None)


def _norm_title(s: str) -> str:
    """标题归一化：去装饰前缀/标点/空白，便于比对。"""
    s = re.sub(r"^[#\s\[]*(排障SOP|经验|工具链路|规约SOP)[\]\s/]*", "", s.strip())
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s).lower()


def _title_similar(a: str, b: str) -> bool:
    """标题是否指向同一件事：归一化后一方包含另一方（长度差需可辨）。

    同事故卡的标题常用词几乎相同（如「探针首字判据与转发侧判据双实现分叉」
    在不同卡里只差装饰词），包含关系足以判定。
    """
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 12 and short in long


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    try:
        from hermes_kb.engine import KBEngine
        _engine = KBEngine()
        logger.info("[KBWrite] KBEngine ready for dedup gate")
    except Exception as e:
        logger.warning("[KBWrite] KBEngine unavailable (%s); dedup gate disabled", e)
        _engine = False
    return _engine


def _slugify(text: str, limit: int = 60) -> str:
    """标题 → 文件名 slug。保留中英文与数字，其余折叠为下划线。"""
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text.strip())
    s = re.sub(r"_{2,}", "_", s).strip("_")
    return (s[:limit] or "reflection").strip("_")


def _card_title_from_heading_path(heading_path: str, real_path: str = "") -> str:
    """候选块所属**卡片**的 H1 标题。

    检索结果的 ``title`` 列是块所在的最深层小节名（``避坑铁律`` / ``次生影响`` /
    ``3. 取标题的现役代码``），拿它当卡标题，模型判重时看到的是「候选卡叫『3. 取标题的
    现役代码』」——既判断不了同不同题，insert 前的同域核对也没依据。所以这里按检索侧
    已校准的口径取标题：``heading_path`` **第一段**就是该块自己那张卡的 H1（多 H1 文件
    也不会串），去掉长卡切段留下的 ``(Part N)`` 尾巴；短到 8 字以内说明第一段只是小节名，
    才退回文件 H1（``_card_title_from_file``）。

    注意与 ``_find_candidates`` 的分工：那边必须用**文件 H1**，因为它的 title 会被原样
    回填给 ``delete_reflection_card`` 的 ``confirm_title``，而删卡校验读的是文件 H1，
    两者口径不能分叉；这里只服务于判重/追加，用块自己那张卡的 H1 更准。
    """
    hp = str(heading_path or "").strip()
    h1 = re.split(r"\s+>\s+", hp)[0].strip() if hp else ""
    h1 = re.sub(r"\s*\(Part \d+\)+$", "", h1).strip()
    if len(h1) <= 8 and real_path:
        h1 = _card_title_from_file(real_path)
    return h1.strip() or (os.path.basename(real_path) if real_path else "")


def _section_from_heading_path(heading_path: str, card_title: str = "") -> str:
    """候选块命中的那一**小节**名；命中在卡片级正文（这张卡没有小节）时返回空串。

    ``heading_path`` 是 ``H1 > H2 > H3`` 的层级串，取**最后一段**就是命中处最深的小节。
    但块有时直接挂在卡片 H1 下（卡里没有 ``##`` 小节），此时最后一段就是卡标题本身，
    再去掉 ``(Part N)`` 之后与 ``card_title`` 逐字相同 —— 那就不是「小节」，是标题复读，
    返回空串让调用方省略该字段，提示里说明「命中在整卡正文」。旧实现直接回填检索的
    ``title`` 列，对这种块会印出「title 与 section 一模一样、都带 Part 2」的噪声。
    """
    hp = str(heading_path or "").strip()
    if not hp:
        return ""
    parts = [p.strip() for p in re.split(r"\s+>\s+", hp) if p.strip()]
    if not parts:
        return ""
    sec = re.sub(r"\s*\(Part \d+\)+$", "", parts[-1]).strip()
    if len(parts) == 1 or (card_title and sec == card_title.strip()):
        return ""
    return sec


def _h1_titles_in_file(p: str) -> list[str]:
    """文件里所有**卡级** H1（`# ` 行）。多 H1 文件 = 一个 .md 里合了多张卡。

    必须跳过 ``` 围栏代码块：卡里常贴 `.pth` / bash / python 片段，行首的 `# comment`
    与 `# TODO` 会被当成 H1 —— 实测某单卡文件因此数出 43 个「H1」，删卡守卫会误拦。
    围栏行本身（``` 或 ~~~，允许带语言标注）不参与判断，进/出围栏靠状态位切换。
    """
    out: list[str] = []
    in_fence = False
    fence_mark = ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                m = re.match(r"^(```+|~~~+)(.*)$", s)
                if m:
                    if not in_fence:
                        in_fence, fence_mark = True, m.group(1)[0]
                    elif m.group(1)[0] == fence_mark:
                        in_fence, fence_mark = False, ""
                    continue
                if in_fence:
                    continue
                if s.startswith("# "):
                    out.append(s[2:].strip())
    except Exception:
        pass
    return out


def _dedup_check(title: str, body: str) -> tuple[bool, list[dict], str]:
    """写卡前硬查重。

    Returns ``(is_duplicate, hits, note)``。hits 为超阈命中，note 为查重结论说明。
    查重 query 用「标题 + 正文前 600 字」—— 实测这个组合对同事故卡的召回最好
    （全文会稀释，只用标题则信息太少）。
    """
    eng = _get_engine()
    if not eng:
        return False, [], "查重引擎不可用，已放行（fail-open）"

    query = f"{title}\n{body[:600]}"
    try:
        # 名次交给 RRF，rerank 分只做阈值过滤（owner 2026-10-23 指令）。
        # 原先这里显式覆写成 rank_by="reranker"，与主检索路径（config.retrieval.rank_by
        # 已是 "rrf"）不一致：同主题候选池上 reranker 分饱和（实测 25 张里 12 张 >=0.99，
        # 分差落到 1e-5 噪声级），拿它排等于随机；RRF 序命中第 1 名 6/6、平均位次 1.00。
        # 故不再覆写排序键 —— 顺序按 rrf_score，DUP_THRESHOLD 仍卡 rerank_score。
        results = eng.search(
            query, rerank_top_n=DUP_RERANK_TOP_N, use_reranker=True, rank_by="rrf"
        )
    except Exception as e:
        logger.warning("[KBWrite] dedup search failed: %s", e)
        return False, [], f"查重检索异常（{e}），已放行"

    # 同一张卡会被切成多个 chunk：**按文件聚合成一条，取 max(rerank) 与 max(rrf)**。
    # 不能「碰巧先出现哪块就用哪块的分」—— 实测同一张卡两块分能差到跨过 0.85 门槛，
    # 用哪块纯粹由排序键决定，会把「该命中」变成「没命中」。
    # 聚合后：过闸门 = 这张卡最好的那块 rerank >= 阈值；名次 = 它的 max(rrf)。
    cards: dict[str, dict] = {}
    unscored = 0  # rerank 全挂时不能把「没分数」当成「0 分」静默吞掉
    for r in results or []:
        fp = r.get("file_path") or ""
        if "reflections" not in fp:
            continue
        raw = r.get("rerank_score")
        if raw is None:
            unscored += 1
            continue
        score = float(raw)
        rrf = float(r.get("rrf_score") or 0.0)
        cur = cards.get(fp)
        if cur is None:
            cards[fp] = {
                "path": fp,
                "heading": r.get("heading_path") or r.get("title") or "",
                # score = 过滤用的 rerank 分（本卡最高那块）；rrf_score = 排序键，两者别混。
                "score": round(score, 4),
                "rrf_score": round(rrf, 5),
                # title = 卡片 H1（heading_path 第一段），section = 命中的那一小节。
                # 只给 title 列（小节名）会让判重变成「靠猜」——模型看到候选叫
                # 「3. 取标题的现役代码」，既判不了同不同题，也做不了 insert 前的同域核对。
                "title": _card_title_from_heading_path(r.get("heading_path") or "", fp),
                # section = 命中处最深那一小节；命中在卡片级正文（卡里没有小节）时留空，
                # 免得标题被复读一遍（旧实现回填 title 列，就是「title 与 section 同句」的来源）。
                "section": _section_from_heading_path(r.get("heading_path") or "", _card_title_from_heading_path(r.get("heading_path") or "", fp)),
                "snippet": " ".join(str(r.get("content") or "").split())[:DUP_SNIPPET_CHARS],
            }
            continue
        # 片段固定取「语义最贴的那块」（rerank 最高），名次用「RRF 最好的那块」。
        if score > cur["score"]:
            cur["score"] = round(score, 4)
            cur["title"] = _card_title_from_heading_path(r.get("heading_path") or "", fp)
            cur["section"] = _section_from_heading_path(
                r.get("heading_path") or "", cur["title"]
            )
            cur["snippet"] = " ".join(str(r.get("content") or "").split())[:DUP_SNIPPET_CHARS]
        if rrf > cur["rrf_score"]:
            cur["rrf_score"] = round(rrf, 5)

    hits = sorted(
        (c for c in cards.values() if c["score"] >= DUP_THRESHOLD),
        key=lambda c: c["rrf_score"],
        reverse=True,
    )

    # 索引延迟兜底：刚写过的卡要等 watcher 切片（秒级）才能被检索到，
    # 同轮内连写两张同题卡会漏检。进程内保留近 24h 的写入标题做近实时比对。
    _prune_recent()
    for rec in _recent_writes.values():
        if _title_similar(title, rec["title"]):
            hits.append({
                "path": rec["path"],
                "heading": rec["title"],
                "score": 1.0,
                "source": "recent_inflight",
            })
            logger.info("[KBWrite] dedup caught by in-flight recent-write list: %r", title)

    if hits:
        note = f"命中 {len(hits)} 张同题卡（阈值 {DUP_THRESHOLD}，按 rrf 排序）"
    else:
        note = f"无同题卡（阈值 {DUP_THRESHOLD}，按 rrf 排序）"
    if unscored:
        # Reranker 超时/接口失败时 engine 会退回 rrf_fallback（不带 rerank_score）。
        # 此时闸门无法判定，必须显式说出来 —— 否则「查重永远放行」会被当成「没有同题卡」。
        note += f"；另有 {unscored} 条候选缺 rerank_score（rerank 不可用），未参与过滤"
        logger.warning(
            "[KBWrite] dedup: %d candidate(s) lacked rerank_score (reranker down?); "
            "threshold gate skipped for them", unscored,
        )
    return bool(hits), hits, note


def _write_dual_root(filename: str, content: str) -> tuple[bool, str, str]:
    """写双根并校验 md5 一致。返回 (ok, inbox_path, md5)。"""
    os.makedirs(REFLECTIONS_INBOX_DIR, exist_ok=True)
    os.makedirs(LOCAL_KB_REFLECTIONS_DIR, exist_ok=True)
    p1 = os.path.join(REFLECTIONS_INBOX_DIR, filename)
    p2 = os.path.join(LOCAL_KB_REFLECTIONS_DIR, filename)
    data = content.encode("utf-8")
    for p in (p1, p2):
        tmp = p + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)

    md5 = hashlib.md5(data).hexdigest()
    m1 = hashlib.md5(open(p1, "rb").read()).hexdigest()
    m2 = hashlib.md5(open(p2, "rb").read()).hexdigest()
    if not (m1 == m2 == md5):
        return False, p1, md5
    return True, p1, md5



# ── 找卡检索常量（edit_reflection_card 的 mode=find 用）────────────────────────
# 2026-10-07 owner 指令「简化工具」：原独立工具 find_reflection_card 已摘除，
# 能力并入 edit_reflection_card(mode="find")，这些常量继续给 _find_candidates 复用。
# 背景：卡路径只能靠语义检索拿（save_reflection 的查重候选需 >=0.85 命中才回候选）。
# 历史事故：某会话拿 save_reflection 当「占位登记」，真写了两张标题为「占位」的空壳卡进双根与索引。
# 把「拿路径」变成一步只读调用 —— 从动机上消灭「造假卡换路径」。
FIND_DEFAULT_TOP_K = int(os.environ.get("KB_WRITE_FIND_TOP_K") or "5")
FIND_MAX_TOP_K = int(os.environ.get("KB_WRITE_FIND_MAX_TOP_K") or "12")
FIND_CANDIDATE_LIMIT = int(os.environ.get("KB_WRITE_FIND_CANDIDATE_LIMIT") or "40")
FIND_RERANK_TOPN = int(os.environ.get("KB_WRITE_FIND_RERANK_TOPN") or "24")

# ── 「改卡 / 删卡」工具（2026-10-07 owner 指令「我觉得应该可以修改和删除」）──
# 背景：原先只有 save（建卡）与 append（尾部追加小节），**改不了卡里一个字、也没有任何删除出口**。
# 实测代价：误写的占位卡只能人工清（双根 rm + delete_by_file_path + 回读确认），
# 也就是「AI 自己能写垃圾卡，却清不掉」。
# 两条工具的设计（owner 拍板）：
#   - edit：任意子串精确替换（能改标题/正文/小节），old_string 命中数 != 1 一律拒写；
#   - delete：**硬删不可恢复**（owner 选「直接硬删」，不做回收站），双根 + 索引一起清，
#     必须 confirm_title 逐字等于该卡 H1 标题；
#   - 两者都**不进 SOP fork 白名单**：后台收尾线程绝不能持有删卡权。
EDIT_REFLECTION_SCHEMA = {
    "name": "edit_reflection_card",
    "description": (
        "修改已有反思卡 —— 找卡 / 插入 / 覆盖 / 删段 四合一"
        "（2026-10-07 起 append_to_reflection 与 find_reflection_card 已并入本工具）。\n"
        "【mode=find】只读找卡：按关键词语义检索，返回候选卡的绝对路径/标题/分数/片段，"
        "不写任何文件、不发回执。**拿已有卡路径的正路。**\n"
        "【mode=insert】插入一个新小节，标题自动写成「## 修正 <日期>：<section_title>」。"
        "`after` 省略 → 插到卡末（等价旧 append_to_reflection）；`after` 给了逐字锚点 → "
        "**插在该锚点之后，可插到卡内任意位置**（锚点命中 0 处或多处一律拒写，"
        "**不会静默退化成插末尾**）。同一小节标题已存在时幂等拒写。\n"
        "【mode=replace】把 `old_string` 精确替换成 `new_string`；old_string 必须逐字精确且"
        "**只命中 1 处**，命中 0 处或 >1 处一律拒写。`new_string` 传空串 = 删掉这段。\n"
        "【mode=delete】删掉 `old_string` 那一段（等价 replace + 空 new_string，语义更明确）。\n"
        "【硬约束】只允许 reflections 双根目录下的 .md；双根 md5 不一致拒写；"
        "后台收尾线程（SOP fork）只放行 find / insert，replace / delete 在 handler 内硬拦。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["find", "insert", "replace", "delete"],
                "description": "find=只读找卡 / insert=插入新小节 / replace=覆盖子串 / delete=删掉子串",
            },
            "card_path": {
                "type": "string",
                "description": "已有卡的路径（candidates[].path）或文件名。mode=find 时可省略，改用 query。",
            },
            "query": {
                "type": "string",
                "description": (
                    "mode=find 必填：要找的主题关键词。**必须带「域词」**——本轮事故/成果的专有名词、"
                    "主机名、服务名、文件路径、命令、端口、配置键、cron jobid"
                    "（如 `thinpool-guard` / `tpool0` / `SNAPGATE` / `restic` / `/etc/tomato` / `50458cb4c0c3`）。"
                    "**严禁只写抽象模式词**（`哨兵` / `告警` / `推送` / `no_agent` / `cron` / `watchdog` / "
                    "`零输出` / `闸门`）：这类词在任何监控类卡上都高分，会把域外卡排到第一"
                    "（历史事故：用 `no_agent cron 哨兵 零输出 告警推送` 去 find，0.9673 命中一张结算计费卡，"
                    "把 thin pool 内容插了进去）。**分数高不等于同域**——insert 前还要核对目标卡 H1 讲的是"
                    "不是同一个系统/同一起事故。"
                ),
            },
            "top_k": {
                "type": "integer",
                "description": f"mode=find 可选：返回候选张数，默认 {FIND_DEFAULT_TOP_K}，上限 {FIND_MAX_TOP_K}。",
            },
            "section_title": {
                "type": "string",
                "description": "mode=insert 必填：新小节标题，如「查重改造后行为变化」。不要自己写「修正 日期」前缀。",
            },
            "section_body": {
                "type": "string",
                "description": "mode=insert 必填：新小节的 Markdown 正文。",
            },
            "after": {
                "type": "string",
                "description": (
                    "mode=insert 可选：逐字锚点，新小节插在该文本**之后**；"
                    "省略 = 插到卡末。锚点命中 0 处或多处一律拒写（不退化插末尾）。"
                ),
            },
            "old_string": {
                "type": "string",
                "description": "mode=replace/delete 必填：要被替换/删除的原文，须逐字精确且只命中 1 处。",
            },
            "new_string": {
                "type": "string",
                "description": "mode=replace 用：替换后的文字；传空串 = 删掉这段。",
            },
        },
        "required": ["mode"],
    },
}

DELETE_REFLECTION_SCHEMA = {
    "name": "delete_reflection_card",
    "description": (
        "**硬删除**一张反思卡：双根 .md 一起删 + 显式清索引切片。**不可恢复，没有回收站。**\n"
        "【什么时候调用】只处理**整张卡本身有问题**的情况，别拿它当「清段落」工具：\n"
        "  · 结论已被证伪 / 根因写错——留着会把下一个读到它的人带进同一个坑（**这类必须删，不要用 insert 续命**）；\n"
        "  · 占位卡、空壳卡、只有标题没有实证的卡；\n"
        "  · 同题重复卡：已被更完整的那张覆盖，留着只会互相污染检索；\n"
        "  · 卡里指的路径/命令/配置键/服务名已不存在，且没有历史教训价值；\n"
        "  · 已被合并进另一张卡、或误写卡的旧卡。\n"
        "【不要删的情况】卡是对的、只是没覆盖本轮新情况 → 用 edit_reflection_card(mode=\"insert\") 追加修正节；"
        "个别字句错、主体仍成立 → mode=\"replace\"。判断「这卡还会不会害人」比判断「它是不是不完美」重要。\n"
        "【参数】`card_path` 用 edit_reflection_card(mode=\"find\") 或 save_reflection 命中候选返回的 "
        "`candidates[].path` 或文件名；"
        "`confirm_title` **必须逐字等于该卡的 H1**（优先直接抄候选里的 `confirm_title` 字段——多 H1 文件里 "
        "它可能与 `title` 不同，而校验读的是文件首个 H1），差一个字符就拒删；分类前缀如「[排障SOP] 」也要带上。\n"
        "【删前必做】先 edit_reflection_card(mode=\"find\") 或用候选里的 path/title 确认目标卡就是你要删的那张；"
        "删除不可逆，没有回收站。\n"
        "【删后】写正确的新卡，或一句话说明「删了哪张、为什么」。\n"
        "【硬约束】只允许 reflections 双根目录下的 .md；confirm_title 不匹配拒删；后台收尾线程（SOP fork）无权调用。\n"
        "【多 H1 守卫】若该 .md 里合了多张卡（多个 `# ` 行），本工具**拒绝删除**并回报 h1_titles —— "
        "整文件 os.remove 会连带销毁其它卡（不可恢复）。改用 edit_reflection_card(mode=\"replace\"/\"delete\") 精确删那一段，"
        "或先把文件拆成一文件一卡。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_path": {"type": "string", "description": "要删的卡路径（candidates[].path）或文件名。"},
            "confirm_title": {
                "type": "string",
                "description": (
                    "该卡 H1 标题的逐字副本，用于防误删。优先直接抄候选里的 `confirm_title` 字段"
                    "（＝文件首个 H1，delete 的校验口径）；mode=\"find\" 返回的 `title` 也是文件 H1，同样可用。"
                    "不要自己重排/简写/去掉「[排障SOP] 」这类分类前缀。"
                ),
            },
        },
        "required": ["card_path", "confirm_title"],
    },
}


def _clean_card_title_for_receipt(raw: str) -> str:
    """清洗卡片标题用于人面回执：去除 [经验 / 工具链路] 等中括号分类前缀。"""
    t = str(raw or "").strip()
    t = re.sub(r"^\[(?:排障SOP|经验|工具链路|规约SOP|[\s/])+\]\s*", "", t).strip()
    return t or raw


def _smart_truncate(text: str, max_len: int = 32) -> str:
    """智能截断短语：超长截断并补省略号，智能避免留下悬空的「或“等开括号。"""
    s = str(text or "").strip()
    if len(s) <= max_len:
        return s
    sub = s[:max_len - 1]
    # 若截断处恰好处于未闭合的「或“内部，做优雅回退防破裂
    last_open_cn = sub.rfind("「")
    if last_open_cn != -1 and "」" not in sub[last_open_cn:]:
        return sub[:last_open_cn].rstrip(" ，,：:、") + "…"
    last_open_quote = sub.rfind("“")
    if last_open_quote != -1 and "”" not in sub[last_open_quote:]:
        return sub[:last_open_quote].rstrip(" ，,：:、") + "…"
    return sub.rstrip(" ，,：:、") + "…"


def _card_title_from_file(p: str) -> str:
    """取卡片的 H1 标题（前 12 行内首个 `# ` 行）；取不到退回文件名。"""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                s = line.strip()
                if s.startswith("# "):
                    return s[2:].strip()
                if i > 12:
                    break
    except Exception:
        pass
    return os.path.basename(p)


def _resolve_existing_card_path(fp: str) -> str:
    """把检索命中的路径归一到**确实存在的**双根路径（优先 inbox 根）；僵尸命中返回 ""。"""
    base = os.path.basename(str(fp or "").strip())
    if not base.endswith(".md"):
        return ""
    for cand in (os.path.join(REFLECTIONS_INBOX_DIR, base),
                 os.path.join(LOCAL_KB_REFLECTIONS_DIR, base)):
        if os.path.isfile(cand):
            return cand
    return ""


def _resolve_card_paths(card_path: Any) -> tuple[str, str] | None:
    """把 candidates[].path / 文件名解析成双根两个路径。越界、非 .md、双根不全 → None。"""
    raw = str(card_path or "").strip()
    if not raw:
        return None
    base = os.path.basename(raw)
    if not base.endswith(".md") or base in (".md",) or base.startswith("."):
        return None
    if raw != base:
        d = os.path.dirname(os.path.abspath(raw))
        allowed = (os.path.abspath(REFLECTIONS_INBOX_DIR), os.path.abspath(LOCAL_KB_REFLECTIONS_DIR))
        if d not in allowed:
            return None
    p1 = os.path.join(REFLECTIONS_INBOX_DIR, base)
    p2 = os.path.join(LOCAL_KB_REFLECTIONS_DIR, base)
    if not (os.path.isfile(p1) and os.path.isfile(p2)):
        return None
    return p1, p2


def _resolve_card_paths_lenient(card_path: Any) -> list[str]:
    """删除用：解析成**确实存在**的双根路径列表（允许只有一根存在，便于修复单边残留）。"""
    raw = str(card_path or "").strip()
    if not raw:
        return []
    base = os.path.basename(raw)
    if not base.endswith(".md") or base in (".md",) or base.startswith("."):
        return []
    if raw != base:
        d = os.path.dirname(os.path.abspath(raw))
        allowed = (os.path.abspath(REFLECTIONS_INBOX_DIR), os.path.abspath(LOCAL_KB_REFLECTIONS_DIR))
        if d not in allowed:
            return []
    return [p for p in (os.path.join(REFLECTIONS_INBOX_DIR, base),
                        os.path.join(LOCAL_KB_REFLECTIONS_DIR, base))
            if os.path.isfile(p)]


def _count_chunks_for(path: str) -> int:
    """数索引里某个 file_path 还剩多少切片（回读验收用；-1 = 查不到）。"""
    eng = _get_engine()
    table = getattr(eng, "table", None) if eng else None
    if table is None:
        return -1
    safe = path.replace("'", "''")
    try:
        return int(table.count_rows(filter=f"file_path = '{safe}'"))
    except Exception:
        pass
    try:
        tbl = table.to_arrow()
        if "file_path" not in tbl.column_names:
            return -1
        return sum(1 for x in tbl["file_path"].to_pylist() if x == path)
    except Exception:
        return -1


def _purge_index_for(paths: list[str]) -> dict:
    """显式清索引切片（不等 watcher），并回读剩余切片数。"""
    out: dict = {}
    eng = _get_engine()
    for p in paths:
        if not eng:
            out[p] = {"purged": False, "residual_chunks": -1, "error": "lancedb 引擎不可用"}
            continue
        try:
            purged = bool(eng.delete_by_file_path(p))
        except Exception as e:
            out[p] = {"purged": False, "residual_chunks": -1, "error": str(e)}
            continue
        out[p] = {"purged": purged, "residual_chunks": _count_chunks_for(p)}
    return out


def _md5_file(p: str) -> str:
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def _find_candidates(query: str, top_k: int) -> dict:
    """只读：按语义检索已有反思卡，返回候选卡 路径/标题/分数/片段。

    原 ``find_reflection_card`` 的实现，2026-10-07 并入 edit 的 mode=find。
    """
    if len(query) < 2:
        return {
            "success": False,
            "error": "query 太短：请写你要找的主题关键词（如「查重 judge 出口」）。",
        }
    top_k = max(1, min(int(top_k or FIND_DEFAULT_TOP_K), FIND_MAX_TOP_K))

    eng = _get_engine()
    if not eng:
        return {
            "success": False,
            "error": "检索不可用：lancedb 引擎未就绪，已放弃查找（未写任何文件）。",
        }
    try:
        results = eng.search(
            query,
            limit=FIND_CANDIDATE_LIMIT,
            rerank_top_n=FIND_RERANK_TOPN,
            use_reranker=True,
            rank_by="reranker",
        )
    except Exception as e:
        return {"success": False, "error": f"检索异常：{e}"}

    best: dict = {}
    for r in results or []:
        fp = str(r.get("file_path") or "")
        if "reflections" not in fp:
            continue
        score = float(r.get("rerank_score") or 0.0)
        cur = best.get(fp)
        if cur is not None and cur["score"] >= score:
            continue
        best[fp] = {
            "score": score,
            "snippet": " ".join(str(r.get("content") or "").split())[:DUP_SNIPPET_CHARS],
        }

    cands: list = []
    for fp, info in sorted(best.items(), key=lambda kv: kv[1]["score"], reverse=True):
        real = _resolve_existing_card_path(fp)
        if not real:
            continue
        cands.append({
            "path": real,
            "title": _card_title_from_file(real),
            "score": round(info["score"], 4),
            "snippet": info["snippet"],
        })
        if len(cands) >= top_k:
            break

    if not cands:
        return {
            "success": True,
            "query": query,
            "count": 0,
            "candidates": [],
            "message": (
                "没找到可用的已有卡（命中若都是已删文件的僵尸块也会被剔掉）。"
                "确属新主题 → 直接 save_reflection 写**真内容**的新卡；"
                "**不要**写「占位/登记/待填」这类空壳卡。"
            ),
        }

    return {
        "success": True,
        "query": query,
        "count": len(cands),
        "candidates": cands,
        "message": (
            f"找到 {len(cands)} 张候选卡（最高 {cands[0]['score']}）。读 snippet 判断："
            "同一主题 → edit_reflection_card(card_path=<candidates[].path>, mode=\"insert\", "
            "section_title=..., section_body=...)；"
            "确为新主题 → save_reflection(..., force=true)。**不要再写占位卡换路径。**"
        ),
    }


def edit_reflection_card_handler(args: dict, **kw: Any) -> str:
    """四合一改卡：find(只读找卡) / insert(插小节) / replace(覆盖子串) / delete(删子串)。

    2026-10-07 owner 指令「简化工具，只留新建、删除、修改」：把 append_to_reflection 与
    find_reflection_card 两条独立工具并入本 handler 的 mode 分支。

    **mode 级安全闸**：``hermes_cli.plugins`` 的线程工具白名单只按**工具名**拦截
    （``allowed is not None and tool_name not in allowed`` → block），**不解析 mode**。
    所以 edit 进 fork 白名单 ≠ fork 有删改权 —— 这里必须自查"当前线程是否受限"，
    受限线程只放行 find / insert。
    """
    mode = str(args.get("mode") or "").strip().lower()
    if mode not in ("find", "insert", "replace", "delete"):
        return json.dumps({
            "success": False,
            "error": "mode 必须是 find / insert / replace / delete 之一。",
            "hint": '找卡=mode"find"；插小节=mode"insert"；覆盖=mode"replace"；删段=mode"delete"。',
        }, ensure_ascii=False)

    # ── find：只读，零副作用、不发回执 ──
    if mode == "find":
        try:
            top_k = int(args.get("top_k") or FIND_DEFAULT_TOP_K)
        except Exception:
            top_k = FIND_DEFAULT_TOP_K
        return json.dumps(
            _find_candidates(str(args.get("query") or "").strip(), top_k),
            ensure_ascii=False,
        )

    # ── 写操作：受限线程（SOP fork / 原生 bg-review）硬拦破坏性 mode ──
    if mode in ("replace", "delete") and _thread_tool_restricted():
        return json.dumps({
            "success": False,
            "error": (
                f"后台收尾线程无权 mode={mode}：它会改到卡里已有正文。"
                "后台只允许 mode=find 与 mode=insert。"
            ),
            "hint": "要覆盖或删除卡内片段，请由主 AI 在主线程发起。",
        }, ensure_ascii=False)

    paths = _resolve_card_paths(args.get("card_path"))
    if not paths:
        return json.dumps({
            "success": False,
            "error": "card_path 非法或双根不同时存在（只允许 reflections 双根目录下的 .md）",
        }, ensure_ascii=False)

    p1, p2 = paths
    m1, m2 = _md5_file(p1), _md5_file(p2)
    if m1 != m2:
        return json.dumps({
            "success": False,
            "error": f"双根 md5 不一致（{m1[:8]} vs {m2[:8]}），拒绝改；请先人工对齐两份卡",
        }, ensure_ascii=False)

    txt = open(p1, encoding="utf-8").read()
    date = time.strftime("%Y-%m-%d")

    # ══ mode=insert：插小节（after 省略=插末尾；给了锚点=插任意位置）══
    if mode == "insert":
        st = str(args.get("section_title") or "").strip().lstrip("#").strip()
        sb = str(args.get("section_body") or "").strip()
        if not st or not sb:
            return json.dumps(
                {"success": False, "error": "section_title 与 section_body 均不能为空"},
                ensure_ascii=False)
        heading = f"## 修正 {date}：{st}"
        if heading in txt:
            return json.dumps({
                "success": False,
                "duplicate_section": True,
                "path": p1,
                "heading": heading,
                "message": "该小节已存在（幂等拒写）。要再加内容请换 section_title。",
            }, ensure_ascii=False)

        block = f"{heading}\n\n{sb}\n"
        after_raw = args.get("after")
        after = "" if after_raw is None else str(after_raw)
        if not after.strip():
            new_txt = txt.rstrip("\n") + "\n\n" + block
            where = "end"
        else:
            hits = txt.count(after)
            if hits == 0:
                return json.dumps({
                    "success": False,
                    "error": "after 锚点在卡里找不到（逐字精确匹配，注意空格/换行/全角半角）。**拒绝退化为插末尾。**",
                    "hint": "先用 read_file 读该卡取原文片段，再原样填入 after；或省略 after 插到卡末。",
                }, ensure_ascii=False)
            if hits > 1:
                return json.dumps({
                    "success": False,
                    "error": f"after 锚点命中 {hits} 处，不唯一，拒绝写。请加长锚点上下文使其唯一。",
                    "occurrences": hits,
                }, ensure_ascii=False)
            i = txt.index(after) + len(after)
            new_txt = txt[:i].rstrip("\n") + "\n\n" + block + txt[i:].lstrip("\n")
            where = "after_anchor"

        data = new_txt.encode("utf-8")
        for p in (p1, p2):
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)

        m3, m4 = _md5_file(p1), _md5_file(p2)
        if not (m3 == m4 == hashlib.md5(data).hexdigest()):
            return json.dumps({"success": False, "error": "双根写入后 md5 不一致"},
                              ensure_ascii=False)

        card_title = ""
        for line in new_txt.splitlines():
            if line.startswith("# "):
                card_title = line[2:].strip()
                break
        added = len(data) - len(txt.encode())
        logger.info("[KBWrite] edit(insert/%s): %s +%s B (md5 %s -> %s) section=%r",
                    where, os.path.basename(p1), added, m1[:8], m3[:8], heading)
        base = os.path.basename(p1)
        if base in _recent_writes:
            _recent_writes[base] = {"title": card_title, "path": p1, "ts": time.time()}

        _clean_card = _smart_truncate(_clean_card_title_for_receipt(card_title or base), 28)
        _clean_sec = _smart_truncate(st, 26)
        receipt = f"已补充《{_clean_card}》· {_clean_sec}"
        if SOP_RECEIPT_ENABLED and not _in_review_fork_thread():
            _enqueue_receipt_for_kw(f"{SOP_RECEIPT_PREFIX}{receipt}", kw)
        return json.dumps({
            "success": True,
            "mode": "insert",
            "where": where,
            "path": p1,
            "heading": heading,
            "card_title": card_title,
            "md5_before": m1,
            "md5_after": m3,
            "bytes_added": added,
            "message": receipt,
        }, ensure_ascii=False)

    # ══ mode=replace / delete：精确子串操作 ══
    old = "" if args.get("old_string") is None else str(args.get("old_string"))
    if mode == "delete":
        new = ""
    else:
        new = "" if args.get("new_string") is None else str(args.get("new_string"))
    if not old:
        return json.dumps({
            "success": False,
            "error": "old_string 不能为空。要整段删除请用 delete_reflection_card（硬删整卡）。",
        }, ensure_ascii=False)
    if mode == "replace" and old == new:
        return json.dumps({"success": False, "error": "old_string 与 new_string 相同，无需修改。"},
                          ensure_ascii=False)

    hits = txt.count(old)
    if hits == 0:
        return json.dumps({
            "success": False,
            "error": "old_string 在卡里找不到（逐字精确匹配，注意空格/换行/全角半角）。",
            "hint": "先用 read_file 读该卡取原文片段，再原样填入 old_string。",
        }, ensure_ascii=False)
    if hits > 1:
        return json.dumps({
            "success": False,
            "error": f"old_string 命中 {hits} 处，不唯一，拒绝改。请加长上下文（含前后行）使其唯一。",
            "occurrences": hits,
        }, ensure_ascii=False)

    new_txt = txt.replace(old, new, 1)
    data = new_txt.encode("utf-8")
    for p in (p1, p2):
        tmp = p + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    m3, m4 = _md5_file(p1), _md5_file(p2)
    if not (m3 == m4 == hashlib.md5(data).hexdigest()):
        return json.dumps({"success": False, "error": "双根写入后 md5 不一致"}, ensure_ascii=False)

    base = os.path.basename(p1)
    title = _card_title_from_file(p1)
    if base in _recent_writes:
        _recent_writes[base] = {"title": title, "path": p1, "ts": time.time()}
    logger.info("[KBWrite] edit(%s): %s (%s -> %s B, md5 %s -> %s, title=%r)",
                mode, base, len(txt.encode()), len(data), m1[:8], m3[:8], title)

    _short = _smart_truncate(_clean_card_title_for_receipt(title), RECEIPT_TITLE_MAX)
    receipt = f"已修改反思卡《{_short}》" if mode == "replace" else f"已删除卡内片段《{_short}》"
    if SOP_RECEIPT_ENABLED and not _in_review_fork_thread():
        _enqueue_receipt_for_kw(f"{SOP_RECEIPT_PREFIX}{receipt}", kw)
    return json.dumps({
        "success": True,
        "mode": mode,
        "path": p1,
        "card_title": title,
        "md5_before": m1,
        "md5_after": m3,
        "bytes_before": len(txt.encode()),
        "bytes_after": len(data),
        "message": receipt,
    }, ensure_ascii=False)


def delete_reflection_card_handler(args: dict, **kw: Any) -> str:
    """硬删一张反思卡：双根 .md + 索引切片；confirm_title 须逐字匹配 H1。"""
    raw = str(args.get("card_path") or "").strip()
    confirm = str(args.get("confirm_title") or "").strip()
    paths = _resolve_card_paths_lenient(raw)
    if not paths:
        return json.dumps({
            "success": False,
            "error": "card_path 非法或文件不存在（只允许 reflections 双根目录下的 .md）",
        }, ensure_ascii=False)
    if not confirm:
        return json.dumps({
            "success": False,
            "error": "缺 confirm_title：请把 edit_reflection_card(mode=\"find\") 返回的 title 逐字原样填入。",
        }, ensure_ascii=False)

    actual = _card_title_from_file(paths[0])
    if confirm != actual:
        return json.dumps({
            "success": False,
            "error": "confirm_title 与该卡 H1 标题不一致，拒绝删除（防误删硬闸）。",
            "expected_title": actual,
            "got": confirm,
        }, ensure_ascii=False)

    # 多 H1 文件守卫（2026-10-26）：一个 .md 里合了多张卡时，下面的 os.remove 会连带
    # 销毁其它卡 —— 历史缺陷实测 438 文件装 1042 卡、604 张非首卡不可恢复。
    # 「该删就删」不等于「允许误删」，所以这里硬拦，把事实回报出去由人拆。
    _h1s = _h1_titles_in_file(paths[0])
    if len(_h1s) > 1:
        return json.dumps({
            "success": False,
            "error": (
                f"拒绝删除：这个文件里合了 {len(_h1s)} 张卡，os.remove 会连带销毁其余 "
                f"{len(_h1s) - 1} 张（不可恢复）。"
            ),
            "h1_count": len(_h1s),
            "h1_titles": _h1s[:10],
            "hint": (
                "需要先把这个 .md 拆成「一文件一卡」再删，或改用 "
                "edit_reflection_card(mode=\"replace\"/\"delete\") 精确删掉那一段正文。"
                "不要为了删一张卡牺牲同文件里的其它卡。"
            ),
        }, ensure_ascii=False)

    before: dict = {}
    for p in paths:
        try:
            data = open(p, "rb").read()
            before[p] = {"md5": hashlib.md5(data).hexdigest(), "bytes": len(data)}
        except Exception as e:
            before[p] = {"md5": "", "bytes": -1, "error": str(e)}

    removed: list = []
    failed: list = []
    for p in paths:
        try:
            os.remove(p)
            removed.append(p)
        except Exception as e:
            failed.append({"path": p, "error": str(e)})

    purge = _purge_index_for(removed)
    residual = 0
    for v in purge.values():
        try:
            residual += max(0, int(v.get("residual_chunks") or 0))
        except Exception:
            pass
    for p in removed:
        _recent_writes.pop(os.path.basename(p), None)

    base = os.path.basename(paths[0])
    logger.info(
        "[KBWrite] delete_reflection_card: %s (title=%r, md5_before=%s, bytes=%s) "
        "removed=%s failed=%s residual_chunks=%s",
        base, actual, str(before.get(paths[0], {}).get("md5", ""))[:8],
        before.get(paths[0], {}).get("bytes"), len(removed), len(failed), residual,
    )

    if not removed:
        return json.dumps({
            "success": False, "error": "删除失败：双根文件均未能移除", "details": failed,
        }, ensure_ascii=False)

    _short = _smart_truncate(_clean_card_title_for_receipt(actual), RECEIPT_TITLE_MAX)
    receipt = f"已删除反思卡《{_short}》"
    if SOP_RECEIPT_ENABLED and not _in_review_fork_thread():
        _enqueue_receipt_for_kw(f"{SOP_RECEIPT_PREFIX}{receipt}", kw)
    return json.dumps({
        "success": not failed,
        "index_clean": residual == 0,
        "deleted_files": removed,
        "failed_files": failed,
        "card_title": actual,
        "md5_before": before,
        "index_purge": purge,
        "residual_chunks": residual,
        "message": receipt,
        "note": "硬删不可恢复。索引已显式清理，residual_chunks 应为 0；若 >0 由 watcher 兜底重切。",
    }, ensure_ascii=False)


def save_reflection_handler(args: dict, **kw: Any) -> str:
    title = str(args.get("title") or "").strip()
    body = str(args.get("body") or "").strip()
    kind = str(args.get("kind") or "排障SOP").strip()
    tags = args.get("tags") or []
    force = bool(args.get("force"))

    if not title or not body:
        return json.dumps({"success": False, "error": "title 与 body 均不能为空"}, ensure_ascii=False)

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    is_dup, hits, note = _dedup_check(title, body)

    if is_dup and not force:
        if JUDGE_MODE:
            best = hits[0]
            cands = []
            for h in hits[:DUP_JUDGE_CANDIDATES]:
                real = _resolve_existing_card_path(h.get("path") or "") or (h.get("path") or "")
                h1s = _h1_titles_in_file(real) if real else []
                cands.append({
                    # title = 卡片 H1（判断「同不同题」看这个）；section = 命中的那一小节
                    # （判断「命中在讲什么」看这个）。分开给，模型不用从一段 heading_path 里拆。
                    "title": (h.get("title") or h.get("heading") or "").strip(),
                    # section 为空 = 命中在整卡正文（这张卡没有 ## 小节），不是缺失字段。
                    "section": (h.get("section") or "").strip(),
                    "heading_path": h.get("heading") or "",
                    # confirm_title = 文件首个 H1（delete 的校验口径）。多 H1 文件里它与 title
                    # 可能不同，所以单独给出让模型直接抄，别自己拿 title 去猜。
                    "confirm_title": h1s[0] if h1s else "",
                    # h1_count > 1 = 这个 .md 里合了多张卡，delete 会连带删掉其它卡 → 必须告警。
                    "h1_count": len(h1s),
                    "other_titles": h1s[1:6],
                    # score 是 rerank 分（只决定「算不算同题」，阈值 0.85）；
                    # rrf_score 才决定名次，列表顺序就是它的降序。
                    "score": h.get("score"),
                    "rrf_score": h.get("rrf_score"),
                    "path": h.get("path") or "",
                    "snippet": str(h.get("snippet") or "")[:DUP_SNIPPET_CHARS],
                })
            logger.info(
                "[KBWrite] dedup -> handed to AI for judgement: %r top(rrf)=%s candidates=%s",
                title, best.get("rrf_score"),
                [(c["title"][:40], c["path"]) for c in cands],
            )
            return json.dumps({
                "success": False,
                "action_required": "judge_duplicate",
                "duplicate_suspected": True,
                "title": title,
                "candidates": cands,
                "message": (
                    f"写卡前查重命中 {len(hits)} 张高分候选（按 rrf 排序，"
                    f"首名 rrf_score={best.get('rrf_score')}，"
                    f"rerank 分只用于过 0.85 闸门）。"
                    "每张候选都带 `title`（卡片 H1）、`section`（命中的小节；**为空表示命中在整卡正文，"
                    "这张卡没有 ## 小节**）、`snippet`（正文片段）"
                    "与 `confirm_title`（删卡时逐字填的那个）。"
                    "**先看 title 判「是不是同一张卡/同一个系统」，再看 section 判「命中在讲哪一块」，"
                    "最后用 snippet 核细节；不要只看分数，也不要只看 snippet 就下结论。**\n"
                    "1) 候选中已有同一主题/同一事故、且那张卡**仍然成立** → 不要写新卡，改用 "
                    "edit_reflection_card(card_path=<candidates[].path>, mode=\"insert\", section_title=..., section_body=...) "
                    "把本轮新内容追加到那张卡；\n"
                    "2) 候选卡**本身是错的**（结论被证伪 / 根因写错 / 占位空壳 / 已被更完整的新卡取代）→ "
                    "**不要 insert 给它续命**（那会让错结论继续被检索到），用 "
                    "delete_reflection_card(card_path=<path>, confirm_title=<candidates[].confirm_title 逐字>) "
                    "删掉，再写正确的新卡；\n"
                    "3) 确认是新主题（只是领域相近、结论不同）→ 再次调用 save_reflection 并带 force=true 写入；\n"
                    "4) 拿不准 → 不写，并在回复里一句话说明为什么放弃。"
                ),
            }, ensure_ascii=False)
        if DRY_RUN:
            logger.info("[KBWrite] DRY-RUN: would have rejected duplicate %r; hits=%s", title, hits)
        else:
            logger.info("[KBWrite] duplicate rejected: %r hit %s", title,
                        [h.get("path") or h.get("existing_card") for h in hits[:3]])
            best = hits[0]
            existing = best.get("path") or best.get("existing_card") or ""
            return json.dumps({
                "success": False,
                "duplicate": True,
                "title": title,
                "existing_card": existing,
                "existing_heading": best.get("heading", ""),
                "score": best["score"],
                "all_hits": hits,
                "message": (
                    f"知识库已有同题卡（相似度 {best['score']}）：{existing}。"
                    "请勿重复沉淀 —— 正确做法是在该卡的合适小节追加修正（注明「修正 <日期>」），"
                    "或确认这是不同主题后带 force=true 重写。"
                ),
            }, ensure_ascii=False)

    # 落卡
    stamp = time.strftime("%Y-%m-%d-%H%M")
    filename = f"{stamp}-{_slugify(title)}.md"
    tag_line = ""
    if isinstance(tags, list) and tags:
        tag_line = "tags: [" + ", ".join(str(t) for t in tags) + "]\n"
    card = (
        f"# [{kind}] {title}\n\n"
        f"- **记录时间 / 生效日期**：{ts} (UTC+8)\n"
        f"{tag_line}"
        f"- **来源**：主AI 收尾反思（hermes-kb-write）\n\n"
        f"{body}\n"
    )
    ok, path, md5 = _write_dual_root(filename, card)
    if not ok:
        return json.dumps({"success": False, "error": f"双根 md5 校验失败: {path}"}, ensure_ascii=False)

    _recent_writes[filename] = {"title": title, "path": path, "ts": time.time()}
    logger.info("[KBWrite] card written: %s (md5=%s)", path, md5)

    # 回执只发一行（用户拍板格式）。i18n 前缀「💾 自我改进回顾：」由原生
    # display.review.summary_callback 自动加，这里只放正文。
    # 路径 / md5 / 查重 / 索引等细节留在工具返回的其它字段里，不进群消息。
    _short = _smart_truncate(_clean_card_title_for_receipt(title), RECEIPT_TITLE_MAX)
    receipt = f"已沉淀反思卡《{_short}》"
    # 主 AI 自己写的卡**同样要回执**：handler 在**主 turn 内**同步执行，入队后
    # 由 finalize_turn 包装器的 _drain_pending_receipts 在**同一轮**收尾冲刷 →
    # 与主回复同批投递，不必等下一轮（也就绕开了 fork 那条 generation 门控）。
    # fork 线程里不重复入队：那两条链各自的发布器负责。
    if SOP_RECEIPT_ENABLED and not _in_review_fork_thread():
        _enqueue_receipt_for_kw(f"{SOP_RECEIPT_PREFIX}{receipt}", kw)
        logger.info("[KBWrite] receipt enqueued by main-AI write: %s", receipt)
    return json.dumps({
        "success": True,
        "staged": False,
        "target": "reflection",
        "title": title,
        "path": path,
        "md5": md5,
        "filename": filename,
        "duplicate_check": note,
        "message": receipt,
    }, ensure_ascii=False)


def _patch_review_notify_tools() -> None:
    """让 background_review 的原生回执拾取 save_reflection 的结果。

    核心源码里 notify_tools 是函数内局部集合 ``{"memory", "skill_manage"}``，且
    ``summarize_background_review_actions`` 会跳过不在 ``call_details`` 里的工具消息
    —— 不补这一步，fork 写的卡不会出现在人面回执里（只写文件、用户看不到）。

    这里在**运行时**替换 ``_collect_review_call_details``，把 save_reflection 纳入白名单；
    不修改 Hermes 源码文件本身。
    """
    try:
        from agent import background_review as br
    except Exception as e:
        logger.debug("[KBWrite] background_review not importable: %s", e)
        return
    orig = getattr(br, "_collect_review_call_details", None)
    if orig is None or getattr(orig, "_kbwrite_patched", False):
        return

    def _collect(review_messages):
        all_ids, details = orig(review_messages)
        try:
            for msg in review_messages or []:
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                for tc in msg.get("tool_calls", []) or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function", {}) or {}
                    _tname = fn.get("name")
                    if _tname not in ("save_reflection", "edit_reflection_card"):
                        continue
                    tcid = tc.get("id")
                    if not tcid:
                        continue
                    all_ids.add(tcid)
                    try:
                        a = json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        a = {}
                    if _tname == "edit_reflection_card":
                        # mode=find 是只读检索（零副作用、零回执）：它的返回 message 是
                        # **给模型看的操作指引**（「找到 N 张候选卡…不要再写占位卡换路径。」），
                        # 一旦被当成「动作行」就会拼进群里的畸形回执（2026-10-08 用户截图实证）。
                        if str(a.get("mode") or "insert").strip() == "find":
                            all_ids.discard(tcid)
                            continue
                        details[tcid] = {
                            "tool": "edit_reflection_card",
                            "title": a.get("section_title", "")
                            or os.path.basename(str(a.get("card_path") or "")),
                        }
                    else:
                        details[tcid] = {"tool": "save_reflection", "title": a.get("title", "")}
        except Exception:
            logger.debug("[KBWrite] notify whitelist patch failed", exc_info=True)
        return all_ids, details

    _collect._kbwrite_patched = True
    br._collect_review_call_details = _collect
    logger.info("[KBWrite] patched review notify whitelist to include save_reflection")

    # 再补 _action_lines：让 save_reflection 的 message 原样回传。
    # 原生判据是 `if "created" in message.lower() or "updated" in ...: return [message]`，
    # 否则对无 target 的非技能工具 `return []` —— 回执完全依赖 message 里那个英文单词，
    # 文案一改回执就静默消失（不报错、工具照常成功）。这里改成按 tool 名直通。
    # 注意：_action_lines 在 background_review.py:761 是同模块直接调用，patch 模块属性生效。
    orig_lines = getattr(br, "_action_lines", None)
    if orig_lines is not None and not getattr(orig_lines, "_kbwrite_patched", False):
        def _action_lines(data, detail, verbose):
            if isinstance(detail, dict) and detail.get("tool") in ("save_reflection", "edit_reflection_card"):
                msg = data.get("message", "") if isinstance(data, dict) else ""
                # 形态白名单（2026-10-08）：只放行**真正的写卡回执** —— 以「已」开头
                # 且长度受限。防的是非回执文本（只读 find 的指引、判重提示等）被当成
                # 执行行拼进回执发给用户；命中丢弃并告警，绝不外发。
                if isinstance(msg, str) and msg.startswith("已") and len(msg) <= 200:
                    return [msg]
                if isinstance(msg, str) and msg.strip():
                    logger.warning("[KBWrite] 丢弃疑似非回执行文本（len=%d head=%r）",
                                   len(msg), msg[:40])
                return []
            return orig_lines(data, detail, verbose)
        _action_lines._kbwrite_patched = True
        br._action_lines = _action_lines
        logger.info("[KBWrite] patched _action_lines to pass reflection receipts through")


# 必须注入核心列表的工具（顺序即日志顺序）。
# 2026-10-07 owner 指令「简化工具，只留新建、删除、修改」：工具集从 5 条收敛为 3 条 ——
# append_to_reflection 与 find_reflection_card 两条工具名**彻底摘除**，
# 能力并入 edit_reflection_card 的 mode=find / mode=insert。
CORE_TOOL_NAMES = (
    "save_reflection", "edit_reflection_card", "delete_reflection_card",
)


def _patch_toolsets_core() -> None:
    """把三条反思卡工具注入核心工具集（lancedb-kb-auto-inject 同款做法，幂等 + 防御）。"""
    try:
        import toolsets
        core = getattr(toolsets, "_HERMES_CORE_TOOLS", None)
        if isinstance(core, list):
            for name in CORE_TOOL_NAMES:
                if name not in core:
                    core.append(name)
            logger.info("[KBWrite] injected %s into _HERMES_CORE_TOOLS",
                        ", ".join(n for n in CORE_TOOL_NAMES if n in core))
        else:
            logger.warning("[KBWrite] toolsets._HERMES_CORE_TOOLS 不可用（类型 %s），跳过注入。"
                           "工具仍可通过 tool_search 找到。", type(core).__name__)
    except Exception as e:
        logger.warning("[KBWrite] could not patch toolsets: %s", e)


# ── 独立 SOP-only fork（与 Hermes 原生 skill/memory review 完全分离） ──────────
# 原生三个变体（_COMBINED/_SKILL/_MEMORY_REVIEW_PROMPT）只管 memory 与 skills，
# 本插件不再改它们一个字符；本 fork 只问一件事：本轮该不该沉淀一张 SOP 卡。
SOP_FORK_PROMPT = """你是一次**只负责反思卡沉淀**的收尾复查（独立于记忆/技能复查，互不干涉）。

判断本轮对话是否产生了具备长期复用价值的技术成果，若有则调用 `save_reflection` 落一张卡；
若没有，**什么都不要做**，直接结束。

## 该写（命中任一）
- 排障与根治：定位并根治了系统 / 服务 / 脚本 / 网络的 Bug、Crash、性能卡顿、死锁；
- 工具与流水线开发：从零搭建或重构了自动化工具、CLI、复合调用流水线；
- 规约与适配补丁：实装了协议适配、平台差异兜底、配置安全调优、插件层改造。

## 不该写（宁可什么都不写）
- 查状态、读文件、列目录、纯问答、闲聊；
- 中途无结论的步骤；已经开始但还没验证成功的尝试。

## 调用方式
save_reflection(title="...", body="...", kind="排障SOP", tags=["..."])

- `title`：必填，须含核心技术术语与目标对象。禁用「修复了一个 bug」这类空泛标题。
- `body`：必填，Markdown，必须含四节：
  1. **触发场景 / 业务背景** —— 什么现象、什么任务引发
  2. **根本原因与核心设计** —— root cause 与机制，带代码/配置证据
  3. **标准解决步骤 / CLI使用 / 关键代码** —— 可复现的命令与参数
  4. **避坑铁律与硬性约束** —— 走死的路、血泪教训
- `kind`：四选一 —— 排障SOP / 经验 / 工具链路 / 规约SOP
- `tags`：可选字符串数组

## 硬性要求
- **严禁占位卡**：任何时候都不要用 `save_reflection` 写「占位 / 登记 / 待填 / 无效」这类空壳卡。
  那不是「先占个位」，而是**真写进双根、真进索引**的垃圾卡（历史事故：两张标题为「占位」的空卡
  以 0.86 分进入过「当前执行依据」，事后要双根+索引一起清）。要把内容追加到旧卡却没有路径时，
  用 `edit_reflection_card(query="…", mode="find")` 拿路径，再用 `mode="insert"` 写。
- **失败路径最值钱**：正文必须写清「先试了什么、为什么不行、最后怎么成的」。
  只写成功路径的卡在检索时几乎没用。
- **查重命中时由你自己判断**：工具会先做语义检索；命中高分候选时返回
  `action_required="judge_duplicate"` 与 `candidates`（`title`=卡片 H1、`section`=命中的小节、
  `snippet`=正文片段、分数）。**先看 title 判「是不是同一张卡/同一个系统」，再看 section 判
  「命中在讲哪一块」，最后用 snippet 核细节；不要只看分数。**
  ① 卡片仍然成立、只是没覆盖本轮新情况 → **不要写新卡**，改用
      `edit_reflection_card(card_path=<candidates[].path>, mode="insert", section_title="...", section_body="...")`
     把本轮的新内容追加到那张卡（会自动带「修正 <日期>：」前缀）；
     **若你判断库里已有同题卡但这次查重没命中、手上没有路径** → 先调只读工具
     `edit_reflection_card(query="关键词", mode="find")` 拿候选卡的**绝对路径**，再 insert。
     **query 必须带「域词」**：本轮事故/成果的**专有名词、主机名、服务名、文件路径、命令、端口、
     配置键、cron jobid**（如 `thinpool-guard`、`tpool0`、`SNAPGATE`、`restic`、`/etc/tomato`、
     `50458cb4c0c3`）。**严禁只写抽象模式词**（`哨兵 / 告警 / 推送 / no_agent / cron / watchdog /
     零输出 / 闸门`）—— 这类词在任何监控类卡上都高分，会把域外卡排到第一（历史事故：用
     `no_agent cron 哨兵 零输出 告警推送` 去 find，0.9673 命中一张**结算计费**卡，把 thin pool 内容
     插了进去）。加域词后重查，若第一名换成了真正同域的卡，用那张。
  ② 候选卡**本身是错的**（结论已被本轮证伪 / 根因写错 / 占位空壳 / 同题重复且已被更完整的卡覆盖）
     → **不要 insert 给它续命，那只会让错的结论多活一天**。后台线程没有删卡权：**不写卡**，
     在收尾说明里明确写「建议删除 `<candidates[].path>`（H1：<title>），原因：…」，由主 AI 执行删除。
  ③ 确为新主题（只是领域相近）→ **再调一次 `save_reflection` 并带 `force=true`** 写入；
  ④ 拿不准 → 不写，并在收尾说明里写一句理由。**禁止只看分数就放弃。**
- **insert 前必须核对「主题域」**：读到候选卡后，**先看它的 H1 标题与首段在讲哪个系统/哪件事**，
  再问「我要插的这段，和这张卡是同一个系统、同一起事故、同一条流水线吗？」
  - 同一个系统/同一起事故 → 才允许 insert；
  - **只是「机制相似 / 都是监控 / 都是哨兵」但系统不同（如 LVM 薄池 vs CCH 计费）→ 禁止 insert**，
    改用 `save_reflection(..., force=true)` 另立新卡（域不同 = 新主题，哪怕机制再像）。
  - **分数高不等于同域**。0.95+ 只说明「讲的是同类问题」，不说明「讲的是同一个东西」。
    误插会让两张卡都变脏、检索时互相污染，比多写一张新卡代价大得多。
- 只做这一件事。不要改记忆、不要改技能。除 `save_reflection` / `edit_reflection_card`
  （**只允许 mode="find" 与 mode="insert"**）/ 只读工具外，不要调用其他工具。
- **禁止 `mode="replace"` 与 `mode="delete"`** —— 那会改到卡里已有正文，后台线程没有这个权限（handler 内会被硬拦）。
"""

# 每轮硬触发；置 0 可一键关停（省 token）。
SOP_FORK_ENABLED = (os.environ.get("KB_WRITE_SOP_FORK") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)
SOP_FORK_MAX_ITERS = int(os.environ.get("KB_WRITE_SOP_FORK_MAX_ITERS") or "8")

# 回执里标题的最大显示长度；超出则截断加省略号（群聊防刷屏，可调）。
RECEIPT_TITLE_MAX = int(os.environ.get("KB_WRITE_RECEIPT_TITLE_MAX") or "48")

# SOP 回执待发队列：fork 侧只入队，由下一轮 finalize_turn 同步冲刷后才真正投递。
# （迟到的回执会被网关的 generation 门控静默丢弃，详见 _drain_pending_receipts。）
# 队列条目 = (入队时刻, 文本, 来源聊天键)。
#
# **来源聊天键是 2026-10-07 跨聊天串台事故的修复**：原先条目只有 (时刻, 文本)，
# 而 drain 又整队列 clear() → 谁先收尾谁把别人聊天的回执投给自己。
# 实测事故：私聊会话的 fork 回执（15:54:48 入队）被群聊会话的收尾（15:55:25）带走，
# 15:55:27 投进了群。空字符串 = 未打标（主 AI 同轮入队，由当轮自己冲刷）。
_PENDING_RECEIPTS: list = []
_PENDING_RECEIPTS_LOCK = threading.Lock()
RECEIPT_QUEUE_TTL = float(os.environ.get("KB_WRITE_RECEIPT_TTL") or "1800")
STATE_DB_PATH = os.environ.get("KB_WRITE_STATE_DB") or os.path.expanduser("~/.hermes/state.db")
_RECEIPT_CHAT_KEY_CACHE: dict = {}

# SOP 回执前缀。刻意不复用原生 i18n 模板 display.review.summary_callback
# （那条是「💾 自我改进回顾：」，属于 memory/skills 原生 fork）——两条链的前缀分开，
# 一眼能看出这条回执来自 SOP 沉淀而不是记忆/技能更新。改措辞只需动这一行。
# 回执前缀（emoji + 空格）。刻意不用原生的 💾 —— 那个是 memory/skills 回执的标识，
# 两条链要能一眼分辨。想换图标：设 KB_WRITE_RECEIPT_PREFIX，或直接改这一行。
SOP_RECEIPT_PREFIX = os.environ.get("KB_WRITE_RECEIPT_PREFIX") or "📝 "

# SOP 回执独立开关。刻意不复用 display.memory_notifications —— 那个开关管的是原生
# memory/skills 回执，若共用，把它设成 off 会让 SOP 回执一起静默（两条链又耦合了）。
SOP_RECEIPT_ENABLED = (os.environ.get("KB_WRITE_RECEIPT") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)


def _in_review_fork_thread() -> bool:
    """当前是否跑在某个 background review fork 线程里。

    fork（``kb-sop-fork`` / 原生 ``bg-review``）写的卡，其回执由各自的发布链负责
    （SOP 链走 `_publish_sop_summary`，原生链走 `_action_lines` 白名单补丁）；
    handler 里再入队一次会出**两条**，所以要挡掉。
    """
    name = threading.current_thread().name or ""
    return ("kb-sop-fork" in name) or ("bg-review" in name)



def _thread_tool_restricted() -> bool:
    """当前线程是否处于**工具白名单限制**下（SOP fork / 原生 bg-review 等后台线程）。

    为什么不能只看线程名：``hermes_cli/plugins.py`` 的白名单只按**工具名**拦截
    （``allowed is not None and tool_name not in allowed`` → block），**不解析 mode**。
    所以 ``edit_reflection_card`` 一旦进 fork 白名单，fork 就能拿它做 replace/delete。
    唯一可靠的 mode 级硬闸在 handler 内部 —— 用同一个 threading.local 判定"受限线程"。
    """
    try:
        from hermes_cli import plugins as _hp
        wl = getattr(_hp, "_thread_tool_whitelist", None)
        if wl is not None and getattr(wl, "allowed", None) is not None:
            return True
    except Exception:
        logger.debug("[KBWrite] thread whitelist probe failed", exc_info=True)
    return _in_review_fork_thread()


def _chat_key_for_session(session_id: Any) -> str:
    """session_id → **聊天级**密钥（``source:chat_id``）；拿不到则退回 ``sid:<id>``。

    为什么按聊天而不是按会话：压缩/换会话会产生新 session_id，但用户看的还是同一个
    聊天窗口 —— 按会话分桶会把「同一聊天的回执」判成别人的而永不投递。
    只读 SELECT，结果按 session_id 缓存（一个 session_id 的归属不会变）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    cached = _RECEIPT_CHAT_KEY_CACHE.get(sid)
    if cached:
        return cached
    key = f"sid:{sid}"
    try:
        import sqlite3
        con = sqlite3.connect("file:" + STATE_DB_PATH + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COALESCE(source, ''), COALESCE(chat_id, '') FROM sessions WHERE id = ? LIMIT 1",
                (sid,),
            ).fetchone()
        finally:
            con.close()
        if row and row[1]:
            key = f"{row[0]}:{row[1]}"
    except Exception as e:
        logger.warning("[KBWrite] receipt chat-key lookup failed for %s: %s", sid, e)
    _RECEIPT_CHAT_KEY_CACHE[sid] = key
    return key


def _receipt_kw_ids(kw: Any) -> tuple:
    """从 handler 的 ``**kw`` 取本会话标识，返回 ``(origin, sid)``。

    ``session_id`` 由 ``model_tools._execute_tool`` 注入，取值就是
    ``agent.session_id or ""``（agent_runtime_helpers 分发处显式传入），
    与 drain 侧 ``agent.session_id`` **逐字同源** —— 所以 handler 打标不会
    「打成别人家的聊天」。取不到就都返回空串：此时条目不外投，留队由 TTL 丢弃。
    """
    try:
        sid = str((kw or {}).get("session_id") or "").strip()
    except Exception:
        sid = ""
    return (_chat_key_for_session(sid) if sid else ""), sid


def _enqueue_receipt_for_kw(msg: str, kw: Any) -> None:
    """handler 侧入队：带本会话标识打标（见 ``_receipt_kw_ids``）。"""
    origin, sid = _receipt_kw_ids(kw)
    _enqueue_receipt(msg, origin=origin, sid=sid)


def _enqueue_receipt(msg: str, origin: str = "", sid: str = "") -> None:
    """把一条面向用户的回执挂进待发队列（由**它自己那个聊天**的收尾统一冲刷）。

    ``origin`` = 来源聊天键；``sid`` = 入队时的原始会话号（origin 解析降级成
    ``sid:<id>`` 时的兜底匹配键，也是「未打标条目只回自己会话」的判据）。
    两者都为空 = 无归属线索：**留队**到 TTL 丢弃，绝不投给任意聊天（宁可不发，
    不投错位置）。

    去重按 **(文本, 来源, 会话号) 三元对**：同一聊天内文本相同只留一条；不同聊天的
    同名回执必须各投各的，不能互相吞掉。
    """
    with _PENDING_RECEIPTS_LOCK:
        if any(m == msg and o == origin and s == sid
               for (_ts, m, o, s) in _PENDING_RECEIPTS):
            return
        _PENDING_RECEIPTS.append((time.time(), msg, origin, sid))
        while len(_PENDING_RECEIPTS) > 8:
            _PENDING_RECEIPTS.pop(0)


def _publish_sop_summary(agent: Any, actions: list) -> None:
    """SOP fork 专用回执发布：前缀由插件控制，不碰原生 memory/skills 回执。

    原生 ``_publish_review_summary`` 硬编码 i18n 前缀「💾 自我改进回顾：」，
    复用它会让两条 fork 的回执长得一模一样，且想改前缀就得改全局模板（污染原生）。
    """
    summary = " · ".join(dict.fromkeys(actions))
    msg = f"{SOP_RECEIPT_PREFIX}{summary}"
    # 只入队，不在这里直接发 —— 见 _drain_pending_receipts 的说明。
    # 必须带上来源聊天键：fork 属于哪个会话，回执就只回哪个聊天（否则会被别的聊天抢走）。
    sid = str(getattr(agent, "session_id", None) or "").strip()
    origin = _chat_key_for_session(sid)
    if not origin:
        logger.warning("[KBWrite] SOP receipt enqueued without origin chat key (session_id 缺失)")
    _enqueue_receipt(msg, origin=origin, sid=sid)


def _drain_pending_receipts(agent: Any) -> None:
    """冲刷积压的 SOP 回执 —— 必须在**同步的** turn 收尾里调用。

    为什么不能在 fork 线程里直接发：fork 是后台 daemon 线程跑 deepseek 8 轮，
    实测耗时约 2~5 分钟，期间用户往往又发了几条消息 → 网关 generation 已前进多轮。
    此时 ``agent.background_review_callback`` 的 send()/deliver() 里
    ``_status_live()`` = ``_ctx._run_still_current()``（比对 run_generation）
    会把迟到的回执判成「上一代 run 的残留」而 **静默 return**：
    无日志、无异常，所以现象是「回执明明生成了却从没进群」。
    （实测：journal 有回执原文、卡也确实写了，但群内零投递。）

    放在这里就与主回复同批投递：send() 先把消息挂进当轮 pending，
    主回复落地后由 post_delivery 钩子 release → 必达。
    """
    now = time.time()
    my_sid = str(getattr(agent, "session_id", None) or "").strip()
    me = _chat_key_for_session(my_sid)
    mine: list = []
    deferred = expired = orphan = 0
    with _PENDING_RECEIPTS_LOCK:
        keep: list = []
        for ts, msg, origin, sid in _PENDING_RECEIPTS:
            if now - ts > RECEIPT_QUEUE_TTL:
                expired += 1                     # 宁可少发，绝不投错聊天
                continue
            # 归属判定（2026-10-08 收紧，修「回执投错位置」）：
            #   1) 打标且聊天键命中本聊天 → 本轮投递；
            #   2) 未打标但入队者会话号就是本会话（逐字相等）→ 本轮投递
            #      （handler 在主 turn 内同步入队，session_id 与 drain 侧同源）；
            #   3) 其余一律**留队**：旧条件里的 `not origin` 会把别的聊天入队的
            #      未打标回执投到本聊天（实测串台），必须去掉。
            if origin and origin == me:
                mine.append(msg)
            elif not origin and sid and sid == my_sid:
                mine.append(msg)
            else:
                keep.append((ts, msg, origin, sid))
                if not origin and not sid:
                    orphan += 1                  # 无归属线索：只留队，不外投
                else:
                    deferred += 1
        _PENDING_RECEIPTS[:] = keep
    if orphan:
        logger.warning("[KBWrite] %d 条回执既无 origin 也无入队会话号，留队不外投（防串台）",
                       orphan)
    if expired:
        logger.warning("[KBWrite] %d 条回执超 TTL(%ss) 未等到来源聊天，已丢弃（不跨聊天投递）",
                       expired, int(RECEIPT_QUEUE_TTL))
    if deferred:
        logger.info("[KBWrite] %d 条回执来自其它聊天，留队等它自己那轮冲刷", deferred)
    # ★同文本二次去重（2026-10-07 修双投）：同一条回执会被两条链各入队一次 ——
    # handler 走未打标的 origin=""，fork 的 _publish_sop_summary 走打标 origin=<chat>。
    # 入队侧的去重键是 (文本, 来源) 成对（分桶需要它，否则不同聊天的同名回执会互相吞掉），
    # 所以这两条谁也吞不掉谁；而下面的取值条件又把两者都判归本聊天 ⇒ 用户收到两条一模一样的。
    # 故在**本批次内**按文本二次去重：只作用于本聊天这一批，不会跨聊天误伤
    # （那正是分桶要避免的退化）。取证见 journalctl 同秒同文本各 2 条。
    if len(mine) > 1:
        deduped = list(dict.fromkeys(mine))
        if len(deduped) != len(mine):
            logger.info(
                "[KBWrite] 本批回执同文本去重：%d → %d 条（同一条被 handler 与 fork 各入队一次）",
                len(mine), len(deduped),
            )
        mine = deduped
    if not mine or not SOP_RECEIPT_ENABLED:
        return
    logger.info("[KBWrite] 冲刷本聊天回执 %d 条（chat=%s）", len(mine), me or "unknown")
    cb = getattr(agent, "background_review_callback", None)
    for msg in mine:
        with suppress(Exception):
            agent._safe_print(msg)
        if cb:
            with suppress(Exception):
                cb(msg)


def _sop_fork_worker(agent: Any, messages_snapshot: list) -> None:
    """独立 SOP fork 的线程体（复刻原生 review 的四段仪式，但只跑 SOP 提示词）。

    与原生 fork 的隔离点：
      - 自己的 prompt（``SOP_FORK_PROMPT``），不碰原生三个变体一个字符；
      - 自己的工具白名单：只放 ``save_reflection``（外加只读 read_file/search_files）；
      - 自己的线程（``kb-sop-fork``），不占 parent 的 ``_background_review_run`` token，
        不会把原生 review 挤掉，也不会被它的 finish 误关。
    """
    if not SOP_FORK_ENABLED:
        return
    try:
        from agent import background_review as br
        from agent.thread_scoped_output import thread_scoped_silence
        from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
    except Exception as e:
        logger.warning("[KBWrite] sop fork imports failed: %s", e)
        return

    with suppress(Exception):
        br._set_thread_approval_callback(br._bg_review_auto_deny)
    review_agent = None
    try:
        # 与原生同参构建 fork：同 system prompt / 同 tools[] → 吃满 prompt cache
        review_agent, _rt, _routed = br.build_cache_parity_fork(
            agent, None, max_iterations=SOP_FORK_MAX_ITERS, write_origin="background_review",
        )
        review_agent._review_attended = False
        set_thread_tool_whitelist(
            {"save_reflection", "edit_reflection_card",
             "read_file", "search_files"},
            deny_msg_fmt=(
                "SOP fork denied non-whitelisted tool: {tool_name}. This fork only writes, "
                "appends or looks up reflection cards via save_reflection / "
                "edit_reflection_card (mode=find|insert only; replace/delete are "
                "hard-blocked inside the handler). Do not retry {tool_name}."
            ),
        )
        with thread_scoped_silence():
            review_agent.run_conversation(
                user_message=(
                    SOP_FORK_PROMPT
                    + "\n\nYou can only call save_reflection / edit_reflection_card "
                    "(mode=find or insert ONLY; replace/delete are hard-blocked) "
                    "(plus read_file/search_files). Other tools will be "
                    "denied at runtime — do not attempt them."
                ),
                conversation_history=(
                    br._digest_history(messages_snapshot) if _routed else messages_snapshot
                ),
            )
        # 回执必须在 silence 块**外**发：块内 stdout 被路由到 devnull，
        # _safe_print 会被吞（原生也是在 silence 之外发布 actions 的）。
        # 回执由本线程自己发 —— 原生 summarize 只扫原生线程的 fork 消息，看不见我们。
        fork_messages = list(getattr(review_agent, "_session_messages", []))
        try:
            # notification_mode 固定 "on"：不回读父 agent 的 memory_notifications，
            # 否则用户在 display 里关掉 memory 通知会连带静音 SOP 回执。
            actions = br.summarize_background_review_actions(
                fork_messages, messages_snapshot, notification_mode="on",
            )
        except Exception as e:
            logger.warning("[KBWrite] sop fork summarize failed (treating as empty): %s", e)
            actions = []
        if actions and SOP_RECEIPT_ENABLED:
            _publish_sop_summary(agent, actions)
    except Exception as e:
        logger.warning("[KBWrite] sop fork failed: %s", e)
    finally:
        with suppress(Exception):
            clear_thread_tool_whitelist()
        with suppress(Exception):
            br._set_thread_approval_callback(None)
        if review_agent is not None:
            # usage 归属父会话（与原生同款，只写 session_model_usage，不动 transcript）
            with suppress(Exception):
                br._record_review_usage_to_parent(agent, br._snapshot_review_usage(review_agent))
            with suppress(Exception):
                br._release_fork_clients(review_agent)


def _patch_sop_fork() -> None:
    """把插件挂到每轮收尾：另起一条独立 SOP-only fork（原生 review 路径一字不改）。

    挂载点 ``agent.turn_finalizer.finalize_turn`` —— 每轮必调的收尾函数
    （``agent/conversation_loop.py:1684``），能拿到本轮 ``messages`` 快照。
    相比包装 ``AIAgent._spawn_background_review``（受 nudge 门控，可能不触发），
    这里才能真正做到「每轮结束都起」。

    必须同时替换两个模块属性：
      - ``agent.turn_finalizer.finalize_turn``（函数定义处）
      - ``agent.conversation_loop.finalize_turn``（**from-import 的局部绑定，真正被调用的**）
    且 wrapper 必须 ``functools.wraps`` —— 调用方用 ``inspect.signature`` 动态取参数名，
    裸 ``*args/**kwargs`` 会让参数表解析为空，原函数收到零参数直接崩。

    安全性：追加语义 —— 先原样跑原生 ``finalize_turn``（skill/memory review 流程不变），
    返回后再起我们的线程；任何异常都不影响原流程。
    """
    try:
        from agent import turn_finalizer as tf
    except Exception as e:
        logger.warning("[KBWrite] could not import turn_finalizer: %s", e)
        return
    orig = getattr(tf, "finalize_turn", None)
    if orig is None:
        logger.warning("[KBWrite] finalize_turn missing; SOP fork not installed")
        return
    if getattr(orig, "_kbwrite_sop_fork", False):
        logger.info("[KBWrite] SOP fork already installed (no-op)")
        return

    import functools
    import threading

    try:
        from tools.thread_context import propagate_context_to_thread
    except Exception:  # pragma: no cover - defensive
        propagate_context_to_thread = None

    @functools.wraps(orig)  # 保 __wrapped__，让 inspect.signature 仍看到真实参数表
    def finalize_turn_with_sop_fork(*args, **kwargs):
        # 防递归：fork 自己也会走收尾路径 —— 用 _persist_disabled / 线程名认出来直接放行
        agent = kwargs.get("agent") or (args[0] if args else None)
        is_fork = bool(getattr(agent, "_persist_disabled", False)) or (
            threading.current_thread().name == "kb-sop-fork"
        )
        result = orig(*args, **kwargs)
        if not is_fork and agent is not None:
            # 冲刷上一轮积压的 SOP 回执：此刻 generation 有效，回执随本轮主回复一起送达。
            _drain_pending_receipts(agent)
        try:
            snapshot = kwargs.get("messages")
            if (
                not is_fork
                and snapshot
                and not kwargs.get("interrupted")
                and not kwargs.get("failed")
                and not getattr(agent, "skip_background_review", False)
            ):
                # 上下文传播挂在线程 target 上（对齐原生 bg-review 的做法）。
                # 绝不能套在这层 wrapper 外面：propagate_context_to_thread 的 _runner
                # 是裸 *args/**kwargs 且未用 functools.wraps，会让调用方的
                # inspect.signature 解析出 args/kwargs 假参数名 → getattr(state, "args")
                # 直接崩，每轮收尾都炸。
                target = _sop_fork_worker
                if propagate_context_to_thread is not None:
                    with suppress(Exception):
                        target = propagate_context_to_thread(_sop_fork_worker)
                threading.Thread(
                    target=target, args=(agent, list(snapshot)),
                    daemon=True, name="kb-sop-fork",
                ).start()
        except Exception as e:
            logger.debug("[KBWrite] sop fork spawn skipped: %s", e)
        return result

    finalize_turn_with_sop_fork._kbwrite_sop_fork = True
    tf.finalize_turn = finalize_turn_with_sop_fork
    targets = ["agent.turn_finalizer"]
    try:
        from agent import conversation_loop as cl
        if getattr(cl, "finalize_turn", None) is orig:
            cl.finalize_turn = finalize_turn_with_sop_fork
            targets.append("agent.conversation_loop")
    except Exception as e:
        logger.warning("[KBWrite] could not rebind conversation_loop.finalize_turn: %s", e)
    logger.info("[KBWrite] installed independent SOP-only fork on finalize_turn (%s)", ", ".join(targets))


REFLECTION_RULES = """## 收尾反思（save_reflection）

每轮任务收尾时自检一次：本轮是否产生了具备长期复用价值的技术成果？若有，调用 `save_reflection` 沉淀一张反思卡。

该调用（命中任一即可）：
- 排障与根治：定位并根治了系统 / 服务 / 脚本 / 网络的 Bug、Crash、性能卡顿、死锁；
- 工具与流水线开发：从零搭建或重构了自动化工具、CLI、复合调用流水线；
- 规约与适配补丁：实装了协议适配、平台差异兜底、配置安全调优、插件层改造。

不该调用（宁可什么都不写）：
- 查状态、读文件、列目录、纯问答、闲聊；
- 中途无结论的步骤；已经开始但还没验证成功的尝试。

硬性要求：
- **查重命中时由你判断（不替你下结论）**：`save_reflection` 返回 `action_required=judge_duplicate` 时会给 `candidates`，每条含 `title`（卡片 H1）、`section`（命中在哪个小节）、`snippet`（正文片段）、`confirm_title`（删卡要逐字填的那个）、分数。**先看 title 判是否同一张卡/同一系统，再看 section 判命中讲哪块，最后用 snippet 核细节——不要只看分数。**判断出口：① 卡仍然成立、只是没覆盖本轮新情况 → **不要写新卡**，用 `edit_reflection_card(card_path, mode="insert", section_title, section_body)` 插到那张卡（默认插末尾；给了 `after` 锚点可插到卡内任意位置）；② 卡本身是错的 → **别 insert 续命，直接删**（见下一条）；③ 确为新主题 → 带 `force=true` 重写；④ 拿不准 → 不写并说明理由。
- **严禁占位卡 / 拿路径用 edit_reflection_card(mode="find")**：不要用 `save_reflection` 写「占位 / 登记 / 待填 / 无效」这类空壳卡（会真写进双根与索引，污染检索）。要把内容追加到旧卡但没有路径时，用 `edit_reflection_card(query="关键词", mode="find")` 拿候选卡的绝对路径（只读，不发回执），再用 `mode="insert"` 写。
- **有问题的卡就该删，别用 insert 给它续命**：`delete_reflection_card` 是正常维护动作，不是「最后手段」。命中候选里出现以下任一情况 → **直接删**，然后在同一张正确的新卡上重新沉淀：
  · 结论已被本轮证伪（老卡说的做法现在会坏事）；
  · 根因写错、会把人带偏（比「没这张卡」危害更大）；
  · 占位卡 / 空壳卡 / 只有标题没有实证的卡；
  · 同题重复卡：内容已被更完整的那张覆盖，留着只会互相污染检索；
  · 卡里指的路径 / 命令 / 配置键 / 服务名已不存在，且没有历史教训价值。
  **什么情况才不该删**：卡是对的、只是没覆盖本轮新情况 → 用 `mode="insert"` 追加修正节（这是首选）；卡片个别字句错、主体仍成立 → `mode="replace"`；拿不准该不该删 → 先 `edit_reflection_card(query="…", mode="find")` 看全文，仍然拿不准就不删、并说明理由。
- **删卡三步，缺一不可**：① `edit_reflection_card(query="域词", mode="find")` 确认目标卡（看 H1 与首段，确认是同一个系统/同一起事故）；② `delete_reflection_card(card_path=<该卡 path>, confirm_title=<该卡 H1 逐字，标题分类前缀如「[排障SOP] 」也要带上>)`；③ 删完可写正确的新卡，或把「删了哪张、为什么」一句话回给用户。**硬删不可恢复，没有回收站**，所以 confirm_title 必须逐字，不要自己重排/简写标题。
- **失败路径最值钱**：正文必须写清「先试了什么、为什么不行、最后怎么成的」。只写成功路径的卡在检索时几乎没用。
- 标题必须含核心技术术语与目标对象，禁用「修复了一个 bug」这类空泛标题。
"""


def register(ctx: Any) -> None:
    _patch_toolsets_core()
    _patch_sop_fork()
    _patch_review_notify_tools()
    try:
        ctx.register_system_prompt_section(
            "kb-write-reflection-rules", REFLECTION_RULES, position="after_memory",
        )
        logger.info("[KBWrite] registered reflection rules system prompt section")
    except Exception as e:
        logger.warning("[KBWrite] could not register system prompt section: %s", e)
    ctx.register_tool(
        name="save_reflection",
        # 归属必须是 toolsets.TOOLSETS 里的合法名：fork 的工具白名单来自
        # get_tool_definitions(enabled_toolsets=["memory","skills"])，靠
        # resolve_toolset(..., include_registry=True) 从 registry 反查该 toolset
        # 下注册的工具。用非法名（如 "knowledge"）会让 fork 看不到本工具。
        toolset="memory",
        schema=SAVE_REFLECTION_SCHEMA,
        handler=save_reflection_handler,
        check_fn=lambda: True,
        description="沉淀本轮的技术反思卡到知识库（写前自动查重；命中候选项时由你读片段判断是否同题）",
        emoji="📥",
    )
    # 改 / 删（2026-10-07 owner 指令）。fork 白名单只放 save_reflection 与 edit_reflection_card；
    # edit 的 mode=replace/delete 在 **handler 内部**硬拦 —— 因为 hermes_cli 的白名单只按工具名拦，
    # 不解析 mode，放进白名单 ≠ 安全。delete_reflection_card 绝不进白名单。
    ctx.register_tool(
        name="edit_reflection_card",
        toolset="memory",
        schema=EDIT_REFLECTION_SCHEMA,
        handler=edit_reflection_card_handler,
        check_fn=lambda: True,
        description=("修改已有反思卡：mode=find 只读找卡 / insert 插入小节 / "
                     "replace 精确覆盖 / delete 删掉片段（双根同步）"),
        emoji="🖊️",
    )
    ctx.register_tool(
        name="delete_reflection_card",
        toolset="memory",
        schema=DELETE_REFLECTION_SCHEMA,
        handler=delete_reflection_card_handler,
        check_fn=lambda: True,
        description="硬删除一张反思卡（双根 + 索引一起清，不可恢复；confirm_title 须逐字等于 H1）",
        emoji="🗑️",
    )
    logger.info(
        "[KBWrite] registered save_reflection (dedup_threshold=%s, rerank_top_n=%s, dry_run=%s, "
        "judge_mode=%s, judge_candidates=%s, snippet_chars=%s)",
        DUP_THRESHOLD, DUP_RERANK_TOP_N, DRY_RUN, JUDGE_MODE, DUP_JUDGE_CANDIDATES, DUP_SNIPPET_CHARS,
    )
    logger.info("[KBWrite] registered edit_reflection_card (4-in-1: find/insert/replace/delete; "
                "whitelisted for SOP fork but replace/delete hard-blocked in-handler)")
    logger.info("[KBWrite] registered delete_reflection_card "
                "(main-AI only; deliberately NOT whitelisted for SOP fork)")
