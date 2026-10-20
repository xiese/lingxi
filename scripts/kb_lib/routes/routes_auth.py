"""知识库 API — 认证路由"""

import hashlib, secrets, threading, time
import logging
from datetime import datetime, timedelta
from flask import request, jsonify

from ..routes import bp, json_body_dict
from ..config import get_setting
from ..store import (load_store, store_transaction, is_expired,
                     token_hash, token_prefix, token_record)
from ..audit import (audit_log, login_log, login_locked, query_logins,
                     query_devices, LOGIN_LOG_DAYS, LOGIN_LOCK_SECONDS,
                     maybe_cleanup_login_log, login_success_once)
from ..auth import require_master, get_client

# ── IP 限流（auth_request 防止滥用）──

_AUTH_REQ_LIMIT = {}  # ip -> [timestamps]
_AUTH_REQ_MAX = 5     # 每 IP 最多 5 次
_AUTH_REQ_WIN = 300   # 5 分钟窗口
_AUTH_REQ_LIMIT_LOCK = threading.Lock()

# 吊销令牌时的最小前缀长度，避免过短前缀误删无关令牌
MIN_REVOKE_HINT = 12


log = logging.getLogger("kb.routes_auth")

def _cleanup_auth_req_limit():
    """后台线程：每 300 秒清理过期 IP 限流记录，防止内存泄漏"""
    while True:
        time.sleep(300)
        now = time.time()
        cutoff = now - _AUTH_REQ_WIN
        with _AUTH_REQ_LIMIT_LOCK:
            for ip in list(_AUTH_REQ_LIMIT.keys()):
                _AUTH_REQ_LIMIT[ip] = [t for t in _AUTH_REQ_LIMIT[ip] if t > cutoff]
                if not _AUTH_REQ_LIMIT[ip]:
                    del _AUTH_REQ_LIMIT[ip]


_auth_limit_cleanup_started = False


def start_auth_limit_cleanup():
    """启动鉴权请求限流记录清理线程（幂等，由 kb_lib.start_background() 调用）。

    2026-10-07 修复：原实现在模块级直接起线程——任何 import kb_lib.routes_auth
    （含 import kb_lib 本身，因为包初始化会导入 routes）都会拉起这个 300 秒循环。
    现收编为显式启动，import 只做声明；线程职责、循环间隔、退出条件均不变。
    """
    global _auth_limit_cleanup_started
    if _auth_limit_cleanup_started:
        return
    _auth_limit_cleanup_started = True
    threading.Thread(target=_cleanup_auth_req_limit, daemon=True,
                     name="kb-auth-limit-cleanup").start()


def _check_auth_rate_limit(ip):
    """检查 IP 是否超限，返回 True 表示允许"""
    now = time.time()
    cutoff = now - _AUTH_REQ_WIN
    with _AUTH_REQ_LIMIT_LOCK:
        if ip in _AUTH_REQ_LIMIT:
            _AUTH_REQ_LIMIT[ip] = [t for t in _AUTH_REQ_LIMIT[ip] if t > cutoff]
            if len(_AUTH_REQ_LIMIT[ip]) >= _AUTH_REQ_MAX:
                return False
        else:
            _AUTH_REQ_LIMIT[ip] = []
        _AUTH_REQ_LIMIT[ip].append(now)
    return True


def _normalize_permission(permission):
    perm = str(permission or "r").strip().lower()
    if perm not in ("r", "rw"):
        return None
    return perm


# 设备 Token TTL 预设（分钟）。master 永不走这里；设备 Token 可单独拉长。
TTL_PRESETS = {
    "1h": 60,
    "1d": 1440,
    "7d": 10080,
    "30d": 43200,
    "90d": 129600,
    "180d": 259200,
    "365d": 525600,
    "1y": 525600,
}


def _parse_ttl(value):
    """解析设备 Token TTL（分钟）。

    支持：
    - 数字分钟：60 / 43200 / 525600
    - 预设：1h / 1d / 7d / 30d / 90d / 180d / 365d / 1y
    - 空值：读设置 token_ttl（默认 43200=30 天）
    上限 2 年，避免误填超大数。
    """
    default = int(get_setting("token_ttl"))
    if value in (None, ""):
        return default
    if isinstance(value, str):
        key = value.strip().lower()
        if key in TTL_PRESETS:
            return TTL_PRESETS[key]
    try:
        ttl = int(value)
    except (TypeError, ValueError):
        log.warning("routes_auth.py 异常: _parse_ttl", exc_info=True)
        return default
    if ttl <= 0:
        return default
    # 上限 2 年
    return min(ttl, 525600 * 2)


