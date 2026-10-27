# [示例·排障SOP] nginx upstream 全 502：keepalive 配了但 upstream 块缺 keepalive 指令

> 这是一张**示例卡**，数据全为编造。照这个结构写你自己的。

## 触发场景 / 业务背景

站点全部 502，nginx error log 里是这样：

```
2026-01-01 10:00:00 [error] 1234#0: *56 connect() failed (111: Connection refused)
while connecting to upstream, client: 203.0.113.10, server: example.com,
request: "GET /api/health HTTP/1.1", upstream: "http://127.0.0.1:8080/api/health"
```

直连后端 `curl 127.0.0.1:8080/health` 是通的，说明后端活着，问题在 nginx 到后端这一段。

## 根本原因与核心设计

**先试了什么、为什么不行：**

- 先怀疑后端挂了 → `systemctl status app` 正常，`curl` 直连 200，排除
- 再怀疑防火墙 → `iptables -L` 干净，`ss -tlnp` 看到 8080 在监听，排除
- 怀疑 `worker_connections` 不够 → 调大后无变化，排除

真正的根因：`upstream` 块里写了 `keepalive 32;`，但 `location` 块里**没有**配套的：

```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
```

HTTP/1.0 默认带 `Connection: close`，nginx 会尝试复用连接但后端立刻关掉，nginx 侧看到连接被复用的瞬间其实已经死了，于是报 `Connection refused`。

**关键认知：`keepalive` 指令单独存在不但没用，还会制造故障。** 它只是「允许」复用，实际复用需要协议版本和 Connection 头配合。

## 标准解决步骤 / CLI使用 / 关键代码

```nginx
upstream backend {
    server 127.0.0.1:8080;
    keepalive 32;                          # 保留
}

server {
    location /api/ {
        proxy_pass http://backend;

        proxy_http_version 1.1;            # ← 必须
        proxy_set_header Connection "";    # ← 必须，清空客户端传来的 Connection 头
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
    }
}
```

验证：

```bash
nginx -t && systemctl reload nginx
curl -sI https://example.com/api/health    # 期望 200
tail -f /var/log/nginx/error.log           # 应无新的 connect() failed
```

## 避坑铁律与硬性约束

- **`keepalive N;` 必须配 `proxy_http_version 1.1` + `proxy_set_header Connection ""`**，三者缺一即故障
- 报错是 `Connection refused` 而不是超时——这个细节排除了「后端慢」的可能，直接指向连接建立失败
- `upstream` 里如果有多台后端且其中一台挂了，默认会继续转发过去。要么配 `max_fails`/`fail_timeout`，要么配健康检查（开源版 nginx 没有主动健康检查，只能用 `upstream_check` 模块或改用 OpenResty）
