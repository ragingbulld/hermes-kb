"""对外暴露的那一件东西：`search_knowledge` 工具（schema + handler）。

handler 只有十几行 —— 它**不自己检索**，只把 query 丢给 `plugin_instance._inline_menu`。
这样主动查库和每轮注入走的是同一条管线、同一个 `#N` 池、同一套去重，
模型在清单里看到的号，`read_file` 一定拿得到。
"""

from typing import Any

from .plugin_class import plugin_instance
from .plugin_common import QUERY_WRITING_GUIDE, QUERY_WRITING_EXAMPLES, _short

SEARCH_KB_SCHEMA = {
    "name": "search_knowledge",
    "description": (
        "主动查本地知识库（排障手册、项目文档、配置指南、反思卡等）。"
        "**返回的是一屏标题清单，不是正文**：每行带绝对路径和命中段行号，"
        "要正文就 `read_file(路径, offset=起行, limit=段行数)` —— 小卡直接整篇读。"
        "它与每次工具调用自带的 kb_query 走同一条管线、同一份编号，不会重复印同一张卡。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": QUERY_WRITING_GUIDE + "\n" + QUERY_WRITING_EXAMPLES,
            },
            "limit": {
                "type": "integer",
                "description": "这一屏最多给几张卡的标题（默认 3，最大 10）",
                "default": 3,
            },
        },
        "required": ["query"],
    },
}


def search_knowledge_handler(args: dict[str, Any], **kw: Any) -> str:
    """主动查库入口。**与 kb_query 走同一条管线**：检索 → 标题清单 → #N 展开。

    从前这里自己拼一份 JSON（full content 全塞进去），跟注入路径各写一套：
    同一份引擎、两套过滤、两套去重、两套编号 —— 模型在 search_knowledge 里
    看到的卡，`read_file` 却拿不到行号（那些号在池子里根本不存在）。
    现在它只是「用 query 触发一次检索」的另一种写法，产物就是同一屏清单。
    """
    query = str(args.get("query", "")).strip()
    if not query:
        return "search_knowledge 需要非空 `query`。\n" + QUERY_WRITING_GUIDE
    limit = max(1, min(int(args.get("limit", 3) or 3), 10))
    body = plugin_instance._inline_menu(query, max_cards=limit)
    if body:
        return body
    return (
        f"<knowledge-base-note>「{_short(query, 60)}」没捞到新卡"
        "（要么过了重排闸门的一张都没有，要么这些卡本轮清单里已经给过了）。"
        "先看本轮已有清单；需要细化时补入对话已明确的对象、现象或新取得的字段，"
        "不要猜根因、错误码或卡名。</knowledge-base-note>"
    )
