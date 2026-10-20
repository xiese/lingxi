#!/bin/bash
# Git pre-commit hook — 提交前自动更新索引
# 安装: 复制到 .git/hooks/pre-commit 并 chmod +x
# 或运行: bash scripts/安装git-hooks.sh

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "🔄 提交前自动刷新索引..."
bash "$ROOT_DIR/scripts/索引更新.sh"
echo "✅ 索引已更新，继续提交..."
