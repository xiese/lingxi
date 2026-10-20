"""知识库 API — 认证"""

import re, time
from pathlib import Path
import ipaddress
import logging
from datetime import datetime
from functools import wraps
from flask import request, jsonify, g

from .config import KB_ROOT, NOTES_DIR, TRUSTED_PROXIES
from .store import (load_store, _load_store_cached, _load_store_fresh, is_master, is_expired,
                    token_hash, store_transaction)



log = logging.getLogger("kb.auth")

# 安全加固：C0 控制字符 + DEL + Unicode 行分隔符。
# \r \n \t 会把 frontmatter 的一行撑成多行，破坏结构；
# \u2028 / \u2029 是 Unicode 行分隔符，对某些解析器同样致命。
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")

def get_token():
    """从请求中提取 Bearer Token（支持 Header 和 URL 参数）"""
    a = request.headers.get("Authorization", "")
    return a[7:] if a.startswith("Bearer ") else request.args.get("token", "")


# ── C1：设备绑定（台账 #22）──
#
# 拦截层只放在 authenticate() 一处：57 个 check_auth() 与 27 个 require_master()
# 全部经它，散到各路由必然漏。语义见下方判定表，一格都不许自创。
DEVICE_HEADER = "X-KB-Device"


class DeviceMismatch(Exception):
    """令牌已绑定别的设备，或已绑定却不带设备标识。由 check_auth 转 403。"""

    def __init__(self, info, dev, bound, reason):
        super().__init__(reason)
        self.info = info
        self.dev = dev
        self.bound = bound
        self.reason = reason  # "mismatch" | "missing"


def _audit_safe(v, limit=64):
    """把设备标识转成审计 extra 里的安全片段。

    extra 是 `k=v k=v` 的扁平串，设备标识若含空格 / `=` / 控制字符，
    就能伪造出假的 `bound=`、`reason=` 字段，污染排障。这里把空白与
    `=` 一律替换成 `_`，控制字符（<0x20、0x7f）丢弃。
    """
    out = []
    for ch in _norm_dev(v)[:limit]:
        if ch == "=" or ch.isspace():
            out.append("_")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            continue
        else:
            out.append(ch)
    return "".join(out)


def _norm_dev(v):
    """设备标识归一化：strip + 截断 256，**存 / 比 / 审计三处只用这一个函数**。

    分开写会出现口径漂移（写入 `strip()[:256]`、读侧只 strip），
    手工塞库或未来写入方漂移时正常设备会永久 mismatch 自锁。
    """
    return (v or "").strip()[:256]


def _device_id():
    """本次请求的 X-KB-Device：去空格后非空即可，原样返回。

    C1 返工（第 4a 条）：只做**最小**校验 —— 不设长度下限、不限定字符集。
    任务书的判定表里没有「格式非法按没带处理」这一格，加了就等于改写判定表：
    已绑定 + 带一个短 ID 或带特殊字符的标识，会被当成「没带」回 missing 文案，
    而正确行为是第 4 条的「此令牌已绑定其他设备」。
    短 ID、特殊字符一律按标识原样存（见 _bind_device）。
    """
    # P2-2：长度上限在出口统一施加，让「存 / 比 / 审计」三处同值。
    # 只在写入处截断会导致超长 ID 首次绑定成功、第二次起永久 mismatch 403 自锁。
    return _norm_dev(request.headers.get(DEVICE_HEADER))


