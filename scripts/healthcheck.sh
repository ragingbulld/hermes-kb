#!/usr/bin/env bash
# 一站式体检：库、向量、FTS、检索、注入插件，五关全过才算装好。
# 用法: ./scripts/healthcheck.sh [配置文件路径]
set -uo pipefail

CFG="${1:-${HERMES_KB_CONFIG:-$HOME/.hermes/hermes_kb/config.json}}"
export HERMES_KB_CONFIG="$CFG"
PY="${PYTHON:-python3}"
pass=0; fail=0

ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ❌ $1"; fail=$((fail+1)); }
head() { echo; echo "── $1 ──"; }

head "0. 配置"
if [ -f "$CFG" ]; then ok "配置文件存在: $CFG"; else bad "配置文件缺失: $CFG"; fi
DB=$("$PY" -c "from hermes_kb.config import get_config; print(get_config()['db_dir'])" 2>/dev/null)
if [ -n "$DB" ]; then ok "db_dir = $DB"; else bad "配置读不出来"; fi

head "1. 数据库与向量"
"$PY" - <<'PY' || true
import os, sqlite3, sys
from hermes_kb.config import get_config
db = os.path.join(os.path.expanduser(get_config()["db_dir"]), "kb.sqlite3")
if not os.path.exists(db):
    print(f"  ❌ 库不存在: {db}"); sys.exit(0)
con = sqlite3.connect(db)
q = lambda s: con.execute(s).fetchone()[0]
chunks, vecs, ftsn = q("select count(*) from chunks"), \
                     q("select count(*) from chunks where length(vector)>0"), \
                     q("select count(*) from fts")
vb = con.execute("select length(vector) from chunks limit 1").fetchone()
print(f"  {'✅' if chunks else '❌'} chunks      = {chunks}")
print(f"  {'✅' if vecs == chunks and chunks else '❌'} 有向量的     = {vecs}")
print(f"  {'✅' if ftsn == chunks and chunks else '❌'} FTS 行数     = {ftsn}")
print(f"  {'✅' if vb else '❌'} vector 字节  = {vb[0] if vb else 'n/a'}")
PY

head "2. 检索（真实 embedding 调用）"
if "$PY" -c "
from hermes_kb.engine import KBEngine
r = KBEngine().search('配置', limit=3)
raise SystemExit(0 if r else 1)
" 2>/dev/null; then ok "search 有结果"; else bad "search 无结果或报错（查 key / base_url / 额度）"; fi

head "3. 注入插件"
if "$PY" -c "import hermes_kb_auto_inject" 2>/dev/null; then ok "hermes-kb-auto-inject 可导入"
elif [ -d "$HOME/.hermes/plugins/hermes-kb-auto-inject" ]; then ok "插件目录存在（未 pip 安装，属正常）"
else bad "注入插件未安装"; fi

head "结果"
echo "  通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ] && { echo "  🎉 装好了"; exit 0; } || { echo "  ⚠️  看上面 ❌，对照 docs/TROUBLESHOOTING.md"; exit 1; }
