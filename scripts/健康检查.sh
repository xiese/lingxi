#!/bin/bash
# 知识库健康监控 — 定期检查服务状态，异常时记录日志
# 用法: bash scripts/健康检查.sh
# cron: */5 * * * * bash <仓库路径>/scripts/健康检查.sh

API="http://localhost:8899"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/.health.log"
DISK_STATE_FILE="/tmp/.kb_disk_last"
MAX_LOG=1000
DISK_WARN=90  # 磁盘使用率超过此值记录警告

RESP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "$API/health" 2>/dev/null)
NOW=$(date '+%Y-%m-%d %H:%M:%S')
DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | tr -d '%')

# 磁盘使用率监控（仅使用率变化时记录，避免日志膨胀）
DISK_LAST=""
[ -f "$DISK_STATE_FILE" ] && DISK_LAST=$(cat "$DISK_STATE_FILE")
if [ "$DISK_USAGE" -gt "$DISK_WARN" ] && [ "$DISK_USAGE" != "$DISK_LAST" ]; then
  echo "$NOW ⚠ 磁盘已用 ${DISK_USAGE}% （警告阈值 ${DISK_WARN}%）" >> "$LOG"
  echo "$DISK_USAGE" > "$DISK_STATE_FILE"
fi

# 告警通道：环境变量 KB_ALERT_WEBHOOK（通用 JSON 文本）或知识库自带 QQ 推送
notify_alert() {
  local msg="$1"
  echo "$NOW 🔔 $msg" >> "$LOG"
  KB_SCRIPTS_DIR="$SCRIPT_DIR" python3 - "$msg" <<'PYEOF' >> "$LOG" 2>&1
import sys, os, json, urllib.request
msg = sys.argv[1]
wh = (os.environ.get("KB_ALERT_WEBHOOK") or "").strip()
if wh:
    try:
        data = json.dumps({"msg_type": "text", "content": {"text": msg}}).encode()
        urllib.request.urlopen(urllib.request.Request(wh, data=data, headers={"Content-Type": "application/json"}), timeout=10)
        print("webhook sent")
    except Exception as e:
        print("webhook error:", e)
try:
    sys.path.insert(0, os.environ.get("KB_SCRIPTS_DIR", "scripts"))
    from kb_lib.push_qq import send_qq_message
    print("qq_push:", send_qq_message(msg))
except Exception as e:
    print("qq_push error:", e)
PYEOF
}

STATE="/tmp/.kb_health_state"
if [ "$RESP" != "200" ]; then
  echo "$NOW ❌ 健康检查失败 HTTP=$RESP" >> "$LOG"
  systemctl restart kb-server 2>/dev/null
  echo "$NOW 🔄 已尝试重启 kb-server" >> "$LOG"
  if [ "$(cat "$STATE" 2>/dev/null)" != "down" ]; then
    notify_alert "[知识库告警] $NOW 健康检查失败 HTTP=$RESP，已重启 kb-server"
    echo down > "$STATE"
  fi
else
  if [ "$(cat "$STATE" 2>/dev/null)" = "down" ]; then
    notify_alert "[知识库恢复] $NOW 健康检查已恢复正常"
  fi
  echo up > "$STATE"
  # 正常时也记录（每30分钟轮询一次真实统计）
  if [ $(( $(date +%s) % 1800 )) -lt 300 ]; then
    HEALTH_STATS=$(curl -s --max-time 5 "$API/health" 2>/dev/null)
    IDX_STATS=$(curl -s --max-time 5 "$API/api/index/status" 2>/dev/null)
    DISK_PCT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
    RECORD="$NOW ✅"
    if [ -n "$HEALTH_STATS" ]; then
      VER=$(echo "$HEALTH_STATS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('version','?'))" 2>/dev/null)
      RECORD="$RECORD v$VER"
    fi
    if [ -n "$IDX_STATS" ]; then
      IDX_INFO=$(echo "$IDX_STATS" | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'indexed={d[\"indexed\"]}')" 2>/dev/null)
      RECORD="$RECORD $IDX_INFO"
    fi
    RECORD="$RECORD disk=${DISK_PCT}%"
    echo "$RECORD" >> "$LOG"
  fi
fi

# 日志轮转
if [ -f "$LOG" ]; then
  LINES=$(wc -l < "$LOG")
  if [ "$LINES" -gt "$MAX_LOG" ]; then
    tail -n "$MAX_LOG" "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
  fi
fi