def _hash_claim(claim):
    return hashlib.sha256(str(claim).encode("utf-8")).hexdigest()


def _verify_claim(record, claim):
    """校验 claim。

    已消费的记录 claim_hash 会清空（C2-12），但 claim 本身仍是客户端手里的
    凭据：把它的哈希另存到 claim_consumed_hash，这样「同一个 claim 再来一次」
    还能认出是本人（→ 410），而「随便编一个 claim」一律 403，问不出任何状态。
    """
    expected = record.get("claim_hash") or record.get("claim_consumed_hash") or ""
    return bool(expected and claim and secrets.compare_digest(expected, _hash_claim(claim)))


def _issue_token(store, *, name, device, permission, ttl, note="", request_id=None, source="manual", client=None):
    """签发令牌：库里只写哈希键，明文只返回给调用方一次。

    C2 存储结构：
      key   = sha256(pepper + 明文) 的 64 位十六进制
      记录  = 原字段不变 + id（可外露的稳定句柄）+ prefix（哈希前 12 位，供吊销）
    记录里不放明文、不放明文任何片段（可还原的信息一律不留）。
    """
    now = datetime.now()
    exp = now + timedelta(minutes=ttl)
    tk = secrets.token_urlsafe(32)
    client = client or {}
    info = {
        "name": name or device or "Token",
        "permission": permission,
        "device": device or name or "Token",
        "ip": client.get("ip", ""),
        "user_agent": client.get("user_agent", ""),
        "created": now.isoformat(),
        "expires_at": exp.isoformat(),
        "ttl": ttl,
        "approved_by": "master",
        "status": "active",
        "request_id": request_id,
        "source": source,
        "note": note or "",
        "platform": client.get("platform", ""),
        "project_scope": client.get("project_scope", ""),
        "default_dir": client.get("default_dir", ""),
        "default_tags": client.get("default_tags", []),
        "identity_label": client.get("identity_label", ""),
    }
    # 同一明文只算一次哈希：token_record 与写键都要它，算两遍等于白读两次 pepper。
    h = token_hash(tk)
    token_record(tk, info, hashed=h)
    store["tokens"][h] = info
    return tk, exp


# ── 认证 ──

@bp.route("/auth/request", methods=["POST"])
def auth_request():
    d = json_body_dict()
    c = get_client()
    ip = c["ip"]
    rid = f"req_{secrets.token_hex(8)}"
    perm = _normalize_permission(d.get("permission", "rw"))
    if not perm:
        return jsonify({"error": "权限必须是 r 或 rw"}), 400
    # 第 15 项：同一 IP 连续失败 5 次锁 15 分钟（主令牌是 require_master，走不到这里，不受影响）
    # 顺手触发「每天最多一次」的 90 天清理，不装定时任务也能把保留策略真正跑起来。
    try:
        maybe_cleanup_login_log()
    except Exception:
        pass
    if login_locked(ip):
        # 锁定期内不再写失败记录：否则攻击者持续请求就能把 15 分钟窗口一直往后推，
        # 变成无限期锁定（正常用户也被锁死）。
        return jsonify({
            "error": f"该网络失败次数过多，请 {LOGIN_LOCK_SECONDS // 60} 分钟后再试",
            "locked": True,
            "retry_after": LOGIN_LOCK_SECONDS,
        }), 429
    if not _check_auth_rate_limit(ip):
        login_log(False, ip=ip, device=c["device"], reason="请求过于频繁")
        return jsonify({"error": "请求过于频繁，请稍后再试"}), 429
    claim = secrets.token_urlsafe(24)
    with store_transaction() as s:
        s["requests"][rid] = {
        "id": rid, "device": d.get("name", c["device"]), "ip": c["ip"],
        "user_agent": c["user_agent"], "permission": perm,
        "created": c["time"], "status": "pending", "note": d.get("note", ""),
        "platform": d.get("platform", ""), "project_scope": d.get("project_scope", ""),
        "default_dir": d.get("default_dir", ""), "default_tags": d.get("default_tags", []),
        "identity_label": d.get("identity_label", "") or d.get("name", c["device"]),
            "claim_hash": _hash_claim(claim), "last_polled_at": None, "delivered_at": None,
        }
    # 注意：这只是「申请登录」，真正拿到令牌是在 /auth/status 领取时，那里才记成功登录。
    # 这里不写 login_log 的失败记录——否则正常申请会被算进「失败 5 次锁定」里误伤。
    audit_log("auth_request", f"/auth/request/{rid}")
    # 第 16 项：真实推送接上事件名，设置页勾选才真正生效
    try:
        from ..push_qq import send_qq_message
        send_qq_message(
            f"有人申请授权\n设备：{d.get('name', c['device'])}\nIP：{ip}\n权限：{perm}",
            event="auth_request",
        )
    except Exception:
        pass  # 推送失败不影响登录申请本身
    return jsonify({"request_id": rid, "claim": claim, "status": "pending", "ttl": get_setting("token_ttl")})


