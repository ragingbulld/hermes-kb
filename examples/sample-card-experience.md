# [示例·经验] SQLite FTS5 bigram 展开让中文两字词召回率从 31% 提到 89%

> 这是一张**示例卡**，数据全为编造。照这个结构写你自己的。

## 触发场景 / 业务背景

本地知识库用 SQLite FTS5 做全文检索，`tokenize='unicode61'`。发现中文查询召回很差：

```
查询 "反向代理" → 命中 3 条（库里实际有 27 条相关内容）
查询 "upstream" → 命中 41 条（英文正常）
```

建了个 100 条中文查询的测试集，逐个人工标注正确答案，基线召回率 **31%**。

## 根本原因与核心设计

`unicode61` 把中文按「非字母数字字符」切分，但中文没有空格，所以：

- `"反向代理配置"` → 整串当成**一个 token**
- 查 `"反向代理"` → 也是一整个 token
- 两者不相等 → 不命中

也就是说 unicode61 对中文基本等于「精确整串匹配」，差一个字就查不到。

**试过但不行的方案：**

| 方案 | 为什么不行 |
|---|---|
| 换 `tokenize='trigram'` | SQLite 的 trigram 要求至少 3 字符，两字词（最常见的汉语词）直接失效。实测召回 31% → 12%，更差 |
| 装 `jieba` 预处理 | 能work，但引入依赖 + 分词结果不可逆（原文改坏），且新词识别不准 |
| 换 Elasticsearch | 运维成本远超收益（单机 6 万行数据） |

**最终方案：入库前做 bigram 展开，另存一份进 FTS。**

```python
# 按「连续 ASCII 串」和「连续汉字段」分开处理
_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9_.\-/]+|[\u4e00-\u9fff]+")

@classmethod
def _fts_tokens(cls, text: str) -> str:
    """汉字段走二字滑窗，ASCII 段整词保留。"""
    s = " ".join(str(text or "").split())   # 空白归一化，否则 "端\n口" 会丢掉跨行的"端口"
    if not s:
        return ""
    out = []
    for m in cls._TOKEN_RUN_RE.finditer(s):
        run = m.group(0)
        if run[0].isascii() or len(run) == 1:
            out.append(run)                                    # ASCII 整词 / 单个汉字
        else:
            out.extend(run[i:i+2] for i in range(len(run)-1))   # 汉字二字滑窗
    return " ".join(out)
```

`"反向代理"` → `"反向 向代 代理"`，`"反向代理配置"` → `"反向 向代 代理 理配 配置"`。查询时同样展开，两条共享 `反向`/`向代`/`代理` 三个 token，BM25 就能给分了。

## 标准解决步骤 / CLI使用 / 关键代码

改完要重建索引（FTS 表里的旧 token 是错的）：

```bash
rm -rf ~/.hermes/data/hermes_kb
python -m hermes_kb.cli sync
```

验证召回率：

```python
from hermes_kb.engine import KBEngine
engine = KBEngine()

tests = [("反向代理", "nginx.md"), ("数据库锁", "sqlite.md")]  # 你的测试集
hit = 0
for q, expect in tests:
    r = engine.search(q, limit=5, rerank_top_n=5, use_reranker=False)
    if any(expect in x["file_path"] for x in r):
        hit += 1
print(f"召回率 {hit}/{len(tests)} = {hit/len(tests):.0%}")
```

## 避坑铁律与硬性约束

- **trigram 是陷阱。** 看起来「不小于 3 字符」很合理，但汉语最常见的词恰好是两字，用它等于把最高频查询全废掉
- **bigram 之后 FTS 表体积涨 1.5~2 倍**（`"反向代理"` 4 个字变成 3 个 token，重叠越多膨胀越大）。这是换准确率的代价，可以接受
- **ASCII 绝不能一起 bigram，这是 IDF 决定的，不是口味问题。** 英文只有 26 个字母、二字组才 676 种，任意英文文本都覆盖绝大多数——实测英文 bigram 平均命中库里 73.7% 的文档，IDF 只有 0.31，切碎等于往候选池灌噪声。真实数字：`"restic"` 切成 `re/es/st/ti/ic` 后命中 27,356 条，而真命中仅 206 条（133 倍差距）；整词保留后回落到 206。`"space-y-2"` 从 6,415 条 → 22 条
- **汉字必须自己切，unicode61 不切汉字**（整段会变成一个 token）。二字滑窗够用——现代汉语双音节词占多数，实测中文 bigram 平均 IDF 3.59（`"端口"` 587 篇占 1.1%，`"快照"` 1,074 篇占 2.0%）
- **空白要先归一化**：`"端\n口"` 直接切会得到 `"端\n"` / `"\n口"`，把 `"端口"` 弄丢，导致跨行位置的词查不到（LIKE 和 bigram 命中数对不上就是这个原因）
- **改了切法必须让 `_FTS_TOKENIZER_VERSION` +1**，`_init_db` 据此自动重建 FTS 表。手工删库也行，但版本号机制能防止「忘了重建」
- 改了 tokenize 逻辑**必须删库重建**，增量更新会留下两种 token 混在一起
