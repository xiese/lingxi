#!/bin/bash
# 知识库多平台同步脚本
# 用法:
#   bash scripts/sync.sh pull <项目名>        从服务器同步到本地
#   bash scripts/sync.sh push <源目录> <类型>  上传本地项目到服务器
#   bash scripts/sync.sh status               查看同步状态
#
# 环境变量:
#   KB_API_BASE  API 地址 (默认 http://127.0.0.1:8899；旧名 KB_HOST 仅兼容一版)
#   KB_TOKEN  API Token

# API 地址解析顺序：KB_API_BASE → 旧名 KB_HOST（兼容一版）→ 默认本机。
# 服务端已把同名双义的 KB_HOST 拆为 KB_BIND（监听绑定）/ KB_API_BASE（上游 API 地址），
# 旧名 KB_HOST 在此仅作 API 地址的一版兼容，请尽快改用 KB_API_BASE。
KB_API_BASE="${KB_API_BASE:-${KB_HOST:-http://127.0.0.1:8899}}"
KB_TOKEN="${KB_TOKEN:-}"
TOKEN_FILE="$HOME/.kb_token"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

[ -z "$KB_TOKEN" ] && [ -f "$TOKEN_FILE" ] && KB_TOKEN=$(head -1 "$TOKEN_FILE")
[ -z "$KB_TOKEN" ] && { echo "❌ 未设置 KB_TOKEN"; echo "   export KB_TOKEN=your_token 或 echo 'token' > ~/.kb_token"; exit 1; }

AUTH="-H \"Authorization: Bearer $KB_TOKEN\""

# ─── pull: 从服务器下载项目到本地 ───
sync_pull() {
  local project="$1"
  [ -z "$project" ] && { echo "用法: sync.sh pull <项目名>"; exit 1; }

  echo "⬇  同步: 服务器 → 本地"
  echo "   项目: $project"
  echo ""

  # 获取项目下所有笔记
  local resp
  resp=$(curl -s $AUTH "$KB_API_BASE/notes?dir=10-项目/$project")
  local count=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null)

  if [ "$count" -eq 0 ]; then
    # 也尝试知识类
    resp=$(curl -s $AUTH "$KB_API_BASE/notes?dir=20-知识/$project")
    count=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null)
  fi

  echo "   服务器上有 $count 个文件"
  echo ""

  # 逐个下载
  local items=$(echo "$resp" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for item in data.get('items', []):
    print(item['path'])
" 2>/dev/null)

  local downloaded=0
  while IFS= read -r path; do
    [ -z "$path" ] && continue
    local local_path="$ROOT_DIR/$path"
    local local_dir=$(dirname "$local_path")
    mkdir -p "$local_dir"

    # 下载整个笔记
    local note=$(curl -s $AUTH "$KB_API_BASE/notes/$path")
    local raw=$(echo "$note" | python3 -c "import sys,json; print(json.load(sys.stdin).get('raw',''))" 2>/dev/null)

    if [ -n "$raw" ]; then
      echo "$raw" > "$local_path"
      echo "   ✅ $path"
      downloaded=$((downloaded + 1))
    fi
  done <<< "$items"

  echo ""
  echo "   已下载 $downloaded 个文件"
  bash "$ROOT_DIR/scripts/索引更新.sh" 2>/dev/null
}

