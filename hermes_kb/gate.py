"""重排分闸门：一个检索块「算不算合格」的唯一判定点。

注入插件（`_search_validated`）与 CLI（`hermes-kb search`）都调本函数，
两边不再各写一份过滤循环。阈值只从 `engine.config["retrieval"]` 读，
默认值只在 `config.DEFAULT_CONFIG` 定义一次，调用点不带字面量兜底。

闸门只有这一道：过了 `min_rerank_score` 就是合格块，不叠条数 / 字数 / 断崖
等第二判据。没有重排分的块（reranker 超时或失败留下的 rrf_fallback）一律
不进，单独计数，不当 0 分处理。
"""

from typing import Any, Dict, List, NamedTuple, Optional


class GatedSearch(NamedTuple):
    kept: List[Dict[str, Any]]  # 过闸的块，保持 engine 给出的名次
    candidates: int  # engine 交回的块数
    below: int  # 有重排分但低于阈值
    unscored: int  # 没有重排分
    min_score: float

    @property
    def dropped(self) -> int:
        return self.below + self.unscored


def gated_search(
    engine: Any,
    query: str,
    candidate_limit: Optional[int] = None,
    top_n: Optional[int] = None,
) -> GatedSearch:
    """`engine.search` + 重排分闸门。`top_n` 为 None 时由 engine 取 `rerank_top_n`。"""
    cfg = engine.config["retrieval"]
    min_score = float(cfg["min_rerank_score"])
    if candidate_limit is None:
        candidate_limit = cfg["candidate_limit"]
    results = engine.search(query, limit=candidate_limit, rerank_top_n=top_n)

    kept: List[Dict[str, Any]] = []
    below = unscored = 0
    for result in results:
        score = result.get("rerank_score")
        if score is None:
            unscored += 1
        elif score >= min_score:
            kept.append(result)
        else:
            below += 1
    return GatedSearch(kept, len(results), below, unscored, min_score)
