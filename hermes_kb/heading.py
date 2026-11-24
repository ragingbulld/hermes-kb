"""标题层级串的拆分与还原 —— 全库唯一一份实现。

背景：``heading_path`` 存的是「H1 > H2 > H3」这样的层级串。历史上用
``" > "`` 拼、也用 ``" > "`` 拆，看起来对称，实际是错的 —— **标题本身就可能
含 ``" > "``**：

    [排障SOP] … 输出上限超出上游模型限制（64000 > 32768）

按 ``" > "`` 拆会把这一条切成两段，卡标题只剩「…（64000」，多出来的
「32768）」还会被当成一个假小节画进树里。

现在的口径有两条，合起来才成立：

1. **写入侧**用 :data:`PATH_SEP`（ASCII 单元分隔符 ``\\x1f``）拼层级。正文里
   不可能出现这个字符，所以拆出来的段数与层级数严格相等。
2. **读取侧**兼容老数据：没有 ``\\x1f`` 的串仍按 ``" > "`` 拆，但拆完要过一遍
   :func:`_rebalance` —— 靠括号是否闭合把被误切的两半合回去。括号平衡是确定性
   信号，不是猜：正常切开的老数据每段本来就平衡，不会被误合。

老数据要彻底干净只能重建索引；在重建之前，第 2 条让它们至少不再把卡标题切碎。
"""

from typing import Dict, List

# 标题层级的分隔符。ASCII 单元分隔符：正文里不可能出现，故不存在歧义。
PATH_SEP = "\x1f"

_OPEN = "（([【《"
_CLOSE = "）)]】》"


def join_heading_path(crumbs: List[str]) -> str:
    """把层级数组拼成 ``heading_path``。空数组返回调用方约定的占位串。"""
    return PATH_SEP.join(crumbs) if crumbs else "知识库文档"


def split_heading_path(heading_path: str) -> List[str]:
    """把 ``heading_path`` 拆成层级数组（从 H1 到最深一级）。

    * 新数据（含 ``PATH_SEP``）：按它拆，零歧义。
    * 老数据（``" > "`` 拼接）：按 ``" > "`` 拆后过 :func:`_rebalance`。

    空串返回空列表，调用方自己决定兜底。
    """
    hp = str(heading_path or "")
    if not hp:
        return []
    if PATH_SEP in hp:
        return [x.strip() for x in hp.split(PATH_SEP) if x.strip()]
    return _rebalance([x.strip() for x in hp.split(" > ") if x.strip()])


def _rebalance(parts: List[str]) -> List[str]:
    """把被标题内容里的 ``" > "`` 误切开的片段合回去。

    判据只有一条：括号没闭合。逐段累加，直到开闭括号数配平才算一段结束。
    正常切分的每段天然平衡，所以这个函数对它们是恒等的。
    """
    out: List[str] = []
    buf = ""
    for p in parts:
        buf = f"{buf} > {p}" if buf else p
        opens = sum(buf.count(c) for c in _OPEN)
        closes = sum(buf.count(c) for c in _CLOSE)
        if opens <= closes:
            out.append(buf)
            buf = ""
    if buf:                       # 末尾仍未配平：原样带上，不丢内容
        out.append(buf)
    return out


def deepest(headings_dict: Dict[int, str]) -> str:
    """``{level: text}`` 里最深一级的标题。

    切分时手上有完整层级字典，末级标题直接取，**不要从拼好的串反推**。
    """
    if not headings_dict:
        return ""
    return str(headings_dict[max(headings_dict)]).strip()
