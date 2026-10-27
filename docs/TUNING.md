# 调参指南

**核心原则：阈值不是普适常数，是跟你用的模型绑定的。换模型必须重标。**

这份文档里的数字全部来自 Qwen3-Reranker-8B + Qwen3-Embedding-8B。换别的模型，照着第 2 节重标一遍。

---

## 一、六个参数，各自管什么

```jsonc
{
  "chunker": {
    "min_chunk_chars": 200,      // ① 小于此值的相邻块尝试合并
    "max_chunk_chars": 1500,     // ② 硬上限
    "target_chunk_chars": 800    // ③ 正常打包目标
  },
  "retrieval": {
    "candidate_limit": 25,       // ④ 向量路候选数
    "min_rerank_score": 0.85,    // ⑤ 去噪闸门 ← 最关键
    "max_inject_blocks": 2       // ⑥ 单次注入几块
  }
}
```

### ① `min_chunk_chars`（默认 200）

太小 → 一句话的章节单独成块，检索时噪声多、占注入位
太大 → 同主题内容被合并成巨块，检索精度下降，且注入时超预算被截断

**判断依据**：如果你的笔记是「每个 `##` 只有两三句话」的形态，调到 400~600；如果是「每个 `##` 都是完整一节」，保持 200 或更小。

### ②③ `max_chunk_chars` / `target_chunk_chars`

`target` 是打包目标，`max` 是硬上限。两者差距大（如 800 / 1500）意味着：正常情况下按 800 打包，遇到不能切的地方（代码块、长表格）允许涨到 1500。

**别把 `max` 设太大**。一个 5000 字的块被检索到，注入时要么超预算被截断（只剩开头），要么挤掉其他块。800~1500 是实测好用的区间。

### ④ `candidate_limit`（默认 25）

向量路捞多少个候选进 RRF。太小会漏（RRF 融合需要两条路都有货），太大浪费算力。

25 是配合 `reranker_max_candidates=25` 的。要调就两个一起调。

### ⑤ `min_rerank_score`（默认 0.85）— 最重要

**这个值决定了「注入的到底是有用知识还是噪声」。**

分数是这个块跟查询的相关度，低于阈值直接丢弃。太低 → 满屏噪声；太高 → 全被丢掉，等于没注入。

**0.85 是 Qwen3-Reranker-8B 上标出来的**，别直接抄到别的模型上。下一节讲怎么标。

### ⑥ `max_inject_blocks`（默认 2）

一次注入几张卡的标题。每条卡标题 + 路径 + 行号约 100~150 字符，2 块约 300 字符，成本可忽略。

**但别贪多。** 注入 10 条的结果是模型读完发现全是噪声，下次就不看注入了。宁可只给 1~2 条准的。

---

## 二、怎么重标 `min_rerank_score`

**这是换模型后必做的一步。** 不同 reranker 的分数量纲完全不同（有的输出 0~1 的 sigmoid，有的输出 -10~10 的 logit）。

### 方法：用你自己库里的真问题做标注

准备 20 个你真实会问的问题，每个问题**人工标出「哪个 chunk 是正确答案」**（不知道就先跑一遍看结果再标）。

```python
import json
from hermes_kb.engine import KBEngine

engine = KBEngine()

# 你的测试集：[{query, correct_file_pattern}, ...]
tests = [
    {"query": "nginx upstream 502", "expect": "nginx.md"},
    {"query": "SQLite 数据库锁住", "expect": "sqlite.md"},
    # ... 至少 20 条
]

scores_hit, scores_miss = [], []

for t in tests:
    # 关掉闸门，拿全量候选带分数
    results = engine.search(t["query"], limit=25, rerank_top_n=25)
    for r in results:
        s = r.get("rerank_score", 0.0)
        if t["expect"] in r["file_path"]:
            scores_hit.append(s)
        else:
            scores_miss.append(s)

print(f"命中块  分数：min={min(scores_hit):.4f} p25={sorted(scores_hit)[len(scores_hit)//4]:.4f} "
      f"中位={sorted(scores_hit)[len(scores_hit)//2]:.4f}")
print(f"噪声块  分数：中位={sorted(scores_miss)[len(scores_miss)//2]:.4f} "
      f"p75={sorted(scores_miss)[len(scores_miss)*3//4]:.4f} max={max(scores_miss):.4f}")
```

**看两个分布的交叉点。**

理想情况：命中块的最低分 > 噪声块的最高分，中间有空隙，阈值放空隙里。

现实情况通常重叠。这时按**代价不对称**来选：

- **阈值偏低** → 噪声进注入。代价：模型读到垃圾，浪费一点 token，下次可能不信任注入
- **阈值偏高** → 真知识被丢掉。代价：模型不知道有这张卡，重复踩坑

**我更倾向偏高。** 一次准确的注入比十次「可能有用」有价值；噪声会训练模型忽略注入位。

### 实测参考（Qwen3-Reranker-8B）

| 检索词类型 | 命中块分数 | 噪声块分数 |
|---|---|---|
| 技术术语串（`nginx upstream 502`） | 0.95~0.99 | 0.02~0.4 |
| 报错原文 | 0.93~0.99 | 0.0~0.5 |
| 长句描述（30 字以上） | 0.9998 | 0.9997 ← **饱和了，没法区分** |

最后一行就是改 `rank_by: "rrf"` 的原因。**长句查询下重排分失去区分度**，只能靠 RRF 排序、重排分只当粗闸门（0.85 对饱和的分数等于不设闸门，但也无害）。

---

## 三、注入预算怎么控

一轮对话的实际成本：