@bp.route("/auth/requests")
@require_master
def list_requests():
    s = load_store()
    return jsonify({
        "pending": sum(1 for r in s.get("requests", {}).values() if r.get("status") == "pending"),
        "requests": s.get("requests", {}),
    })


@bp.route("/auth/notifications")
@require_master
def notifications():
    s = load_store()
    pending = [(rid, r) for rid, r in s.get("requests", {}).items() if r.get("status") == "pending"]
    if not pending:
        return jsonify({"has_pending": False})
    lines = ["📋 新设备授权请求"]
    for rid, r in pending:
        lines.append(f"\n🔹 {rid}")
        lines.append(f"   {r.get('device', '?')} | {r.get('ip', '?')} | {r.get('permission', 'rw')}")
        lines.append(f"   {r.get('created', '')[:16].replace('T', ' ')}")
    lines.append(f"\n── {len(pending)} 条 ──")
    return jsonify({"has_pending": True, "count": len(pending), "message": "\n".join(lines), "pending": pending})


@bp.route("/auth/token", methods=["POST"])
@require_master
def create_token():
    d = json_body_dict()
    c = get_client()
    perm = _normalize_permission(d.get("permission", "r"))
    if not perm:
        return jsonify({"error": "权限必须是 r 或 rw"}), 400
    ttl = _parse_ttl(d.get("ttl"))
    with store_transaction() as s:
        identity_label = d.get("identity_label", "").strip() or d.get("name", "").strip() or d.get("device", "").strip()
        client_meta = dict(c)
        client_meta.update({
            "platform": d.get("platform", "").strip(),
            "project_scope": d.get("project_scope", "").strip(),
            "default_dir": d.get("default_dir", "").strip(),
            "default_tags": d.get("default_tags", []),
            "identity_label": identity_label,
        })
        tk, exp = _issue_token(
            s,
            name=d.get("name", "").strip() or d.get("device", "").strip() or "手动 Token",
            device=d.get("device", "").strip() or d.get("name", "").strip() or "手动 Token",
            permission=perm,
            ttl=ttl,
            note=d.get("note", ""),
            request_id=None,
            source="manual",
            client=client_meta,
        )
    audit_log("auth_token_create", "/auth/token", extra=f"device={d.get('device','')} perm={perm}")
    return jsonify({
        "token": tk,
        "name": d.get("name", "") or d.get("device", "") or "手动 Token",
        "device": d.get("device", "") or d.get("name", "") or "手动 Token",
        "permission": perm,
        "expires_at": exp.isoformat(),
        "ttl": f"{ttl}分钟",
    })




@bp.route("/auth/ttl-presets")
@require_master
def ttl_presets():
    """设备 Token 常用 TTL 预设（分钟）。master 不使用 TTL。"""
    return jsonify({
        "default_minutes": int(get_setting("token_ttl")),
        "presets": TTL_PRESETS,
        "unit": "minutes",
        "note": "设备/平台 Token 可单独拉长到 90d/180d/1y；勿与 master 混用",
    })

