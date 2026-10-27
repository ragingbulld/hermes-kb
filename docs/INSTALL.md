# 安装教程（从零到跑通）

按顺序做，每一步都有验收命令。**任何一步验收不过，不要往下走**——后面所有步骤都建立在前一步真的成立上。

预计耗时：30 分钟（不含等模型下载/索引时间）。

---

## 0. 前置要求

| 项 | 要求 | 检查 |
|---|---|---|
| Python | ≥ 3.10（用了 `X | None` 语法） | `python3 -V` |
| 磁盘 | 每万 chunk 约 200MB（向量 + FTS） | `df -h` |
| 内存 | 索引时约 200MB / 万 chunk | `free -h` |
| 嵌入服务 | 任一 OpenAI 兼容 `/v1/embeddings` | 见步骤 2 |
| 网络 | 能连到嵌入服务 | `curl` 测 |

**不需要**：Docker、向量数据库、GPU、外部分词器、CUDA。

---

## 1. 装依赖

```bash
pip install watchdog numpy requests
```

对应关系：

| 包 | 干什么 | 缺了会怎样 |
|---|---|---|
| `watchdog` | 监听文件变化 | 守护进程起不来（`Observer` 导入失败） |
| `numpy` | 向量矩阵运算 | 检索报错（余弦计算用 numpy 做） |
| `requests` | 调嵌入/重排 API | 建库时全部 chunk 失败 |

**验收：**

```bash
python3 -c "import watchdog, numpy, requests; print('ok')"
```

> ⚠️ **如果你在 Hermes 里把插件依赖写在 `plugin.yaml` 的 `python_dependencies` 里**，注意 Hermes 每次更新会换 venv，依赖会丢，插件会在网关启动那刻加载失败——而钩子只在进程启动时注册一次，结果就是**注入静默归零**（没有报错，只是不再注入）。这是真实踩过的坑。装完依赖后确认 `plugin.yaml` 里声明了它们，Hermes 的 PM 会在构建插件环境时安装。

---

## 2. 挑一个嵌入服务

引擎只要求「OpenAI 兼容的 `/v1/embeddings`」，任何一家都行。

| 服务 | base_url | 模型示例 | 备注 |
|---|---|---|---|
| Gitee AI | `https://ai.gitee.com/v1` | `Qwen3-Embedding-8B` | 小批量有免费额度，重排也在这 |
| SiliconFlow | `https://api.siliconflow.cn/v1` | `BAAI/bge-m3` | 中文效果好，便宜 |
| OpenAI | `https://api.openai.com/v1` | `text-embedding-3-small` | 贵，中文一般 |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `bge-m3` | 零成本，要自己拉模型 |

**先验证服务通不通**（别等到建库时才排查）：

```bash
export EMB_KEY=sk-你的key
export EMB_URL=https://ai.gitee.com/v1
export EMB_MODEL=Qwen3-Embedding-8B

curl -s "$EMB_URL/embeddings" \
  -H "Authorization: Bearer $EMB_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$EMB_MODEL\",\"input\":\"测试\"}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('维度 =', len(d['data'][0]['embedding']))"
```

**期望**：打印出维度数字（如 `维度 = 1024`）。

**如果报错**：
- `401` → key 错或没传
- `404` → base_url 少了或多了 `/v1`
- `model not found` → 模型名拼错，或该服务没这个模型
- `超时` → 网络问题，换服务或检查代理

### 重排服务（可选但强烈建议）

```bash
export RR_URL=https://ai.gitee.com/v1/rerank
export RR_MODEL=Qwen3-Reranker-8B

curl -s "$RR_URL" \
  -H "Authorization: Bearer $RR_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$RR_MODEL\",\"query\":\"测试\",\"documents\":[\"文档一\",\"文档二\"]}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('返回', len(d['results']), '条打分')"
```

**期望**：`返回 2 条打分`。

