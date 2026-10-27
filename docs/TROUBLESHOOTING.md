# 故障排查

按现象找。每条都给出「怎么确认」和「怎么修」。

---

## 1. 索引是空的（`count_rows() == 0`）

### 怎么确认

```bash
python3 -c "from hermes_kb.engine import KBEngine; print(KBEngine().count_rows())"
python -m hermes_kb.cli sync   # 看输出
```

看 `sync` 输出的三个数：`collected N files` / `reindexed M files / K chunks` / `errors: E`。

### 常见原因

| 现象 | 原因 | 修法 |
|---|---|---|
| `collected 0 files` | `watch_dirs` 路径错 / 不存在 / 无读权限 | `ls` 那个目录；注意 `~` 是否被 shell 展开 |
| `collected N` 但 `reindexed 0` | 文件 hash 和状态库一致，判定无需重索引 | 正常。想强制重建就删 `state_db` |
| `reindexed N` 但 `chunks 0` | **文件太短且无标题**，被当成空内容丢弃 | 见下 |
| `errors: N > 0` | 嵌入 API 报错 | 看日志里的具体错误，通常 401/429/超时 |

### 「文件太短且无标题」

`min_chunk_chars=200` 是合并下限，但如果整个文件**没有 `#` 标题且内容极短**，chunker 产不出有效块。

验证：

```bash
python3 -c "
from hermes_kb.chunker import MarkdownChunker
c = MarkdownChunker(200, 1500, 800)
s = c.split_markdown_into_sections(open('你的文件.md').read(), 'x.md')
print('切出', len(s), '块')
for x in s: print('  path=', x['heading_path'], 'len=', len(x['content']))
"
```

**修法**：给文件加个 `#` 标题。真的只有一句话的文件本来也不值得进知识库。

### `exclude_*` 过滤掉太多

```bash
python3 -c "
from hermes_kb.watcher import KnowledgeWatcher
w = KnowledgeWatcher()
print('排除规则:', w.config['source_filters'])
"
```

注意 `exclude_path_prefixes` 是**前缀匹配**，写 `/home/me/notes` 会连 `/home/me/notes-old` 一起排掉。

---

## 2. 检索零命中

先分清是「检索坏了」还是「库里真没有」。

```bash
# 找个你确定库里有的独特字符串
grep -rl "某个独特的词" ~/notes | head -3

# 用它查
python -m hermes_kb.cli search "那个独特的词"
```

### 如果用独特词也查不到

说明索引没建进去（回到第 1 节），或者查的是被排除的目录。

### 如果词能查到但语义查询查不到

```bash
python3 -c "
from hermes_kb.engine import KBEngine
e = KBEngine()
# 分别测两条路
r1 = e.search('你的查询', limit=5, rerank_top_n=5)
print('带重排:', len(r1))
r2 = e.search('你的查询', limit=5, rerank_top_n=5, use_reranker=False)
print('不带重排:', len(r2))
"
```

- **不带重排能查到、带重排查不到** → 阈值太高，见第 3 节
- **两个都查不到** → 向量和 FTS 都没命中，检查 `dimensions` 配置是否和服务实际返回一致

### 检查维度是否匹配

**这是最隐蔽的坑**：配置写 1024，服务返回 768，建库时向量存了 768 维，但查询时按 1024 去算，要么报错要么算出垃圾。

```bash
python3 -c "
from hermes_kb.engine import KBEngine
e = KBEngine()
v = e.compute_embedding('测试')
print('实际维度 =', len(v))
from hermes_kb.config import get_config
print('配置维度 =', get_config()['embedding']['dimensions'])
"
```

两个数必须相等。不等就改配置，**然后删库重建**（旧向量作废）。

---

## 3. `rerank_score` 恒为 0

分两种：**服务没配** vs **服务配了但失败**。

```bash
python3 -c "
from hermes_kb.engine import KBEngine
e = KBEngine()
r = e.rerank_pairs('测试查询', ['文档一', '文档二'])
print(r)
"
```

- 返回 `[]` 或 `None` → 服务调用失败
- 返回有分数但 `search()` 里是 0 → 说明 `use_reranker: false` 或配置没读到

### 手工验证重排端点

```bash
curl -s "$HERMES_KB_RERANK_BASE_URL" \
  -H "Authorization: Bearer $HERMES_KB_RERANK_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$HERMES_KB_RERANK_MODEL\",\"query\":\"q\",\"documents\":[\"a\",\"b\"]}"
```

