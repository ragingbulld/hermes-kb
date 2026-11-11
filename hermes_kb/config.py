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
    # 检索参数的默认值只在这里定义一次。get_config() 把 config.json 深合并到本表上，
    # 所以每个键必然存在；读取方直接写 cfg["retrieval"]["<key>"]，不要再带
    # .get("<key>", <字面量>) 兜底（那是第二份默值，曾与本表漂移成 40/25、6/25）。
    "retrieval": {
        "candidate_limit": 25,
        "fts_candidate_limit": 15,
        "rrf_k": 60,
        "rerank_top_n": 25,
        # 去噪闸门：低于此重排分的块直接丢弃。这是**唯一**的合格判定 ——
        # 不叠条数 / 字数 / 断崖第二判据（见 docs/TUNING.md）。换模型必须重标定。
        "min_rerank_score": 0.65,
        "use_reranker": True,
        # 排序键：hybrid = 分段取键，rerank >= hybrid_rank_threshold 的饱和区按
        #   rrf_score 排（该段 rerank 挤在 1.0 附近，跨度仅 0.08，无区分度），
        #   低于该线的有梯度区按 rerank_score 排（跨度 0.55~0.88，rrf 反而退化）；
        # rrf = 全程按 RRF 梯度排，rerank_score 只做去噪闸门；
        # reranker = 旧行为（全程按重排分排，高分区会因饱和退化成随机）。
        "rank_by": "hybrid",
        # 混合排序分界线（6 query × 25 候选实测标定）。
        "hybrid_rank_threshold": 0.9,
        # 重排送检文档数：Gitee 侧 <=25 走小批量免费档（硬上限是 200）。
        "reranker_max_candidates": 25,
    },
}

# 密钥类字段 → 环境变量名。配置文件里这些字段留空即视为「从环境读」，
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


def get_config():
    """DEFAULT_CONFIG 叠上 config.json。文件不存在用默认值；存在却读不出来就抛。

    读不出来不退回默认值：engine 只在启动时取一次配置，退回去会让整个进程
    悄悄按另一套参数跑到下次重启。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _deep_merge(DEFAULT_CONFIG, json.load(f))
    except FileNotFoundError:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
    return _backfill_secrets(cfg)