@bp.route("/auth/request/<rid>/status")
def request_status(rid):
    """审批状态查询（C2：令牌只下发一次）。

    库里没有明文，所以「第一次取走明文」必须在取走的那一刻现场签发：
    approve 只批准不签发，客户端首次轮询时在同一个 store_transaction 内
    签发 + 标记已消费 + 返回明文；之后同一个 rid 再查一律 410。

    判定顺序：404 → 403（claim 不对）→ 410（已领取）。
    先验 claim 再判已消费：已消费的记录仍保留 claim 的哈希（claim_consumed_hash），
    本人拿原 claim 重放得到 410，别人拿错误 claim 得到 403，状态无从探测。
    """
    claim = request.args.get("claim", "")
    first_delivery = False
    tk = ""
    res = {}
    with store_transaction() as s:
        r = s.get("requests", {}).get(rid)
        if not r:
            return jsonify({"error": "不存在"}), 404
        # 先验 claim 再看状态：反过来的话，已消费时根本不验 claim，任意错误 claim
        # 都能拿到 410，配合 403 就能把「这个 rid 是否存在、是否已批准」问出来。
        if not _verify_claim(r, claim):
            return jsonify({"error": "claim 无效"}), 403
        # 已下发过就永不再发：旧记录可能只有 delivered_at 没有 claim_consumed_at，
        # 只判后者会让它被二次签发（DS 第 10 条）。两个字段任一存在即视为已领取。
        if r.get("delivered_at") or r.get("claim_consumed_at"):
            return jsonify({"error": "令牌已领取，仅显示一次", "code": "claim_consumed"}), 410
        r["last_polled_at"] = datetime.now().isoformat()
        status = r.get("status", "pending")
        res = {
            "request_id": rid,
            "status": status,
            "permission": r.get("permission", ""),
            "device": r.get("device", ""),
            "created": r.get("created", ""),
        }
        if status == "rejected":
            res["rejected_at"] = r.get("rejected_at", "")
        elif status == "approved":
            # 惰性签发：明文只在这里生成一次，库里只落哈希。
            perm = _normalize_permission(r.get("permission", "rw")) or "r"
            ttl = _parse_ttl(None)
            tk, exp = _issue_token(
                s,
                name=r.get("device", ""),
                device=r.get("device", ""),
                permission=perm,
                ttl=ttl,
                note=r.get("note", ""),
                request_id=rid,
                source="request",
                client={
                    "ip": r.get("ip", ""), "user_agent": r.get("user_agent", ""),
                    "platform": r.get("platform", ""), "project_scope": r.get("project_scope", ""),
                    "default_dir": r.get("default_dir", ""), "default_tags": r.get("default_tags", []),
                    "identity_label": r.get("identity_label", "") or r.get("device", ""),
                },
            )
            first_delivery = not r.get("delivered_at")
            now_iso = datetime.now().isoformat()
            r["delivered_at"] = r.get("delivered_at") or now_iso
            # 消费 claim：与签发同一把锁，并发轮询只有第一个能进来。
            # claim_hash 清空（C2-12 口径），原哈希留到 claim_consumed_hash，
            # 供本人重放时仍能验出「是你，但来晚了」→ 410。
            r["claim_consumed_at"] = now_iso
            r["claim_consumed_hash"] = r.get("claim_hash", "")
            r["claim_hash"] = ""
            res.update({
                "token": tk,
                "expires_at": exp.isoformat(),
                "ttl": f"{ttl}分钟",
            })
    # 第 15 项：令牌首次交付给设备＝这台设备登录成功。令牌只会下发一次，
    # first_delivery 在这里必然为真，保留判定是为了结构不变与幂等。
    if status == "approved" and first_delivery:
        # 「判新设备」和「写这条成功记录」必须在同一把锁里原子完成：
        # 只给判定加锁没用——查完释放到调用方写记录之间有空档，并发会双双判新。
        try:
            from ..audit import login_success_once
            _new_dev = login_success_once(
                ip=r.get("ip", ""),
                device=r.get("device", ""),
                token_name=r.get("device", ""),
                token_prefix=(token_prefix(tk) + "...") if tk else "",
                reason="领取令牌成功",
            )
        except Exception:
            _new_dev = False
            login_log(
                True,
                ip=r.get("ip", ""),
                device=r.get("device", ""),
                token_name=r.get("device", ""),
                token_prefix=(token_prefix(tk) + "...") if tk else "",
                reason="领取令牌成功",
            )
        # 第 16 项：这台设备是第一次登录才推「新设备登录」，同一设备反复轮询不刷屏
        try:
            from ..push_qq import send_qq_message
            if _new_dev:
                send_qq_message(
                    f"新设备登录\n设备：{r.get('device', '')}\nIP：{r.get('ip', '')}",
                    event="new_device",
                )
        except Exception:
            pass
    return jsonify(res)


@bp.route("/auth/approve/<rid>", methods=["POST"])
@require_master
def approve(rid):
    """批准申请：只改状态，不签发、不返回明文（C2「只显示一次」）。

    令牌由客户端首次轮询 status 时惰性签发并当场返回一次。
    管理员这边拿到明文会让同一把令牌出现两次，与「只显示一次」矛盾。
    """
    with store_transaction() as s:
        r = s.get("requests", {}).get(rid)
        if not r:
            return jsonify({"error": "不存在"}), 404
        if r["status"] != "pending":
            return jsonify({"error": "状态:" + r["status"]}), 400
        perm = _normalize_permission(r.get("permission", "rw")) or "r"
        r["permission"] = perm
        r["status"] = "approved"
        r["approved_at"] = datetime.now().isoformat()
    audit_log("auth_approve", f"/auth/approve/{rid}", extra=f"device={r.get('device')} perm={perm}")
    return jsonify({
        "status": "approved", "device": r.get("device"), "permission": perm,
        "note": "令牌将在客户端首次查询状态时下发一次",
    })


