#!/usr/bin/env bash
# ============================================================
# 知识库 Release 部署脚本（生产库专用，第 9 项）
#
# 设计原则：
#   1. 生产不再 git pull。代码只从 GitHub Release 包来，数据目录与代码目录分离
#   2. 代码放 releases/vX.Y.Z/，current 软链指向当前版本
#   3. 部署前自检（语法、依赖、版本一致性），通过才切软链
#   4. 失败自动切回上一版，并留下 last-good 记录
#   5. 笔记/设置/索引/令牌这些数据不进版本目录，原地不动
#
# 用法：
#   bash scripts/release-deploy.sh --version 1.0.0
#   bash scripts/release-deploy.sh --version 1.0.0 --dry-run
#   bash scripts/release-deploy.sh --rollback          # 切回上一版
#   bash scripts/release-deploy.sh --check             # 只体检
# ============================================================
set -uo pipefail

KB_ROOT="${KB_ROOT:-/srv/notes/知识库}"
RELEASES_DIR="$KB_ROOT/releases"
CURRENT_LINK="$KB_ROOT/current"
STATE_FILE="$KB_ROOT/.deploy-state.json"
REPO="xiese/lingxi"
SERVICES=("kb-server")
HEALTH_URL="http://127.0.0.1:8899/health"

VERSION=""
DRY_RUN=0
ROLLBACK=0
CHECK_ONLY=0
NO_RESTART=0

# 用 while 手动消费参数：--version 要吃掉下一个值，用 for 循环 + shift 会错位
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
die()   { red "❌ $*"; exit 1; }

echo "════════════════════════════════════════════"
echo " 知识库 Release 部署  $(date '+%F %T')"
echo " 根目录: $KB_ROOT"
echo "════════════════════════════════════════════"

[ -f "$KB_ROOT/.kb-env" ] || die "缺少 .kb-env 标记文件"
[ "$(tr -d '[:space:]' < "$KB_ROOT/.kb-env")" = "prod" ] || die "本目录不是生产库"

current_version() {
  if [ -L "$CURRENT_LINK" ]; then
    basename "$(readlink -f "$CURRENT_LINK")" | sed 's/^kb-//'
  else
    echo ""
  fi
}

write_state() {
  # 只记版本与时间，不记任何令牌
  cat > "$STATE_FILE" <<EOF
{"current": "$1", "previous": "$2", "deployed_at": "$(date '+%F %T')"}
EOF
}

restart_services() {
  if [ "$NO_RESTART" = "1" ] || [ "$DRY_RUN" = "1" ]; then
    yellow "跳过重启（--no-restart 或 --dry-run）"
    return 0
  fi
  for svc in "${SERVICES[@]}"; do
    echo "重启 $svc …"
    systemctl restart "$svc" || return 1
    sleep 2
    systemctl is-active --quiet "$svc" || { red "$svc 未起来"; return 1; }
  done
  # 健康检查：最多等 20 秒
  for i in $(seq 1 10); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null || echo 000)
    [ "$code" = "200" ] && { green "✅ 健康检查 200（第 ${i} 次）"; return 0; }
    sleep 2
  done
  red "健康检查未通过（最后状态 $code）"
  return 1
}

do_rollback() {
  local prev cur
  prev="$(python3 -c "import json;print(json.load(open('$STATE_FILE')).get('previous',''))" 2>/dev/null || echo '')"
  cur="$(current_version)"
  [ -n "$prev" ] && [ -d "$RELEASES_DIR/kb-$prev" ] || die "找不到上一版可回滚（state=$STATE_FILE）"
  if [ "$prev" = "$cur" ]; then
    die "上一版与当前版相同（$prev），没有可回滚的目标"
  fi
  yellow "回滚到 $prev（当前 $cur）"
  [ "$DRY_RUN" = "1" ] && { echo "（dry-run）会把 current → kb-$prev"; return 0; }
  ln -sfn "$RELEASES_DIR/kb-$prev" "$CURRENT_LINK"
  # 回滚后 current=prev，previous 记成刚被换下的版本，这样还能再往前退一次
  write_state "$prev" "$cur"
  restart_services || die "回滚后服务仍不健康，需要人工介入"
  green "✅ 已回滚到 $prev"
}

if [ "$ROLLBACK" = "1" ]; then
  do_rollback
  exit $?
fi

# ── 体检 ─────────────────────────────────────────────
if [ "$CHECK_ONLY" = "1" ]; then
  echo "当前版本: $(current_version)"
  echo "releases:"; ls -1 "$RELEASES_DIR" 2>/dev/null | sed 's/^/   /' || echo "   （还没有）"
  [ -f "$STATE_FILE" ] && cat "$STATE_FILE"
  systemctl is-active kb-server || true
  curl -s -o /dev/null -w 'health: %{http_code}\n' --max-time 5 "$HEALTH_URL" || true
  exit 0
fi

[ -n "$VERSION" ] || die "必须指定 --version X.Y.Z"

PREV="$(current_version)"
TARGET="$RELEASES_DIR/kb-$VERSION"

echo "上一版: ${PREV:-（无）}"
echo "目标版: $VERSION"
echo "目标目录: $TARGET"

