# 架构：它是怎么工作的

## 一、为什么是 SQLite 而不是向量数据库

早期版本用的是 LanceDB。检索质量没问题，**垮的是索引维护层**：

LanceDB 的 `create_fts_index(replace=True)` 没有追加 API，任何一次写入都要全表重建。6.1 万行实测 **46 秒/次**。配上文件监听器 2 秒 debounce 的刷新线程，42.9 小时里累计烧掉 **40.04 小时单核 CPU**——接近 100% 利用率一直在重建索引。

换成 SQLite 单文件后：

| 指标 | LanceDB | SQLite |
|---|---|---|
| 写入 | 全表重建 46s | 增量，毫秒级 |
| FTS 刷新 | 单独线程轮询 | 写入即索引，无需刷新 |
| 依赖 | `lancedb` + `pyarrow` + `pylance`（三个重包，版本互相打架） | stdlib 自带 |
| 备份 | 目录 + 多文件 | 拷一个 `.sqlite3` |

向量检索走**内存矩阵暴力余弦**：启动时把全部向量 load 进一个 `numpy` 的 `float32` 矩阵，查询时 `matrix @ qvec`。6 万行 1024 维，**15.7ms**。

**不用 ANN 是刻意的判断**：6 万行暴力扫描已经比一次网络往返快得多，上 ANN 换来的是「要建索引、要维护索引、要处理索引和正文不一致」——全是 LanceDB 踩过的坑。涨到百万级再评估 `sqlite-vec`。

## 二、数据模型

三张表 + 一张 FTS 虚拟表。**实际 schema**（从建出来的库里拉的，不是设计稿）：

```sql
CREATE TABLE chunks (
    id            INTEGER PRIMARY KEY,
    chunk_id      TEXT UNIQUE NOT NULL,     -- 稳定标识，跨重建不变
    file_path     TEXT NOT NULL,
    heading_path  TEXT DEFAULT '',          -- "部署手册 > nginx 配置"
    title         TEXT DEFAULT '',          -- 文档级标题（取自 H1 或文件名）
    content       TEXT NOT NULL,
    token_estimate INTEGER DEFAULT 0,
    vector        BLOB NOT NULL,            -- float32 numpy tobytes()
    updated_at    REAL NOT NULL,
    text_hash     TEXT                      -- sha256(正文)，向量缓存命中用
);

-- 文件级同步状态：hash + mtime，判断是否需要重新索引
CREATE TABLE file_records (
    file_path   TEXT PRIMARY KEY,
    file_hash   TEXT,
    mtime       REAL,
    chunk_count INTEGER,
    updated_at  REAL
);

-- 元信息：切词版本号等
CREATE TABLE kb_meta (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);

-- FTS5 external-content 虚表，只存展开后的 token 串
-- （bi = bigram expanded；rowid 隐式对应 chunks.id）
CREATE VIRTUAL TABLE "fts" USING fts5(bi, tokenize='unicode61');
```

三个要注意的点：

**1. `fts` 只有一列 `bi`。** 不是把 `content`/`heading_path`/`file_path` 都塞进 FTS，而是**把这三段拼起来做 bigram 展开后存成一列**。查询也走同一套展开，靠 `rowid` 回 `chunks` 取原文。这样 FTS 表小很多，也避免了多列 BM25 权重调参。

**2. `chunk_id` 而不是 `id` 做业务标识。** `id` 是 SQLite 自增行号，删过一次就变；`chunk_id` 是内容派生的稳定值，重建库后同一个块还是同一个 `chunk_id`。用于跨版本追踪和向量缓存。

**3. 没有 `embedding_cache` 表。** 向量缓存走 `chunks.text_hash` + 内存缓存：同一段文本在库里出现多次时，第二次不再调 embedding API。跨文件去重靠 `text_hash`，不落独立表。

### FTS5 的中文处理

`unicode61` 分词器把中文按字符切，`"反向代理"` 会变成四个单字 token，BM25 打分就废了。

解法：**入库前把正文做 bigram 展开**，`"反向代理"` → `"反向 向代 代理"` 一并存进 FTS。查询时同样展开。这样中文两字词能精确命中，又不用外挂 `jieba`。

```python
_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9_.\-/]+|[\u4e00-\u9fff]+")

@classmethod
def _fts_tokens(cls, text: str) -> str:
    s = " ".join(str(text or "").split())      # 空白归一化
    if not s:
        return ""
    out = []
    for m in cls._TOKEN_RUN_RE.finditer(s):
        run = m.group(0)
        if run[0].isascii() or len(run) == 1:
            out.append(run)                                 # ASCII 整词 / 单汉字
        else:
            out.extend(run[i:i+2] for i in range(len(run)-1))  # 汉字二字滑窗
    return " ".join(out)
```