@bp.route("/auth/reject/<rid>", methods=["POST"])
@require_master
def reject(rid):
    with store_transaction() as s:
        r = s.get("requests", {}).get(rid)
        if not r:
            return jsonify({"error": "不存在"}), 404
        r["status"] = "rejected"
        r["rejected_at"] = datetime.now().isoformat()
    return jsonify({"status": "rejected"})


@bp.route("/auth/sessions")
@require_master
def sessions():
    s = load_store()
    now = datetime.now()
    items = []
    for tk, info in s.get("tokens", {}).items():
        exp = info.get("expires_at", "")
        mins = 0
        rem = ""
        if exp:
            try:
                d = datetime.fromisoformat(exp) - now
                mins = max(0, int(d.total_seconds() / 60))
                rem = f"剩{mins}分钟" if mins > 0 else "已过期"
            except Exception:
                log.warning("routes_auth.py 异常", exc_info=True)
        items.append({
            "prefix": info.get("prefix") or tk[:12], "name": info.get("name", ""),
            "device": info.get("device", ""), "ip": info.get("ip", ""),
            "permission": info.get("permission", ""),
            "created": info.get("created", "")[:16],
            "expires_at": exp[:16], "remaining": rem,
            "mins_left": mins, "status": info.get("status", ""),
        })
    items.sort(key=lambda x: x.get("mins_left", 0))
    return jsonify({"count": len(items), "sessions": items})


@bp.route("/auth/logins")
@require_master
def auth_logins():
    """登录记录：时间、IP、设备、令牌名、成功/失败、失败原因（第 15 项）。

    参数：page / limit / ok(1|0) / device / ip
    """
    page = request.args.get("page", 1)
    limit = request.args.get("limit", 100)
    ok_raw = request.args.get("ok", "")
    ok = None
    if ok_raw == "1":
        ok = True
    elif ok_raw == "0":
        ok = False
    data = query_logins(
        page=page,
        limit=limit,
        ok=ok,
        device=request.args.get("device", ""),
        ip=request.args.get("ip", ""),
    )
    data["retention_days"] = LOGIN_LOG_DAYS
    return jsonify(data)