if [ "$DRY_RUN" = "1" ]; then
  echo "（dry-run）将下载 https://github.com/$REPO/releases/download/v$VERSION/kb-$VERSION.tar.gz"
  echo "（dry-run）解到 $TARGET，自检通过后 current → kb-$VERSION，重启 ${SERVICES[*]}"
  exit 0
fi

mkdir -p "$RELEASES_DIR"

# ── 1. 下载并校验 ─────────────────────────────────────
# 私有仓的 Release 资源走浏览器直链会 404，必须带令牌走 API：
#   先查 release 里的 asset id，再按 Accept: application/octet-stream 下二进制。
# 令牌只从本机凭据文件读，不落日志、不写进命令回显。
CRED_FILE="${KB_CRED_FILE:-$HOME/.git-credentials}"
get_token() {
  [ -r "$CRED_FILE" ] || { echo ""; return; }
  python3 - "$CRED_FILE" <<'PY'
import re, sys
try:
    txt = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except Exception:
    print(""); raise SystemExit
m = re.search(r"https://[^:]+:([^@]+)@github\.com", txt)
print(m.group(1) if m else "")
PY
}

TOKEN="$(get_token)"
[ -n "$TOKEN" ] || die "读不到 GitHub 凭据（$CRED_FILE），无法下载私有仓 Release"

API="https://api.github.com/repos/$REPO/releases/tags/v$VERSION"
ASSET_ID="$(curl -fsS --max-time 60 -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.github+json" "$API" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);a=[x for x in d.get('assets',[]) if x['name']=='kb-$VERSION.tar.gz'];print(a[0]['id'] if a else '')" 2>/dev/null || echo '')"
[ -n "$ASSET_ID" ] || die "Release v$VERSION 里没有 kb-$VERSION.tar.gz 资产"

TMP_TGZ="/tmp/kb-$VERSION.$$.tar.gz"
echo "下载 v$VERSION 资产 #$ASSET_ID"
if ! curl -fsSL --max-time 600 -H "Authorization: Bearer $TOKEN" \
     -H "Accept: application/octet-stream" \
     -o "$TMP_TGZ" "https://api.github.com/repos/$REPO/releases/assets/$ASSET_ID"; then
  rm -f "$TMP_TGZ"; die "下载失败（asset $ASSET_ID）"
fi
gzip -t "$TMP_TGZ" || die "包损坏（gzip -t 失败）"
green "✅ 下载完成 $(du -h "$TMP_TGZ" | cut -f1)"

# ── 2. 解到暂存目录，先自检再换名 ──────────────────────
STAGE="$RELEASES_DIR/.stage-$VERSION-$$"
rm -rf "$STAGE"; mkdir -p "$STAGE"
tar -xzf "$TMP_TGZ" -C "$STAGE" --strip-components=1 || die "解包失败"

# 禁区自检：包里绝不能有个人数据与密钥
if find "$STAGE" -type f | grep -qE '/(笔记|00-索引|10-项目|50-档案|_assets)/|\.api_tokens\.json|\.kb_settings\.json|\.audit\.db$|/\.env$'; then
  rm -rf "$STAGE"; die "包内出现个人数据或密钥文件，拒绝部署"
fi

# 版本一致性：包内 VERSION 必须等于请求版本
PKG_VER="$(python3 -c "import re,pathlib;print(re.search(r'^VERSION = \"([^\"]+)\"', pathlib.Path('$STAGE/scripts/kb_lib/config.py').read_text(encoding='utf-8'), re.M).group(1))" 2>/dev/null || echo '')"
[ "$PKG_VER" = "$VERSION" ] || { rm -rf "$STAGE"; die "包内 VERSION=$PKG_VER 与请求 $VERSION 不一致"; }

# 语法编译
python3 -m compileall -q "$STAGE/scripts" >/dev/null 2>&1 || { rm -rf "$STAGE"; die "包内代码语法检查失败"; }
green "✅ 包自检通过（无禁区文件、VERSION=$PKG_VER、语法 OK）"

# ── 3. 就位 ───────────────────────────────────────────
if [ -d "$TARGET" ]; then
  yellow "目标版本目录已存在，保留旧目录为 $TARGET.bak-$(date +%Y%m%d-%H%M%S)"
  mv "$TARGET" "$TARGET.bak-$(date +%Y%m%d-%H%M%S)"
fi
mv "$STAGE" "$TARGET"
rm -f "$TMP_TGZ"

# ── 4. 切软链 + 重启 + 健康检查，失败自动回滚 ──────────
if [ -n "$PREV" ]; then
  ln -sfn "$RELEASES_DIR/kb-$PREV" "$CURRENT_LINK"
fi
write_state "$PREV" ""
ln -sfn "$TARGET" "$CURRENT_LINK"
write_state "$VERSION" "$PREV"

# 服务从 current 起，所以改软链等价于换代码；必须重启进程
if ! restart_services; then
  red "新版本启动失败，自动回滚"
  if [ -n "$PREV" ]; then
    ln -sfn "$RELEASES_DIR/kb-$PREV" "$CURRENT_LINK"
    write_state "$PREV" "$VERSION"
    restart_services && green "✅ 已自动回滚到 $PREV"
  else
    red "没有上一版可回滚，需要人工介入"
  fi
  exit 1
fi

green "✅ 部署完成：$PREV → $VERSION"
echo "current → $(readlink -f "$CURRENT_LINK")"