def _bind_device(key, dev):
    """首次绑定：持 flock 重新读盘判定，跨进程只有一台设备能绑上。

    不能直接改 authenticate() 手里那份 30 秒缓存：缓存不是写点，下次
    load_store() 会覆盖，别的进程（kb-mcp-sse）也看不见。
    返回 ("bound"|"same"|"mismatch"|"gone", 实际占位设备)。
    "bound" 只表示本次真的写入了绑定；"same" 表示库里早已绑定同一设备。
    实际占位设备在事务内就已经拿到，必须从这里带回：事后再去读会走 30 秒缓存，
    跨进程抢绑（kb-mcp-sse 绑走的）场景下缓存还是旧快照，审计里依旧是空。
    """
    with store_transaction() as s:
        rec = s.get("tokens", {}).get(key)
        if not rec:
            return "gone", ""
        # TOCTOU（DS C13o P2）：外层用 fresh 快照判过未过期，但进事务前可能刚好过期。
        # 事务内不复查的话，刚过期的令牌仍能完成首次绑定，持久占住设备槽位，
        # 留下一条永远没人认领的孤儿绑定记录。
        if is_expired(rec):
            return "gone", ""
        cur = _norm_dev(rec.get("bound_device"))
        if not cur:
            # 长度上限不是「格式判定格」，不与 4a 的判定表冲突；只防异常客户端撑大 store。
            rec["bound_device"] = _norm_dev(dev)
            rec["bound_at"] = datetime.now().isoformat()
            return "bound", dev
        # P2-1：cur == dev 说明「事务内发现已绑定同设备」，不是本次写入。
        # 不能复用 "bound"，否则 30 秒缓存窗口内每个请求都写一条 device_bind 审计
        # + 一次 flock 全量事务，审计语义从「首次绑定」劣化成「每请求一条」。
        return ("same" if cur == dev else "mismatch"), cur


def _audit_bind(info, dev):
    """首次绑定写 device_bind 事件（C1 返工第 5 条）。

    只写令牌标识（哈希前 12 位，由 g.tk 派生）与设备标识，不写明文。
    审计失败不能影响本次认证：绑定已经落盘，写日志只是可观测性。
    """
    try:
        from .audit import audit_log
        # g.tk 由调用方（authenticate 首次绑定分支）已设为哈希键；这里只在它
        # 缺失或为空时兜底成 anon，不重复计算 token_hash（两处各算一次容易漂移）。
        if not getattr(g, "tk", None):
            g.tk = "anon"
        g.ti = info
        audit_log("device_bind", request.path, request.method, 200,
                  extra=f"dev={_audit_safe(dev)}")
    except Exception:
        log.warning("auth.py 异常: 设备绑定审计", exc_info=True)


