#!/usr/bin/env python3
"""检测知识库 Token 申请并通过 QQ 通道推送。

默认 dry-run，不安装 cron，不保存 token/claim/secret。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
import logging

log = logging.getLogger("kb_auth_watch")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from kb_lib.config import TOKEN_FILE  # noqa: E402
from kb_lib.push_qq import send_qq_message  # noqa: E402

DEFAULT_STATE_FILE = ROOT / ".kb_auth_watch_state.json"


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("读取 JSON 失败，使用默认值: %s", path, exc_info=True)
        return default


def save_json(path, data):
    path = Path(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        log.warning("设置文件权限 0600 失败: %s", path, exc_info=True)


def pending_requests(store):
    return [
        (rid, req) for rid, req in store.get("requests", {}).items()
        if req.get("status") == "pending"
    ]


def build_message(items):
    lines = ["新设备授权请求"]
    for rid, req in items:
        lines.append("")
        lines.append(rid)
        lines.append(f"设备：{req.get('device', '?')}")
        lines.append(f"IP：{req.get('ip', '?')}")
        lines.append(f"权限：{req.get('permission', 'r')}")
        lines.append(f"时间：{req.get('created', '')[:16].replace('T', ' ')}")
    lines.append("")
    lines.append(f"共 {len(items)} 条")
    return "\n".join(lines)


def run_once(state_file=DEFAULT_STATE_FILE, dry_run=True, all_pending=False):
    store = load_json(TOKEN_FILE, {"requests": {}})
    state = load_json(state_file, {"notified_request_ids": {}, "last_run": None})
    notified = state.setdefault("notified_request_ids", {})
    pending = pending_requests(store)
    if all_pending:
        targets = pending
    else:
        targets = [(rid, req) for rid, req in pending if rid not in notified]
    message = build_message(targets) if targets else ""
    push = None
    if targets:
        # 第 16 项：这里是「有人申请授权」的真实推送，必须带事件名，
        # 否则设置页的勾选对这条推送完全不起作用。
        push = send_qq_message(message, dry_run=dry_run, event="auth_request")
        now = datetime.now().isoformat()
        for rid, _ in targets:
            notified[rid] = now
    state["last_run"] = datetime.now().isoformat()
    save_json(state_file, state)
    return {
        "pending": len(pending),
        "new": len(targets),
        "dry_run": dry_run,
        "message": message,
        "push": push,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="执行一次检测")
    ap.add_argument("--dry-run", action="store_true", help="只生成消息，不真实发送")
    ap.add_argument("--all-pending", action="store_true", help="忽略去重状态，推送所有 pending 申请")
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    args = ap.parse_args()
    result = run_once(args.state_file, dry_run=args.dry_run or not args.once, all_pending=args.all_pending)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