看 HTTP 状态和返回体。常见错误：

| 错误 | 原因 |
|---|---|
| `404` | base_url 少了 `/rerank`，或该服务没有 rerank 接口 |
| `model not found` | 模型名错。注意有些服务模型名带前缀（`BAAI/bge-reranker-v2-m3`） |
| `403` | key 没开通 rerank 权限 |
| 返回格式不是 `{"results":[...]}` | 该服务不是 OpenAI rerank 兼容格式 |

### 写卡查重依赖重排

如果 `use_reranker: false`，`hermes-kb-write` 的查重会**静默失效**（不报错，只是不再拦截重复卡）。要么开重排，要么别用写卡功能。

---

## 4. 注入没生效（插件装了但模型没拿到知识）

**99% 是进程没重启。**

### 怎么确认插件加载了

```bash
journalctl -u hermes-gateway -n 300 --no-pager | grep -i "KBAutoInject"
```

**必须有这一行：**

```
hermes-kb-auto-inject code md5=abc123def456 (1234 行, 7 个模块) registered — ...
```

### 没有这一行 → 逐项排查

| 检查 | 命令 |
|---|---|
| 插件目录在、名字对 | `ls ~/.hermes/plugins/hermes-kb-auto-inject/` |
| `plugin.yaml` 可读 | `cat ~/.hermes/plugins/hermes-kb-auto-inject/plugin.yaml` |
| 在 `config.yaml` 里 enable 了 | `grep -A5 'plugins:' ~/.hermes/config.yaml` |
| Python 依赖装在**当前 venv** | `systemctl cat hermes-gateway \| grep ExecStart` 看用的是哪个 python，然后 `<那个python> -c "import watchdog"` |
| 没有语法错误 | `<那个python> -c "import ast; ast.parse(open('.../__init__.py').read())"` |

### 有这一行但注入还是没内容

说明插件加载了但检索返回空。看日志里有没有：

```
<KBAutoInject> 检索失败: ...
<KBAutoInject> 零命中 ...
```

**逐层验证：**

```bash
# 1. 引擎本身能查吗（用同一个 python！）
<网关的python> -c "
import sys; sys.path.insert(0, '/path/to/hermes_kb_parent')
from hermes_kb.engine import KBEngine
print(KBEngine().count_rows())
print(len(KBEngine().search('test', limit=3)))
"

# 2. 插件用的 config 和引擎用的是同一份吗
<网关的python> -c "from hermes_kb.config import get_config; print(get_config()['db_dir'])"

# 3. 阈值是不是把结果全砍了
<网关的python> -c "
from hermes_kb.config import get_config
print('阈值 =', get_config()['retrieval']['min_rerank_score'])
"
```

### 最容易忽略的：插件和引擎用了不同的 python

插件跑在网关的 venv 里，你手工测用的是系统 python。**如果 `hermes_kb` 只装在其中一边，另一边就 import 不到**，插件静默失败。

```bash
# 看网关真身用的哪个解释器
PID=$(systemctl show -p MainPID --value hermes-gateway)
tr '\0' '\n' < /proc/$PID/environ | grep -E 'PATH=|VIRTUAL_ENV'
ls -l /proc/$PID/exe      # ← 这一行才是真的
```

---

## 5. 文件改了但索引不更新

### 先确认守护进程在跑

```bash
systemctl is-active hermes-kb
journalctl -u hermes-kb -n 30 --no-pager
```

### 手工测实时索引

```bash
echo "# 实时测试 $(date +%s)" > ~/notes/rt-test.md
sleep 3
journalctl -u hermes-kb -n 10 --no-pager | grep -i "sync\|index"
```

**3 秒内应该有索引日志。** 没有就是 watchdog 没收到事件。

### 常见原因

| 原因 | 修法 |
|---|---|
| 文件在 `exclude_*` 里 | 检查规则 |
| 文件后缀不支持 | 只吃 `.md` / `.txt` 等文本格式 |
| 网络盘 / SMB 挂载 | inotify 在部分网络文件系统上不工作，改用手动 `sync` 或定时任务 |
| 编辑器「原子保存」 | 写入临时文件再 rename，`on_moved` 可能漏。用 `on_created` + `on_modified` 双保险（已在代码里） |
| `inotify` 达到上限 | `cat /proc/sys/fs/inotify/max_user_watches`，太小就调大 |

### 兜底：定期全量同步