def authenticate():
    """认证主路径（C2：哈希查找 + C1：设备绑定）。

    存储键是 sha256(pepper + 明文)，不是明文。这里拿到请求里的明文后
    先算哈希再查表，O(1) 与改造前完全一致。

    C1 判定表（逐格实现，顺序不能换）：
      主令牌                → 放行，永不绑定（上面 is_master 已返回，天然豁免）
      未绑定 + 不带标识     → 放行，保持未绑定（存量无感，已拍板）
      未绑定 + 带任意非空标识 → 首次绑定并放行（含短 ID、含特殊字符，原样存）
      已绑定 + 标识相同     → 放行
      已绑定 + 标识不同     → DeviceMismatch("mismatch") → 403
      已绑定 + 不带标识     → DeviceMismatch("missing")  → 403
    """
    tk = get_token()
    if not tk:
        return None, None
    if is_master(tk):
        return tk, {"permission": "master", "name": "master"}
    key = token_hash(tk)
    # 设备绑定判定必须看到**最新**快照：另一个进程解绑/改绑后，30 秒 TTL 缓存
    # 会让新设备在这段时间里一直 403（失败路径不刷新缓存，用户重试也解不开）。
    #
    # 为什么不用「只在要判 403 时再 fresh 复核」：那个折中只能解开误拒，
    # 反方向解不掉——缓存读到「未绑定」时若直接放行，别的进程刚把设备绑上，
    # 本次请求就会绕过绑定校验直接 200。两个方向都要最新，所以每个请求都读。
    # 实测代价：6.2 微秒/次（含 1 次 stat + 1 秒内缓存命中），对比 cached 的
    # 1.0 微秒，多出的 5 微秒远小于一次 HTTP 请求开销；`_load_store_fresh`
    # 自带 max_age 短路，没有退化成"每请求都读整个 JSON"。
    info = _load_store_fresh().get("tokens", {}).get(key)
    if not info or is_expired(info):
        return None, None
    # 双保险：master 记录若被手工塞进 tokens 表，同样不参与绑定。
    if info.get("permission") == "master":
        return tk, info
    dev = _device_id()
    # 读侧也要截断：写入侧存的是 dev[:256]，比较侧若用未截断原值，
    # 手工塞库或未来写入方漂移时，正常设备会永久 mismatch 自锁。
    # 存 / 比 / 审计三处同口径（GLM C13i P2-5）。
    bound = _norm_dev(info.get("bound_device"))
    if bound:
        if not dev:
            raise DeviceMismatch(info, dev, bound, "missing")
        if dev != bound:
            raise DeviceMismatch(info, dev, bound, "mismatch")
        return tk, info
    if not dev:
        # 第 6 条：未绑定 + 不带标识 → 放行，且不许顺手写任何字段。
        return tk, info
    verdict, occupied = _bind_device(key, dev)
    if verdict == "gone":
        return None, None
    if verdict == "same":
        # 函数契约是 (tk, info)；写成 (info, dev) 会让 g.tk 变 dict、g.ti 变 str，
        # audit_log 里 tk[:12] / ti.get() 直接炸，普通请求审计全丢。
        return tk, info
    if verdict == "mismatch":
        # 抢绑失败：occupied 是事务内读到的真实占位设备（不是 30 秒缓存），
        # 跨进程抢绑时审计里也不会是空。
        raise DeviceMismatch(info, dev, occupied, "mismatch")
    # 首次绑定写 device_bind 审计（g.tk 用哈希键，audit 切前 12 位即 prefix）
    g.tk = key
    # 本次请求的 info 还是绑定前的快照（事务里改的是存储里的 rec）。
    # 不同步回来的话，这一次请求的审计行仍按创建时的 device 落库，
    # 设备页会先显示旧名字，下次请求才更正——绑定后的第一次就错了。
    info = dict(info or {})
    info["bound_device"] = _norm_dev(dev)
    g.ti = info
    _audit_bind(info, dev)
    return tk, info


def _device_mismatch_response(exc):
    """403 文案 + 审计事件。审计里只写设备标识与令牌前缀，绝不写明文。"""
    tk = get_token()
    try:
        g.tk = token_hash(tk) if tk else "anon"
        g.ti = exc.info
        from .audit import audit_log
        audit_log(
            "device_mismatch", request.path, request.method, 403,
            # 截断长度与 _audit_bind 统一为 64（GLM C13i P3-3）。
            # reason 是内部常量枚举（missing/mismatch），但仍按字符串兜底，
            # 避免将来有人塞 None 时 [:64] 抛 TypeError 把审计整条吞掉。
            extra=(f"reason={str(exc.reason or '')[:16]} "
                   f"dev={_audit_safe(exc.dev)} "
                   f"bound={_audit_safe(exc.bound)}"),
        )
    except Exception:
        log.warning("auth.py 异常: 设备绑定审计", exc_info=True)
    if exc.reason == "missing":
        return jsonify({"error": "此令牌已绑定设备，请求缺少设备标识",
                        "code": "device_mismatch"})
    return jsonify({"error": "此令牌已绑定其他设备", "code": "device_mismatch"})


