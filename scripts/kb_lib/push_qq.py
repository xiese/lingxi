"""知识库 QQ 推送适配层。

第一阶段只提供通用 webhook payload、禁用态和 dry-run。
真实 QQ 通道参数由运行期 settings 提供，不写入仓库。
"""

import json
import urllib.request

from .config import load_settings
import logging

log = logging.getLogger("kb.push_qq")



# ─── 按事件勾选推送（2026-10-18 第 16 项）─────────────────────────────
# 口径：全部默认关闭，只推维护者勾上的。事件键一旦确定就不能随便改名，
# 否则已勾选的配置会静默失效——改名要同时写迁移。
PUSH_EVENTS = {
    "auth_request": "有人申请授权",
    "new_device": "新设备登录",
    "login_failed": "连续登录失败",
    "token_created": "令牌新建",
    "token_revoked": "令牌吊销",
    "release_deployed": "发版与部署结果",
    "job_failed": "索引或任务失败",
    "todo_due": "待办到期",
}


def enabled_events(settings=None):
    """返回已勾选的事件键集合。设置里存的是逗号分隔字符串。"""
    s = settings or load_settings()
    raw = s.get("qq_push_events", "") or ""
    if isinstance(raw, (list, tuple, set)):
        picked = [str(x).strip() for x in raw]
    else:
        picked = [x.strip() for x in str(raw).split(",")]
    return {e for e in picked if e in PUSH_EVENTS}


def is_event_enabled(event, settings=None):
    return event in enabled_events(settings)


def _public_settings(s):
    return {
        "enabled": bool(s.get("qq_push_enabled")),
        "target": s.get("qq_push_target", ""),
        "payload_mode": s.get("qq_push_payload_mode", "generic"),
        "has_webhook_url": bool(s.get("qq_push_webhook_url")),
        "has_secret": bool(s.get("qq_push_webhook_secret")),
        "events": sorted(enabled_events(s)),
    }


def build_qq_payload(message, settings=None):
    s = settings or load_settings()
    return {
        "target": s.get("qq_push_target", ""),
        "message": str(message or ""),
    }


def send_qq_message(message, dry_run=False, event=""):
    """发一条推送。

    event 非空时按勾选过滤：没勾上的事件一律不发，也不发「跳过通知」刷屏。
    event 为空表示手动测试推送（/admin/push/qq/test），不受勾选限制。
    """
    s = load_settings()
    payload = build_qq_payload(message, s)
    public = _public_settings(s)
    if event and event not in enabled_events(s):
        return {
            "sent": False,
            "dry_run": bool(dry_run),
            "skipped": True,
            "event": event,
            "reason": f"event {event} not enabled",
            "settings": public,
        }
    if dry_run:
        return {
            "sent": False,
            "dry_run": True,
            "disabled": not public["enabled"],
            "reason": "dry_run",
            "payload": payload,
            "settings": public,
        }
    if not public["enabled"]:
        return {
            "sent": False,
            "dry_run": False,
            "disabled": True,
            "reason": "qq_push_enabled=false",
            "payload": payload,
            "settings": public,
        }
    url = s.get("qq_push_webhook_url", "").strip()
    if not url:
        return {
            "sent": False,
            "dry_run": False,
            "disabled": True,
            "reason": "qq_push_webhook_url empty",
            "payload": payload,
            "settings": public,
        }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = s.get("qq_push_webhook_secret", "").strip()
    if secret:
        headers["X-Webhook-Secret"] = secret
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(1000).decode("utf-8", errors="replace")
        return {
            "sent": True,
            "dry_run": False,
            "status": getattr(resp, "status", 200),
            "response": body,
            "settings": public,
        }
    except Exception as e:
        log.warning("push_qq.py 异常: send_qq_message", exc_info=True)
        return {
            "sent": False,
            "dry_run": False,
            "error": str(e),
            "settings": public,
        }
