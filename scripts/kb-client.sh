#!/bin/bash
# 知识库 API 客户端 — 从任意设备访问知识库
# 用法:
#   source scripts/kb-client.sh
#   或直接:
#   bash scripts/kb-client.sh dashboard
#   bash scripts/kb-client.sh search "Docker"
#   bash scripts/kb-client.sh read 20-知识/技术/Docker.md
#   bash scripts/kb-client.sh create 知识 "标题" "内容" "子目录" "tag1,tag2"
#   bash scripts/kb-client.sh upload /path/to/file.png
#
# 配置: 编辑此文件或设置环境变量 KB_TOKEN / KB_API_BASE（旧名 KB_HOST 仅兼容一版）

# API 地址解析顺序：KB_API_BASE → 旧名 KB_HOST（兼容一版）→ 默认本机。
# 服务端已把同名双义的 KB_HOST 拆为 KB_BIND（监听绑定）/ KB_API_BASE（上游 API 地址），
# 旧名 KB_HOST 在此仅作 API 地址的一版兼容，请尽快改用 KB_API_BASE。
KB_API_BASE="${KB_API_BASE:-${KB_HOST:-http://127.0.0.1:8899}}"
KB_TOKEN="${KB_TOKEN:-}"
TOKEN_FILE="$HOME/.kb_token"

# 尝试从 token 文件读取
if [ -z "$KB_TOKEN" ] && [ -f "$TOKEN_FILE" ]; then
  KB_TOKEN=$(head -1 "$TOKEN_FILE")
fi

AUTH_HEADER="Authorization: Bearer $KB_TOKEN"

usage() {
  echo "知识库 API 客户端"
  echo ""
  echo "用法:"
  echo "  kb dashboard                   查看仪表盘"
  echo "  kb list [目录]                 列出笔记"
  echo "  kb read <路径>                 读取笔记"
  echo "  kb search <关键词>             搜索笔记"
  echo "  kb search --tag <标签>         按标签搜索"
  echo "  kb create <类型> <标题> <内容> [子目录] [标签]"
  echo "  kb upload <文件路径> [目录]    上传文件"
  echo "  kb token-new <名称>            创建只读 token"
  echo ""
  echo "首次使用需设置 token:"
  echo "  export KB_TOKEN=your_token_here"
  echo "  或 echo 'your_token' > ~/.kb_token"
  exit 0
}

api_call() {
  local method="$1"
  local path="$2"
  local data="$3"
  local url="${KB_API_BASE}${path}"

  if [ -n "$data" ]; then
    curl -s -X "$method" "$url" -H "$AUTH_HEADER" -H "Content-Type: application/json" -d "$data"
  else
    curl -s -X "$method" "$url" -H "$AUTH_HEADER"
  fi
}

[ -z "$KB_TOKEN" ] && { echo "❌ 未设置 KB_TOKEN"; echo "   export KB_TOKEN=your_token 或 echo 'token' > ~/.kb_token"; exit 1; }

CMD="$1"
shift 2>/dev/null || true

case "$CMD" in
  dashboard|dash)
    api_call GET "/dashboard" | python3 -m json.tool 2>/dev/null || api_call GET "/dashboard"
    ;;
  list|ls)
    dir="${1:-}"
    api_call GET "/notes?dir=$dir" | python3 -m json.tool 2>/dev/null || api_call GET "/notes?dir=$dir"
    ;;
  read|cat|get)
    [ -z "$1" ] && { echo "用法: kb read <路径>"; exit 1; }
    api_call GET "/notes/$1" | python3 -m json.tool 2>/dev/null || api_call GET "/notes/$1"
    ;;
  search|find)
    if [ "$1" = "--tag" ]; then
      api_call GET "/search?tag=$2" | python3 -m json.tool 2>/dev/null
    else
      api_call GET "/search?q=$*" | python3 -m json.tool 2>/dev/null
    fi
    ;;
  create|new)
    [ -z "$1" ] && { echo "用法: kb create <类型> <标题> <内容> [子目录] [标签]"; exit 1; }
    _type="$1"; _title="$2"; _content="$3"; _subdir="${4:-}"; _tags="${5:-}"
    _tags_json="[]"
    [ -n "$_tags" ] && _tags_json="[$(echo "$_tags" | sed 's/, */\", \"/g' | sed 's/^/\"/;s/$/\"/')]"
    data="{\"type\":\"$_type\",\"title\":\"$_title\",\"content\":\"$(echo "$_content" | sed 's/"/\\"/g')\",\"subdir\":\"$_subdir\",\"tags\":$_tags_json}"
    api_call POST "/notes" "$data" | python3 -m json.tool 2>/dev/null
    ;;
  upload|up)
    [ -z "$1" ] && { echo "用法: kb upload <文件路径> [目录]"; exit 1; }
    _file="$1"; _dir="${2:-_assets}"
    curl -s -X POST "${KB_API_BASE}/upload" -H "$AUTH_HEADER" -F "file=@$_file" -F "dir=$_dir" | python3 -m json.tool 2>/dev/null
    ;;
  token-new|tk)
    _name="${1:-client}"
    _perm="${2:-r}"
    data="{\"name\":\"$_name\",\"permission\":\"$_perm\"}"
    api_call POST "/admin/tokens" "$data" | python3 -m json.tool 2>/dev/null
    ;;
  health|ping)
    api_call GET "/health" | python3 -m json.tool 2>/dev/null
    ;;
  *)
    usage
    ;;
esac