def check_auth(perm="r"):
    try:
        tk, info = authenticate()
    except DeviceMismatch as exc:
        return None, _device_mismatch_response(exc), 403
    if not info:
        return None, jsonify({"error": "未授权或Token已过期"}), 401
    if info.get("permission", "r") not in ("master", "rw") and perm == "w":
        return None, jsonify({"error": "权限不足"}), 403
    # g.tk 是审计日志的 token_prefix 来源（audit.py 取 tk[:12] + "..."）。
    # C2 之后不能把明文放进去——那等于把明文前 12 位写进审计库。
    # 存哈希（64 位十六进制），audit 切前 12 位正好等于记录里的 prefix 字段，
    # 与 /auth/devices 的「前缀反查」口径一致。
    # 例外：master 保持**明文**前缀，不哈希。
    #
    # 原因：设备页（routes_auth.auth_devices）用 `master["token"][:12]` 去匹配
    # audit_log.token_prefix 来认这条审计属于 master。若这里改存哈希，两边永远
    # 对不上，master 会从设备列表里凭空消失，而且历史审计行（明文前缀）也全部
    # 断链。所以 master 这一条必须沿用原有口径。
    #
    # 代价：主令牌前 12 位会进审计库。这是既有行为，不是本次引入；要改必须
    # 同时改设备页的匹配逻辑 + 迁移历史行，属独立改造，登记在 KNOWN-ISSUES。
    g.tk, g.ti = (tk if info.get("permission") == "master" else token_hash(tk)), info
    return tk, None, 200


def require_master(fn):
    @wraps(fn)
    def w(*a, **kw):
        try:
            tk, info = authenticate()
        except DeviceMismatch as exc:
            # 被绑定的非 master 令牌本来也过不了 permission 判断，这里仍按 403
            # 返回绑定文案，避免「换台机器调 master 接口」时文案对不上。
            return _device_mismatch_response(exc), 403
        if not info or info.get("permission") != "master":
            return jsonify({"error": "需主Token"}), 403
        # 与 check_auth 同口径：master 存明文前缀（设备页靠它匹配），其余存哈希。
        g.tk, g.ti = (tk if info.get("permission") == "master" else token_hash(tk)), info
        return fn(*a, **kw)

    return w


def safe(rel):
    """安全解析路径，防止路径穿越。

    参照系是 **KB_ROOT**（文件系统根）。它只保证结果落在知识库根目录之内，
    不保证落在笔记库内：`safe("笔记/../.api_tokens.json")` 会被放行。

    因此凡是语义边界是「笔记库」的调用点，必须用 safe_note()，
    不要用 `safe("笔记/" + x)` 拼前缀——`..` 会把前缀抵消掉。

    同样不要再对入参 unquote()（同 safe_note 的说明里讲的原因）。
    """
    p = (KB_ROOT / str(rel).lstrip("/")).resolve()
    try:
        p.relative_to(KB_ROOT)
    except ValueError:
        raise ValueError(f"路径穿越拒绝: {rel}")
    return p


def safe_note(rel):
    """安全解析**笔记库内**路径，防止逃出笔记目录。

    与 safe() 的区别：参照系是 NOTES_DIR 而不是 KB_ROOT。
    笔记类路由的语义边界是笔记库，用 safe() 拼 "笔记/" 前缀时，
    `../` 可以抵消前缀，逃到 KB_ROOT 下的任意文件（含令牌文件）。

    注意：**不要再对入参 unquote()**。Flask 已经把 URL 路径解码过一次，
    这里再 unquote 会把「文件名里合法含 %2e 的笔记」二次解码成另一个名字，
    表现为请求 A 拿到 B 的内容。调用方传什么就用什么字面值。
    需要读「文件名含百分号编码」的笔记时，客户端应双写编码（%252e）。
    """
    p = (NOTES_DIR / str(rel).lstrip("/")).resolve()
    try:
        p.relative_to(NOTES_DIR.resolve())
    except ValueError:
        raise ValueError(f"路径穿越拒绝: {rel}")
    return p


def sanitize(n):
    """清理文件名中的非法字符，保留路径分隔符 / 不变"""
    return re.sub(r'[<>:"\\|?*]', "_", n).strip()


def sanitize_path_segment(segment):
    """清理单段路径名，不破坏多级路径结构"""
    return sanitize(segment)


