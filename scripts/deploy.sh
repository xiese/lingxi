#!/bin/bash
# ============================================================
# 生产库部署脚本（只在生产库 远程服务器 上运行）
#
# 设计原则：
#   1. 生产库代码只能从 GitHub main 拉，不在生产改、不在生产 commit
#   2. 有任何本地代码改动就停下，绝不自动覆盖
#   3. 部署前打提交号，部署后再打一次，可对照可回滚
#
# 用法：
#   bash scripts/deploy.sh            # 正常部署
#   bash scripts/deploy.sh --dry-run  # 只看会做什么，不动
#   bash scripts/deploy.sh --check    # 只体检，不部署
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

REMOTE_NAME="origin"
REMOTE_URL="https://github.com/xiese/lingxi.git"
BRANCH="main"
SERVICES=("kb-server" "kb-mcp-sse")
HEALTH_URL="http://127.0.0.1:8899/health"
DRY_RUN=0
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --check)   CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
die()   { red "❌ $*"; exit 1; }

echo "════════════════════════════════════════════"
echo " 生产库部署  $(date '+%F %T')"
echo " 目录: $REPO_DIR"
echo "════════════════════════════════════════════"

# ── 闸 1：必须站在生产库 ──────────────────────────────
if [ ! -f .kb-env ]; then
  die "缺少 .kb-env 标记文件。生产库根目录应有内容为 prod 的 .kb-env。
   如果这是开发库，请去本机 kb-public 改代码，不要在生产部署。"
fi
ENV_MARK="$(tr -d '[:space:]' < .kb-env)"
if [ "$ENV_MARK" != "prod" ]; then
  die "本目录 .kb-env=$ENV_MARK，不是 prod。deploy.sh 只能在生产库运行。"
fi
green "✅ 闸1 通过：这是生产库"

# ── 闸 2：git 仓库检查 ────────────────────────────────
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "不是 git 仓库"

# ── 闸 3：工作区必须干净（代码部分）────────────────────
# 笔记/ 与运行期文件被 gitignore，不算改动
if ! git diff --quiet || ! git diff --cached --quiet; then
  red "❌ 生产库有未提交的代码改动，拒绝部署（不会自动覆盖你的改动）"
  echo
  echo "改动的文件："
  git status --porcelain | sed 's/^/   /'
  echo
  echo "处理方式（二选一）："
  echo "  A. 这些改动不要了 → git checkout -- <文件> 或 git reset --hard"
  echo "  B. 这些改动要保留 → 去开发库（本机 kb-public）重新实现，走正常流程"
  echo
  echo "禁止在生产库直接 commit。生产库的 git hooks 会拦截。"
  exit 1
fi
green "✅ 闸3 通过：工作区干净"

UNTRACKED="$(git ls-files --others --exclude-standard | head -20)"
if [ -n "$UNTRACKED" ]; then
  yellow "⚠️  有未跟踪文件（不影响部署，仅提示）："
  echo "$UNTRACKED" | sed 's/^/   /'
  echo
fi

BEFORE="$(git rev-parse --short HEAD)"
BEFORE_MSG="$(git log -1 --format='%s' | cut -c1-60)"
echo "部署前: $BEFORE  $BEFORE_MSG"
echo

if [ "$CHECK_ONLY" = "1" ]; then
  echo "--- 远端状态 ---"
  git fetch "$REMOTE_NAME" "$BRANCH" --quiet 2>/dev/null || yellow "⚠️  fetch 失败（网络？）"
  LOCAL="$(git rev-parse HEAD)"
  REMOTE="$(git rev-parse "$REMOTE_NAME/$BRANCH" 2>/dev/null || echo '')"
  if [ -z "$REMOTE" ]; then
    yellow "取不到 $REMOTE_NAME/$BRANCH"
  elif [ "$LOCAL" = "$REMOTE" ]; then
    green "✅ 已是最新（HEAD == $REMOTE_NAME/$BRANCH）"
  else
    yellow "落后 $(git rev-list --count HEAD.."$REMOTE_NAME/$BRANCH") 个提交，可部署"
  fi
  echo
  echo "--- 服务状态 ---"
  for s in "${SERVICES[@]}"; do
    printf '   %-14s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null)"
  done
  exit 0
fi

# ── 闸 4：远端必须是 GitHub main ──────────────────────
CUR_URL="$(git remote get-url "$REMOTE_NAME" 2>/dev/null || echo '')"
if [ "$CUR_URL" != "$REMOTE_URL" ]; then
  yellow "⚠️  远端 $REMOTE_NAME 当前是: $CUR_URL"
  yellow "   期望: $REMOTE_URL"
  [ "$DRY_RUN" = "1" ] && { echo "[dry-run] 会执行 git remote set-url"; exit 0; }
  git remote set-url "$REMOTE_NAME" "$REMOTE_URL" || die "改远端失败"
  green "✅ 远端已改为 $REMOTE_URL"
fi