@bp.route("/auth/devices")
@require_master
def auth_devices():
    """设备清单：**每台设备一行**，行内 `tokens` 是该设备关联的令牌列表。

    注意 `count` 是设备数，不是令牌数；一行可以挂多把令牌
    （同一设备名下、以及绑到这台设备上但创建时 device 字段不同的那些）。

    吊销不在这里做——沿用既有的 DELETE /auth/revoke/<hint>，
    它删掉令牌后该设备立即失效（令牌存在 store 里，删了就不认）。

    C3 要求条目里能看到：令牌 label / prefix、当前 IP、最后活跃时间、
    是否已绑定设备、绑定到哪个设备。前四项来自 audit_log（query_devices），
    绑定状态来自令牌记录（bound_device，C1 写入）。
    """
    items = query_devices()
    s = load_store()
    # 前缀 → 令牌记录。审计里的 token_prefix 是「哈希前 12 位 + ...」，
    # 与记录里的 prefix 字段同一口径（C2/D1），剥掉省略号即可对上。
    by_prefix = {}
    for _key, info in s.get("tokens", {}).items():
        p = info.get("prefix", "")
        if p:
            by_prefix[p] = info
    # master 令牌单独存（store["master"]），不在 tokens 表里。
    # 2026-10-19：手机用的就是 master，之前没并进来，设备页一律显示「无有效令牌」。
    _m = s.get("master") or {}
    _master_prefix = ((_m.get("token") or "")[:12]) if isinstance(_m, dict) else ""
    _master_rec = None
    if _master_prefix:
        _master_rec = {
            "prefix": _master_prefix,
            "name": (_m.get("name") or "master"),
            "permission": "master",
            "expires_at": "不过期",
            "is_master": True,
        }

    def _tk_view(info, is_master=False):
        return {
            "prefix": info.get("prefix", ""),
            "id": info.get("id", ""),
            "name": info.get("name", ""),
            "permission": info.get("permission", ""),
            "expires_at": "不过期" if is_master else (info.get("expires_at", "") or "")[:16],
            # label 优先读记录自己的 label 字段，没有才回落 name / device。
            # 不能只拼 name/device：记录里显式写的 label 会被直接吞掉。
            "label": (info.get("label") or info.get("name")
                      or info.get("device") or ""),
            "bound_device": info.get("bound_device", "") or "",
            "bound_at": info.get("bound_at", "") or "",
            "bound": bool(info.get("bound_device")),
            "expired": False if is_master else bool(is_expired(info)),
            "is_master": is_master,
        }

    # 设备名 → 该名下所有「还活着」的令牌（旧实现有这张表，重写时丢了）。
    #
    # 为什么必须有：列表骨架是审计行，每行只挂「最后一次使用的前缀」那一条。
    # 一把已绑定、但最近没用过的令牌不会出现在任何审计行的最新前缀上，
    # 于是整页看不到它，也就没法对它调 unbind（C1 第 9 条的操作闭环会断）。
    # 这里按令牌记录里创建时写死的 device 字段把漏掉的补回同一行，
    # 不改列表骨架、不改 count 口径（count 仍等于设备数）。
    # 建键要同时认 device 与 bound_device，两个原因是实测的：
    #   ① 审计行的 device 取的是令牌记录里的 `device` 字段（创建时写死），
    #      而 C1 绑定写的是 `bound_device`，两者可能不一致
    #      （创建时 device="Token"、后来绑到某台设备就是这么来的）；
    #   ② 若只按 device 建键，绑定后 device 为空的令牌会被整类丢掉，
    #      而「绑上但之后没再用过」恰好是 unbind 闭环最需要看见的那一类。
    # 同一令牌挂到两个键下；后面用 (id, prefix) 去重，不会重复显示。
    alive_by_dev = {}
    for _k, _info in s.get("tokens", {}).items():
        if is_expired(_info):
            continue                      # 死令牌不补挂（旧实现同样过滤）
        _bd = (_info.get("bound_device") or "").strip()
        # 已绑定的令牌只认 bound_device，不再按创建时的 device 挂一份：
        # 那个名字是令牌创建时的标签（常见是令牌名本身），不是它现在所在的设备。
        # 补行逻辑会按这个键**造出设备行**，若不排除，一台换绑过的设备会
        # 在列表里多出一个「创建时叫某某」的幽灵行（实测：bind-ev-1）。
        for _d in ((_bd,) if _bd else ((_info.get("device") or "").strip(),)):
            if _d:
                alive_by_dev.setdefault(_d, []).append(_info)

    # 补「只绑定、从未使用」的设备行（GLM R10 h1 第 2 条，实测确认）：
    # 列表骨架是 audit_log 的普通请求流水，而 device_bind 被过滤掉了。
    # 于是「绑上之后一次都没正常用过」的令牌在 items 里没有任何行，
    # 整台设备不可见、也就解不了绑（验收标准第 2 条会断）。
    # 这里只为「items 里还不存在的设备名」造一行骨架，字段全部来自令牌记录，
    # 不伪造使用次数（uses=0）、不编 IP/时间（留空，前端显示为无记录）。
    _have_dev = {(it.get("device") or "").strip() for it in items}
    for _dev, _tks in alive_by_dev.items():
        if _dev in _have_dev:
            continue
        items.append({
            "device": _dev,
            "uses": 0,
            "first_seen": "",
            "last_seen": "",
            "last_ip": "",
            "last_token": "",
            "token_prefix": "",
            "fails": 0,
            "tk": "",
            "_bind_only": True,
        })
        _have_dev.add(_dev)

    from ..audit import device_label
    for it in items:
        raw_prefix = (it.get("token_prefix") or it.get("last_token") or "").strip()
        # 两条来源的清理规则不同，不能共用一句 endswith：
        #   - token_prefix 是 audit.py 已清理过的（无省略号、≤12 位）；
        #   - last_token 是存储原值，长度 > 12 时才带 "..."。
        # 若只判 endswith，一个恰好以 "..." 结尾的 12 位前缀会被误砍后三位，
        # by_prefix 查不到 → 令牌挂不上、设备行变成「已吊销」。
        if len(raw_prefix) > 12 and raw_prefix.endswith("..."):
            raw_prefix = raw_prefix[:-3]
        p = raw_prefix
        info = by_prefix.get(p) if p else None
        tks = []
        if info is not None:
            tks = [_tk_view(info)]
        elif p and _master_prefix and p == _master_prefix:
            tks = [_tk_view(_master_rec, is_master=True)]
        # 令牌已吊销/过期：仍列出来（审计流水是真的），但标清楚挂不上令牌。
        # revoked=True 的判据是「审计里这个前缀确实出现过，但令牌表里已经没有它」——
        # 令牌表只删不软删，所以「查不到」= 被吊销（或被清理掉的过期令牌）。
        # 前端据此显示「已吊销」，不能把它当成「未绑定设备」。
        #
        # 匿名桶（token_prefix 为空或字面 'anon'）**不是令牌**，没有「吊销」可言，
        # 一律 revoked=False。否则 curl/健康检查这类无令牌流量会在设备页里
        # 显示成一堆「已吊销」的假条目。
        # 补挂「同设备名下、但不在本行最新前缀上」的活令牌。
        # 必须排除已挂上的那条，否则同一把令牌会在这行重复出现两次。
        _dev = (it.get("device") or "").strip()
        if _dev:
            _seen = {(t.get("id") or "", t.get("prefix") or "") for t in tks}
            for _info in alive_by_dev.get(_dev, []):
                _key2 = (_info.get("id") or "", _info.get("prefix") or "")
                if _key2 in _seen:
                    continue
                tks.append(_tk_view(_info))
                _seen.add(_key2)
        is_anon = (not p) or p == "anon"
        revoked = (not is_anon) and info is None and not (
            p and _master_prefix and p == _master_prefix)
        it["tokens"] = tks
        it["token_prefix"] = p
        it["token_label"] = tks[0]["label"] if tks else ""
        # revoked 的语义 = 「该行最后一次使用的那把令牌，令牌表里已经查不到了」。
        # 它与补挂可以同时为真（最后那把被吊销、但同设备还有别的活令牌），
        # 前端据此只在 `tks` 为空时才用它（见 app.js 的 else 分支），
        # 所以不会出现「已吊销」和活令牌同时展示的矛盾。
        it["revoked"] = revoked
        # 绑定状态取「这一行里任一活令牌绑了设备」：只看 tks[0] 的话，
        # 补挂进来的已绑定令牌会被漏判成未绑定，页面仍看不到/解不了。
        _bnd = next((t for t in tks if t.get("bound")), None)
        it["bound"] = bool(_bnd)
        it["bound_device"] = _bnd["bound_device"] if _bnd else ""
        # `label` 是**设备行标题**，必须恒等于 device_label(设备名)——旧实现就是
        # 这样，前端也拿它当设备名渲染（app.js：esc(it.label||it.device)）。
        # 令牌的 label 另有 token_label 字段，不能拿来盖设备标题，否则
        # 设备页会把「这台设备」显示成「某把令牌的名字」。
        it["label"] = device_label(it.get("device", ""))

    # 验收标准第 1 条是**每台设备一行**，但 audit 层是按令牌分组的：
    # 同一台设备上用过多把令牌（换令牌、重登录的常态）会出好几行，而补挂后
    # 每行的 `tokens` 又是同一份完整集合 —— 设备卡会重复展示，完全一样。
    # 这里按设备名归并：uses 求和、first_seen 取最早、last_seen 取最近、
    # tokens 按 (id, prefix) 取并集、绑定状态任一为真、revoked 全为真才算。
    merged, order = {}, []
    for it in items:
        _d = (it.get("device") or "").strip()
        if not _d:
            order.append(it)          # 无设备名的行不参与归并
            continue
        m = merged.get(_d)
        if m is None:
            merged[_d] = it
            order.append(it)
            continue
        try:
            m["uses"] = int(m.get("uses") or 0) + int(it.get("uses") or 0)
        except (TypeError, ValueError):
            pass
        if (it.get("first_seen") or "") and (
                not m.get("first_seen") or it["first_seen"] < m["first_seen"]):
            m["first_seen"] = it["first_seen"]
        if (it.get("last_seen") or "") > (m.get("last_seen") or ""):
            m["last_seen"] = it.get("last_seen")
            m["last_ip"] = it.get("last_ip")
            # last_token 必须跟 last_ip 一起换：只换 IP 不换令牌前缀，
            # 归并后这一行会出现「IP 是最新那次的、令牌前缀是旧那次的」错配。
            m["last_token"] = it.get("last_token")
            m["token_prefix"] = it.get("token_prefix")
        _seen2 = {(t.get("id") or "", t.get("prefix") or "") for t in (m.get("tokens") or [])}
        for t in (it.get("tokens") or []):
            if (t.get("id") or "", t.get("prefix") or "") not in _seen2:
                m["tokens"].append(t)
                _seen2.add((t.get("id") or "", t.get("prefix") or ""))
        m["bound"] = bool(m.get("bound")) or bool(it.get("bound"))
        if not m.get("bound_device"):
            m["bound_device"] = it.get("bound_device") or ""
        m["revoked"] = bool(m.get("revoked")) and bool(it.get("revoked"))
        # 标题恒为设备名，归并不会改变它；token_label 用并集后的第一把。
        m["token_label"] = (m.get("tokens") or [{}])[0].get("label", "") if m.get("tokens") else ""
    items = order
    return jsonify({"count": len(items), "devices": items})