```bash
# 每 30 分钟补一次（看门狗漏了就靠这个）
*/30 * * * * cd /home/me && /usr/bin/python3 -m hermes_kb.cli sync >> /tmp/kb-sync.log 2>&1
```

`sync` 有 hash 比对，没有变化的文件不会重新索引，跑得很快。

---

## 6. SQLite 相关错误

### `database is locked`

SQLite 默认单写者。守护进程在写、你同时手工 `sync`，就会撞。

```bash
# 看有没有多个进程在跑
ps aux | grep -E 'hermes_kb|kb.*daemon'
```

**修法**：只留一个。或者临时停服务再手工 sync。

代码里已经有 30 秒 busy timeout，一般会自动等待，报这个说明等待超时了。

### `disk I/O error` / `ENOSPC`

**`/tmp` 是 tmpfs 的经典坑**：SQLite 排序溢出写临时文件，把内存盘写满，然后**同机所有进程一起报 ENOSPC**。

```bash
df -h /tmp
mount | grep /tmp
```

**修法**：把 `TMPDIR` 指到真磁盘。

```bash
# systemd
Environment=TMPDIR=/var/tmp/hermes-kb
Environment=TEMP=/var/tmp/hermes-kb

# 手工跑
export TMPDIR=/var/tmp/hermes-kb
```

### 库文件越来越大

```bash
python3 -c "
from hermes_kb.engine import KBEngine
e = KBEngine()
print('优化结果:', e.optimize_table(older_than_days=1))
print('向量统计:', e.vector_stats())
"
```

`optimize_table` 会清死文件切片 + `PRAGMA optimize` + 条件 `VACUUM`。守护进程每 6 小时自动跑一次。

---

## 7. 注入内容有但模型不照着做

这**不是**检索的问题，是提示词的问题。

可能原因：

| 原因 | 表现 | 修法 |
|---|---|---|
| 注入的是标题清单，模型没去 read_file | 模型说「我没有这个信息」 | 在系统提示里明确：看到 `<knowledge-base-titles>` 要 read_file |
| 卡本身写得烂 | 模型读了但没用上 | 改进卡片质量，见 CARD-FORMAT.md |
| 注入位置在末尾，被长上下文淹没 | 上下文很长时失效 | 减少单轮上下文，或提高注入优先级 |
| 检索词不对 | 注入的是无关卡 | 见 TUNING.md 第 4 节 |

### 验证注入内容真的进了模型上下文

不同框架方式不同。Hermes 下可以看 gateway 日志里的请求体，或临时在插件里加一行日志打印注入的文本长度。

**注意**：注入是「只进发给模型的副本」，**不写回会话历史**。所以你在对话记录里看不到注入内容，这是设计如此（否则下一轮会重复注入）。

---

## 8. 性能问题

### 检索慢

```bash
python3 -c "
import time
from hermes_kb.engine import KBEngine
e = KBEngine()
t = time.time(); r = e.search('测试', limit=25, rerank_top_n=25); print(f'{(time.time()-t)*1000:.0f} ms')
t = time.time(); r = e.search('测试', limit=25, rerank_top_n=25, use_reranker=False); print(f'不带重排: {(time.time()-t)*1000:.0f} ms')
"
```

两个数对比就知道慢在哪：

- 都是几百毫秒 → 重排慢，砍 `reranker_max_candidates`
- 不带重排也要几秒 → 向量矩阵太大或磁盘慢

### 建库慢

`chunks/s` 低于 10 说明 embedding API 限速了。检查：

- `batch_size` 是否太小（默认 12，可以试 20~30）
- 服务是否有 QPS 限制
- 网络延迟

### 内存占用高

向量矩阵全在内存：`chunk 数 × 维度 × 4 字节`。6 万 chunk × 1024 维 ≈ 245 MB。

降低办法：换小维度模型（768 或 512），或者接受它——现代机器上 245MB 不算多。

---

## 9. 想完全重来

```bash
# 停服务
sudo systemctl stop hermes-kb

# 备份配置（万一改错了还能回去）
cp ~/.hermes/hermes_kb/config.json /tmp/kb-config-backup.json

# 删数据和状态（$HERMES_KB_DATA_DIR 指向的目录）
rm -rf ~/.hermes/data/hermes_kb

# 重建
export HERMES_KB_EMBED_API_KEY=sk-...
export HERMES_KB_RERANK_API_KEY=sk-...
python -m hermes_kb.cli sync

# 起服务
sudo systemctl start hermes-kb
```

