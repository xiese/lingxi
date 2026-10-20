"""知识库 API — 审计日志（SQLite 后端，兼容旧 JSONL 导入）"""

import json, re, sys, threading, time as _time, sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from flask import g, request

from . import config as _config
from .config import get_setting


def _audit_db():
    """运行时解析审计库路径（便于测试隔离与改库根目录）。"""
    return _config.KB_ROOT / ".audit.db"


def _log_file():
    return _config.LOG_FILE

_LOG_LOCK = threading.Lock()
_LAST_CLEANUP = [0.0]
_CLEANUP_INTERVAL = 300  # 每 5 分钟清理一次旧日志
_MAX_ROWS = 50000        # 最大行数，超过时自动清理最旧记录
_DB_INITED = False


log = logging.getLogger("kb.audit")

def _get_db():
    """获取审计日志 SQLite 连接（线程安全）"""
    global _DB_INITED
    if not _DB_INITED:
        _init_db()
    conn = sqlite3.connect(str(_audit_db()), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db():
    """初始化 SQLite 表结构 + 从旧 JSONL 导入数据"""
    global _DB_INITED
    with _LOG_LOCK:
        if _DB_INITED:
            return
        conn = sqlite3.connect(str(_audit_db()), timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                token_prefix TEXT,
                device TEXT,
                ip TEXT,
                action TEXT,
                path TEXT,
                method TEXT,
                status INTEGER,
                extra TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_device ON audit_log(device)")
        # 2026-10-18 第 15 项：登录记录与设备清单
        # 单独建表而不是复用 audit_log：登录是「事件」，要能按设备聚合、按成败过滤，
        # 且默认保留 90 天（audit_log 是按 max_log_days 走的请求流水）。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                ip TEXT,
                device TEXT,
                token_name TEXT,
                token_prefix TEXT,
                ok INTEGER NOT NULL DEFAULT 0,
                reason TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_time ON login_log(time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_device ON login_log(device)")
        conn.commit()
        conn.close()

        # 从旧 JSONL 文件导入历史数据（仅首次迁移）
        if _log_file().exists() and _log_file().stat().st_size > 0:
            _migrate_from_jsonl()
        _DB_INITED = True


def _migrate_from_jsonl():
    """将旧 JSONL 审计日志导入 SQLite"""
    try:
        conn = sqlite3.connect(str(_audit_db()), timeout=10)
        rows = []
        for line in _log_file().read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                rows.append((
                    e.get("time", ""),
                    e.get("token_prefix", ""),
                    e.get("device", ""),
                    e.get("ip", ""),
                    e.get("action", ""),
                    e.get("path", ""),
                    e.get("method", ""),
                    int(e.get("status", 200)),
                    e.get("extra", ""),
                ))
            except Exception:
                log.debug("audit.py 异常: _migrate_from_jsonl", exc_info=True)
                continue
        if rows:
            conn.executemany(
                "INSERT INTO audit_log (time, token_prefix, device, ip, action, path, method, status, extra) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
        conn.close()
        # 备份旧文件后重命名（不删除，保留安全回滚）
        backup = _log_file().with_suffix(".jsonl.bak")
        _log_file().rename(backup)
    except Exception:
        log.warning("audit.py 异常", exc_info=True)


def audit_log(action, path="", method="", status=200, extra=""):
    """写入审计日志到 SQLite"""
    try:
        if not _DB_INITED:
            _init_db()
        # 用 truthy 口径而不是「属性存在」口径：g.tk 存在但为空串时
        # （例如某条路径写了 g.tk = ""），getattr 的默认值不生效，会把空前缀
        # 落库，设备页就多出一条查不到令牌的空桶。统一按真假兜底。
        tk = getattr(g, "tk", None) or "anon"
        ti = getattr(g, "ti", None) or {}
        # D2：ip 必须是**本次请求**的来源，不是签发令牌时写进记录的 ip。
        # 取法统一走 auth.client_ip()（受信代理/CF 头的边界都在那里判定），
        # 不能直接用 request.remote_addr，否则隧道场景全是 127.0.0.1。
        # 用 getattr 取而不是 `from .auth import client_ip`：后者在 client_ip 被改名/删除时
        # 也抛 ImportError，会被吞成 remote_addr 回落——那正是要暴露的真 bug。
        # P3-5：用 sys.modules 精确判「模块在不在」，不用 try/except ImportError。
        # 后者在 auth 模块**内部** import 失败（如 from .store import ... 笔误）时
        # 同样抛 ImportError，照样被吞成 remote_addr 回落——正是 D2 要暴露的 bug 类别。
        # 走到这里说明请求已过认证，auth 模块必然已加载。
        # 以包内模块导入时 __package__ 是包名；以顶层名导入时它是 ''（falsy），
        # 此时该模块与 auth 同级，直接找 "auth"。原来的 `or __name__.rsplit(...)`
        # 在顶层导入时会拼出不存在的 "<模块>.auth"，恒走回落——注释说的和写反了。
        _pkg = __package__ or ""
        _auth_mod = sys.modules.get((_pkg + ".auth") if _pkg else "auth")
        client_ip = getattr(_auth_mod, "client_ip", None)
        if client_ip is None:
            # 走到这里说明 auth 模块在、但 client_ip 不见了（被改名/误删）。
            # 回落 remote_addr 会让隧道场景悄悄记成 127.0.0.1，必须留信号。
            log.warning(
                "audit.py: auth.client_ip 不可用（模块=%s），IP 回落 remote_addr",
                "缺失" if _auth_mod is None else "存在但无该属性")
            ip = request.remote_addr
        else:
            ip = client_ip()
        entry = (
            datetime.now().isoformat(),
            (tk[:12] + "...") if len(tk) > 12 else tk,
            # 设备标识优先级：
            #   ① bound_device —— C1 绑定后「这台令牌现在归哪台设备」的真实答案；
            #   ② device       —— 令牌创建时写死的名字（旧口径，很多令牌就是名字）；
            #   ③ UA 原文      —— 没有令牌的匿名流量兜底。
            # 顺序不能反：绑定到新设备后仍取 device，设备页会继续显示创建时的
            # 旧名字，「谁在用这台设备」就看不出来了（#17 的死角就是这样来的）。
            ((ti.get("bound_device") or "").strip()
             or ti.get("device")
             or request.headers.get("User-Agent", "?")[:60]),
            ip,
            action,
            path,
            method,
            int(status),
            extra,
        )
        with _LOG_LOCK:
            conn = _get_db()
            conn.execute(
                "INSERT INTO audit_log (time, token_prefix, device, ip, action, path, method, status, extra) VALUES (?,?,?,?,?,?,?,?,?)",
                entry,
            )
            conn.close()
        # 定期清理
        now = _time.time()
        if now - _LAST_CLEANUP[0] > _CLEANUP_INTERVAL:
            _LAST_CLEANUP[0] = now
            max_days = get_setting("max_log_days")
            cutoff = (datetime.now() - timedelta(days=max_days)).isoformat()
            cleanup_logs(cutoff)
    except Exception:
        log.warning("audit.py 异常", exc_info=True)


def cleanup_logs(before_iso):
    """清理过期日志（按天数 + 按行数上限）"""
    try:
        if not _DB_INITED:
            return
        conn = _get_db()
        # 1. 按天数清理
        conn.execute("DELETE FROM audit_log WHERE time < ?", (before_iso,))
        # 2. 按行数上限清理（保留最新 _MAX_ROWS 条）
        conn.execute("""
            DELETE FROM audit_log WHERE id NOT IN (
                SELECT id FROM audit_log ORDER BY id DESC LIMIT ?
            )
        """, (_MAX_ROWS,))
        # 3. 回收空间（WAL 模式下碎片整理）
        conn.execute("PRAGMA optimize")
        conn.close()
    except Exception:
        log.warning("audit.py 异常", exc_info=True)


# ── 手动清理（第 4 项，2026-10-18）──
#
# 自动清理只按「超过 _MAX_ROWS=50000 才动手」这一条走，生产 3.9 万条永远到不了，
# 所以日志只增不减。下面三个函数给「日志页手动清理」用，全部返回受影响行数，
# 由路由层决定要不要先导出备份。

CATEGORIES = ("read", "write", "auth", "system")

# 分类口径：先按 auth 前缀判认证，再按 HTTP 方法判读写，剩下的归系统。
# 实测生产 action 列没有 read*/write* 这种前缀（是 notes/search/index/...），
# 所以只能靠 method 列分读写 —— 这一列是真实存在的，不靠猜 action 名字。
_CAT_SQL = {
    "auth": "({a} = 'auth' OR {p} LIKE '/auth/%')",
    "read": "({m} = 'GET' AND NOT ({a} = 'auth' OR {p} LIKE '/auth/%'))",
    "write": "({m} IN ('POST','PUT','PATCH','DELETE') AND NOT ({a} = 'auth' OR {p} LIKE '/auth/%'))",
    "system": "(NOT ({a} = 'auth' OR {p} LIKE '/auth/%') AND {m} NOT IN ('GET','POST','PUT','PATCH','DELETE'))",
}


def _cat_clause(cat):
    """返回某分类的 SQL 片段；未知分类返回 None。"""
    if cat not in _CAT_SQL:
        return None
    return _CAT_SQL[cat].format(a="action", p="path", m="method")


def count_categories():
    """各分类条数，供前端标签页显示。"""
    if not _DB_INITED:
        _init_db()
    conn = _get_db()
    out = {}
    for cat in CATEGORIES:
        out[cat] = conn.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE {_cat_clause(cat)}"
        ).fetchone()[0]
    conn.close()
    return out


def count_older_than(days):
    """N 天前的条数，供清理弹窗显示「将删除 X 条」。"""
    if not _DB_INITED:
        _init_db()
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE time < ?", (cutoff,)).fetchone()[0]
    conn.close()
    return n


def export_rows(before_iso=None, limit=None):
    """把待删记录导出成 JSON 可序列化的列表（导出备份用）。

    limit=None 表示不截断：备份必须包含全部待删记录，
    否则「备份了但少几行」和「没备份」没区别（审查点出 200000 上限的隐患）。
    """
    if not _DB_INITED:
        _init_db()
    conn = _get_db()
    sql = "SELECT * FROM audit_log"
    args = ()
    if before_iso:
        sql += " WHERE time < ?"
        args = (before_iso,)
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        args = args + (int(limit),)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_older_than(days):
    """删除 N 天前的记录，返回实际删除条数。"""
    if not _DB_INITED:
        _init_db()
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _get_db()
    cur = conn.execute("DELETE FROM audit_log WHERE time < ?", (cutoff,))
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.execute("PRAGMA optimize")
    conn.close()
    return n


def delete_all():
    """清空整表，返回实际删除条数。"""
    if not _DB_INITED:
        _init_db()
    conn = _get_db()
    n = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.execute("DELETE FROM audit_log")
    conn.execute("PRAGMA optimize")
    conn.close()
    return n


def query_audit(page=1, limit=100, action="", device="", category=""):
    """分页查询审计日志，返回 dict（兼容旧 API 格式）"""
    if not _DB_INITED:
        _init_db()
    conn = _get_db()
    where = []
    params = []
    if action:
        where.append("action = ?")
        params.append(action)
    if device:
        where.append("device LIKE ?")
        params.append(f"%{device}%")
    if category:
        clause = _cat_clause(category)
        if clause is None:
            conn.close()
            return {"error": "未知分类"}
        where.append(clause)
    where_clause = " WHERE " + " AND ".join(where) if where else ""

    # 总数
    total = conn.execute(f"SELECT COUNT(*) FROM audit_log{where_clause}", params).fetchone()[0]
    total_pages = max(1, (total + limit - 1) // limit)
    page = min(page, total_pages)
    offset = (page - 1) * limit

    rows = conn.execute(
        f"SELECT * FROM audit_log{where_clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    conn.close()

    entries = []
    for r in rows:
        entries.append({
            "time": r["time"],
            "token_prefix": r["token_prefix"],
            "device": r["device"],
            "ip": r["ip"],
            "action": r["action"],
            "path": r["path"],
            "method": r["method"],
            "status": r["status"],
            "extra": r["extra"],
        })
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "entries": entries,
    }


def audit_stats():
    """审计日志统计（兼容旧 API 格式）"""
    if not _DB_INITED:
        _init_db()
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE time LIKE ?", (today + "%",)
    ).fetchone()[0]

    # 按操作统计
    actions = {}
    for r in conn.execute("SELECT action, COUNT(*) as cnt FROM audit_log GROUP BY action").fetchall():
        actions[r["action"]] = r["cnt"]

    # 按设备统计
    devices = {}
    for r in conn.execute("SELECT device, COUNT(*) as cnt FROM audit_log GROUP BY device").fetchall():
        d = (r["device"] or "?")[:30]
        devices[d] = devices.get(d, 0) + r["cnt"]

    conn.close()
    return {
        "total": total,
        "today": today_count,
        "actions": actions,
        "devices": devices,
    }


def log_request():
    """Flask after_request 钩子：记录每个请求"""
    def hook(response):
        if request.path.startswith("/web/") or request.path == "/health" or request.path == "/favicon.ico" or request.path == "/auth/notifications":
            return response
        action = request.path.split("/")[1] if len(request.path) > 1 else "root"
        if request.path.startswith("/auth/"):
            action = "auth"
        if request.path.startswith("/api/"):
            action = (
                request.path.split("/")[2]
                if len(request.path.split("/")) > 2
                else "api"
            )
        audit_log(action, request.path, request.method, response.status_code)
        return response

    return hook


# ─── 登录记录 / 设备清单（2026-10-18 第 15 项）─────────────────────────────
# 口径：登录日志默认保留 90 天；同一 IP 连续失败 5 次锁定 15 分钟；
#      吊销设备后该设备的令牌立即失效（吊销本身沿用 routes_auth 的既有实现）。
LOGIN_LOG_DAYS = 90
LOGIN_FAIL_MAX = 5
LOGIN_LOCK_SECONDS = 15 * 60


def login_log(ok, *, ip="", device="", token_name="", token_prefix="", reason=""):
    """写一条登录记录。ok=True 表示成功，False 表示失败。

    失败也要记，否则「连续失败」无从判断，也没法给维护者看是被谁在试。
    """
    try:
        if not _DB_INITED:
            _init_db()
        with _LOG_LOCK:
            conn = _get_db()
            conn.execute(
                "INSERT INTO login_log (time, ip, device, token_name, token_prefix, ok, reason) VALUES (?,?,?,?,?,?,?)",
                (
                    datetime.now().isoformat(),
                    str(ip or "")[:64],
                    str(device or "")[:80],
                    str(token_name or "")[:64],
                    str(token_prefix or "")[:24],
                    1 if ok else 0,
                    str(reason or "")[:120],
                ),
            )
            conn.close()
    except Exception:
        log.warning("audit.py 异常: login_log", exc_info=True)


def login_failures_recent(ip, seconds=LOGIN_LOCK_SECONDS):
    """统计该 IP 在最近 seconds 秒内的失败次数（只统计失败，成功会清零口径）。"""
    try:
        if not _DB_INITED:
            _init_db()
        cutoff = (datetime.now() - timedelta(seconds=seconds)).isoformat()
        conn = _get_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM login_log WHERE ok=0 AND ip=? AND time>=?",
            (str(ip or ""), cutoff),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        log.warning("audit.py 异常: login_failures_recent", exc_info=True)
        return 0


def login_failures_since_success(ip):
    """该 IP 自最近一次成功登录之后，又连续失败了几次。

    口径是「连续失败」，不是「窗口内累计」：成功一次就把前面的失败清零。
    只看时间窗口会把一整天零散输错累加成锁定，NAT/校园网共用出口还会连坐。
    """
    try:
        if not _DB_INITED:
            _init_db()
        conn = _get_db()
        try:
            row = conn.execute(
                "SELECT MAX(time) FROM login_log WHERE ip=? AND ok=1", (ip,)
            ).fetchone()
            since = (row[0] if row else None) or ""
            n = conn.execute(
                "SELECT COUNT(*) FROM login_log WHERE ip=? AND ok=0 AND time>?",
                (ip, since),
            ).fetchone()[0]
        finally:
            conn.close()
        return int(n)
    except Exception:
        log.warning("audit.py 异常: login_failures_since_success", exc_info=True)
        return 0


def login_locked(ip):
    """同一 IP 连续失败 5 次就锁 15 分钟；主令牌不受影响（调用方按需区分）。

    「连续」口径用 login_failures_since_success（成功即清零）；
    同时要求这几次失败落在锁定窗口内，避免很久以前的失败也把号锁住。
    """
    if login_failures_since_success(ip) < LOGIN_FAIL_MAX:
        return False
    return login_failures_recent(ip) >= LOGIN_FAIL_MAX


def query_logins(page=1, limit=100, ok=None, device="", ip=""):
    """分页查登录记录，可按成功/失败、设备、IP 过滤。"""
    try:
        if not _DB_INITED:
            _init_db()
        page = max(1, int(page or 1))
        limit = max(1, min(int(limit or 100), 500))
        where, args = [], []
        if ok is not None:
            where.append("ok=?")
            args.append(1 if ok else 0)
        if device:
            where.append("device LIKE ?")
            args.append(f"%{device}%")
        if ip:
            where.append("ip=?")
            args.append(ip)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        conn = _get_db()
        total = conn.execute(f"SELECT COUNT(*) FROM login_log{clause}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT id,time,ip,device,token_name,token_prefix,ok,reason FROM login_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [limit, (page - 1) * limit],
        ).fetchall()
        conn.close()
        return {
            "total": int(total),
            "page": page,
            "limit": limit,
            "items": [dict(r) for r in rows],
        }
    except Exception:
        log.warning("audit.py 异常: query_logins", exc_info=True)
        return {"total": 0, "page": 1, "limit": 100, "items": []}


def query_devices(limit=200):
    """设备清单：一把令牌一个条目（C3 口径）。

    2026-10-19 修正（根因）：原来只查 login_log，但 login_log 只在「设备首次领取令牌」
    时写一条。手机/浏览器用存好的令牌长期访问时永远不会再走领令牌流程，于是表一直是 0，
    设备列表永远空白——数据其实一直在 audit_log 里（每次请求都写）。

    现在改为以 audit_log 为主，login_log 仅用于补充失败次数：失败次数是
    「登录失败」，audit_log 里没有这个概念。

    **C3 分组口径：按 Token 分组，不按 UA 分组。**
      · 同一把令牌换 UA（系统升级/WebView 更新）→ 一条，UA 只是展示字段；
      · 同一台手机换令牌（重新登录/轮换）→ 两条，因为令牌不同、吊销按钮不同。
    UA 族合并只在**匿名桶**（没有令牌的请求）里保留：匿名请求没有令牌可分组，
    按 UA 族合并才能不把 curl 的各种版本拆成一堆。
    """
    try:
        if not _DB_INITED:
            _init_db()
        conn = _get_db()
        lim = max(1, min(int(limit or 200), 500))
        # 注意：LIMIT 不能放在 SQL 里。SQL 的每一行是 (device, token_prefix) 分组，
        # 一把令牌可能占好几行；若先截断再合并，curl 的各种版本分组能把名额占满，
        # 真实设备整条丢失。所以 SQL 不设 LIMIT，合并、排序完再截。
        # last_ip 必须真的是「最近一次的 IP」：MAX(ip) 只是字典序最大。
        # 按 (device, token_prefix) 分组：token_prefix 为空/anon 的统一归到一个桶，
        # 避免把匿名请求拆成一堆；device 保留是为了匿名桶还能按 UA 族合并。
        #
        # 为什么用相关子查询而不是窗口函数：生产实测（4.1 万行 audit_log）
        # 子查询 105ms、窗口函数 273ms —— SQLite 对窗口函数的 PARTITION BY
        # 要做一次全表排序 + 临时表，反而比按 idx_audit_device 索引点查慢。
        # 两者都只在设备页打开时跑一次，105ms 可接受。
        rows = conn.execute(
            """
            SELECT device,
                   COALESCE(NULLIF(NULLIF(token_prefix,''),'anon'),'') AS tk,
                   MIN(time) AS first_seen,
                   MAX(time) AS last_seen,
                   COUNT(*) AS uses,
                   -- a2/a3 必须与外层同款过滤：否则「最近一行是抢绑失败」时，
                   -- last_ip 取到 mismatch 行的 IP，而 last_seen 取到更早的正常请求，
                   -- 同一行里两个字段互相打架（实测复现过）。
                   (SELECT a2.ip FROM audit_log a2
                     WHERE a2.device = audit_log.device
                       AND COALESCE(NULLIF(NULLIF(a2.token_prefix,''),'anon'),'') = COALESCE(NULLIF(NULLIF(audit_log.token_prefix,''),'anon'),'')
                       AND COALESCE(a2.action,'') NOT IN ('device_bind', 'device_mismatch')
                     ORDER BY a2.time DESC, a2.id DESC LIMIT 1) AS last_ip,
                   (SELECT a3.token_prefix FROM audit_log a3
                     WHERE a3.device = audit_log.device
                       AND COALESCE(NULLIF(NULLIF(a3.token_prefix,''),'anon'),'') = COALESCE(NULLIF(NULLIF(audit_log.token_prefix,''),'anon'),'')
                       AND COALESCE(a3.action,'') NOT IN ('device_bind', 'device_mismatch')
                     ORDER BY a3.time DESC, a3.id DESC LIMIT 1) AS last_token
            FROM audit_log
            WHERE device IS NOT NULL AND device != '' AND device != '?'
              -- C1 新增的绑定事件不是「这台设备用了系统」：
              --   device_bind     = 首次绑定的记账，算进 uses 会虚增一次使用；
              --   device_mismatch = **抢绑失败方**的设备，不过滤会在列表里凭空
              --                     长出一台从未真正登录过的设备。
              -- 只排这两类，其他既有 action 的口径不动。
              AND COALESCE(action,'') NOT IN ('device_bind', 'device_mismatch')
            GROUP BY device, COALESCE(NULLIF(NULLIF(token_prefix,''),'anon'),'')
            ORDER BY last_seen DESC
            """,
        ).fetchall()
        # 试过把 last_ip / last_token 合并成一次子查询（`ip || char(31) || token_prefix`），
        # 结果慢到 317ms —— `||` 拼接让 SQLite 没法走索引，每个分组还是要全扫。
        # 分开两个子查询反而快（105~129ms）。别"优化"回去。
        items = [dict(r) for r in rows]
        # 失败次数从 login_log 补（该表只记登录事件，可能是空表）
        fails = {}
        try:
            for fr in conn.execute(
                "SELECT device, COUNT(*) FROM login_log WHERE ok=0 AND device IS NOT NULL "
                "AND device != '' GROUP BY device"
            ):
                fails[fr[0]] = fr[1]
        except Exception:
            pass
        for it in items:
            it["fails"] = fails.get(it.get("device", ""), 0)
            it["label"] = device_label(it.get("device", ""))
        conn.close()

        # 合并键（C3）：
        #   有令牌 → 只按令牌前缀，一把令牌一条。UA 不再参与分组，否则同一把令牌
        #            换个系统版本就被拆成两条，「吊销」按钮也不知道该吊销哪把。
        #   匿名   → 没有令牌可分组，退回按设备族（保留机型）合并，否则 curl 的
        #            各种版本、同一台手机的 UA 变化会把列表塞满。
        def _merge_key(it):
            # 匿名桶要按 UA 族合并，不能并成一条——否则 curl/python/各种壳
            # 全部挤进同一个 "anon" 行，看不出到底谁在调。
            #
            # 这里必须显式排除字面 "anon"，不能只靠 `if tk:`：tk 的取值来自
            # SELECT 的归一表达式，若将来有人把归一拿掉（或旧数据里存的是
            # 'anon' 而非 ''），'anon' 是 truthy，就会走进 ("tk","anon") 分支，
            # 把匿名桶反转成单条。把判据写在 Python 层，不依赖 SQL 层归一穿透。
            tk = it.get("tk") or ""
            if tk and tk != "anon":
                return ("tk", tk)
            return ("ua", device_family(it.get("device") or ""))

        merged = {}
        # 每个设备名只贡献一次 fails（login_log 是按 device 整名统计的）
        _fails_by_dev = {}
        for it in items:  # items 已按 last_seen DESC，先到的就是最近的
            key = _merge_key(it)
            dev = it.get("device") or ""
            if key not in merged:
                merged[key] = dict(it)
                _fails_by_dev[key] = {}
            else:
                m = merged[key]
                m["uses"] = (m.get("uses") or 0) + (it.get("uses") or 0)
                if (it.get("first_seen") or "") < (m.get("first_seen") or ""):
                    m["first_seen"] = it.get("first_seen")
                # SQL 已按 last_seen DESC 排序，同组先到的就是最近一行，
                # 所以这里的 > 实际不会成立，是**防御性**分支：只在将来
                # SQL 去掉 ORDER BY（或改排序）时兜住「更近的行覆盖更旧行」。
                # 用 > 不用 >=：相等（同一秒）时若用 >= 会让更旧的行反超，
                # 把 last_ip 改成旧值。别因为"跑不到"就删掉。
                if (it.get("last_seen") or "") > (m.get("last_seen") or ""):
                    m["last_seen"] = it.get("last_seen")
                    m["last_ip"] = it.get("last_ip")
                    m["last_token"] = it.get("last_token")
                    m["device"] = dev
                    # device 换了，label 必须跟着换：否则设备名和展示名对不上
                    # （例如「安卓手机 12」配「安卓手机 13」的 UA 原文）。
                    m["label"] = it.get("label") or device_label(dev)
            # fails 是 login_log 按 device 整名统计的，不是按 (device, token_prefix)。
            # 同一个 device 名出现在多个分组时，每份都带着同一个总数，
            # 所以按设备名记一份，最后再求和，既不翻倍也不漏。
            _fails_by_dev[key][dev] = it.get("fails") or 0
        for key, per_dev in _fails_by_dev.items():
            merged[key]["fails"] = sum(per_dev.values())
        out = list(merged.values())
        # 排序：真人设备在前，本机自动化请求（curl/脚本）在后。
        # 否则 curl 三万多次会把手机挤到看不见的地方，设备页就失去意义了。
        # 只把「明确的自动化」排后面。不能按 Mac/Linux/Windows 这类桌面系统名判断——
        # 真人用 Mac 或 Linux 桌面浏览器访问，label 就是「Mac」「Linux」，
        # 按系统名归类会把人家的设备排到一堆脚本后面去。
        # 覆盖实测出现过的自动化 UA：wget / okhttp / axios / Apache-HttpClient /
        # PostmanRuntime / Java / Mozilla/5.0 (compatible; ...) 这类爬虫。
        # 漏一个就会让脚本排进真人区，把真设备挤下去。
        _AUTO = ("curl", "wget", "Python 脚本", "Node 脚本", "Go-http-client", "verify",
                 "zmap", "Hello from", "扫描器", "未知浏览器",
                 "Mozilla/5.0 (compatible", "okhttp", "axios", "Apache-HttpClient",
                 "PostmanRuntime", "libwww", "httpclient")
        # 只在原始 UA 里查的关键词。不能放进 _AUTO 用 startswith 比 label：
        # "bot" 会误伤自定义设备名 nanobot-159。
        # 只在原始 UA 里查（label 里查会误伤 JavaDev-MBP / botao 这类自定义设备名）
        _AUTO_RAW = ("headlesschrome", "phantomjs", "puppeteer", "playwright",
                     "selenium", "scrapy", "python-requests", "aiohttp", "httpx",
                     "guzzlehttp", "java/", "apache-httpclient", "postmanruntime",
                     "googlebot", "bingbot", "baiduspider", "yandexbot",
                     "semrushbot", "ahrefsbot", "bytespider", "spider/", "crawler")
        def _is_auto(x):
            # 必须同时看 label 和原始 UA：
            # HeadlessChrome 的 UA 里带 `Linux`，device_label 会把它归成「Linux」，
            # 只看 label 就会被当成真人桌面浏览器。原始 UA 才是判据。
            lb = x.get("label") or ""
            raw = (x.get("device") or "").lower()
            # label 用 startswith（避免 nanobot-159 被 "bot" 误伤），
            # 原始 UA 用整段包含判断（HeadlessChrome 的 label 是「Linux」，
            # 只有查原文才看得出是自动化）。
            if any(lb.startswith(a) for a in _AUTO):
                return True
            return any(a.lower() in raw for a in _AUTO_RAW)
        # 内部字段不往外带（前端用不到，还会把设备名列表暴露给接口调用方）。
        # tk 换成对外的 token_prefix（裸 12 位，不带审计里的省略号）：
        # C3 要按令牌展示 label/prefix，接口得能直接拿它与令牌记录比对。
        for x in out:
            x.pop("devices", None)
            tk = x.pop("tk", "") or ""
            # 存储侧规则是 len(tk) > 12 才加 "..."，≤12 的整串入库。
            #
            # 这里**不能**用 `tk.endswith("...")` 判断是否截断过：
            # 短令牌若本身就以 "..." 结尾，会被误删后三位，输出前缀与令牌记录
            # 对不上。改用长度判定：存储侧保证带省略号的串长度恒 > 12，
            # 所以「长度 > 12 且以 ... 结尾」才是真的截断。
            if len(tk) > 12 and tk.endswith("..."):
                tk = tk[:-3]
            # 无论走哪条分支都再切 [:12]：万一将来令牌生成变短，
            # 也不会把完整凭证当"前缀"外发。不依赖存储侧约定。
            #
            # 字面 "anon" 与 _merge_key 同口径排除：SQL 里已经 NULLIF 过一次，
            # 但输出侧不能依赖 SQL 归一（旧数据、归一失效都会漏出来），
            # 否则匿名桶会对外显示成 token_prefix="anon"。
            _pfx = "" if tk == "anon" else tk[:12]
            x["token_prefix"] = _pfx
            # last_token 与 token_prefix 必须同口径：两个字段都表示「最后使用的
            # 令牌前缀」，一个带省略号一个不带，前端拿去做并集/比对时会当成两把
            # 不同的令牌（GLM R9 h1 第 5 条）。同样按长度判、同样排除 anon，
            # 同样切到 ≤12 位——不把存储原值直接外发。
            _lt = (x.get("last_token") or "") or ""
            if len(_lt) > 12 and _lt.endswith("..."):
                _lt = _lt[:-3]
            x["last_token"] = "" if _lt == "anon" else _lt[:12]
        human = [x for x in out if not _is_auto(x)]
        auto = [x for x in out if _is_auto(x)]
        human.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
        auto.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
        return (human + auto)[:lim]
    except Exception:
        log.warning("audit.py 异常: query_devices", exc_info=True)
        return []


def device_label(device):
    """把 audit_log 里的 device 原文转成人能看懂的名字。

    audit_log 的 device 优先存令牌绑定的设备名（如 hark / dsh），没有令牌时回落
    UA 前 60 字符。直接展示 UA 是一长串，手机上看不出是哪台设备，所以这里做归一：
    Android / iPhone / iPad / Mac / Windows / Linux 各给一个短名，其余原样截断。
    """
    s = (device or "").strip()
    if not s:
        return "未知设备"
    low = s.lower()
    # 技术性 UA 先归一，否则会被下面的「短名直接返回」放过（如 Python-urllib/3.11 只有 19 字符）
    if "python-urllib" in low:
        return "Python 脚本"
    if "python-requests" in low:
        return "Python 脚本"
    if low.startswith("node") or "node-fetch" in low or "undici" in low:
        return "Node 脚本"
    if low.startswith("curl"):
        return "curl"
    if low.startswith("wget"):
        return "wget"
    if low == "mozilla/5.0":
        return "未知浏览器"
    # 已经是短名（令牌绑定的设备名，如 hark / dsh / 手动 Token）就直接用
    if len(s) <= 24 and "Mozilla/" not in s and "curl" not in low:
        return s
    if "android" in low:
        # 抓 "Android 12" 这类版本号
        import re as _re
        m = _re.search(r"android[\s/]?([0-9.]+)", low)
        ver = (" " + m.group(1)) if m else ""
        # 抓机型代号，如 22041216C。复用 _android_model()，
        # 别在这里再写一套正则——两套解析口径迟早会分叉。
        mdl = _android_model(s)
        model = (" · " + mdl) if mdl else ""
        return "安卓手机" + ver + model
    if "iphone" in low:
        return "iPhone"
    if "ipad" in low:
        return "iPad"
    if "macintosh" in low or "mac os x" in low:
        return "Mac"
    if "windows" in low:
        return "Windows"
    if "linux" in low:
        return "Linux"
    if "python-urllib" in low:
        return "Python 脚本"
    if "curl" in low:
        return "curl"
    return s[:24]


def _android_model(ua):
    """从安卓 UA 里取机型，取不到返回「手机」。

    UA 的安卓段格式有好几种，直接拿第一个分号后的字段会踩坑：
      `Android 12; 22041216C Build/SP1A`      -> 22041216C
      `Android 12; zh-cn; 22041216C Build/..` -> 不能取 zh-cn（语言区段）
      `Android 12; Pixel 6 Build/SQ3A`        -> Pixel 6（机型带空格，不能只取首词）
      `Android 12; Redmi K50 Build/SKQ1`      -> Redmi K50
    所以：先切出 `Android <版本>;` 之后的部分，去掉 Build/ 尾巴，
    再跳过纯语言区段（zh-cn / en-us 这种），剩下的整段就是机型。
    """
    m = re.search(r"android[\s/]*[\d.]*\s*;\s*(.+)", ua, re.I)
    if not m:
        return "手机"
    rest = m.group(1)
    # UA 的安卓段以 `)` 结束，右括号之后是 AppleWebKit 等尾巴，先切掉
    rest = rest.split(")")[0]
    # 去掉 Build/xxx 及其后所有内容
    rest = re.split(r"\s*Build/", rest, maxsplit=1, flags=re.I)[0]
    parts = [x.strip() for x in rest.split(";") if x.strip()]
    # 这些不是机型：新版 Chrome 的安卓 UA 已缩减成 `Android 10; K`，
    # Firefox 给的是 `Android 12; Mobile` / `Android 12; Tablet`。
    # 拿它们当机型，所有安卓机会被并成「安卓 K」「安卓 Mobile」一条。
    _NOT_MODEL = {"k", "mobile", "tablet", "phone", "build", "wv", ""}
    for seg in parts:
        # 语言区段形如 zh-cn / en-US，跳过
        if re.fullmatch(r"[a-zA-Z]{2}[-_][a-zA-Z]{2}", seg):
            continue
        if seg.lower() in _NOT_MODEL:
            continue
        # rv:109.0 / Version/17.0 这类是渲染引擎版本，不是机型
        if re.match(r"^(rv|version|wv)[:\s]", seg, re.I):
            continue
        # 首个有效段就是机型（可能带空格，整段保留）
        return seg
    return ""


def device_family(device):
    """设备族：用于判断「是不是同一台设备」，不含系统版本号，但**保留机型**。

    为什么要单独一个函数：`device_label()` 的展示名带系统版本（安卓手机 12 / 13），
    手机系统升级后展示名就变了。拿展示名当合并键，同一台手机会被拆成两条，
    设备页上看着像两台手机。合并要用「不含版本号」的族名。

    为什么必须保留机型：只返回「安卓手机」的话，家里两台不同的安卓机、
    公司的测试机全都会被并成一台，设备页就分不出哪台是哪台。
    UA 里的机型串（如 `22041216C`）在同一台手机上不变，系统升级也不会变。
    """
    s = (device or "").strip()
    if not s:
        return "未知设备"
    low = s.lower()
    if "python-urllib" in low or "python-requests" in low:
        return "Python 脚本"
    if low.startswith("node") or "node-fetch" in low or "undici" in low:
        return "Node 脚本"
    if low.startswith("curl") or "curl/" in low:
        return "curl"
    if low.startswith("wget"):
        return "wget"
    if low == "mozilla/5.0":
        return "未知浏览器"
    if len(s) <= 24 and "mozilla/" not in low:
        return s
    # UA 型设备：带上机型/平台标识，否则同系统多台设备会并成一台
    if "android" in low:
        model = _android_model(s)
        if model:
            return "安卓 " + model
        # 机型不可信（新版 Chrome 的 K、Firefox 的 Mobile/Tablet）：
        # 退回用整段 UA 当身份，宁可同一台设备因 Chrome 升级拆两条，
        # 也不能把所有安卓机并成一条 —— 那等于设备页上只有一台安卓。
        return "安卓 UA " + s[:40]
    if "iphone" in low:
        return "iPhone"
    if "ipad" in low:
        return "iPad"
    if "macintosh" in low or "mac os x" in low:
        return "Mac"
    if "windows" in low:
        return "Windows"
    if "linux" in low:
        return "Linux"
    return s[:24]


def cleanup_login_log(before_iso):
    """清理超过保留期的登录记录（默认 90 天，独立于 audit_log 的 max_log_days）。"""
    try:
        if not _DB_INITED:
            _init_db()
        conn = _get_db()
        cur = conn.execute("DELETE FROM login_log WHERE time < ?", (before_iso,))
        deleted = cur.rowcount
        conn.close()
        return deleted
    except Exception:
        log.warning("audit.py 异常: cleanup_login_log", exc_info=True)
        return 0


# 上次清理的日期（YYYY-MM-DD）。每天最多清一次，靠进程内变量记，
# 不装定时任务（维护者没授权装 cron），进程重启后第一天会再清一次，无副作用。
_LOGIN_CLEANED_ON = ""


def maybe_cleanup_login_log():
    """每天最多一次，把超过 LOGIN_LOG_DAYS 的登录记录删掉。

    为什么要这个函数：第 15 项要求「日志默认保留 90 天」，
    光有 cleanup_login_log() 没有被调用 —— 那是死代码，等于没做保留策略。
    """
    global _LOGIN_CLEANED_ON
    today = datetime.now().strftime("%Y-%m-%d")
    if _LOGIN_CLEANED_ON == today:
        return 0
    before = (datetime.now() - timedelta(days=LOGIN_LOG_DAYS)).isoformat()
    n = cleanup_login_log(before)
    _LOGIN_CLEANED_ON = today
    return n

_NEW_DEV_LOCK = threading.Lock()


def is_new_device(device):
    """这台设备是否第一次出现在登录记录里。

    必须在写本次成功记录**之前**调用（调用方已按这个顺序写）：
    此时查到的条数是「这台设备之前成功登录过几次」，0 就是第一次。
    如果先插后查，本条会被自己数进去，并发下两台新设备会双双漏推。
    """
    if not device:
        return False
    # 进程内加锁：两个协程同时判「新设备」会双双拿到 True 并各推一次。
    # 这里把「查」串行化，调用方紧接着的 login_log 写也在同一线程内完成，
    # 第一个进去的判 True 并写入，第二个进去时已经能查到 1 条，判 False。
    with _NEW_DEV_LOCK:
        try:
            conn = _get_db()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM login_log WHERE device=? AND ok=1", (device,)
                ).fetchone()[0]
            finally:
                conn.close()
            return n == 0
        except Exception:
            return False


def login_success_once(ip, device, token_name="", token_prefix="", reason=""):
    """判「是不是新设备」与写这条成功记录，放在同一把锁里原子完成。

    为什么要合成一个函数：只给 is_new_device 加锁是没用的——
    「查完释放锁」到「调用方写记录」之间存在空档，两个协程照样双双判新。
    返回 True 表示这次是这台设备的第一次成功登录（调用方据此推「新设备登录」）。
    """
    with _NEW_DEV_LOCK:
        try:
            conn = _get_db()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM login_log WHERE device=? AND ok=1", (device,)
                ).fetchone()[0]
            finally:
                conn.close()
        except Exception:
            n = 0
        login_log(True, ip=ip, device=device, token_name=token_name,
                  token_prefix=token_prefix, reason=reason)
        # 设备名为空时无法判「是不是新设备」，不能当成新设备去推（否则每次轮询都推一条空白通知）。
        return bool(device) and n == 0