@bp.route("/auth/revoke/<hint>", methods=["DELETE"])
@require_master
def revoke(hint):
    """吊销令牌：按「哈希前缀」匹配（D1 修复）。

    改造前 hint 是明文前 12 位 + "..."，拿去 startswith 比 43 位明文键永远
    不相等，前端「吊销」按钮实际一直是坏的。现在 prefix 就是哈希前 12 位，
    前端传什么（带不带尾部省略号）都能命中。
    """
    # 前端历史上传的是 prefix + "..."，先剥掉尾部省略号再比长度，否则 15 个字符
    # 里带着三个点，既过不了长度闸也匹配不上。
    hint = str(hint or "").strip()
    while hint.endswith("..."):
        hint = hint[:-3]
    hint = hint.strip()
    # hint 是令牌前缀。不做长度约束时，传单个字符就能删掉约 1/64 的令牌，
    # 传 "-"/"_" 这类高频字符会命中多个设备，且事后无法复盘删了什么。
    if len(hint) < MIN_REVOKE_HINT:
        return jsonify({
            "error": f"hint 至少 {MIN_REVOKE_HINT} 个字符，避免误删无关令牌",
            "hint_len": len(hint),
        }), 400
    with store_transaction() as s:
        tokens = s.get("tokens", {})
        rm = []
        for k, info in tokens.items():
            if info.get("id") == hint or k.startswith(hint) or info.get("prefix") == hint:
                rm.append(k)
        if not rm:
            return jsonify({"error": "未找到"}), 404
        if len(rm) > 1 and request.args.get("confirm_all") != "1":
            return jsonify({
                "error": "hint 命中多个令牌，需显式确认",
                "count": len(rm),
                "matches": [
                    {"prefix": tokens[k].get("prefix", ""), "id": tokens[k].get("id", ""),
                     "device": tokens[k].get("device", ""),
                     "permission": tokens[k].get("permission", "")}
                    for k in rm
                ],
            }), 409
        info = tokens.get(rm[0], {})
        devices = [tokens[k].get("device", "") for k in rm]
        for k in rm:
            del tokens[k]
    audit_log("auth_revoke", f"/auth/revoke/{hint}",
              extra=f"count={len(rm)} devices={devices}")
    return jsonify({"revoked": len(rm), "device": info.get("device"), "ip": info.get("ip")})


