#!/bin/bash
# 索引更新脚本
# 用法: bash scripts/索引更新.sh
# 自动更新 00-索引 下的页面；内容根统一为 笔记/

set -euo pipefail

# 并发锁：避免多个索引更新同时写 00-索引（sed -i 互相覆盖）
exec 9>/var/lock/kb-index-update.lock
if ! flock -n 9; then
  echo "⚠️ 已有索引更新在运行，本次跳过"
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NOTES_DIR="$ROOT_DIR/笔记"
INDEX_DIR="$ROOT_DIR/00-索引"
TAGS_FILE="$INDEX_DIR/标签索引.md"
RECENT_FILE="$INDEX_DIR/最近更新.md"
README_FILE="$INDEX_DIR/README.md"

mkdir -p "$INDEX_DIR"

frontmatter_value() {
  local file="$1"
  local key="$2"
  if [ -f "$file" ]; then
    sed -n "s/^$key: //p" "$file" | head -n 1 | tr -d '"'
  fi
}

stable_created() {
  local file="$1"
  local fallback="$2"
  local value
  value=$(frontmatter_value "$file" "created")
  if [ -n "$value" ]; then
    echo "$value"
  else
    echo "$fallback"
  fi
}

RECENT_ALL_TMP=$(mktemp)
RECENT_TMP=$(mktemp)
find "$NOTES_DIR" -name "*.md" \
  -not -path "*/_模板/*" \
  -not -path "*/_assets/*" \
  -not -path "*/.git/*" \
  -printf '%T@ %p\n' 2>/dev/null \
  | sort -rn > "$RECENT_ALL_TMP"
head -20 "$RECENT_ALL_TMP" > "$RECENT_TMP"

latest_timestamp=$(head -n 1 "$RECENT_TMP" | cut -d' ' -f1)
if [ -n "$latest_timestamp" ]; then
  generated_at=$(date -d "@$latest_timestamp" '+%Y-%m-%d %H:%M' 2>/dev/null)
else
  generated_at=$(date -d "@0" '+%Y-%m-%d %H:%M' 2>/dev/null)
fi
generated_date=${generated_at%% *}

TAGS_CREATED=$(stable_created "$TAGS_FILE" "$generated_date")
RECENT_CREATED=$(stable_created "$RECENT_FILE" "$generated_date")

# --- 更新标签索引 ---
echo "---" > "$TAGS_FILE"
echo "id: \"idx-tags\"" >> "$TAGS_FILE"
echo "title: \"标签索引\"" >> "$TAGS_FILE"
echo "type: index" >> "$TAGS_FILE"
echo "created: $TAGS_CREATED" >> "$TAGS_FILE"
echo "updated: $generated_date" >> "$TAGS_FILE"
echo "---" >> "$TAGS_FILE"
echo "" >> "$TAGS_FILE"
echo "# 🏷 标签索引" >> "$TAGS_FILE"
echo "" >> "$TAGS_FILE"

# 收集所有标签（支持单行内联格式: tags: [a, b, c]）
# 单进程批量提取（2026-10-08 优化）：原实现逐文件 fork grep、每行 echo|sed、每标签 echo|xargs，
# 1600+ 笔记在负载机上实测 90+ 秒（更慢的机器上 pre-commit hook 数分钟跑不完、被迫 --no-verify）。
# 现改为一次 find 遍历 + 单个 awk 完成解析，输出记录后由下方 bash 按原语义聚合。
# 输出逐字节一致的语义保证：
#   1) 只处理 grep -E '^tags: ' 命中的行（行首 tags: + 空格）；
#   2) 每行按 sed 's/.*tags: \[\(.*\)\].*/\1/p' 提取：最右一个「其后尚有 ]」的 "tags: ["
#      到行内最后一个 "]" 之间的内容（贪婪匹配，模板注释行里的 tags: [..] 同样会被提取）；
#   3) 逗号分隔后逐项复刻 echo|xargs：去首尾空白、去引号、反斜杠转义、词间空白折叠为单空格；
#   4) 空标签跳过；同一 (文件, 行, 标签) 出现几次就记录几条（与原实现的追加行为一致）；
#   5) 记录顺序 = find 遍历序（与原逐文件循环一致），聚合时按首次出现顺序拼接。
declare -A TAG_FILES
TAG_RAW_TMP=$(mktemp)
find "$NOTES_DIR" -name "*.md" \
  -not -path "*/_模板/*" \
  -not -path "*/_assets/*" \
  -not -path "*/.git/*" \
  -exec awk -v root="$NOTES_DIR" '
    function xargs_norm(s,    n, i, c, nx, out, word, q, esc, pend, SQ) {
      # 复刻 echo "$s" | xargs：空白分词、单/双引号去引、反斜杠转义、词间以单空格拼接
      n = length(s); out = ""; word = ""; q = 0; esc = 0; pend = 0
      SQ = sprintf("%c", 39)
      for (i = 1; i <= n; i++) {
        c = substr(s, i, 1)
        if (esc) { word = word c; esc = 0; pend = 1; continue }
        if (c == "\\") {
          nx = substr(s, i + 1, 1)
          if (q == 2 && nx != "\"" && nx != "\\") { word = word "\\"; pend = 1; continue }
          esc = 1; pend = 1; continue
        }
        if (q == 0 && c == SQ) { q = 1; pend = 1; continue }
        if (q == 0 && c == "\"") { q = 2; pend = 1; continue }
        if (q == 1 && c == SQ) { q = 0; continue }
        if (q == 2 && c == "\"") { q = 0; continue }
        if (q == 0 && (c == " " || c == "\t" || c == "\n" || c == "\v" || c == "\f" || c == "\r")) {
          if (pend) { out = (out == "" ? word : out " " word); word = ""; pend = 0 }
          continue
        }
        word = word c; pend = 1
      }
      if (pend) { out = (out == "" ? word : out " " word) }
      return out
    }
    FNR == 1 {
      rel = substr(FILENAME, length(root) + 2)
      wiki = "笔记/" rel
    }
    /^tags: / {
      # 最右一个 "tags: ["
      q = 0
      while ((i = index(substr($0, q + 1), "tags: [")) > 0) q = q + i
      # 该括号后必须还有 "]""，否则回退到前一个 "tags: ["（复刻 sed 贪婪回退）
      while (q > 0) {
        p = 0
        while ((j = index(substr($0, p + 1), "]")) > 0) p = p + j
        if (p >= q + 7) break
        qn = 0
        prev = substr($0, 1, q - 1)
        while ((j = index(substr(prev, qn + 1), "tags: [")) > 0) qn = qn + j
        q = qn
      }
      if (q > 0) {
        content = substr($0, q + 7, p - q - 7)
        m = split(content, arr, ",")
        for (k = 1; k <= m; k++) {
          tag = xargs_norm(arr[k])
          if (tag != "") printf "%s\001%s\n", tag, wiki
        }
      }
    }
  ' {} + 2>/dev/null > "$TAG_RAW_TMP" || true

