#!/bin/bash
# 搜索笔记脚本
# 用法:
#   bash scripts/搜索笔记.sh <关键词>              # 全文搜索
#   bash scripts/搜索笔记.sh --tag <标签>          # 按标签搜索
#   bash scripts/搜索笔记.sh --tag <标签> <关键词>  # 标签+关键词组合搜索
# 示例:
#   bash scripts/搜索笔记.sh "Docker"
#   bash scripts/搜索笔记.sh --tag python
#   bash scripts/搜索笔记.sh --tag python "API"

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 拼接排除规则
EXCLUDE="-not -path \"*/.git/*\" -not -path \"*/scripts/*\" -not -path \"*/00-索引/*\" -not -path \"*/_模板/*\" -not -path \"*/_spec/*\""

usage() {
  echo "用法:"
  echo "  bash scripts/搜索笔记.sh <关键词>              全文搜索"
  echo "  bash scripts/搜索笔记.sh --tag <标签>          按标签搜索"
  echo "  bash scripts/搜索笔记.sh --tag <标签> <关键词>  标签+关键词组合"
  echo ""
  echo "示例:"
  echo "  bash scripts/搜索笔记.sh \"Docker\""
  echo "  bash scripts/搜索笔记.sh --tag python"
  echo "  bash scripts/搜索笔记.sh --tag python \"API\""
  exit 1
}

TAG_FILTER=""
KEYWORD=""

# 解析参数
if [ "$1" = "--tag" ]; then
  if [ -z "$2" ]; then
    usage
  fi
  TAG_FILTER="$2"
  KEYWORD="$3"
elif [ -z "$1" ]; then
  usage
else
  KEYWORD="$1"
fi

# 按标签搜索
if [ -n "$TAG_FILTER" ]; then
  echo "========== 搜索: 标签 [$TAG_FILTER] =========="
  if [ -n "$KEYWORD" ]; then
    echo "        + 关键词 \"$KEYWORD\""
  fi
  echo ""

  COUNT=0
  while IFS= read -r file; do
    # 检查文件是否包含目标标签
    if grep -qE "^tags: .*\<${TAG_FILTER}\>.*" "$file" 2>/dev/null; then
      rel_path="${file#$ROOT_DIR/}"
      title=$(awk '/^---$/{c++; next} c==2 && /^# /{sub(/^# /,""); print; exit}' "$file")
      [ -z "$title" ] && title=$(basename "$file" .md)

      if [ -n "$KEYWORD" ]; then
        # 标签 + 关键词: 显示匹配行
        matches=$(grep -n "$KEYWORD" "$file" 2>/dev/null)
        if [ -n "$matches" ]; then
          echo "📄 [[$rel_path|${rel_path%.md}]]  — $title"
          echo "   $matches" | while IFS= read -r line; do echo "   $line"; done
          echo ""
          COUNT=$((COUNT + 1))
        fi
      else
        echo "📄 [[$rel_path|${rel_path%.md}]]  — $title"
        COUNT=$((COUNT + 1))
      fi
    fi
  done < <(eval "find \"$ROOT_DIR\" -name \"*.md\" $EXCLUDE")

  echo "---"
  echo "找到 $COUNT 个结果"
  exit 0
fi

# 全文搜索
if [ -n "$KEYWORD" ]; then
  echo "========== 搜索: \"$KEYWORD\" =========="
  echo ""

  grep -rn --color=always "$KEYWORD" \
    $(eval "find \"$ROOT_DIR\" -name \"*.md\" $EXCLUDE") 2>/dev/null \
    | while IFS= read -r line; do
        filepath=$(echo "$line" | cut -d: -f1)
        lineno=$(echo "$line" | cut -d: -f2)
        content=$(echo "$line" | cut -d: -f3-)
        rel_path="${filepath#$ROOT_DIR/}"
        echo "  $rel_path:$lineno: $content"
      done

  echo ""
  MATCH_COUNT=$(grep -rl "$KEYWORD" $(eval "find \"$ROOT_DIR\" -name \"*.md\" $EXCLUDE") 2>/dev/null | wc -l)
  echo "匹配 $MATCH_COUNT 个文件"
fi