def sanitize_title(title):
    """把「笔记标题」压成**单个**文件名段（2026-09-18 修复）。

    sanitize() 故意保留 `/`（它同时用于多级路径），但标题不是路径：
    标题里的 `/` 会被当成目录分隔符，而目标目录并未创建 → 落盘时
    FileNotFoundError → 500。更严重的是 `../` 可以逃出笔记库
    （实测可写到 KB_ROOT 外任意 .md，且服务以 root 运行）。

    因此标题必须单独净化：路径分隔符一律压成 `_`，杜绝多级/穿越。
    """
    s = str(title or "")
    # 安全加固：清掉所有控制字符。\r \n \t 会原样进入 frontmatter 的 title 行，
    # 把「一行 key: value」撑成多行，破坏结构；文件名里也会带上换行。
    # \u2028 / \u2029 是 Unicode 行分隔符，同样致命。
    s = _CTRL_RE.sub("", s)
    # 半角与全角路径分隔符都压平
    s = re.sub(r"[/\\\uFF0F\uFF3C]", "_", s)
    s = sanitize(s)
    # 去掉首尾的点/空白：避免 "." ".." 以及隐藏文件
    s = s.strip().strip(".").strip()
    if not s or s in (".", ".."):
        s = "未命名"
    # 单段名上限，避免超出文件系统限制
    return s[:120]


def is_within(path, root):
    """path 是否落在 root 之内（两者都 resolve 后比较）。

    用于落盘前的最后一道围栏：任何用户输入参与拼路径的地方，
    写之前都应过一遍。
    """
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False
    except OSError:
        return False


def parse_fm(source):
    """解析 YAML 前置元数据，支持 Path 对象或文本字符串"""
    if hasattr(source, "read_text"):
        c = source.read_text(encoding="utf-8")
    else:
        c = str(source)
    # 统一换行符：\r\n → \n，\r → \n
    c = c.replace("\r\n", "\n").replace("\r", "\n")
    if not c.startswith("---"):
        return {}, c
    m = re.match(r"^---\n(.*?)\n---\n?", c, re.DOTALL)
    if not m:
        return {}, c
    meta, body = {}, c[m.end():]
    lines = m.group(1).split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in ("tags", "relations"):
            # 安全加固：补块序列与无括号两种写法。原实现只认
            # `tags: [a, b]`，于是块序列写法返回空串、无括号写法返回字符串，
            # 同一字段在读取侧出现 list / str / '' 三种类型，标签云还会被
            # 逐字符切碎（`tags: 运营, 部署` → 运 / 营 / , / 部 / 署）。
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip('"').strip("'")
                     for x in v[1:-1].split(",") if x.strip()]
            elif v:
                v = [x.strip().strip('"').strip("'")
                     for x in re.split(r"[,，]", v) if x.strip()]
            else:
                # 块序列：tags: 之后紧跟缩进的 "- 值" 行
                items = []
                while i < len(lines):
                    mi = re.match(r"^[ \t]+-([ \t]+.*)?$", lines[i])
                    if not mi:
                        break
                    val = (mi.group(1) or "").strip().strip('"').strip("'")
                    if val:
                        items.append(val)
                    i += 1
                v = items
        meta[k] = v
    return meta, body


def finfo(fp, pre_meta=None):
    try:
        s = fp.stat()
    except OSError:
        log.warning("auth.py 异常: finfo", exc_info=True)
        return {"path": str(fp.relative_to(NOTES_DIR)) if NOTES_DIR in fp.parents else str(fp), "title": fp.stem, "error": "access_denied"}
    info = {
        "path": str(fp.relative_to(NOTES_DIR)),
        "size": s.st_size,
        "modified": datetime.fromtimestamp(s.st_mtime).isoformat(),
        "is_dir": fp.is_dir(),
    }
    if fp.suffix == ".md" and fp.is_file():
        if pre_meta is not None:
            m = pre_meta
        else:
            try:
                m, _ = parse_fm(fp)
            except Exception:
                log.warning("auth.py 异常: finfo", exc_info=True)
                info["title"] = fp.stem
                return info
        info.update(
            title=m.get("title", fp.stem),
            tags=m.get("tags", []),
            type=m.get("type", "?"),
            status=m.get("status", ""),
        )
    return info