# ── 闸 5：fetch + 只允许快进 ──────────────────────────
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] git fetch $REMOTE_NAME $BRANCH"
  echo "[dry-run] git merge --ff-only $REMOTE_NAME/$BRANCH"
  exit 0
fi

echo "→ fetch $REMOTE_NAME/$BRANCH ..."
git fetch "$REMOTE_NAME" "$BRANCH" --quiet || die "fetch 失败（检查网络 / 凭据）"

BEHIND="$(git rev-list --count HEAD.."$REMOTE_NAME/$BRANCH" 2>/dev/null || echo 0)"
if [ "$BEHIND" = "0" ]; then
  green "✅ 已是最新，无需部署（HEAD=$BEFORE）"
  exit 0
fi
echo "落后 $BEHIND 个提交，开始快进..."

# ── 保险：pull 前把「将被删除的 笔记/ 文件」先拷出来，pull 后拷回 ──
# 笔记是数据，只住生产库。若某篇笔记曾被误提交进代码库、之后又从仓库移除，
# 快进会把生产磁盘上的这份一起删掉。这里先备份、后恢复，避免数据丢失。
HOLD_DIR="/srv/notes/kb-notes-hold/$(date '+%Y%m%d-%H%M%S')"
PENDING_DEL="$(git -c core.quotePath=false diff --name-only --diff-filter=D "HEAD..$REMOTE_NAME/$BRANCH" 2>/dev/null | grep -E '^笔记/' || true)"
if [ -n "$PENDING_DEL" ]; then
  mkdir -p "$HOLD_DIR"
  echo "$PENDING_DEL" | while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ -f "$f" ]; then
      mkdir -p "$HOLD_DIR/$(dirname "$f")"
      cp -p "$f" "$HOLD_DIR/$f"
      echo "   已拷出: $f  ($(stat -c%s "$f") 字节)"
    fi
  done
  yellow "⚠️  本次部署会从仓库移除 笔记/ 下的文件，已备份到 $HOLD_DIR"
  echo
fi

echo "→ git merge --ff-only ..."
if ! git merge --ff-only "$REMOTE_NAME/$BRANCH" 2>&1 | tail -5; then
  die "无法快进。生产库有分叉历史，请人工排查（不要强行 merge/rebase）"
fi

AFTER="$(git rev-parse --short HEAD)"
echo
green "代码已更新: $BEFORE → $AFTER"

# ── 保险续：把被删的 笔记/ 文件拷回原路径并核对 ──────────
if [ -n "$PENDING_DEL" ] && [ -d "$HOLD_DIR" ]; then
  echo
  echo "→ 恢复被移除的笔记文件 ..."
  RESTORED=0
  echo "$PENDING_DEL" | while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ -f "$HOLD_DIR/$f" ]; then
      mkdir -p "$(dirname "$f")"
      cp -p "$HOLD_DIR/$f" "$f"
      SZ="$(stat -c%s "$f")"
      MD5="$(md5sum "$f" | cut -d' ' -f1)"
      echo "   已拷回: $f  ($SZ 字节, md5=$MD5)"
    else
      yellow "   ⚠️ 备份里没有: $f（跳过）"
    fi
  done
  green "✅ 笔记文件已拷回（备份留存于 $HOLD_DIR）"
fi

echo
echo
echo "--- 本次带进来的改动 ---"
git log --oneline "$BEFORE..$AFTER" | sed 's/^/   /'
echo

# ── 重启服务 ──────────────────────────────────────────
for s in "${SERVICES[@]}"; do
  echo "→ 重启 $s ..."
  systemctl restart "$s" || { red "❌ $s 重启失败"; systemctl status "$s" --no-pager | tail -10; exit 1; }
done

# ── 健康检查（每 2 秒探测一次，最多等 30 秒）──────────
echo "→ 健康检查 $HEALTH_URL（每 2 秒探测一次，最多等 30 秒）..."
OK=0
DEADLINE=$(( $(date +%s) + 30 ))
i=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  i=$((i+1))
  CODE="$(curl -s -o /tmp/.deploy-health.json -w '%{http_code}' --max-time 10 "$HEALTH_URL" 2>/dev/null)"
  if [ "$CODE" = "200" ]; then OK=1; break; fi
  REMAIN=$(( DEADLINE - $(date +%s) ))
  echo "   第 $i 次: HTTP=$CODE，2 秒后重试（剩余 ${REMAIN}s）"
  sleep 2
done

if [ "$OK" != "1" ]; then
  red "❌ 健康检查失败（HTTP=$CODE）"
  echo
  echo "回滚命令："
  echo "   git reset --hard $BEFORE && systemctl restart kb-server kb-mcp-sse"
  exit 1
fi

green "✅ /health 200"
cat /tmp/.deploy-health.json 2>/dev/null; echo

for s in "${SERVICES[@]}"; do
  printf '   %-14s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null)"
done

echo
echo "════════════════════════════════════════════"
green "部署完成: $BEFORE → $AFTER"
echo "回滚: git reset --hard $BEFORE && systemctl restart kb-server kb-mcp-sse"
echo "════════════════════════════════════════════"