# ─── push: 上传本地文件到服务器 ───
sync_push() {
  local src_dir="$1"
  local note_type="${2:-project}"

  [ -z "$src_dir" ] && { echo "用法: sync.sh push <源目录> [类型: project|knowledge|writing]"; exit 1; }
  [ ! -d "$src_dir" ] && { echo "❌ 目录不存在: $src_dir"; exit 1; }

  echo "⬆  同步: 本地 → 服务器"
  echo "   源目录: $src_dir"
  echo "   类型: $note_type"
  echo ""

  local project_name=$(basename "$src_dir")
  local uploaded=0
  local skipped=0

  # 扫描源目录下所有 .md 文件
  while IFS= read -r file; do
    local rel=$(realpath --relative-to="$src_dir" "$file" 2>/dev/null || echo "$file")
    local title=$(basename "$file" .md)
    local content=$(cat "$file")

    # 提取 frontmatter 头部交给 Python 侧解析 tags。
    # 只 grep 一行会漏掉块序列写法（tags: 之后跟缩进的 "- 值"），
    # 那种笔记的标签就永远同步不过去。
    local fm_head
    fm_head=$(awk '/^---$/{c++; if (c==2) exit; next} c==1 {print}' "$file" 2>/dev/null | head -40)

    # 提取内容（跳过 frontmatter）
    local body=$(awk '/^---$/{c++; next} c>=2 {print}' "$file" 2>/dev/null || echo "$content")

    # 构建 JSON：经 argv 传值，tags 由 Python 侧解析，不拼进源码。
    local data
    data=$(python3 - "$note_type" "$title" "$body" "$project_name" "$fm_head" <<'PY' 2>/dev/null
import json, re, sys

note_type, title, body, project_name, fm_head = sys.argv[1:6]


def parse_tags(fm):
    """解析 frontmatter 的 tags，支持三种写法：行内数组 / 块序列 / 无括号分隔。"""
    m = re.search(r"^tags:[ \t]*\[(.*?)\]", fm, re.S | re.M)          # 1) tags: [a, b]
    if m:
        raw = m.group(1)
    else:
        # 2) 块序列：tags: 之后跟缩进的 "- 值" 行
        m = re.search(r"^tags:[ \t]*\n((?:[ \t]+-[^\n]*\n?)+)", fm, re.M)
        if m:
            raw = "\n".join(ln.strip()[1:].strip()
                            for ln in m.group(1).splitlines() if ln.strip())
        else:
            m = re.search(r"^tags:[ \t]*(.+)$", fm, re.M)              # 3) tags: a, b
            raw = m.group(1) if m else ""
    return [t.strip().strip('"').strip("'")
            for t in re.split(r"[,，、\n]", raw)
            if t.strip().strip('"').strip("'")]

print(json.dumps({
    "type": note_type,
    "title": title,
    "content": body,
    "subdir": project_name,
    "tags": parse_tags(fm_head),
}, ensure_ascii=False))
PY
)

    local resp=$(curl -s -X POST $AUTH "$KB_API_BASE/notes" \
      -H "Authorization: Bearer $KB_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$data" 2>/dev/null)

    local action=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action','error'))" 2>/dev/null)
    echo "   $action: $title"
    [ "$action" = "created" ] && uploaded=$((uploaded + 1))
    [ "$action" = "updated" ] && uploaded=$((uploaded + 1))
  done < <(find "$src_dir" -name "*.md" -not -path "*/.git/*" 2>/dev/null)

  echo ""
  echo "   上传 $uploaded 个文件"
}

# ─── status: 查看同步状态 ───
sync_status() {
  echo "📊 知识库同步状态"
  echo "=================="
  echo "  API: $KB_API_BASE"
  echo ""

  local dash
  dash=$(curl -s $AUTH "$KB_API_BASE/dashboard" 2>/dev/null)
  echo "$dash" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  项目: {d.get(\"projects\",0)} | 知识: {d.get(\"knowledge\",0)} | 写作: {d.get(\"writing\",0)}')
print(f'  日记: {d.get(\"diaries\",0)} | 归档: {d.get(\"archived\",0)} | 总计: {d.get(\"total\",0)}')
" 2>/dev/null
  echo ""
  echo "  本地:"
  local proj=$(find "$ROOT_DIR/10-项目" -name "*.md" -not -path "*/.git/*" 2>/dev/null | wc -l)
  local know=$(find "$ROOT_DIR/20-知识" -name "*.md" -not -path "*/.git/*" 2>/dev/null | wc -l)
  local writ=$(find "$ROOT_DIR/30-写作" -name "*.md" -not -path "*/.git/*" 2>/dev/null | wc -l)
  echo "  项目: $proj | 知识: $know | 写作: $writ"
}

# ─── main ───
case "${1:-}" in
  pull|download)  sync_pull "$2" ;;
  push|upload)    sync_push "$2" "$3" ;;
  status|st)      sync_status ;;
  *)  echo "知识库同步工具"
      echo "用法:"
      echo "  bash scripts/sync.sh pull <项目名>        从服务器下载到本地"
      echo "  bash scripts/sync.sh push <源目录> [类型]  上传本地项目到服务器"
      echo "  bash scripts/sync.sh status               查看同步状态"
      echo ""
      echo "配置: export KB_TOKEN=your_token"
      echo "      export KB_API_BASE=http://127.0.0.1:8899"
      ;;
esac