def client_ip():
    """取客户端 IP，只在对端是受信代理时才采信转发头。

    默认 KB_TRUSTED_PROXIES 为空 = 不信任任何转发头，与零配置直连部署
    （KB_HOST=127.0.0.1）一致。若不加这层校验，任何人都能用
    `X-Forwarded-For: <随机值>` 伪造来源：/auth/request 的每 IP 限流会
    形同虚设（每次换一个值就是一个新计数桶），审计日志的 ip 也不再可信。

    C3/D2 补充 `CF-Connecting-IP`：cloudflared 在本机回连 127.0.0.1，
    隧道场景只有这个头带得出真实客户端 IP。口径（C1 返工第 4b 条）：
      ① **只在对端 IP 出现在 KB_TRUSTED_PROXIES 配置里时才采信**，环回不等于受信：
         本机任何进程、任何本机转发都能伪造这个头，无条件信任环回等于把
         last_ip / 每 IP 限流交给调用方；
      ② `KB_TRUSTED_PROXIES` 为空 → 不采信任何客户端头，一律用 request.remote_addr；
      ③ 采信时值必须能被 ipaddress 解析成合法 IP，否则丢弃（否则能往 last_ip 塞任意串），
         丢弃后继续按 ④ 的常规判定走（受信代理场景仍可取 XFF 最右段）；
      ④ 对端**不在**受信列表 → 不采信任何客户端头（含 XFF），直接返回对端地址。

    ⚠️ 配置约束：`KB_TRUSTED_PROXIES` **只许填 Cloudflare / cloudflared 的回连地址**。
    `CF-Connecting-IP` 是单值头，不像 XFF 有「上游代理追加、取最右段」的保护；
    一旦把别的代理（例如本机 nginx）也写进这个列表，客户端就能自带
    `CF-Connecting-IP` 直接伪造来源 —— last_ip 失真、/auth/request 的每 IP
    限流被换桶绕过。这正是 ① 要防的威胁。
    """
    peer = request.remote_addr or ""
    cf = (request.headers.get("CF-Connecting-IP", "") or "").strip()
    if cf and TRUSTED_PROXIES and peer in TRUSTED_PROXIES:
        try:
            return str(ipaddress.ip_address(cf))
        except ValueError:
            pass  # 非法值丢弃，继续走下面的常规判定
    if not TRUSTED_PROXIES or peer not in TRUSTED_PROXIES:
        return peer
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        # 取链**最右**段（安全加固）：标准反向代理是 append 语义，
        # 把自己看到的 peer 追加到末尾，最左段是客户端可自造的值。原先取最左段，
        # 等于让调用方自己决定「我是谁」——配了受信代理后 /auth/request 的
        # 每 IP 限流（5 次/5 分钟）可被伪造 XFF 无限绕过，审计日志的 ip 也不可信。
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1] or peer
    return request.headers.get("X-Real-IP", peer) or peer


def get_client():
    ua = request.headers.get("User-Agent", "?")
    ip = client_ip()
    d = "Unknown"
    ual = ua.lower()
    for kw, n in [
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("android", "Android"),
        ("mac", "Mac"),
        ("windows", "Windows"),
        ("linux", "Linux"),
        ("curl", "cURL"),
        ("python", "Python"),
    ]:
        if kw in ual:
            d = n
            break
    br = ""
    for kw, n in [
        ("chrome", "Chrome"),
        ("firefox", "Firefox"),
        ("safari", "Safari"),
        ("edg", "Edge"),
    ]:
        if kw in ual and (kw != "chrome" or "edg" not in ual):
            br = n
            break
    return {
        "ip": ip,
        "device": f"{d} / {br}" if br else d,
        "user_agent": ua,
        "time": datetime.now().isoformat(),
    }


