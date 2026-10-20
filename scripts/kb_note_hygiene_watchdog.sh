#!/usr/bin/env bash
# 知识库笔记卫生周扫：只报警不删除。
# 有问题时输出 Markdown 报告（cron no_agent 会投递）；干净时静默（无 stdout）。
set -euo pipefail
SCRIPT="$(cd "$(dirname "$0")" && pwd)/kb_note_hygiene.py"
OUT=$(python3 "$SCRIPT" --quiet-ok 2>&1) || true
# --quiet-ok: exit 0 + empty when clean; exit 1 + report when issues
if [[ -n "${OUT//[[:space:]]/}" ]]; then
  echo "### 知识库笔记卫生周扫"
  echo
  echo "时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo
  echo "有问题（只报告，未自动归档/删除/push）。"
  echo "处理：回我「按你推荐的来」或点名条目。"
  echo
  echo '```'
  # 限制长度，避免刷屏
  echo "$OUT" | head -n 120
  echo '```'
  echo
  echo "扫描命令：\`python3 $SCRIPT\`"
fi