| 项 | 量级 |
|---|---|
| 每条卡标题清单 | 100~150 字符 |
| `max_inject_blocks: 2` | 约 300 字符 |
| `kb_query` 建议词 | 约 200 字符（协议提示词） |
| 合计（假设一轮 5 次工具调用） | **约 1200~1800 字符 ≈ 600~900 token** |

按 deepseek-v4.1-flash 的价格，一天几百轮对话也就几分钱。**这个成本不该成为你调参的约束。**

真正该控的不是 token，是**噪声比**：注入的块里有多少是模型真的用上的。

### 怎么量注入有用率

最笨也最有效的办法：跑一天，翻日志，人工数「这次注入的卡，模型有没有真的去 read_file 它」。

```bash
# 从日志里抽注入记录
journalctl -u hermes-gateway --since "24 hours ago" --no-pager \
  | grep -i "knowledge-base-titles" > /tmp/injections.log
wc -l /tmp/injections.log
```

然后随机抽 20 条，看模型下一轮有没有 `read_file` 对应路径。有用率低于 30% 说明阈值偏低或检索词写法有问题。

---

## 四、检索词写法（比调参影响更大）

**再好的阈值也救不了烂检索词。**

`kb_query` 用的是「三段式」，`｜` 分隔：

```
病灶现象｜目标对象｜专名
```

| 段 | 抄什么 | 举例 |
|---|---|---|
| ① 病灶现象 | 你眼睛看见的原文 | `502 Bad Gateway` |
| ② 目标对象 | 哪个服务/文件/模块 | `nginx upstream 块` |
| ③ 专名 | 字段名/参数/路径/版本号 | `keepalive 指令` |

完整例子：

```
502 Bad Gateway 且 upstream 日志空｜nginx 反向代理配置｜keepalive 指令 proxy_http_version
```

### 四个禁忌

| 别写 | 为什么 |
|---|---|
| 动作词（排查 / 修复 / 优化 / 确认） | 库里没人这么写，纯噪声。实测加 12 个动作词，相关度从 0.9953 掉到 0.8226 |
| 评价词（太慢 / 不正常 / 更优雅） | 同上 |
| 自造术语 | 编一个库里没有的词，相关度直落到 0.37 以下，直接被闸门砍死 |
| 空值 / `none` / `无` | 没有任何检索价值 |

**字面抄不出来，说明你还该先去看，不是先去搜。**

### 为什么技术术语串比长句好

实测对比（同一个问题，两种写法）：

| 写法 | 正确块排名 | 噪声块数 |
|---|---|---|
| `nginx upstream keepalive proxy_http_version` | 第 1 | 0 |
| `我配置了 nginx 的反向代理，但是 upstream 一直报 502 错误，我怀疑是 keepalive 的问题，应该怎么排查` | 第 3 | 16 |

长句有两个问题：向量被无关词稀释，且重排分饱和失去区分度。

---

## 五、什么时候该重建索引

不用重建。SQLite 后端是**写入即索引**，没有「重建 FTS」这一步。

需要重建的只有三种情况：

| 情况 | 操作 |
|---|---|
| 换了 embedding 模型（维度变了） | 删库重建 |
| 换了 FTS 分词策略（bigram 参数变了） | 删库重建 |
| 索引和实际文件严重不一致（大量删除但没同步） | 删库重建，或先 `reconcile_and_prune_dead_files()` |

```bash
# 删库重建（会重新调 embedding API，注意成本）
rm -rf ~/.hermes/data/hermes_kb
python -m hermes_kb.cli sync
```

**换 embedding 模型必须重建**，因为旧向量和新模型不在同一个语义空间，混在一起检索结果是乱的。`dimensions` 变了更会直接报错。

---

## 六、参数改动后怎么验证真的生效

**改配置不等于生效。** 插件和引擎都可能在进程内缓存了配置。

```bash
# 1. 确认配置读到了新值
python3 -c "
from hermes_kb.config import get_config
c = get_config()
print('min_rerank_score =', c['retrieval']['min_rerank_score'])
print('max_inject_blocks =', c['retrieval']['max_inject_blocks'])
"

# 2. 重启守护进程（引擎侧）
sudo systemctl restart hermes-kb

# 3. 重启网关（插件侧）—— 钩子只在进程启动时注册
sudo systemctl restart hermes-gateway

# 4. 确认插件重新加载了（md5 会变）
journalctl -u hermes-gateway -n 100 --no-pager | grep "code md5="
```

**第 4 步的 md5 必须跟改之前不一样**，否则说明还是旧代码在跑。

### 常见的「改了没生效」

| 现象 | 原因 |
|---|---|
| 配置读出来是旧值 | 改了错的 `config.json`（有多份备份时容易搞混），或 `HERMES_KB_CONFIG` 指向别处 |
| 插件 md5 没变 | 只重启了守护进程，没重启网关 |
| 检索结果没变化 | 结果被缓存了，或改的是 `candidate_limit` 而闸门还是老的 |
| 注入条数没变 | `max_inject_blocks` 改了但插件进程没重启 |

---

## 七、性能基线（供对照）

单机、SQLite、无 ANN：

| 场景 | 指标 |
|---|---|
| 6 万 chunk 向量检索 | 15.7 ms |
| FTS5 检索 | 5~10 ms |
| RRF 融合 | < 1 ms |
| Reranker（远端 API，25 候选） | 200~600 ms ← **瓶颈在这** |
| 全量建库 | 15~30 chunks/s（受 embedding API 限速） |
| 缓存命中的重复文本 | 不重复调 API（`embedding_cache` 表） |

**重排是唯一的慢环节。** 想更快就砍 `reranker_max_candidates`（25 → 10），代价是精度略降。或者直接 `use_reranker: false` 走纯 RRF（但写卡查重会失效）。

**实际对话中你不该感知到这些**——注入是异步的、fail-open 的，检索 600ms 时对话照常进行。
