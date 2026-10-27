"""配置加载：默认值 + `config.json` 深合并。

默认值刻意全部走环境变量与 `~` 展开，**不写死任何绝对路径和密钥**，
克隆下来改 `config.json` 就能跑，不需要动代码。
"""

import copy
import json
import os

# 配置位置可用 HERMES_KB_CONFIG 覆盖（测试 / 多实例场景）
CONFIG_PATH = os.path.expanduser(
    os.environ.get("HERMES_KB_CONFIG", "~/.hermes/hermes_kb/config.json")
)

_DATA_DIR = os.path.expanduser(os.environ.get("HERMES_KB_DATA_DIR", "~/.hermes/data/hermes_kb"))

DEFAULT_CONFIG = {
    "db_dir": _DATA_DIR,
    "state_db": os.path.join(_DATA_DIR, "sync_state.sqlite"),
    # 要索引的目录。**只放你自己的知识内容**：笔记、手册、skills 都行，
    # 但别把「你的知识」提交进这个仓库（见 README「什么该开源、什么不该」）。
    "watch_dirs": [
        os.path.expanduser("~/knowledge_base"),
        os.path.expanduser("~/.hermes/skills"),
    ],
    "embedding": {
        # 任何 OpenAI 兼容的 /v1/embeddings 端点都能用：
        #   Gitee AI     https://ai.gitee.com/v1
        #   SiliconFlow  https://api.siliconflow.cn/v1
        #   OpenAI       https://api.openai.com/v1
        #   本地 Ollama   http://127.0.0.1:11434/v1
        "base_url": os.environ.get("HERMES_KB_EMBED_BASE_URL", "https://api.siliconflow.cn/v1"),
        # 密钥一律从环境变量读，绝不落盘进仓库：
        #   export HERMES_KB_EMBED_API_KEY=sk-...
        "api_key": os.environ.get("HERMES_KB_EMBED_API_KEY", ""),
        "model": os.environ.get("HERMES_KB_EMBED_MODEL", "BAAI/bge-m3"),
        "dimensions": 1024,
        "batch_size": 12,
        "timeout": 30,
    },
    "reranker": {
        "base_url": os.environ.get(
            "HERMES_KB_RERANK_BASE_URL", "https://api.siliconflow.cn/v1/rerank"
        ),
        "api_key": os.environ.get("HERMES_KB_RERANK_API_KEY", ""),
        "model": os.environ.get("HERMES_KB_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
    },
    "chunker": {
        "min_chunk_chars": 200,
        "max_chunk_chars": 1500,
        "target_chunk_chars": 800,
    },
    "source_filters": {
        "exclude_dirs": [".archive", "__pycache__", ".git"],
        "exclude_file_prefixes": [".tmp", ".usage-", ".usage_"],
        "exclude_component_substrings": [".rollback-"],
        # 想排除特定文件/目录就填绝对路径（默认空）
        "exclude_paths": [],
        # 只想索引某目录的一份副本时用它（例如双根镜像只留一根）
        "exclude_path_prefixes": [],
        "exclude_file_suffixes": [".bak", ".part", ".tmp", ".swp"],
    },
    "retrieval": {
        "candidate_limit": 25,
        "fts_candidate_limit": 20,
        "rrf_k": 60,
        "rerank_top_n": 25,
        # 用户原话路径（每轮一次）与 kb_query 路径（每次工具调用一次）统一 25 候选
        "user_top_k": 25,
        "doing_top_k": 25,
        "doing_candidate_limit": 25,
        # 去噪闸门：低于此重排分的块直接丢弃。
        # 0.85 是 Qwen3-Reranker-8B 上的实测值，**换模型必须重标定**（见 docs/TUNING.md）
        "min_rerank_score": 0.85,
        "use_reranker": True,
        # 排序键：rrf = 用 RRF 梯度排序、重排分只做闸门（推荐）；
        # reranker = 旧行为，按重排分排序，会因长句分数饱和退化成随机。
        "rank_by": "rrf",
        "reranker_max_candidates": 25,
        # 单次注入块数上限（用户消息路径与 doing 路径共用）
        "max_inject_blocks": 2,
    },
}


def _deep_merge(base, override):
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# 密钥类字段：配置文件里留空 / 不写时，从这些环境变量回填。
# 意义在于「配置文件可以安全提交」，密钥只活在环境里。
_SECRET_ENV = {
    ("embedding", "api_key"): "HERMES_KB_EMBED_API_KEY",
    ("reranker", "api_key"): "HERMES_KB_RERANK_API_KEY",
}


def _backfill_secrets(cfg: dict) -> dict:
    """配置文件把这些字段留空时，从环境变量补上。

    注意：**空字符串视为「未设置」**。否则用户在 config.json 里写
    `"api_key": ""` 会把 DEFAULT_CONFIG 里从环境变量读到的值覆盖成空，
    表现是「明明 export 了密钥却报 401 / 建库 0 chunk」——
    这个坑在 e2e 安装验证里真实踩到过。
    """
    for (section, field), env_name in _SECRET_ENV.items():
        sec = cfg.get(section)
        if not isinstance(sec, dict):
            continue
        if not sec.get(field):                       # 空 / None 都算没设
            env_val = os.environ.get(env_name, "")
            if env_val:
                sec[field] = env_val
    return cfg


def get_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _deep_merge(DEFAULT_CONFIG, json.load(f))
    except Exception:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
    return _backfill_secrets(cfg)
