#!/bin/bash
# 周报生成脚本
# 用法: bash scripts/周报生成.sh [偏移周数]
# 默认生成本周周报，-1 = 上周
# 输出: 40-日记/周报-YYYY-WXX.md

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WEEK_OFFSET="${1:-0}"

# 计算周范围
if [ "$WEEK_OFFSET" -eq 0 ]; then
  WEEK_START=$(date -d "last monday" +%Y-%m-%d 2>/dev/null || date -d "last Monday" +%Y-%m-%d)
  WEEK_END=$(date -d "next sunday" +%Y-%m-%d 2>/dev/null || date -d "next Sunday" +%Y-%m-%d)
  WEEK_NUM=$(date +%W)
else
  WEEK_START=$(date -d "$WEEK_OFFSET weeks last monday" +%Y-%m-%d 2>/dev/null || date -v-${WEEK_OFFSET}w -v-mon +%Y-%m-%d)
  WEEK_END=$(date -d "$WEEK_OFFSET weeks next sunday" +%Y-%m-%d 2>/dev/null || date -v-${WEEK_OFFSET}w -v+sun +%Y-%m-%d)
  WEEK_NUM=$(date -d "$WEEK_OFFSET weeks" +%W)
fi

YEAR=$(date +%Y)
OUTPUT_DIR="$ROOT_DIR/40-日记"
mkdir -p "$OUTPUT_DIR"
OUTPUT_FILE="$OUTPUT_DIR/周报-${YEAR}-W${WEEK_NUM}.md"

echo "---" > "$OUTPUT_FILE"
echo "id: \"weekly-${YEAR}-W${WEEK_NUM}\"" >> "$OUTPUT_FILE"
echo "title: \"周报 ${WEEK_START} ~ ${WEEK_END}\"" >> "$OUTPUT_FILE"
echo "type: report" >> "$OUTPUT_FILE"
echo "tags: [周报]" >> "$OUTPUT_FILE"
echo "date: $WEEK_START" >> "$OUTPUT_FILE"
echo "created: $(date +%Y-%m-%d)" >> "$OUTPUT_FILE"
echo "---" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"
echo "# 📅 周报: ${WEEK_START} ~ ${WEEK_END}" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

DIARY_COUNT=0
# 遍历本周每一天
d="$WEEK_START"
while [ "$d" != "$(date -d "$WEEK_END +1 day" +%Y-%m-%d 2>/dev/null)" ]; do
  d_year=$(date -d "$d" +%Y)
  d_month=$(date -d "$d" +%m)
  diary_file="$ROOT_DIR/40-日记/$d_year/$d_month/$d.md"

  # 也尝试旧格式
  if [ ! -f "$diary_file" ]; then
    diary_file="$ROOT_DIR/40-日记/$d.md"
  fi

  if [ -f "$diary_file" ]; then
    DIARY_COUNT=$((DIARY_COUNT + 1))
    echo "## $d ($(date -d "$d" '+%A' 2>/dev/null || echo ''))" >> "$OUTPUT_FILE"
    echo "" >> "$OUTPUT_FILE"

    # 提取"今天做了什么"部分
    awk '
      /^## 今天做了什么/ { in_section=1; next }
      in_section && /^## / { exit }
      in_section && length($0)>0 { print }
    ' "$diary_file" >> "$OUTPUT_FILE"

    echo "" >> "$OUTPUT_FILE"
  fi

  d=$(date -d "$d +1 day" +%Y-%m-%d 2>/dev/null)
done

echo "## 统计" >> "$OUTPUT_FILE"
echo "- 写日记: $DIARY_COUNT 天 / 7 天" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"
echo "_自动生成于 $(date '+%Y-%m-%d %H:%M')_" >> "$OUTPUT_FILE"

echo "✅ 周报已生成: $OUTPUT_FILE"
echo "   日记天数: $DIARY_COUNT / 7"
