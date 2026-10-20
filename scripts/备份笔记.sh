#!/bin/bash
# 每日备份笔记内容（笔记/ 与 00-索引/ 未纳入 git，单独打包）
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${KB_BACKUP_DIR:-$ROOT_DIR/../kb-backups}"
KEEP_DAYS="${KB_BACKUP_KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/notes-$STAMP.tar.gz"
tar czf "$OUT" -C "$ROOT_DIR" 笔记 00-索引
find "$BACKUP_DIR" -name "notes-*.tar.gz" -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') 备份完成: $OUT ($(du -h "$OUT" | cut -f1))"
