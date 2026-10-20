#!/bin/bash
# 新建笔记脚本
# 用法: bash scripts/新建笔记.sh <类型> <标题> [子分类]
# 类型: 知识 | 项目 | 写作 | 日记
# 示例:
#   bash scripts/新建笔记.sh 知识 "Docker 网络模型"
#   bash scripts/新建笔记.sh 知识 "Docker 网络模型" 技术     # → 20-知识/技术/Docker_网络模型.md
#   bash scripts/新建笔记.sh 项目 "新功能开发" 示例项目       # → 10-项目/示例项目/新功能开发.md
#   bash scripts/新建笔记.sh 写作 "第一期脚本" 小说推文       # → 30-写作/小说推文/第一期脚本.md
#   bash scripts/新建笔记.sh 日记 ""                          # → 40-日记/YYYY/MM/YYYY-MM-DD.md

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE_DIR="$ROOT_DIR/_模板"

TYPE="$1"
TITLE="$2"
SUBCAT="$3"   # 可选的子分类目录名
TIMESTAMP=$(date +%Y%m%d%H%M%S)
TODAY=$(date +%Y-%m-%d)

if [ -z "$TYPE" ] || [ -z "$TITLE" ]; then
  echo "用法: bash scripts/新建笔记.sh <类型> <标题> [子分类]"
  echo "类型: 知识 | 项目 | 写作 | 日记"
  echo "示例:"
  echo "  bash scripts/新建笔记.sh 知识 \"Docker 网络模型\""
  echo "  bash scripts/新建笔记.sh 知识 \"Docker 网络模型\" 技术"
  echo "  bash scripts/新建笔记.sh 项目 \"新功能开发\" 示例项目"
  echo "  bash scripts/新建笔记.sh 写作 \"第一期脚本\" 小说推文"
  echo "  bash scripts/新建笔记.sh 日记 \"\""
  exit 1
fi

case "$TYPE" in
  知识)
    TEMPLATE="$TEMPLATE_DIR/知识笔记模板.md"
    TARGET_DIR="$ROOT_DIR/20-知识"
    PREFIX="kb"
    ;;
  项目)
    TEMPLATE="$TEMPLATE_DIR/项目模板.md"
    TARGET_DIR="$ROOT_DIR/10-项目"
    PREFIX="proj"
    ;;
  写作)
    TEMPLATE="$TEMPLATE_DIR/写作模板.md"
    TARGET_DIR="$ROOT_DIR/30-写作"
    PREFIX="write"
    ;;
  日记)
    TEMPLATE="$TEMPLATE_DIR/日记模板.md"
    TARGET_DIR="$ROOT_DIR/40-日记"
    PREFIX="day"
    TODAY_SHORT=$(date +%Y-%m-%d)
    YEAR=$(date +%Y)
    MONTH=$(date +%m)
    DIARY_SUBDIR="$TARGET_DIR/$YEAR/$MONTH"
    mkdir -p "$DIARY_SUBDIR"
    TARGET_FILE="$DIARY_SUBDIR/${TODAY_SHORT}.md"
    if [ -f "$TARGET_FILE" ]; then
      echo "⚠️  今天的日记已存在: $TARGET_FILE"
      exit 1
    fi
    sed -e "s/{{timestamp}}/$TIMESTAMP/g" \
        -e "s/YYYY-MM-DD/$TODAY/g" \
        -e "s/{{title}}/$TITLE/g" \
        "$TEMPLATE" > "$TARGET_FILE"
    echo "✅ 已创建: $TARGET_FILE"
    exit 0
    ;;
  *)
    echo "❌ 未知类型: $TYPE"
    echo "可选: 知识 | 项目 | 写作 | 日记"
    exit 1
    ;;
esac

# 如果指定了子分类，拼接到目标目录
if [ -n "$SUBCAT" ]; then
  TARGET_DIR="$TARGET_DIR/$SUBCAT"
fi
mkdir -p "$TARGET_DIR"

# 安全文件名（移除空格用于文件名）
SAFE_TITLE=$(echo "$TITLE" | tr ' ' '_')
TARGET_FILE="$TARGET_DIR/${SAFE_TITLE}.md"

if [ -f "$TARGET_FILE" ]; then
  echo "⚠️  文件已存在: $TARGET_FILE"
  exit 1
fi

sed -e "s/{{timestamp}}/$TIMESTAMP/g" \
    -e "s/YYYY-MM-DD/$TODAY/g" \
    -e "s/{{title}}/$TITLE/g" \
    "$TEMPLATE" > "$TARGET_FILE"

echo "✅ 已创建: $TARGET_FILE"
