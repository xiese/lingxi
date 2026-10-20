#!/bin/bash
# 安装 Git hooks 脚本
# 用法: bash scripts/安装git-hooks.sh

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$ROOT_DIR/.git/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
  echo "❌ 未找到 .git/hooks 目录，请先初始化 Git 仓库"
  exit 1
fi

cp "$ROOT_DIR/scripts/git-pre-commit.sh" "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"

echo "✅ Git pre-commit hook 已安装"
echo "   每次 git commit 前会自动运行 scripts/索引更新.sh"
