# Hermes KB — 本地 Markdown 知识库 + Agent 上下文注入

一套**纯本地、零容器**的 Markdown 知识库：文件放进目录 → 自动分块索引 → 语义 + 全文混合检索 → 每次工具调用前把最相关的卡片**自动注入**给模型。

为 [Hermes Agent](https://github.com/) 写的，但引擎和注入逻辑与具体 Agent 框架解耦，你可以把它接到任何需要「让 LLM 记住自己那堆笔记」的场景。

```
你的 Markdown 文件
      │  watchdog 监听（写入即索引，无重建索引这一说）
      ▼
  语义分块（按标题层级切，保护代码围栏）
      │
      ├──► SQLite chunks 表：正文 + float32 向量 BLOB
      └──► SQLite FTS5：bigram 展开正文（CJK 友好）
                │
    查询 ───────┴──► 向量暴力余弦(top25) + FTS5 BM25(top20)
                          │
                     RRF 融合排序
                          │
                     Reranker 去噪闸门（默认 0.85）
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
      pre_llm_call 自动注入      search_knowledge 主动查
      （每轮/每次工具调用）        （模型自己决定何时用）
```

---

## 三个设计决定，决定了它跟别的 RAG 有什么不同

### 1. 不注入正文，注入「标题清单 + 行号」

最常见 RAG 的做法是把 top-k 切片全文塞进 prompt。代价是：模型拿到一堆它没要求的文本，还占了几千 token。

这里注入的是**一屏卡片标题清单**，每行带绝对路径和命中段行号，模型看到标题觉得相关，才自己去 `read_file(路径, offset, limit)` 拉正文。

好处是注入成本恒定且小（千字符级），模型按需取用，不会为了「可能有用」付全额 token。代价是模型得多走一步工具调用——实测这一步它很愿意走。

### 2. Agent 必须在每次工具调用时都带检索词

注入点不止「用户说话那一刻」。真正决定下一步做什么的，是**模型正在调用什么工具、带什么参数**。所以门禁层会把 `kb_query` 提升成所有工具的顶层必填参数，dispatch 前再把参数剥掉，不让它污染真正的工具签名。

检索词从「你要做的事」里长出来，比从「用户说的话」里长出来准得多。用户说「搞不定了」，你是检索不到任何东西的；但模型要执行 `systemctl restart foo` 时带的参数，能直接命中相关排障卡。

### 3. Fail-open，任何一环挂了都不能挡住对话

检索超时 → 放行，不注入。reranker 报错 → 退回 RRF 排序结果。嵌入服务挂着 → 只走 FTS。引擎崩了 → 插件加载失败但 Agent 照常工作。

知识库的价值是「有时有用」，不是「必须有」。**绝不能因为检索失败让用户等**。

---

## 适用场景

**适合：**
- 自己的排障笔记、运维手册、项目文档越积越多，翻不动了
- 想让 AI 助手带着你的历史经验工作，而不是每次从零解释
- 中文为主的知识（FTS5 用 bigram 展开，中文分词不依赖外部分词器）
- 想要零运维：无 Docker、无向量数据库服务、单文件 SQLite

**不适合：**
- 语料在百万级以上（向量走暴力扫描，6 万行 15ms，百万级要换 ANN）
- 需要多租户 / 权限隔离
- 需要多模态（只吃文本 + Markdown + 少量文档格式）
- 想开箱即用不写代码（这玩意儿要改配置、要自己起服务）

---

## 什么在这个仓库里，什么不在

**在：** 引擎代码、注入插件、写卡插件、配置模板、安装教程、格式规范、调优指南。

**不在（刻意）：** 反思卡内容、技能库 references、任何真实笔记。

原因很简单——**开源的是框架，不是我的知识**。这堆卡里全是我自己环境的内网 IP、主机名、项目细节，对别人没用还泄漏隐私。你用这套框架，装的是你自己的知识。

仓库里 `docs/` 有格式规范，`examples/` 有造假数据的演示卡片，照格式写自己的就行。

---

## 快速开始

```bash
git clone https://github.com/<you>/hermes-kb.git
cd hermes-kb

# 1. 装包（会带上 watchdog / numpy / requests）
pip install -e .

# 2. 配置（密钥走环境变量，不要写进配置文件）
export HERMES_KB_EMBED_API_KEY=sk-...
export HERMES_KB_RERANK_API_KEY=sk-...
mkdir -p ~/.hermes/hermes_kb
cp config/config.example.json ~/.hermes/hermes_kb/config.json
$EDITOR ~/.hermes/hermes_kb/config.json   # 改 watch_dirs 指向你的笔记目录

# 3. 建库
hermes-kb sync

# 4. 查一下
hermes-kb search "你的问题"

# 5. 体检（库/向量/FTS/检索/插件五关）
./scripts/healthcheck.sh
```

`pip install -e .` 别忘了——少了它 `import hermes_kb` 找不到包。**也别加 `--no-deps`**：少了 numpy 建库照样成功、search 照样出结果，但注入会静默失效（只有协议没有卡片）。

跑通了再装插件、起守护进程。**详细步骤在 [docs/INSTALL.md](docs/INSTALL.md)**，包括 systemd 单元、插件注册、故障排查。

---

## 仓库结构

```
hermes-kb/
├── hermes_kb/                   引擎（纯 Python，跟 Agent 框架无关）
│   ├── chunker.py               语义分块：按标题切，保护代码围栏
│   ├── engine_sqlite.py         SQLite 存储 + FTS5 + 向量检索 + RRF + 重排
│   ├── watcher.py               watchdog 监听，增量索引
│   ├── daemon.py                watcher 常驻进程
│   ├── cli.py                   hermes-kb {sync,watch,search,stats}
│   └── config.py                配置加载（密钥从环境变量读）
│
├── plugins/
│   ├── hermes-kb-auto-inject/   注入插件：pre_llm_call 注入清单 + kb_query 门禁
│   └── hermes-kb-write/         写卡插件：查重 + H1 硬闸 + 落盘
│
├── config/
│   └── config.example.json      全配置模板（密钥留空）
│
├── docs/
│   ├── INSTALL.md               从零安装，每步带验收命令
│   ├── ARCHITECTURE.md          数据模型、检索链路、为什么不用向量库
│   ├── CARD-FORMAT.md           卡片格式规范
│   ├── TUNING.md                阈值怎么标、换模型怎么重标
│   └── TROUBLESHOOTING.md       五个真实踩过的陷阱
│
├── examples/
│   ├── sample-card-*.md         两张造假数据的示例卡（排障类 / 经验类）
│   └── reflections/             空目录 + 格式说明 —— 你的卡放这里
│
├── scripts/
│   ├── healthcheck.sh           一键体检五关
│   └── verify_injection.py      验证注入真的在工作（唯一可信判据）
│
├── pyproject.toml
└── LICENSE
```

---

## 文档

| 文档 | 讲什么 |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | 从零安装：依赖、配置、建库、起服务、装插件、验收 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 引擎怎么工作：分块策略、双索引、RRF、注入链路 |
| [docs/CARD-FORMAT.md](docs/CARD-FORMAT.md) | 卡片格式规范（写卡插件按这个校验） |
| [docs/TUNING.md](docs/TUNING.md) | 调参：阈值怎么标、换模型后怎么重标、注入预算 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 五个真实陷阱：注入静默失效、api_key 被空串顶掉、懒加载误判、FTS 表名、验收脚本自去重 |
| [scripts/healthcheck.sh](scripts/healthcheck.sh) | 一键体检：库/向量/FTS/检索/插件，五关 |
| [scripts/verify_injection.py](scripts/verify_injection.py) | 注入验收：直调 hook 看有没有 `<knowledge-base-titles>` |

---

## 已知限制

1. **纯文本 + Markdown 为主。** 有 PDF/DOCX 的抽取路径，但效果不如 Markdown。
2. **向量暴力扫描。** 单机 6 万 chunk 约 15ms，再大要换 `sqlite-vec` 或 ANN。
3. **阈值不通用。** `min_rerank_score=0.85` 是 Qwen3-Reranker-8B 上标的，换模型必须重标（见 TUNING.md）。
4. **中文靠 bigram。** 不装分词器，召回靠字面重叠，语义靠向量兜。
5. **单机单进程。** 没有分布式、没有并发写入协调（watcher 带文件锁，但多实例会打架）。
6. **注入是 fail-open 的，坏了不报错。** 检索异常被吞掉只留一行日志，返回值仍然有协议部分——所以「有返回值」不等于「注入成功」。验收必须看 `<knowledge-base-titles>` 是否出现，见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

---

## 许可

MIT。随便用，出问题别找我。
