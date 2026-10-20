#!/bin/bash
# 看板（仪表盘）脚本
# 用法: bash scripts/看板.sh
# 输出: 知识库全景统计 + 项目状态 + 日记连续天数

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TODAY=$(date +%Y-%m-%d)

echo "╔══════════════════════════════════════════╗"
echo "║         📊 知识库看板 — $TODAY         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 统计 ──
PROJ_COUNT=$(find "$ROOT_DIR/10-项目" -name "*.md" -not -path "*/.git/*" 2>/dev/null | wc -l)
KNOW_COUNT=$(find "$ROOT_DIR/20-知识" -name "*.md" -not -path "*/.git/*" 2>/dev/null | wc -l)
WRITE_COUNT=$(find "$ROOT_DIR/30-写作" -name "*.md" -not -path "*/.git/*" 2>/dev/null | wc -l)
DIARY_COUNT=$(find "$ROOT_DIR/40-日记" -name "*.md" 2>/dev/null | wc -l)
ARCHIVE_COUNT=$(find "$ROOT_DIR/50-档案" -name "*.md" 2>/dev/null | wc -l)
TOTAL=$((PROJ_COUNT + KNOW_COUNT + WRITE_COUNT + DIARY_COUNT + ARCHIVE_COUNT))

echo "📦 内容总量"
echo "  项目: $PROJ_COUNT | 知识: $KNOW_COUNT | 写作: $WRITE_COUNT"
echo "  日记: $DIARY_COUNT | 归档: $ARCHIVE_COUNT | 合计: $TOTAL"

# 标签数
TAG_COUNT=$(grep -rh '^tags: \[' "$ROOT_DIR" --include="*.md" 2>/dev/null | \
  sed 's/tags: \[//;s/\]//' | tr ',' '\n' | sed 's/^ *//;s/ *$//' | sort -u | wc -l)
echo "  标签: $TAG_COUNT 个"
echo ""

# ── 项目状态 ──
echo "📁 项目状态"
for proj_dir in "$ROOT_DIR/10-项目"/*/; do
  [ ! -d "$proj_dir" ] && continue
  [ "$(basename "$proj_dir")" = "_assets" ] && continue
  proj_name=$(basename "$proj_dir")
  readme="$proj_dir/README.md"
  if [ -f "$readme" ]; then
    status=$(grep -oP 'status:\s*\K\w+' "$readme" 2>/dev/null | head -1)
    priority=$(grep -oP 'priority:\s*\K\w+' "$readme" 2>/dev/null | head -1)
    # 统计任务完成情况
    total_tasks=$(grep -c '^\- \[.\]' "$readme" 2>/dev/null || echo 0)
    done_tasks=$(grep -c '^\- \[x\]' "$readme" 2>/dev/null || echo 0)
    icon="📄"
    [ "$status" = "active" ] && icon="🟢"
    [ "$status" = "paused" ] && icon="🟡"
    [ "$status" = "done" ] && icon="✅"
    echo "  $icon $proj_name  status=$status  priority=$priority  tasks=$done_tasks/$total_tasks"
  else
    echo "  ⬜ $proj_name  (无 README)"
  fi
done
echo ""

# ── 日记连续天数 ──
echo "📅 日记连续天数"
STREAK=0
d="$TODAY"
while true; do
  year=$(date -d "$d" +%Y 2>/dev/null)
  month=$(date -d "$d" +%m 2>/dev/null)
  f1="$ROOT_DIR/40-日记/$year/$month/$d.md"
  f2="$ROOT_DIR/40-日记/$d.md"
  if [ -f "$f1" ] || [ -f "$f2" ]; then
    STREAK=$((STREAK + 1))
    d=$(date -d "$d -1 day" +%Y-%m-%d 2>/dev/null)
  else
    break
  fi
  [ "$STREAK" -gt 365 ] && break  # 安全上限
done
echo "  连续写日记: $STREAK 天"
echo ""

# ── 悬空链接 ──
echo "🔗 链接健康"
BROKEN=$(bash "$ROOT_DIR/scripts/断链检查.sh" 2>/dev/null | grep -c "悬空")
echo "  悬空链接: ${BROKEN:-0} 个"
echo ""

# ── 最近更新 ──
echo "🕐 最近变更 (Top 5)"
find "$ROOT_DIR" -name "*.md" \
  -not -path "*/.git/*" \
  -not -path "*/scripts/*" \
  -not -path "*/00-索引/*" \
  -not -path "*/_模板/*" \
  -not -path "*/_spec/*" \
  -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn \
  | head -5 \
  | while IFS= read -r line; do
      ts=$(echo "$line" | cut -d' ' -f1)
      fp=$(echo "$line" | cut -d' ' -f2-)
      rel="${fp#$ROOT_DIR/}"
      mt=$(date -d "@$ts" '+%m-%d %H:%M' 2>/dev/null)
      echo "  $mt  $rel"
    done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
