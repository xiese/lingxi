#!/bin/bash
# 第 18 项反向验证：逐条把修复改坏，确认 tests/test_health_lightweight.py 报红。
# 全绿 = 测试没有约束力，等于没修。
set -u
cd "$(dirname "$0")/.." || exit 1
RG=scripts/kb_lib/routes/routes_general.py
SM=scripts/kb_lib/search.py
US=scripts/update_stats.py
TMPD=$(mktemp -d)
cp "$RG" "$TMPD/rg.bak"; cp "$SM" "$TMPD/sm.bak"; cp "$US" "$TMPD/us.bak"
trap 'cp "$TMPD/rg.bak" "$RG"; cp "$TMPD/sm.bak" "$SM"; cp "$TMPD/us.bak" "$US"; rm -rf "$TMPD"' EXIT

OKS=0; FAILS=0
run_case() {
  local name="$1"
  # 加超时：变异若造成死锁，按「有约束力」算（测试确实报错了），但脚本不能挂死
  if timeout 120 python3 -m pytest tests/test_health_lightweight.py -q >/tmp/h18.out 2>&1; then
    echo "❌ $name —— 改坏了测试仍全绿（无约束力）"
    FAILS=$((FAILS+1))
  else
    echo "✅ $name —— 报红（有约束力）"
    OKS=$((OKS+1))
  fi
  cp "$TMPD/rg.bak" "$RG"; cp "$TMPD/sm.bak" "$SM"; cp "$TMPD/us.bak" "$US"
}

mutate() { python3 "$(dirname "$0")/_mut18.py" "$@"; }

# ① /health 改回同步 count_stale（原样，就是「卡」的根因）
mutate rg_sync "$RG"
run_case "① /health 改回同步 count_stale"

# ② peek_stale 冷启动返回 0（冒充「跟得上」）
mutate peek_zero "$SM"
run_case "② peek_stale 冷启动返回 0"

# ③ prime_stale_async 改成同步执行（阻塞请求）
mutate prime_sync "$SM"
run_case "③ prime_stale_async 改成同步"

# ④ 去掉「不重复起线程」的判断
mutate no_dedup "$SM"
run_case "④ 去掉不重复起线程判断"

# ⑤ update_stats 改回把 null 当 0
mutate us_null_zero "$US"
run_case "⑤ update_stats 改回 null 当 0"

# ⑥ _stale_from_health 脏值改回 0
mutate us_dirty_zero "$US"
run_case "⑥ _stale_from_health 脏值返回 0"

# ⑦ /health 不返回 stale_cached 字段
mutate no_cached_field "$RG"
run_case "⑦ /health 不返回 stale_cached"

# ⑧ count_stale 不写缓存（预热无效，/health 永远 null）
mutate count_no_cache "$SM"
run_case "⑧ count_stale 不写缓存"

# ⑨ 竞态改回：锁内判断、锁外赋值+start
mutate prime_race "$SM"
run_case "⑨ 竞态改回（锁外赋值）"

# ⑩ 渲染改回直接插值（输出字面 None）
mutate us_none_literal "$US"
run_case "⑩ 渲染改回直接插值"

echo "-----------------------------------------"
echo "合计：有约束力 $OKS 条，无约束力 $FAILS 条"
[ "$FAILS" -eq 0 ] || exit 1
exit 0