> 不装重排也能用（把 `use_reranker` 设 `false`），但**写卡插件的查重会失效**——查重靠的就是重排分。如果要开写卡功能，重排必须配。

---

## 3. 安装代码

两种方式，选一种。

### 方式 A：pip 安装（推荐）

```bash
git clone https://github.com/<you>/hermes-kb.git
cd hermes-kb
pip install -e .
```

`pip install -e .` 会把仓库里的 `hermes_kb/` 包装成可导入模块，并装好 `watchdog`/`numpy`/`requests`。

> ⚠️ **别加 `--no-deps`。** 少了 `numpy` 建库照样成功、`search` 照样出结果，但**注入会静默失效**（只有协议没卡片），见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 头号陷阱。

### 方式 B：手工放（不想装包）

```bash
# 把仓库里的 hermes_kb/ 目录放到 python 找得到的地方
cp -r hermes_kb ~/.hermes/hermes_kb
pip install watchdog numpy requests          # 依赖要自己装
export PYTHONPATH="$HOME/.hermes:$PYTHONPATH"
```

**验收（两种方式通用）：**

```bash
python3 -c "from hermes_kb.config import get_config; print(get_config()['db_dir'])"
```

**期望**：打印出一行路径，不报 `ModuleNotFoundError`。

---

## 4. 写配置

```bash
mkdir -p ~/.hermes/hermes_kb
cp config/config.example.json ~/.hermes/hermes_kb/config.json
```

打开改这几项：

```jsonc
{
  // 索引数据放哪（会自动创建）
  "db_dir": "~/.hermes/data/hermes_kb",
  "state_db": "~/.hermes/data/hermes_kb/sync_state.sqlite",

  // ⚠️ 最重要：改成你自己的笔记目录，可以有多个
  "watch_dirs": [
    "~/notes",
    "~/work-docs"
  ],

  "embedding": {
    "base_url": "https://ai.gitee.com/v1",
    "api_key": "",              // 留空！用环境变量
    "model": "Qwen3-Embedding-8B",
    "dimensions": 1024,          // ⚠️ 必须和服务实际返回的维度一致
    "batch_size": 12,
    "timeout": 30
  },

  "reranker": {
    "base_url": "https://ai.gitee.com/v1/rerank",
    "api_key": "",              // 留空！用环境变量
    "model": "Qwen3-Reranker-8B"
  },

  "chunker": {
    "min_chunk_chars": 200,      // 小于这个值的相邻块会尝试合并
    "max_chunk_chars": 1500,     // 硬上限，超了强制切
    "target_chunk_chars": 800    // 正常打包目标
  },

  "retrieval": {
    "candidate_limit": 25,
    "min_rerank_score": 0.85,    // ⚠️ 换重排模型必须重标，见 TUNING.md
    "max_inject_blocks": 2,
    "rank_by": "rrf"             // 别改成 reranker，会因分数饱和退化
  }
}
```

### 密钥走环境变量

```bash
# 写进 ~/.bashrc 或 systemd 的 Environment=
export HERMES_KB_EMBED_API_KEY=sk-...
export HERMES_KB_RERANK_API_KEY=sk-...
```

`config.py` 里 `api_key` 默认读这两个环境变量。配置文件里写空字符串即可——**不要把自己的 key 提交进 git**。

**验收：**

```bash
python3 -c "
from hermes_kb.config import get_config
import os
c = get_config()
print('db_dir     =', c['db_dir'])
print('watch_dirs =', c['watch_dirs'])
print('embed key  =', '已设置' if os.environ.get('HERMES_KB_EMBED_API_KEY') else '❌ 没设')
print('维度        =', c['embedding']['dimensions'])
for d in c['watch_dirs']:
    print(('  ✅ ' if os.path.isdir(os.path.expanduser(d)) else '  ❌ 不存在 '), d)
"
```

**四个 `✅` 才能往下走。**

---

## 5. 首次建库

