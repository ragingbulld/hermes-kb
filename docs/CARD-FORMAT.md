# 卡片格式规范

`hermes-kb-write` 的 `save_reflection` 按这份规范校验卡片。引擎本身不强制格式（它只认 Markdown 标题），但**按格式写检索质量会好很多**。

## 最小要求：三个字段

```python
save_reflection(
    title="标题",      # 必填，会成为 H1
    body="正文",       # 必填，Markdown
    kind="工具链路",    # 可选，决定文件名前缀
)
```

对应磁盘上的文件：

```markdown
# [工具链路] 标题

正文……
```

## 标题怎么写

**必须含三样东西：核心术语 + 目标对象 + 现象/结论。**

```
❌ 修复了一个 bug
❌ 知识库问题
❌ 今天排查记录

✅ [排障SOP] nginx upstream 全部 502：keepalive 配了但 upstream 块缺 keepalive 指令
✅ [工具链路] SQLite FTS5 bigram 展开让中文两字词召回率从 31% 提到 89%
✅ [经验] Reranker 长句分数饱和（top1/top2 差 1e-5）导致排序退化，须改用 RRF 排序
```

原因很实际：**注入给模型的是标题，不是全文**。标题写得糊，模型看不出这张卡跟手头的事有没有关系，就不会去翻。

### 四种 `kind` 前缀

| kind | 用在哪 |
|---|---|
| `排障SOP` | 出了问题怎么一步步定位、怎么修 |
| `工具链路` | 搭了什么工具/流水线、怎么用 |
| `经验` | 判断、取舍、反直觉的实测结论 |
| `规约SOP` | 必须遵守的规则、配置约束 |

## 正文必须有的四节

这不是形式主义——这四节对应检索时会用到的四种信号。

```markdown
## 触发场景 / 业务背景

什么现象、什么任务引发的。**抄原始报错**，不要改写。
这是「病灶现象」段，检索词主要从这里长出来。

## 根本原因与核心设计

root cause 与机制，带代码/配置证据。
讲清「为什么会这样」，不只是「怎么改」。

## 标准解决步骤 / CLI使用 / 关键代码

可复现的命令，带真实参数。
让人（和 AI）能照抄执行。

## 避坑铁律与硬性约束

走死的路、血泪教训。
**这一段最有价值**——踩过的坑写清楚了，下次才不会再踩。
```

### 为什么「失败路径」最值钱

大多数技术文档只写「正确答案」。但真实排障里，**「我试了 A、不行、因为 B」比「应该用 C」信息量大得多**。

检索时，模型往往正卡在失败路径上——它写的检索词就是当前那个报错。所以：

```markdown
## 根本原因与核心设计

**先试了什么、为什么不работа** ← 这种句子要显式写出来

试过 `pip install lancedb` 全部平台解，报 `no matching distribution`——
pyarrow 没有 riscv64 的 wheel。改用 SQLite 就没有这个依赖。
```

（上面那句中俄混排是故意的错例，别学。）

## 长度

| 项 | 建议 |
|---|---|
| 标题 | 20~70 字符。超过 48 字符在回执里会被截断显示 |
| 正文 | 300~3000 字。低于 200 字检索价值低，高于 5000 字建议拆多张 |
| 单节 | 不超过 800 字（接近 chunk 大小，超了会被切开，注入时只见半节） |

## 示例卡片

`examples/` 目录有两张用假数据写的示范卡，可以直接照抄结构：

- [`examples/sample-card-troubleshooting.md`](../examples/sample-card-troubleshooting.md) — 排障类
- [`examples/sample-card-experience.md`](../examples/sample-card-experience.md) — 经验类

## 写卡前先查重

AI 写卡时容易「同一个主题写五张」。`save_reflection` 内置语义查重：写入前会拿标题+正文去检索现有卡，高分命中就拒绝写入并返回已有卡的路径。

拿到查重拒绝后的三种正确反应：

| 情况 | 该怎么做 |
|---|---|
| 已有卡讲的是同一件事，且**仍然成立** | 用 `edit_reflection_card(mode="insert")` 追加新发现的小节，不要新建 |
| 已有卡**结论被证伪** / 根因写错了 | **删掉它再写新的**。留着会把下一个读到它的人带进同一个坑 |
| 已有卡是同主题但覆盖的完全是另一面 | 确认不是重复后，强制新建 |

**最坏的做法是把错卡留着、在旁边再写一张对的。** 两张互相矛盾的卡同时被检索到，模型会随机挑一张信。

## 双根落盘（可选）

如果启动 `hermes-kb-write`，卡片默认写两份（md5 必须一致）：

```
$KB_REFLECTIONS_LOCAL_DIR/     # 给人看、同步备份
$KB_REFLECTIONS_INBOX_DIR/     # 给引擎索引
```

这两个路径用环境变量配置：

```bash
export KB_REFLECTIONS_LOCAL_DIR=~/knowledge_base/reflections
export KB_REFLECTIONS_INBOX_DIR=~/hermes-kb-inbox/reflections
```

**索引只建一份**——在 `config.json` 里用 `exclude_path_prefixes` 排掉镜像根，否则同一张卡的两个副本会各占一个注入位。

只想要单根就设成同一个路径，或把 `exclude_path_prefixes` 留空并删掉一份。
