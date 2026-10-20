#!/usr/bin/env bash
# 本机最短上传：直连 REST，不走 MCP
# 用法:
#   kb-up 笔记.md
#   kb-up a.md b.pdf --type knowledge
#   kb-up 笔记.md --dir 20-知识/工具研究
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="${KB_CLI:-$SCRIPT_DIR/kb-cli.py}"

if [[ $# -lt 1 ]]; then
  cat <<'EOF'
用法: kb-up <文件...> [--type TYPE] [--dir DIR]

示例:
  kb-up 笔记.md
  kb-up a.md b.pdf --type knowledge
  kb-up 笔记.md --dir 20-知识/工具研究

说明:
  - 本命令直连知识库 REST /upload
  - 请勿经由 MCP 的 kb_upload 转发本机文件（见 客户端接入指南.md）
  - 首次请先: python3 kb-cli.py config init
EOF
  exit 1
fi

# 兼容 --type/--dir 与 kb-cli 的 -type/-dir
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --type)
      args+=(-type "$2"); shift 2 ;;
    --dir)
      args+=(-dir "$2"); shift 2 ;;
    --json)
      args+=(-json); shift ;;
    *)
      args+=("$1"); shift ;;
  esac
done

exec python3 "$CLI" upload "${args[@]}"
