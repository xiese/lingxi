#!/bin/bash
# 摘要生成脚本 — 模块 06 记忆整合
# 用法: bash scripts/摘要生成.sh [天数]
# 扫描最近 N 天内变更的笔记，提取概述摘要
# 默认: 1 天（24小时）
# 输出: 00-索引/摘要队列.md

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DAYS="${1:-1}"
OUTPUT="$ROOT_DIR/00-索引/摘要队列.md"
TODAY=$(date +%Y-%m-%d)

# 提取概述：跳过 frontmatter，取 ## 概述 下的第一段内容
get_summary() {
  local file="$1"
  awk '
    /^---$/ { c++; next }
    c>=2 && /^## 概述/ { in_summary=1; next }
    in_summary && /^$/ { next }
    in_summary && /^#/ { exit }
    in_summary && length($0)>0 { print; exit }
    END { if(!in_summary) print "(无概述)" }
  ' "$file"
}

echo "---" > "$OUTPUT"
echo "id: \"idx-summary-queue\"" >> "$OUTPUT"
echo "title: \"摘要队列\"" >> "$OUTPUT"
echo "type: index" >> "$OUTPUT"
echo "created: $TODAY" >> "$OUTPUT"
echo "updated: $TODAY" >> "$OUTPUT"
echo "---" >> "$OUTPUT"
echo "" >> "$OUTPUT"
echo "# 📋 摘要队列" >> "$OUTPUT"
echo "" >> "$OUTPUT"
echo "> 最近 ${DAYS} 天内变更的笔记摘要，供记忆系统消费。" >> "$OUTPUT"
echo "> 生成时间: $(date '+%Y-%m-%d %H:%M')" >> "$OUTPUT"
echo "" >> "$OUTPUT"

COUNT=0
while IFS= read -r file; do
  rel_path="${file#$ROOT_DIR/}"
  title=$(awk '/^---$/{c++; next} c==2 && /^# /{sub(/^# /,""); print; exit}' "$file")
  [ -z "$title" ] && title=$(basename "$file" .md)
  summary=$(get_summary "$file")
  tags=$(grep -oP 'tags:\s*\[([^\]]*)\]' "$file" 2>/dev/null | head -1 | sed 's/tags:\s*\[//;s/\]//')

  echo "## [[$rel_path|${rel_path%.md}]]" >> "$OUTPUT"
  echo "- **标题**: $title" >> "$OUTPUT"
  echo "- **标签**: ${tags:-(无)}" >> "$OUTPUT"
  echo "- **摘要**: $summary" >> "$OUTPUT"
  echo "" >> "$OUTPUT"
  COUNT=$((COUNT + 1))
done < <(find "$ROOT_DIR" -name "*.md" \
  -mtime -"${DAYS}" \
  -not -path "*/.git/*" \
  -not -path "*/scripts/*" \
  -not -path "*/00-索引/*" \
  -not -path "*/_模板/*" \
  -not -path "*/_spec/*")

echo "---" >> "$OUTPUT"
echo "_共 $COUNT 条摘要_" >> "$OUTPUT"

echo "✅ 摘要队列已生成: $OUTPUT"
echo "   共 $COUNT 条摘要"
