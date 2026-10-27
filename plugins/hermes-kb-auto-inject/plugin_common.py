"""hermes-kb-auto-inject · 常量 / 正则 / 工具函数 / 协议段。

从原 1502 行单文件拆出：这里只放「不依赖插件实例」的部分。
`KBAutoInjectPlugin` 类在 `.plugin_class`，schema + handler + 注册在 `.registration`。
"""

import json
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# 唯一的元参数：每一次工具调用都要写，本插件用它取知识库检索词，动手前剥掉。
# 曾经还有 kb_open / kb_keep 两个兄弟参数 + 「一条调用只能写一个」的单槽闸门，
# 后来又有一个专门的 open_knowledge 展开工具 —— 全都删了：清单里直接给
# `read_file` 现成的行号，正文由 read_file 取，插件既不需要第二个工具，
# 也不需要持有任何一块原文。
KB_FIELD = "kb_query"

# 一屏最多印几张卡 + 这一屏的字数预算。
# 清单的用途是「零成本筛相关性」，不是把相似度前 25 名全倒出来：实测一次 kb_query
# 命中 22 张同主题卡，全倒出来 2,400 字，而值得读的通常 1~2 张。
# **控制手段是少印几张，不是把每张卡的标题/链路截断**（截断省下的正是判断依据）。
# 标题与命中链一个字不省，靠 `MENU_BUDGET` 兜底：超预算就从尾部撤几张，
# 抬头照实写「另有 N 张相近卡没印」。
MENU_MAX_CARDS = 6
MENU_BUDGET = 4000

# Markdown 标题行：`## 二、xxx`。用于从块头往上找「这一节从哪开始」。
_HDR_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# dispatch 前要从入参里剥掉的键。第一个是本插件自己的元参数；
# 后三个是旧 doing 协议的字段（协议已删，检索词只走 kb_query），**但不能跟着删**：
# 旧会话仍可续用，其历史里 assistant 的 tool_call 带着这些键，模型会照着续写，
# 裸传给底层工具就是「未知参数」报错。实测会话存档文件里仍有 12 份
# 请求转储带 `"doing":` 键（2026-08-27 ~ 09-28 的长会话）。剥离成本只是一次元组
# 成员判断，留着是保险，不是尸体。
_STRIP_KEYS = (KB_FIELD, "doing", "execution_state", "_hermes_execution")

# 豁免名单：这些工具不校验 kb_query。
# 后台收尾线程（SOP fork / 反思卡沉淀）的 prompt 里可能没有协议提示词，
# 硬拦只会把知识库沉淀静默吞掉（实测被拦 12 次）。
_EXEMPT_TOOLS = frozenset({
    "save_reflection",
    "edit_reflection_card",
    "delete_reflection_card",
})

# 这些工具既不注入 kb_query 字段，也不受 kb_query 门禁管：
#   · 上面三个沉淀工具（后台收尾线程的 prompt 里没有协议）
#   · （历史上的 open_knowledge）—— 它干的是「展开」，不是「检索」，带检索词自相矛盾
#   · search_knowledge —— 它自己就是查库入口，检索词走它自己的 query 参数
_NO_KB_FIELD = _EXEMPT_TOOLS | frozenset({"search_knowledge"})


def _is_guard_layer(value: Any, want_name: str) -> bool:
    """闭包里的这个 cell 是不是「我们要继续往下挖的那一层」。

    只按名字认 ``dispatch`` / ``get_definitions`` 是不够的：本插件自己的 wrapper
    叫 ``patched_dispatch`` / ``patched_get_definitions``，热重载时旧 wrapper 会被
    当成「原方法」捕获进 cell。旧版 wrapper 没有标记属性，所以按名字结构认
    ``patched_<want_name>``；以后若改名，装上 ``_kb_guard_wrapper`` 属性即可。
    """
    name = getattr(value, "__name__", "")
    if name == want_name:
        return True
    if name == "patched_" + want_name:
        return True
    return bool(getattr(value, "_kb_guard_wrapper", False))


def _innermost(fn: Any, want_name: str) -> Any:
    """沿闭包往下挖，拿到**真正的原始方法**（不是本插件上一版装的 wrapper）。

    为什么必须挖：wrapper 是「校验 → 剥掉 kb_query 等元参数 → 转交原方法」。
    热重载时若把上一版 wrapper 当成原方法，就会变成
    「外层剥完 → 内层再拿剥空的 args 校验 kb_query」→ 永远 null → 所有工具被拦。
    判据见 ``_is_guard_layer``：真方法 ``dispatch`` 与本插件的 ``patched_dispatch``
    都算「中间层」，一直挖到闭包里没有这两者为止。
    """
    for _ in range(8):
        nxt = None
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if value is not fn and _is_guard_layer(value, want_name):
                nxt = value
                break
        if nxt is None:
            return fn
        fn = nxt
    return fn


