#!/bin/bash
# 图谱生成脚本
# 用法: bash scripts/图谱生成.sh
# 输出: scripts/图谱数据.json

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 依赖检查
command -v jq >/dev/null 2>&1 || { echo "❌ 需要 jq，请先安装: apt install jq"; exit 1; }
OUTPUT="$ROOT_DIR/scripts/图谱数据.json"
NODES_JSON="[]"
LINKS_JSON="[]"

# 取标题：跳过 frontmatter（--- 到 ---），取第一个 # 开头的行
get_title() {
  local file="$1"
  local title
  title=$(awk '/^---$/{c++; next} c==2 && /^# /{sub(/^# /,""); print; exit}' "$file")
  if [ -z "$title" ]; then
    title=$(basename "$file" .md)
  fi
  echo "$title"
}

# 构建节点
while IFS= read -r file; do
  rel_path="${file#$ROOT_DIR/}"
  title=$(get_title "$file")
  NODES_JSON=$(echo "$NODES_JSON" | jq --arg id "$rel_path" --arg title "$title" \
    '. + [{"id": $id, "title": $title}]')
done < <(find "$ROOT_DIR" -name "*.md" \
  -not -path "*/.git/*" \
  -not -path "*/scripts/*")

# 构建链接
while IFS= read -r file; do
  rel_path="${file#$ROOT_DIR/}"
  while IFS= read -r link; do
    # 提取链接目标（支持 [[目标|别名]] 和 [[目标]] 两种格式）
    target=$(echo "$link" | sed -n 's/.*\[\[\([^]#|]*\).*/\1/p')
    [ -z "$target" ] && continue
    LINKS_JSON=$(echo "$LINKS_JSON" | jq --arg src "$rel_path" --arg tgt "$target" \
      '. + [{"source": $src, "target": $tgt}]')
  done < <(grep -o '\[\[[^]]*\]\]' "$file" 2>/dev/null)
done < <(find "$ROOT_DIR" -name "*.md" \
  -not -path "*/.git/*" \
  -not -path "*/scripts/*")

# 输出最终 JSON
jq -n \
  --argjson nodes "$NODES_JSON" \
  --argjson links "$LINKS_JSON" \
  '{nodes: $nodes, links: $links}' > "$OUTPUT"

NODE_COUNT=$(echo "$NODES_JSON" | jq 'length')
LINK_COUNT=$(echo "$LINKS_JSON" | jq 'length')

echo "图谱数据已生成: $OUTPUT"
echo "节点数: $NODE_COUNT"
echo "链接数: $LINK_COUNT"
