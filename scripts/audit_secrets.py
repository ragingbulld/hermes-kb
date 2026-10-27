"""脱敏审计（白名单版）——转 public 前的硬闸。"""
import re
from pathlib import Path

ROOT = Path('/root/hermes-kb-opensource')
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', 'node_modules',
             'hermes_kb.egg-info', '.venv', 'venv', 'build', 'dist'}

# 白名单：公开服务 / RFC 文档地址 / 占位符
WHITELIST = [
    'ai.gitee.com',          # 公开商业 API 服务，文档里作为示例提供方
    'api.siliconflow.cn',
    'api.openai.com',
    '/home/you/',            # 占位符，不是真实路径
    '/home/me/',             # 同上，文档里的示例路径
    '203.0.113.',            # RFC 5737 文档专用地址
    '127.0.0.1',             # 本地回环
    '192.0.2.',              # RFC 5737
    'audit_secrets.py',      # 审计脚本自身含检测用的正则字面量
]

RULES = [
    ("内网 IP",        re.compile(r'\b(?:192\.168)\.[\d.]+')),
    ("10.x 内网",      re.compile(r'\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')),
    ("172.16-31 内网", re.compile(r'\b172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}\b')),
    ("疑似真实 key",    re.compile(r'\bsk-[A-Za-z0-9_\-]{25,}')),
    ("本机 /root 路径", re.compile(r'/(?:root|home)/(?!you)[a-z]+/')),
    ("Bearer 占位残留", re.compile(r'Bearer\s+(?:\*+|<[^>]*>)')),
    ("占位 key 字样",   re.compile(r'(?i)your[-_ ]?key[-_ ]?here|YOUR_API_KEY')),
    ("私有 token",     re.compile(r'\b(?:ghp|gho|github_pat|xox[baprs])_[A-Za-z0-9]{20,}')),
    ("SSH 主机串",     re.compile(r'\b(?:root|admin)@[\w.-]+\.(?:local|lan|internal)\b')),
]

def iter_files():
    for p in ROOT.rglob('*'):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
            # 审计脚本自己含检测用的正则字面量，扫它必然是自匹配
            if p.name == 'audit_secrets.py':
                continue
            yield p

hits = []
for f in iter_files():
    try:
        txt = f.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        continue
    for lineno, line in enumerate(txt.splitlines(), 1):
        if any(w in line for w in WHITELIST):
            continue
        for name, rx in RULES:
            m = rx.search(line)
            if m:
                hits.append((str(f.relative_to(ROOT)), lineno, name,
                             m.group(0)[:60], line.strip()[:100]))

print("=" * 72)
print("脱敏审计（白名单：公开 API 服务 / RFC 文档地址 / 占位符）")
print("=" * 72)
if not hits:
    print("✅ 零命中 —— 可以转 public")
else:
    print(f"❌ {len(hits)} 处需处理：\n")
    for rel, ln, name, got, ctx in hits:
        print(f"  [{name}] {rel}:{ln}\n     命中: {got}\n     行  : {ctx}\n")

print("=" * 72)
print(f"扫描文件: {sum(1 for _ in iter_files())}")