**注意**：重建会重新调 embedding API，大库可能产生真实费用。先用小目录试。

---

## 10. 收集排查信息（报 issue 时用）

```bash
{
  echo "=== python ==="
  python3 -V
  echo "=== 依赖 ==="
  python3 -c "import watchdog, numpy, requests; print('watchdog', watchdog.version.VERSION); print('numpy', numpy.__version__); print('requests', requests.__version__)"
  echo "=== 配置（脱敏）==="
  python3 -c "
from hermes_kb.config import get_config
import json, re
c = get_config()
for k in ('embedding', 'reranker'):
    if c.get(k): c[k] = {kk: ('***' if 'key' in kk else vv) for kk, vv in c[k].items()}
print(json.dumps(c, indent=2, ensure_ascii=False))
"
  echo "=== 索引状态 ==="
  python3 -c "
from hermes_kb.engine import KBEngine
e = KBEngine()
print('chunks:', e.count_rows())
print('vectors:', e.vector_stats())
"
  echo "=== 日志 ==="
  journalctl -u hermes-kb -n 50 --no-pager
} > /tmp/kb-debug.txt 2>&1
```

**贴 issue 前把密钥、内网 IP、主机名替换掉。**

---

## 头号陷阱：注入"看起来成功"，实际一张卡都没注入

这是整套系统里**最危险的失败模式**，因为它不报错。

### 症状

- 会话照常跑，模型表现正常
- 插件加载日志显示成功
- `handle_pre_llm_call` 返回了 dict，`context` 长度 800+ 字符
- 但**那 800 字符全是 `kb_query` 协议提示词，一张知识卡都没有**

### 根因

`handle_pre_llm_call` 是 **fail-open** 设计：检索过程任何异常都被 `try/except` 吞掉，只 `logger.error` 一行，然后返回协议部分。协议是静态的，所以返回值永远非空——**"有返回值"不等于"注入成功"**。

真实案例（本项目 e2e 安装验证时踩到）：

```
[KBAutoInject] retrieve summary error for 'nginx upstream 502...': No module named 'numpy'
```

`numpy` 是 `hermes_kb` 的硬依赖（向量运算）。漏装时：

- 建库照样成功（`sync` 写在 SQLite 里，不碰 numpy）
- `hermes-kb search` 照样能出结果（走 FTS 那条路）
- **只有注入这条路断**，因为它要 `_ensure_vectors()` 做语义检索

于是「库是好的、检索也能用、注入却哑了」，排查方向会被彻底带偏。

### 怎么验（不要读源码推理，直接调）

```python
import sys, os, importlib.util, pathlib
sys.path.insert(0, "/path/to/hermes-kb-opensource")   # 换成你的路径
os.environ["HERMES_KB_CONFIG"] = "/path/to/config.json"

pkg = pathlib.Path("/path/to/hermes-kb-opensource/plugins/hermes-kb-auto-inject")
spec = importlib.util.spec_from_file_location(
    "hermes_kb_auto_inject", pkg/"__init__.py",
    submodule_search_locations=[str(pkg)])
m = importlib.util.module_from_spec(spec); sys.modules["hermes_kb_auto_inject"] = m
spec.loader.exec_module(m)

out = m.plugin_instance.handle_pre_llm_call(
    session_id="manual-test",
    messages=[{"role": "user", "content": "随便一句你库里肯定有的关键词"}],
)
ctx = (out or {}).get("context", "")
print("总长      :", len(ctx))
print("含协议    :", "【工具调用协议：kb_query】" in ctx)
print("★含卡清单 :", "<knowledge-base-titles>" in ctx)   # ← 只有这个为 True 才算真注入
```

**判据只有一条：`<knowledge-base-titles>` 必须出现。** 有协议没清单 = 检索断了。

### 三种常见断因

| `contains 清单` | 缺什么 | 修法 |
|---|---|---|
| False，且 stderr 有 `No module named numpy` | 依赖没装 | `pip install -e .` 重装（别只 `pip install -e . --no-deps`） |
| False，无任何报错 | 库里没内容 / 检索词不命中 | 先 `hermes-kb search` 手动确认库是好的 |
| False，报 `401`/`403` | key 没读到 | 查 `HERMES_KB_EMBED_API_KEY` 环境变量，见下条 |

---

## 第二陷阱：`config.json` 里写了 `"api_key": ""` 就把环境变量顶掉了

### 症状

