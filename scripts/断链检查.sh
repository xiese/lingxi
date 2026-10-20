#!/bin/bash
# 断链检查脚本
# 用法: bash scripts/断链检查.sh
# 输出: 悬空链接列表（排除 _spec/ _模板/ 中的示例链接）

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BROKEN=0

echo "========== 断链检查 =========="
echo ""

# 预构建文件名索引（一次 find，O(n)）
# 排除 _spec/ _模板/ .git/ scripts/ 中的文件
declare -A FILE_INDEX
while IFS= read -r f; do
  basename_no_ext=$(basename "$f" .md)
  FILE_INDEX["$basename_no_ext"]="$f"
done < <(find "$ROOT_DIR" -name "*.md" \
  -not -path "*/.git/*" \
  -not -path "*/scripts/*" \
  -not -path "*/_spec/*" \
  -not -path "*/_模板/*")

# 扫描所有内容笔记（排除索引/模板/规格/脚本/归档）
while IFS= read -r file; do
  rel_path="${file#$ROOT_DIR/}"
  while IFS= read -r link; do
    # 提取链接目标：支持 [[目标|别名]] 和 [[目标]] 两种格式
    # 兼容 markdown 表格中的 \| 转义（如 [[README\|别名]]）
    target=$(echo "$link" | sed -n 's/.*\[\[\([^]#|]*\).*/\1/p')
    # 去掉尾部反斜杠（来自表格转义 \|）
    target="${target%\\}"
    [ -z "$target" ] && continue

    # 跳过纯路径引用（如 [[40-日记/]]）
    [[ "$target" == */ ]] && continue

    # 查表，O(1)
    if [ -z "${FILE_INDEX[$target]}" ]; then
      echo "🔗 悬空: $rel_path -> [[${target}]]"
      BROKEN=$((BROKEN + 1))
    fi
  done < <(grep -o '\[\[[^]]*\]\]' "$file" 2>/dev/null)
done < <(find "$ROOT_DIR" -name "*.md" \
  -not -path "*/.git/*" \
  -not -path "*/scripts/*" \
  -not -path "*/_spec/*" \
  -not -path "*/_模板/*" \
  -not -path "*/00-索引/*")

echo ""
if [ "$BROKEN" -eq 0 ]; then
  echo "✅ 没有悬空链接"
else
  echo "⚠️  发现 $BROKEN 个悬空链接"
fi