```bash
python -m hermes_kb.cli sync
```

**期望输出**（真实格式）：

```
[KBWatcher] Starting full scan...
[KBWatcher] Scan collected 128 files, 128 need reindexing
[KBWatcher] Batch #1: 12/12 files, 187 chunks, 143820 chars, 8.2s (progress 12/128)
[KBWatcher] Batch #2: 12/12 files, 203 chunks, 158100 chars, 9.1s (progress 24/128)
...
[KBWatcher] Full scan complete. Monitored: 128 files, reindexed: 128 files / 2104 chunks in 11 batches, errors: 0, pruned: 0, index time: 94.3s (22.3 chunks/s)
```

关键看三个数：
- `errors: 0` —— 有 error 说明部分文件索引失败
- `reindexed: N files / M chunks` —— M 应该明显大于 N（一篇笔记切成多块）
- `chunks/s` —— 正常 15~30。如果是个位数，多半是嵌入服务限速或网络慢

**验收：**

```bash
python3 -c "
from hermes_kb.engine import KBEngine
e = KBEngine()
print('已索引 chunk 数 =', e.count_rows())
e._ensure_vectors()          # 向量是懒加载，不先 load 会看到 size=0
print('向量 stats =', e.vector_stats())    # 期望 size == chunk 数
"
```

**期望**：`chunk 数 > 0`，且 `向量 stats` 的 `size` 与之相等。

> ⚠️ 直接调 `vector_stats()` 会看到 `size: 0`，那是**懒加载**不是没向量。
> 要么先 `_ensure_vectors()`，要么直接查库（见步骤 9 的硬验收）。

> ⚠️ **如果 `chunk 数 = 0`**：看 TROUBLESHOOTING.md 第 1 节。常见原因是文件太短（小于 `min_chunk_chars` 且没有标题），被当成空内容丢掉。

---

## 6. 检索验收（这一步最关键）

```bash
python -m hermes_kb.cli search "你的笔记里肯定有的某个词"
```

**期望输出：**

```
🔍 Search Query: nginx 反向代理 upstream
🎯 Results Found: 3
============================================================
[1] Score: 0.9883 | Path: 部署手册 > nginx 配置
    File: /home/you/notes/deploy/nginx.md
    Content Preview:
## nginx 配置
upstream backend { server 127.0.0.1:8080; }...
------------------------------------------------------------
```

**三个都要成立：**

1. `Results Found` 大于 0
2. `Score` 大于 0 —— **如果是 0.0000，说明重排没生效**（见 TROUBLESHOOTING 第 3 节）
3. `File` 指向的文件真的是你期望的那篇

**这一步过了，说明整条链路（分块 → 向量 → FTS → RRF → 重排）都通了。**

---

## 7. 起守护进程（可选，但推荐）

没有守护进程也能用，只是文件改了要手动 `sync`。

### 手工前台跑（先验证）

```bash
python -m hermes_kb.cli watch
```

**期望：**

```
[KB Daemon] Initializing knowledge base watcher...
[KB Daemon] Running initial scan...
[KBWatcher] Full scan complete...
[KB Daemon] Watching directory: /home/you/notes
[KB Daemon] Periodic storage compaction worker started (interval: 6h).
[KB Daemon] Real-time file observer started.
```

**测试实时索引：**

另开一个终端，往笔记目录扔个新文件：

```bash
cat > ~/notes/test-live.md <<'EOF'
# 实时索引测试

## 独特标记

内容包含 LIVETEST_MARKER_9x7 这个独有字符串。
EOF
```

等 3 秒，然后查：

```bash
python -m hermes_kb.cli search "LIVETEST_MARKER_9x7"
```

**期望**：能查到刚才那个文件。**查不到 = watchdog 没工作**（见 TROUBLESHOOTING 第 5 节）。