```bash
export HERMES_KB_EMBED_API_KEY=sk-xxxx      # 明明设了
hermes-kb sync
# [KBWatcher] Batch #1 FAILED: embedding failed for 2/2 texts; refusing to write zero vectors
# Monitored: 2 files, reindexed: 0 files / 0 chunks
```

### 根因

`config.example.json` 里 `api_key` 是空串，复制成 `config.json` 后，深合并时**空串是有效值**，会把 `DEFAULT_CONFIG` 里从环境变量读到的 key 覆盖成 `""`。

代码层面已修（`_backfill_secrets()`：空串视为未设置，从环境变量回填）。如果你用的是修之前的版本，或者自己 fork 改过，检查：

```python
from hermes_kb.config import get_config
c = get_config()
print("embed key 长度:", len(c["embedding"]["api_key"]))   # 必须 > 0
print("rerank key 长度:", len(c["reranker"]["api_key"]))   # 必须 > 0
```

是 0 就说明被覆盖了。两个选择：

1. 升级到含 `_backfill_secrets` 的版本（推荐）
2. 直接在 `config.json` 里填真实 key（但**别提交进 git**，`.gitignore` 已排除 `config.json`）

---

## 第三陷阱：`vector_stats()` 显示 `size: 0` 但向量其实存在

### 症状

```python
e = KBEngine()
print(e.count_rows())        # 2  ✅
print(e.vector_stats())      # {'size': 0, 'capacity': 0, ...}  ❌ 看着像没向量
```

### 根因

向量是**懒加载**的：`__init__` 时只建 SQLite 连接，不读向量矩阵（省内存/启动时间）。`vector_stats()` 读的是内存矩阵的状态，没加载当然是 0。

### 修法

```python
e._ensure_vectors()          # 先加载
print(e.vector_stats())      # {'size': 2, 'capacity': 1024, ...}
```

或者绕过 Python 层直接查库，这个最硬：

```bash
python -c "
import sqlite3, os
con = sqlite3.connect(os.path.expanduser('~/.hermes/data/hermes_kb/kb.sqlite3'))
print('chunks  :', con.execute('select count(*) from chunks').fetchone()[0])
print('有向量的 :', con.execute('select count(*) from chunks where length(vector)>0').fetchone()[0])
print('字节数   :', con.execute('select length(vector) from chunks limit 1').fetchone()[0])
"
```

`字节数` 应该等于 `维度 × 4`（1024 维 → 4096）。

---

## 第四陷阱：FTS 表名是 `fts`，不是 `chunks_fts`

自己写 SQL 验收时最容易错的一处。实际建出来的是：

```sql
CREATE VIRTUAL TABLE "fts" USING fts5(bi, tokenize='unicode61');
```

**只有一列 `bi`**（bigram expanded，展开后的 token 串），靠隐式 `rowid` 关联 `chunks.id`。不是把 `content`/`heading_path`/`file_path` 分别建列。

所以 `select count(*) from chunks_fts` 会 `no such table`。正确的：

```sql
select count(*) from fts;      -- 期望等于 chunks 行数
```

---

## 第五陷阱：验收脚本自己把自己去重掉了

### 症状

`scripts/verify_injection.py` 第一次跑通过，再跑就失败：

```
★含卡清单      : False
❌ 注入没生效：有返回值但没有卡清单。
```

但什么都没改。日志里能看到 `inline MENU DUP skipped`。

### 根因

插件做**轮作用域去重**：同一个 `(session_id, 用户消息内容)` 算同一轮，同一张卡在一轮里只印一次。

验收脚本如果写死 `session_id="verify-injection"` 和固定检索词，**第二次运行时就是「同一轮又来一遍」**，于是清单被去重跳过——看起来像注入坏了，其实是去重在正常工作。

### 修法

`session_id` 每次用新的：

```python
import uuid
out = plugin.handle_pre_llm_call(
    session_id=f"verify-{uuid.uuid4()}",      # ← 必须
    messages=[{"role": "user", "content": query}],
)
```

仓库里的 `scripts/verify_injection.py` 已经这么写了，可反复跑。

### 记住这条区分

| 现象 | 含义 |
|---|---|
| `context` 只有 860 字符（纯协议），**且日志有 `DUP skipped`** | 去重生效，**正常** |
| `context` 只有 860 字符，**日志有 `retrieve summary error`** | 检索真断了（常见 `numpy` 缺失） |
| `context` 只有 860 字符，**日志什么都没** | 库里没内容 / 检索词不命中 |

**判据要连日志一起看，光看「有没有清单」会误判。**
