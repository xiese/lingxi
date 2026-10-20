#!/bin/bash
# ============================================================
# 开发库一键初始化
#
# 做两件事：
#   1. 设置 core.hooksPath=.githooks，启用「笔记/ 不许进代码库」钩子
#   2. 创建 .kb-env=dev（内容为 dev，且被 .gitignore 排除）
#
# 为什么要单独跑：git 的钩子路径是本地配置，不入库。新克隆的仓库
# 默认没有 core.hooksPath，.githooks/pre-commit 不会自动生效。
#
# 用法：
#   bash scripts/setup-dev.sh
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || { echo "❌ 进不去仓库目录"; exit 1; }

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }

echo "════════════════════════════════════════════"
echo " 开发库初始化  $(date '+%F %T')"
echo " 目录: $REPO_DIR"
echo "════════════════════════════════════════════"

# ── 前置检查：这是开发库，不是生产库 ──────────────────
if [ -f .kb-env ]; then
  CUR="$(tr -d '[:space:]' < .kb-env)"
  if [ "$CUR" = "prod" ]; then
    red "❌ 本目录 .kb-env=prod，这是生产库。"
    echo "   setup-dev.sh 只在开发库（本机 kb-public）运行。"
    echo "   生产库不需要钩子路径，它用的是仓库外的硬闸。"
    exit 1
  fi
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  red "❌ 不是 git 仓库：$REPO_DIR"
  exit 1
}

# ── 第 1 步：钩子路径 ────────────────────────────────
echo
echo "→ 设置 core.hooksPath = .githooks ..."
if [ ! -f .githooks/pre-commit ]; then
  red "❌ 缺少 .githooks/pre-commit（仓库里应该有这个文件）"
  echo "   请先确认克隆完整：git checkout -- .githooks/"
  exit 1
fi
chmod +x .githooks/pre-commit 2>/dev/null || true
git config core.hooksPath .githooks || { red "❌ 设置 core.hooksPath 失败"; exit 1; }
green "   ✅ core.hooksPath = $(git config --get core.hooksPath)"

# ── 第 2 步：.kb-env ────────────────────────────────
echo
echo "→ 设置 .kb-env = dev ..."
printf 'dev\n' > .kb-env
if git check-ignore -q .kb-env; then
  green "   ✅ .kb-env 已写入（dev），且被 .gitignore 排除，不会入库"
else
  yellow "   ⚠️  .kb-env 已写入，但没被 .gitignore 排除！"
  echo "      检查 .gitignore 是否包含 .kb-env"
fi

# ── 自检 ────────────────────────────────────────────
echo
echo "→ 自检 ..."
OK=1

HP="$(git config --get core.hooksPath 2>/dev/null || echo '')"
[ "$HP" = ".githooks" ] && green "   ✅ 钩子路径: $HP" || { red "   ❌ 钩子路径: ${HP:-未设置}"; OK=0; }

[ -x .githooks/pre-commit ] && green "   ✅ pre-commit 可执行" || { red "   ❌ pre-commit 不可执行"; OK=0; }

[ "$(tr -d '[:space:]' < .kb-env 2>/dev/null)" = "dev" ] \
  && green "   ✅ .kb-env = dev" || { red "   ❌ .kb-env 不是 dev"; OK=0; }

if git -c core.quotePath=false status --porcelain | grep -qE '^\?\? \.kb-env$'; then
  yellow "   ⚠️  .kb-env 出现在未跟踪列表里（.gitignore 可能缺这条）"
fi

echo
if [ "$OK" = "1" ]; then
  green "✅ 开发库初始化完成"
  echo
  echo "   现在起，暂存区里只要有 笔记/ 下的路径，commit 会被拒绝。"
  echo "   笔记是数据，只住生产库（远程服务器），写/改走知识库 API。"
  echo "   项目文档要进代码库的，放 docs/，不放 笔记/。"
  exit 0
else
  red "❌ 初始化未完成，请按上面 ❌ 项排查"
  exit 1
fi