@bp.route("/auth/devices/<tid>/unbind", methods=["POST"])
@require_master
def unbind_device(tid):
    """解绑（C1 第 9 条）：仅 master 可调，清掉 bound_device 即回到「未绑定」。

    解绑后下一次带任何合法 X-KB-Device 的请求会重新绑定；不带标识的请求
    按第 6 条放行。注意各进程的 bound_device 走 30 秒 TTL 缓存（与吊销同源），
    所以解绑后最长 30 秒内，新设备仍可能吃到 mismatch 403——这是既有缓存的
    已知代价，不是解绑失败。按 id 匹配（id 是 C2 起可外露的稳定句柄），也接受 prefix，
    与吊销接口的 hint 口径保持一致。
    """
    tid = str(tid or "").strip()
    while tid.endswith("..."):
        tid = tid[:-3]
    tid = tid.strip()
    if len(tid) < MIN_REVOKE_HINT:
        return jsonify({"error": f"id 至少 {MIN_REVOKE_HINT} 个字符", "id_len": len(tid)}), 400
    old = ""
    hit = ""
    with store_transaction() as s:
        tokens = s.get("tokens", {})
        key = None
        for k, info in tokens.items():
            # 精确匹配：id / prefix / 完整哈希键，三选一相等。
            # 不用 k.startswith(tid)：prefix 本身已是精确值，前缀匹配只是多余的
            # 风险面（一个短 hint 能命中别的令牌，且命中哪一把取决于字典顺序）。
            if info.get("id") == tid or info.get("prefix") == tid or k == tid:
                key = k
                break
        if key is None:
            return jsonify({"error": "未找到"}), 404
        rec = tokens[key]
        old = rec.get("bound_device", "") or ""
        hit = rec.get("id", "") or rec.get("prefix", "")
        rec.pop("bound_device", None)
        rec.pop("bound_at", None)
    audit_log("auth_unbind", f"/auth/devices/{tid}/unbind",
              # P3-4：与 _audit_bind（64）/ mismatch（32）口径一致，不整段写 256。
              extra=f"id={hit} was_bound={old[:64]}")
    return jsonify({"unbound": True, "id": hit, "was_bound": old})