`"反向代理"` → `"反向 向代 代理"`，`"反向代理配置"` → `"反向 向代 代理 理配 配置"`，查询同样展开，共享三个 token。

**为什么 ASCII 不能一起 bigram（这是 IDF 算出来的，不是口味）：** 英文只有 26 个字母、二字组才 676 种，任意英文文本都覆盖绝大多数——实测英文 bigram 平均命中库里 73.7% 的文档、IDF 只有 0.31，切碎等于往候选池灌噪声。真实数字：`"restic"` 切成 `re/es/st/ti/ic` 后命中 27,356 条而真命中仅 206 条（**133 倍差距**）；整词保留后回落到 206。`"space-y-2"` 从 6,415 条 → 22 条。

**为什么汉字必须自己切：** `unicode61` 不切汉字，整段会变成一个 token，等于精确整串匹配，差一个字就查不到。二字滑窗够用——现代汉语双音节词占多数，实测中文 bigram 平均 IDF 3.59（`"端口"` 587 篇占 1.1%，`"快照"` 1,074 篇占 2.0%）。

代价是 FTS 表比正文大 1.5~2 倍，换来的是零依赖的中文召回。

## 三、分块策略

`MarkdownChunker` 按标题层级切，不是按字符数硬切。

```python
def split_markdown_into_sections(self, content, file_path):
    # 状态机：跟踪当前标题栈 + 是否在代码围栏内
    for line in lines:
        if self._fence_marker(line):        # ``` 或 ~~~
            in_code_block = not in_code_block
            continue                        # 围栏内的 # 不是标题！
        if in_code_block:
            continue
        if heading_match:
            flush_section()                 # 遇到标题就切
            update_heading_stack(level, text)
```

三个关键设计：

**1. 代码围栏内的 `#` 不算标题。** 否则一段 bash 注释（`# 重启服务`）会把 chunk 从中间劈开。

**2. 每个 chunk 的 `heading_path` 拼进向量文本。** 不只是存进字段——计算 embedding 时用的是 `heading_path + "\n" + content`。标题里全是领域信号（「排障SOP」「配置」「部署」），扔掉太浪费。

**3. 短块尝试合并，超长强制切。** `min_chunk_chars=200` 以下尝试和邻居合并；`max_chunk_chars=1500` 以上按段落/围栏边界切，绝不切在代码块中间。

### 实测分块行为

一篇 400 字符、有两个 `##` 章节的短文档，实测**只切出 1 个 chunk**，内容包含全文两个章节：

```
--- 测试文档 3 > 章节 A
## 章节 A
这是第 3 篇测试文档的 A 章节...
## 章节 B
B 章节讲的是 nginx 反向代理配置与 upstream 负载均衡。
```

原因：`min_chunk_chars=200` 让两个短章节被合并了。**这是设计行为，不是 bug**——把一句话的章节单独成块只会让检索噪声变多。

要让它切得更碎就把 `min_chunk_chars` 调小（如 80），代价是同主题内容散落在多块、召回时要多捞几块。

## 四、混合检索 + RRF

两条路并行，各自出候选，再用 RRF（Reciprocal Rank Fusion）融合。

```
查询 "nginx upstream 负载均衡"
        │
        ├──► 向量路：embedding → 余弦 top-25
        │     （语义相近，"反向代理" 也能命中）
        │
        └──► FTS 路：bigram 展开 → BM25 top-20
              （字面精确，"upstream" 这种专名不会漏）
                        │
                   RRF 融合
        score(d) = Σ 1 / (k + rank_i(d))       k=60
                        │
                   Rerank 闸门
        rerank_score < 0.85 → 丢弃
                        │
                    按 RRF 分排序
```

**为什么按 RRF 排序而不是按重排分排序？**

这是踩过的坑。Qwen3-Reranker-8B 在长句上有**分数饱和**：top1 和 top2 的分数可能只差 1e-5（实测 0.9998 vs 0.9997），排序退化成随机。RRF 只用排名位置，天然免疫饱和。

所以重排分的角色是**去噪闸门**（这个块到底相不相关），不是排序依据。`rank_by: "rrf"` 就是这个意思。改成 `"reranker"` 会退回旧行为，效果更差。

## 五、注入链路

### 两个入口

```
入口 A：用户原话（每轮一次）
  用户消息 ──► 取原话 ──► 检索 ──► 一屏标题清单 ──► 拼进本轮 user message 尾部

入口 B：kb_query（每次工具调用一次）
  模型调工具 ──► 门禁提取 kb_query 参数 ──► 检索 ──► 标题清单
                                                  ──► 从工具参数里剥掉 kb_query
                                                  ──► 真正的工具照常执行
```

