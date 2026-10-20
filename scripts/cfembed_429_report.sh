#!/bin/bash
# CF 嵌入路由 429 日报
# 统计 /var/log/cfembed-router.log 昨日 429 / cooldown / used up 次数，
# 一行摘要追加到 scripts/.cfembed-429.log。只记录，不告警（用户明确不要告警）。
set -u

LOG=/var/log/cfembed-router.log
OUT=/srv/notes/知识库/scripts/.cfembed-429.log
DAY=$(date -d 'yesterday' +%Y-%m-%d)

n429=0; ncool=0; nused=0; nlines=0
if [ -f "$LOG" ]; then
  TMP=$(mktemp)
  grep "^$DAY" "$LOG" > "$TMP" 2>/dev/null || true
  nlines=$(wc -l < "$TMP")
  n429=$(grep -c "429" "$TMP" || true)
  ncool=$(grep -c "cooldown" "$TMP" || true)
  nused=$(grep -c "used up" "$TMP" || true)
  rm -f "$TMP"
fi

echo "$(date '+%F %T') day=$DAY 429=$n429 cooldown=$ncool used_up=$nused log_lines=$nlines" >> "$OUT"
