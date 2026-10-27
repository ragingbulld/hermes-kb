# reflections/ —— 你的卡片放这里

**这个目录是空的，而且本该是空的。**

开源的是这套知识库的**框架**，不是我的知识内容。这 2000 多张卡里全是我自己环境的内网 IP、主机名、项目细节——对别人没用，还泄漏隐私。

你用这套框架，装的是你自己的知识。把这个目录换成你的笔记路径，或者删掉它、把 `watch_dirs` 指向你现有的笔记目录。

## 目录约定

```
reflections/
├── 2026-01-15-1030-排障SOP_nginx_upstream_全部_502_keepalive_缺少_http11.md
├── 2026-01-20-1420-工具链路_SQLite_FTS5_bigram_中文召回率提升.md
├── 2026-02-03-0915-经验_Reranker_长句分数饱和_satern_须改用_RRF.md
└── ...
```

文件名建议 `日期-时间-类型_标题片段.md`，不是强制，但有两个好处：

1. `ls` 出来按时间排序，能看出知识积累的轨迹
2. 检索结果里光看路径就知道大概内容

`kind` 前缀决定文件名首段：

| kind | 前缀 |
|---|---|
| `排障SOP` | 出了什么问题、怎么修的 |
| `工具链路` | 搭了什么工具、怎么用 |
| `经验` | 判断、取舍、反直觉的实测结论 |
| `规约SOP` | 必须遵守的规则、配置约束 |

## 卡片格式

完整规范见 [../../docs/CARD-FORMAT.md](../../docs/CARD-FORMAT.md)。

两个可以直接照抄的示例：

- [../sample-card-troubleshooting.md](../sample-card-troubleshooting.md) — 排障类
- [../sample-card-experience.md](../sample-card-experience.md) — 经验类

最小可用的卡片长这样：

```markdown
# [排障SOP] nginx upstream 全 502：keepalive 缺少配套指令

## 触发场景 / 业务背景
站点全 502，error log 报 `connect() failed (111: Connection refused)`。

## 根本原因与核心设计
`keepalive 32;` 配了，但 location 块缺 `proxy_http_version 1.1` 和
`proxy_set_header Connection ""`，导致连接复用瞬间失效。

## 标准解决步骤 / CLI使用 / 关键代码
```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
```
`nginx -t && systemctl reload nginx`

## 避坑铁律与硬性约束
`keepalive N;` 必须配那两个指令，三者缺一即故障。
```

## 让 `watch_dirs` 指到这里

```json
{
  "watch_dirs": ["~/hermes-kb-opensource/examples/reflections"]
}
```

或者你已经有自己的笔记目录，直接指过去更省事：

```json
{
  "watch_dirs": ["~/notes", "~/work-docs"]
}
```

**不用非得叫 `reflections`。** 这个目录名只是沿用「反思卡」的叫法，任何 Markdown 目录都能索引。