注意：`sync` 是给刚建库用的，`watch` 启动时**会先跑一次 full_scan**，所以生产环境重启守护进程会重新扫一遍（有文件 hash 比对，只重索引变化的，通常很快）。

### systemd 常驻

```bash
sudo tee /etc/systemd/system/hermes-kb.service > /dev/null <<'EOF'
[Unit]
Description=Local Markdown Knowledge Base (SQLite + auto index)
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER

# 环境变量：密钥放这里，不要写进配置文件
Environment=HERMES_KB_EMBED_API_KEY=sk-...
Environment=HERMES_KB_RERANK_API_KEY=sk-...

# TEMP 必须改！见下方说明
Environment=TMPDIR=/var/tmp/hermes-kb
Environment=TEMP=/var/tmp/hermes-kb

ExecStart=/usr/bin/python3 -m hermes_kb.cli watch
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo mkdir -p /var/tmp/hermes-kb
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-kb
```

**验收：**

```bash
systemctl is-active hermes-kb          # 期望 active
journalctl -u hermes-kb -n 20 --no-pager
```

日志里应看到 `Real-time file observer started.`。

> ⚠️ **`TMPDIR` 为什么要改**：SQLite 排序溢出会写临时文件。如果 `/tmp` 是 tmpfs（内存盘），大库排序会把它写爆，然后**同机其他进程也一起报 ENOSPC**（真的踩过，vitest 都跑挂了）。指向真磁盘的 `/var/tmp` 就没事。

---

## 8. 装注入插件（Hermes Agent 专用）

前面 7 步做完，你已经有了一个能查的知识库。这一步才是「让模型自动用上它」。

### 8.1 复制插件

```bash
cp -r plugins/hermes-kb-auto-inject ~/.hermes/plugins/
cp -r plugins/hermes-kb-write ~/.hermes/plugins/     # 可选：写卡功能
```

### 8.2 在 config.yaml 里启用

```yaml
plugins:
  enabled:
    - hermes-kb-auto-inject
    # - hermes-kb-write      # 要写卡才开
  entries:
    hermes-kb-auto-inject:
      allow_tool_override: false
```

### 8.3 重启网关

```bash
systemctl restart hermes-gateway    # 或你实际的进程名
```

> ⚠️ **必须重启进程，不是 reload 配置。** 钩子只在进程启动那一刻注册一次，热重载配置不会重新注册 `pre_llm_call`。这是最常见的「装了没生效」原因。

### 8.4 验收：注入真的发生了

```bash
journalctl -u hermes-gateway -n 200 --no-pager | grep -i "KBAutoInject"
```

日志里应该有 `code md5=... registered` 这样的行。**但这一行不能证明注入真的在工作**——它只证明模块被导入了。

> ⚠️ **`code md5=` 这个判据会骗人。** 任何日志里同时出现 `code md5=` 和 `hermes-kb-auto-inject` 的文本（包括你自己 grep 时把这段教程贴进终端）都能让它变绿。真判据只有下面这条。

**真判据：直调 `handle_pre_llm_call`，看返回值里有没有 `<knowledge-base-titles>`。**

```python
import sys, os, importlib.util, pathlib
sys.path.insert(0, os.path.expanduser("~/.hermes"))       # 让 hermes_kb 可导入

pkg = pathlib.Path(os.path.expanduser("~/.hermes/plugins/hermes-kb-auto-inject"))
spec = importlib.util.spec_from_file_location(
    "hermes_kb_auto_inject", pkg/"__init__.py",
    submodule_search_locations=[str(pkg)])
m = importlib.util.module_from_spec(spec); sys.modules["hermes_kb_auto_inject"] = m
spec.loader.exec_module(m)

out = m.plugin_instance.handle_pre_llm_call(
    session_id="install-check",
    messages=[{"role": "user", "content": "写一句你库里肯定有的关键词"}],
)
ctx = (out or {}).get("context", "")
print("总长      :", len(ctx))
print("含协议    :", "【工具调用协议：kb_query】" in ctx)
print("★含卡清单 :", "<knowledge-base-titles>" in ctx)    # ← 这个为 True 才算成功
```