# 按标签聚合：拼接规则同原 TAG_FILES["$tag"]="${TAG_FILES[$tag]:-} [[路径|名字]]"
while IFS=$'\001' read -r tag wiki; do
  [ -n "$wiki" ] || continue
  TAG_FILES["$tag"]="${TAG_FILES[$tag]:-} [[$wiki|${wiki%.md}]]"
done < "$TAG_RAW_TMP"
rm -f "$TAG_RAW_TMP"

TAG_COUNT=0
while IFS= read -r tag; do
  [ -z "$tag" ] && continue
  echo "### $tag" >> "$TAGS_FILE"
  echo "${TAG_FILES[$tag]}" | sed 's/[[:space:]]*$//' >> "$TAGS_FILE"
  echo "" >> "$TAGS_FILE"
  TAG_COUNT=$((TAG_COUNT + 1))
done < <(printf '%s\n' "${!TAG_FILES[@]}" | sort)

# --- 更新最近更新 ---
echo "---" > "$RECENT_FILE"
echo "id: \"idx-recent\"" >> "$RECENT_FILE"
echo "title: \"最近更新\"" >> "$RECENT_FILE"
echo "type: index" >> "$RECENT_FILE"
echo "created: $RECENT_CREATED" >> "$RECENT_FILE"
echo "updated: $generated_date" >> "$RECENT_FILE"
echo "---" >> "$RECENT_FILE"
echo "" >> "$RECENT_FILE"
echo "# 🕐 最近更新" >> "$RECENT_FILE"
echo "" >> "$RECENT_FILE"

while IFS= read -r line; do
  timestamp=$(echo "$line" | cut -d' ' -f1)
  filepath=$(echo "$line" | cut -d' ' -f2-)
  rel_path="${filepath#$NOTES_DIR/}"
  wiki_path="笔记/$rel_path"
  mod_time=$(date -d "@$timestamp" '+%Y-%m-%d %H:%M' 2>/dev/null)
  echo "- $mod_time — [[$wiki_path|${wiki_path%.md}]]" >> "$RECENT_FILE"
done < "$RECENT_TMP"

rm -f "$RECENT_TMP" "$RECENT_ALL_TMP"

echo "" >> "$RECENT_FILE"
echo "---" >> "$RECENT_FILE"
echo "_自动更新于 ${generated_at}_" >> "$RECENT_FILE"

# --- 更新 README 统计 ---
count_md() {
  local dir="$1"
  if [ -d "$dir" ]; then
    find "$dir" -name "*.md" -not -path "*/_assets/*" 2>/dev/null | wc -l
  else
    echo 0
  fi
}

PROJECT_COUNT=$(count_md "$NOTES_DIR/10-项目")
KNOWLEDGE_COUNT=$(count_md "$NOTES_DIR/20-知识")
WRITING_COUNT=$(count_md "$NOTES_DIR/30-写作")
DIARY_COUNT=$(count_md "$NOTES_DIR/40-日记")

if [ -f "$README_FILE" ]; then
  grep -q '^- 项目数：' "$README_FILE" || sed -i '/^## 统计/a\- 项目数：0' "$README_FILE"
  grep -q '^- 知识笔记：' "$README_FILE" || sed -i '/^- 项目数：/a\- 知识笔记：0' "$README_FILE"
  grep -q '^- 写作：' "$README_FILE" || sed -i '/^- 知识笔记：/a\- 写作：0' "$README_FILE"
  grep -q '^- 日记：' "$README_FILE" || sed -i '/^- 写作：/a\- 日记：0' "$README_FILE"
  grep -q '^- 标签数：' "$README_FILE" || sed -i '/^- 日记：/a\- 标签数：0' "$README_FILE"
  sed -i "s/^- 项目数：.*$/- 项目数：$PROJECT_COUNT/" "$README_FILE"
  sed -i "s/^- 知识笔记：.*$/- 知识笔记：$KNOWLEDGE_COUNT/" "$README_FILE"
  sed -i "s/^- 写作：.*$/- 写作：$WRITING_COUNT/" "$README_FILE"
  sed -i "s/^- 日记：.*$/- 日记：$DIARY_COUNT/" "$README_FILE"
  sed -i "s/^- 标签数：.*$/- 标签数：$TAG_COUNT/" "$README_FILE"
fi

echo "✅ 索引更新完成"
echo "   项目数: $PROJECT_COUNT | 知识笔记: $KNOWLEDGE_COUNT | 写作: $WRITING_COUNT | 日记: $DIARY_COUNT | 标签数: $TAG_COUNT"