def _resolve_original(current: Any, want_name: str, recorded: Any) -> Any:
    """决定「真·原方法」：闭包链挖到底优先，只有链外的外部注入才认类属性。

    三种情形：
      * 正常热重载：recorded 就是真原方法，且必然在链上 → 挖到底，等于 recorded
      * 类属性被旧版 wrapper 污染：旧 wrapper 也在链上 → 挖到底，避开套娃自锁
      * 单测/宿主把类属性换成假实现（不在任何闭包链上）→ 尊重注入，
        否则 peek_guard_test.py 第 6 节没法用类属性换 fake。
    """
    deepest = _innermost(current, want_name)
    if recorded is None or recorded is deepest:
        return deepest
    cursor = current
    for _ in range(8):
        if cursor is recorded:
            return deepest  # 在链上 → 用挖到底的那个，绝不套娃
        nxt = None
        for cell in getattr(cursor, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if value is not cursor and _is_guard_layer(value, want_name):
                nxt = value
                break
        if nxt is None:
            break
        cursor = nxt
    return recorded  # 不在链上 = 外部注入


def _kb_targets(raw_args: Any, _depth: int = 0) -> List["Tuple[str, Dict[str, Any]]"]:
    """列出这条 dispatch 真正要校验的 (工具名, 入参) 对。

    走 `tool_call` 桥时外层是 {"calls":[{name, arguments}]}：外层自己没有 kb_query，
    真实调用在内层。不逐条看内层就会把整条桥误判成「漏写」，或者反过来放进一个
    内层没写的调用。**这是 kb_query 必填之后唯一的坑。**
    """
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(raw_args, dict):
        return []
    _calls = raw_args.get("calls")
    if _depth < 2 and isinstance(_calls, list):
        out: List["Tuple[str, Dict[str, Any]]"] = []
        for entry in _calls:
            if not isinstance(entry, dict):
                continue
            _inner = entry.get("name") or ""
            _args = entry.get("arguments")
            _sub = _kb_targets(_args, _depth + 1)
            if _sub:
                out.extend([(_inner or _n, _a) for _n, _a in _sub])
            elif isinstance(_args, dict):
                out.append((_inner, _args))
        return out
    return [("", raw_args)]


# 弃权字面：从前这些是「合法表态」，现在是纯坏值 —— 每一条调用都必须真检索。
_PUNT_WORDS = frozenset(
    ('""', "''", "“”", "「」", "none", "null", "n/a", "na", "no",
     "无", "空", "不要", "没有", "-", "—", "skip", "omit")
)


def _kb_query_is_usable(val: Any) -> bool:
    """kb_query 是否可用：非空、非弃权字面、至少 2 个字符。

    不做动作词过滤（会误伤「触发」「删除」这类带信息的词），
    也不做稀有度过滤（中文词 DF 恒为 0，会把专名挤掉，实测 3/3→0/3）。
    只挡真正的空填：空串 / 字面引号 / null / 弃权词 / 单字符。
    """
    if val is None:
        return False
    if not isinstance(val, str):
        val = str(val)
    s = val.strip()
    if not s:
        return False
    if s.lower() in _PUNT_WORDS:
        return False
    # 去掉标点后至少还剩 2 个字符，挡掉 "。" "…" "-" 这类填充
    core = "".join(ch for ch in s if ch.isalnum())
    return len(core) >= 2


def _short(text: Any, limit: int = 140) -> str:
    """Single-line, length-capped rendering used by diagnostic log lines."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)] + "…"


# ============ 用户原话 → 检索词 ============
#
# kb_query 是模型自拟的词，好不好取决于它当时已经懂了多少；用户原话是**现象本身**，
# 天然带任务对象（「rerank 会误杀相关块吗」「番茄下载器地址」）。所以每轮开跑之前
# 先拿用户原话检索一屏，不等模型自己想词 —— 这是 dispatch 层 kb_query 之外的第二个入口。
#
# 三个必须在这里挡掉的坑（都踩过）：
#   · 平台前缀 `[owner]<OpenID>` —— 约 45 个字符的纯噪声，短消息时占一半以上，
#     曾把「先重启」检索成跑题卡（那轮的结论就是「剥掉前缀」）
#   · 系统块 `<memory-context>` / `[System note: …]` —— 不是用户说的话，混进来等于
#     拿会话摘要去检索，只会捞到元讨论
#   · 零信息量短消息（「嗯」「继续」「做」）—— 检索必跑偏，宁可不检索
_USER_QUERY_MAX_CHARS = 400     # 送进检索的用户原话上限（超出取首尾各一半）
_USER_QUERY_MAX_CARDS = 3       # 用户路径这一屏最多印几张（成本闸门）
_USER_QUERY_MIN_CHARS = 6       # 少于此长度整条不检索

_USER_QUERY_NOISE_RE = re.compile(
    r"\[(?:owner|admin|user|system)\]\s*(?:<[^>\n]{0,64}>)?"
    r"|<memory-context>.*?</memory-context>"
    r"|\[System note:[^\]]{0,400}\]",
    re.S,
)
_USER_QUERY_BLOCK_RE = re.compile(
    r"<knowledge-base-[a-z]+>.*?</knowledge-base-[a-z]+>", re.S
)

# 整条都是这类词 → 不检索（判据是「整条」，不是「含」：含「继续」的长句照样有用）
_EMPTY_UTTERANCES = frozenset({
    "嗯", "嗯嗯", "哦", "好", "好的", "好滴", "行", "可以", "继续", "接着",
    "然后", "做", "干的", "干", "谢谢", "在吗", "在么", "?", "？", "。",
    "ok", "okay", "yes", "no", "收到", "明白",
})


def _user_query_text(raw: Any) -> str:
    """用户原话 → 可用的检索词。返回空串表示这条消息不值得检索。

    清洗只做减法（剥前缀、剥系统块、压空白），不改写、不抽关键词：
    用户原话本身已经是现象描述，拆词反而丢掉上下文。
    """
    text = _USER_QUERY_NOISE_RE.sub(" ", str(raw or ""))
    text = _USER_QUERY_BLOCK_RE.sub(" ", text)
    text = " ".join(text.split())
    if len(text) < _USER_QUERY_MIN_CHARS:
        return ""
    if text.strip("。，！？、,.!?~ 　").lower() in _EMPTY_UTTERANCES:
        return ""
    if len(text) > _USER_QUERY_MAX_CHARS:
        # 长消息两头都留：开头是任务对象，结尾常是报错原文/贴的命令
        half = _USER_QUERY_MAX_CHARS // 2
        text = f"{text[:half]} {text[-half:]}"
    return text


def _content_text(content: Any) -> str:
    """Extract searchable text from string or OpenAI content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                for key in ("text", "content", "value"):
                    value = part.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "\n".join(parts)
    return ""


PROTOCOL_PROMPT = """【工具调用协议：kb_query】

每一次工具调用都必须写 `kb_query`。它只干一件事：**把库里那张对的卡捞出来**，不是任务描述。

**形状：三段，用 `｜` 隔开，一段都不能空 —— `病灶现象｜目标对象｜专名`**

| 段 | 抄什么 |
|---|---|
| ① 病灶现象 | 你眼睛看见的原文：报错、字段值、状态、参数名 |
| ② 目标对象 | 哪个容器 / 服务 / 文件 / 模块 / 页面 |
| ③ 专名 | 这件事独有的 1~3 个标识符（字段名、参数名、路径、版本号） |

`URLs (0个) 未找到可用的下载链接｜traffic-consumer 容器｜--auto-remove-failed-url Config.Cmd`

字面只认四处：用户这番话里的产品名/文件名/报错、工具调用参数、工具返回值、本轮读到的文件。原样抄，大小写和 `-` `_` `.` 一个都别改。

**字面抄不出来，就是你还该先去看，不是先去检索。**

| 禁止 | 为什么 |
|---|---|
| 照抄用户原话 | 那是**要求**（去掉/优化/看看/有没有更好的），要求里没有检索词 |
| 动作词：去掉 / 优化 / 排查 / 修复 / 继续 / 确认 | 实测加 12 个动作词，0.9953 → 0.8226 |
| 评价词：更优雅 / 太慢 / 不正常 | 库里没人这么写 |
| 替库想象卡名 | 编一个库里没有的词：0.9995 → 0.3718，被阈值直接砍死 |
| `none` / `null` / 空串 / `无` | 没有「这条跳过」这种写法，一律打回 |

写完当场生效：这条工具结果后面立刻多一屏**标题清单**（标题、命中小节、绝对路径、
命中断在原文的行号）。要正文就 `read_file` 那个路径，行号照抄清单里的，别整卡乱读。
经 `tool_call` 桥调用时，`kb_query` 写在 `calls[].arguments` 内层。""".strip()