### 入口 B 的门禁是怎么做的

**问题**：想让模型每次调工具都带检索词，但不能改所有工具的 schema（那是核心代码，改了升级会冲突）。

**解法**：在 registry 出口打补丁。

```python
def _install_schema_guard():
    # 1. 劫持 get_definitions：给所有工具的 schema 加上 kb_query 必填字段
    orig_get_definitions = registry.get_definitions
    def patched_get_definitions(self, *args, **kwargs):
        defs = orig_get_definitions(self, *args, **kwargs)
        for d in defs:
            inject_kb_query_field(d)      # 加进 parameters.properties + required
        return defs

    # 2. 劫持 dispatch：执行前把 kb_query 剥掉，不让它进真正的 handler
    orig_dispatch = registry.dispatch
    def patched_dispatch(self, name, args, *pos_args, **kwargs):
        kb_query = extract_and_remove(args, "kb_query")
        if kb_query:
            inject_context(retrieve(kb_query))
        return orig_dispatch(self, name, args, *pos_args, **kwargs)
```

对豁免工具（`read_file`、`search_knowledge` 等本身就在读库的）不加这个字段。

### 单池归一 + 轮作用域去重

`search_knowledge` 工具和自动注入**共用同一个检索池和编号空间**：

```
本轮第一次注入  →  [1] 卡片A   [2] 卡片B
本轮第二次注入  →  [3] 卡片C              （A、B 已给过，不重复印）
模型主动查      →  [4] 卡片D              （同一个池子，编号接着数）
```

模型在清单里看到的 `[3]`，`read_file` 一定拿得到——因为编号就是它这轮的真实索引。这是「标题清单」方案能成立的前提。

### Fail-open

```python
def handle_pre_llm_call(self, **kwargs):
    try:
        ...检索、构造清单...
        return {"context": menu}      # 注入
    except Exception:
        logger.warning(...)
        return None                   # 不注入，对话照常
```

所有异常路径都返回 `None`。**检索失败绝不能挡住对话。**

## 六、写卡链路（可选）

`hermes-kb-write` 提供 `save_reflection` 工具，让 AI 在任务收尾时把经验写成卡片。

关键机制是**语义查重闸门**：写入前先拿卡片的标题+正文去检索，如果 top1 的相关度超过阈值，说明已经有类似的卡，拒绝写入并返回已存在的卡路径。

```
AI 调 save_reflection
       │
       ├─► 检查：这张卡还有效吗？结论被证伪的卡应该删掉不是追加
       │
       ├─► 语义检索现有卡库
       │       │
       │       ├─ 高分命中 → 返回 existing，让 AI 决定：追加 / 删了重写 / 强制新建
       │       └─ 无命中   → 写入
       │
       └─► 双根落盘（两个目录各写一份 md5 一致的副本）
```

**双根落盘**是为了让「知识内容」和「索引数据」分离：一个目录给人看/同步备份，一个给引擎索引。索引只建一份（`exclude_path_prefixes` 排掉镜像根），避免同一张卡的两份副本各占一个注入位。

## 七、失败模式与兜底

| 环节挂了 | 表现 | 兜底 |
|---|---|---|
| 嵌入服务不可达 | 新文件索引失败 | 已有索引照常可查（FTS 路仍工作） |
| 重排服务不可达 | `rerank_score=0` | 退回 RRF 排序（会配置 warn 日志） |
| 引擎初始化失败 | 插件加载失败 | Agent 正常工作，只是没注入 |
| 检索超时 | 本轮不注入 | fail-open |
| 守护进程挂了 | 文件改动不索引 | 手动 `cli sync`；或重启服务 |
| `tmpfs` 写满 | SQLite 报 ENOSPC | `TMPDIR` 指向真磁盘 |

## 八、为什么不用别的方案

**为什么不用 LlamaIndex / LangChain？** 它们假设你要建的是一套「文档问答应用」。我要的是「让 Agent 少犯错」——注入标题而不是正文、检索词从工具调用里长出来、fail-open，这些都不是它们的设计目标，硬套要写一堆绕过代码。

**为什么不用向量数据库服务？** 单机、单用户、6 万行。起 Qdrant/Milvus 的运维成本远超收益，且引入「服务挂了怎么办」这个新失败模式。

**为什么不用 FAISS？** 它只是索引库，还得自己管元数据、全文检索、增量更新。SQLite 一个文件全覆盖了。

**为什么注入标题不注入正文？** 见 README 第 1 节。核心理由：模型不需要的东西不要给它，但要让它在需要时能一步取到。
