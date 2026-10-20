#!/bin/bash
# 知识库 API 回归测试
# 用法: bash scripts/API测试.sh

API="http://localhost:8899"
KB_ROOT="${KB_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
MASTER=$(python3 -c "import json,sys;s=json.loads(open(sys.argv[1]).read());print(s['master']['token'])" "$KB_ROOT/.api_tokens.json")
PASS=0; FAIL=0

test() {
  local name="$1"; local method="$2"; local url="$3"; local expected="$4"; local data="$5"
  local code
  if [ "$method" = "POST" ]; then
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST "$url" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" -d "$data" 2>/dev/null)
  elif [ "$method" = "GETN" ]; then
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null)
  else
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 -X "$method" "$url" -H "Authorization: Bearer $MASTER" 2>/dev/null)
  fi
  if [ "$code" = "$expected" ]; then
    echo "  ✅ $name (HTTP $code)"; PASS=$((PASS+1))
  else
    echo "  ❌ $name (期望 $expected, 实际 $code)"; FAIL=$((FAIL+1))
  fi
}

echo "════════ 知识库 API 测试 ════════"
echo ""

echo "── 基础 ──"
test "健康检查(无需认证)" GETN "$API/health" 200
test "首页" GET "$API/" 200
test "仪表盘" GET "$API/dashboard" 200

echo "── 笔记 CRUD ──"
test "列出笔记" GET "$API/notes?limit=5" 200
test "读取笔记" GET "$API/notes/README.md" 200
test "搜索" GET "$API/search?q=知识库" 200

echo "── 图谱 ──"
test "知识图谱" GET "$API/api/graph" 200
test "项目列表" GET "$API/api/projects" 200

echo "── 搜索 ──"
test "语义搜索" GET "$API/api/search/semantic?q=项目" 200
test "索引状态" GET "$API/api/index/status" 200

echo "── LLM ──"
test "智能标签" POST "$API/api/llm/tags" 200 '{"text":"测试标签推荐"}'

echo "── 管理(Master Only) ──"
test "审计日志" GET "$API/admin/audit?page=1&limit=2" 200
test "审计统计" GET "$API/admin/audit/stats" 200
test "系统设置" GET "$API/admin/settings" 200

echo ""
echo "════════════════════════════════"
echo "  通过: $PASS | 失败: $FAIL | 总计: $((PASS+FAIL))"
echo "════════════════════════════════"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