**`含卡清单` 为 True 才算装好。** 为 False 就是检索断了——最常见原因是 `numpy` 没装在**当前这个解释器环境**里。

如果 False，先看 stderr 有没有 `retrieve summary error ... No module named 'numpy'`，有就是依赖问题；没报错则是库里没内容或检索词不命中（先用 `hermes-kb search` 单独确认库是好的）。

---

## 9. 完整验收清单

跑完所有步骤，逐条打勾：

```bash
# 1 依赖
python3 -c "import watchdog, numpy, requests" && echo "✅ 依赖"

# 2 导入
python3 -c "from hermes_kb.engine import KBEngine" && echo "✅ 包可导入"

# 3 配置
python3 -c "from hermes_kb.config import get_config; get_config()" && echo "✅ 配置可读"

# 4 索引非空
python3 -c "
from hermes_kb.engine import KBEngine
n = KBEngine().count_rows()
print(f'✅ 已索引 {n} chunks' if n > 0 else '❌ 索引是空的')
"

# 5 检索能出分
python3 -c "
from hermes_kb.engine import KBEngine
r = KBEngine().search('test', limit=1, rerank_top_n=1)
print(f'✅ 检索返回 {len(r)} 条' if r else '❌ 检索零命中')
print(f'   重排分 = {r[0].get(\"rerank_score\")}' if r else '')
"

# 6 服务在跑
systemctl is-active hermes-kb && echo "✅ 守护进程"

# 7 向量真的落库（绕过 Python 层，最硬）
python3 -c "
import sqlite3, os, sys
from hermes_kb.config import get_config
db = os.path.join(os.path.expanduser(get_config()['db_dir']), 'kb.sqlite3')
if not os.path.exists(db): print('❌ 库不存在:', db); sys.exit()
con = sqlite3.connect(db); q = lambda s: con.execute(s).fetchone()[0]
c, v, f = q('select count(*) from chunks'), q('select count(*) from chunks where length(vector)>0'), q('select count(*) from fts')
print(f'✅ chunks={c} 有向量={v} fts={f}' if c and c == v == f else f'❌ 不一致 chunks={c} 有向量={v} fts={f}')
"

# 8 注入真在工作（唯一可信判据）
#    跑上面步骤 8.4 那段 Python，确认 <knowledge-base-titles> 出现
```

**7 项全 ✅ + 步骤 8.4 的 `含卡清单=True`** 才算装完。

> 注意第 7 项：**四个数必须全部相等**。`fts` 表名就是 `fts`，不是 `chunks_fts`（自己写 SQL 验收时最容易错这里）。

---

## 10. 接非 Hermes 的其他框架

引擎是纯 Python 库，跟 Agent 框架无关：

```python
from hermes_kb.engine import KBEngine

engine = KBEngine()
results = engine.search("你的问题", limit=5, rerank_top_n=5)

for r in results:
    print(r["heading_path"], r["file_path"], r["rerank_score"])
    print(r["content"])          # 整块正文，直接喂给模型
```

`search()` 返回 dict 列表，字段：`file_path` / `heading_path` / `content` / `rerank_score` / `rrf_score` / `chunk_index`。

**注意**：生产环境复用 `KBEngine` 实例（构造时会 load 向量矩阵进内存，每次新建都重读一遍）。单例即可：

```python
_engine = None
def get_engine():
    global _engine
    if _engine is None:
        _engine = KBEngine()
    return _engine
```

---

## 下一步

- 调参（阈值、注入预算）：[TUNING.md](TUNING.md)
- 出问题了：[TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- 想懂原理：[ARCHITECTURE.md](ARCHITECTURE.md)
- 写卡片：格式在 [CARD-FORMAT.md](CARD-FORMAT.md)
