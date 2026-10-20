"""知识库 API — Token 存储"""

import os, json, secrets, time, threading, hashlib
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import fcntl  # POSIX；Windows 下降级为无锁（本机与部署环境均为 Linux）
except ImportError:  # pragma: no cover
    fcntl = None

from . import config as _config


def _token_file():
    """运行时解析 Token 文件路径。

    不在导入时绑定值，这样测试可以 patch config.TOKEN_FILE 做隔离，
    也允许部署后通过环境变量改变库根目录。
    """
    return _config.TOKEN_FILE


# ── C2：Token 哈希化（台账 #23）──
#
# 存储键从「明文令牌」改为 sha256(pepper + 明文) 的 64 位十六进制。
# 明文只存在于签发响应里一次，库里不留任何可直接还原明文的信息。
#
# pepper 取值顺序（缺一不可，否则多进程会各生成一个 pepper）：
#   1. 环境变量 KB_TOKEN_PEPPER（非空即用）
#   2. $KB_ROOT/.kb_pepper（0600）
#   3. 都没有 → 原子创建（O_CREAT|O_EXCL，并发下只有一个进程写得进去）
# 代码里不允许出现任何 pepper 字面量，DEFAULT_SETTINGS 里也不放。

_PEPPER_CACHE = {}          # str(pepper_file) -> pepper 值
_PEPPER_LOCK = threading.Lock()
PEPPER_FILE_NAME = ".kb_pepper"

# 已比对通过的 (store 路径, pepper 指纹)；命中后不再重复读盘，鉴权热路径无额外开销。
_PEPPER_ID_CHECKED = set()
# 在 store_transaction 内发现 meta 缺 pepper_id 时暂存这里（按线程隔离，
# 否则 A 线程的待写值可能被 B 线程的落盘顺手带走），由本次 save_store 一并落盘。
_PEPPER_PENDING = threading.local()
# 标记「当前线程正持有 store_transaction」，用于避免嵌套抢同一把文件锁。
_TX_ACTIVE = threading.local()

PEPPER_RETRY_TIMES = 5      # FileExistsError 后读空文件的次数
PEPPER_RETRY_SLEEP = 0.02   # 每次间隔 20ms


def _pepper_file(root=None):
    """pepper 文件路径。root 缺省时跟随 config.KB_ROOT（测试可 patch）。

    统一转成 Path：KB_ROOT 可能被设成字符串，直接 `base / name` 会 AttributeError。
    """
    base = root if root is not None else _config.KB_ROOT
    return Path(base) / PEPPER_FILE_NAME


def get_pepper(root=None):
    """取 pepper。环境变量优先；其次读文件；都没有就原子生成并落盘（0600）。"""
    env = os.environ.get("KB_TOKEN_PEPPER", "").strip()
    if env:
        return env
    pf = _pepper_file(root)
    key = str(pf)
    with _PEPPER_LOCK:
        if key in _PEPPER_CACHE:
            return _PEPPER_CACHE[key]
        if pf.exists():
            val = pf.read_text(encoding="utf-8").strip()
            if val:
                _PEPPER_CACHE[key] = val
                return val
        # 原子创建：先 exists 再 write 会让并发进程各写一份，令牌全部对不上。
        pf.parent.mkdir(parents=True, exist_ok=True)
        val = secrets.token_hex(32)
        try:
            fd = os.open(str(pf), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # 另一个进程刚建了文件。它可能还没写完，读到空值只能等，绝不能自己
            # 再写一遍——那会让两个进程各持一个 pepper，两半令牌互相认不出。
            val = ""
            for _ in range(PEPPER_RETRY_TIMES):
                val = pf.read_text(encoding="utf-8").strip()
                if val:
                    break
                time.sleep(PEPPER_RETRY_SLEEP)
            if not val:
                raise RuntimeError(
                    f"pepper 文件 {pf} 存在但内容为空（并发创建未完成），"
                    "拒绝自行生成以免 pepper 分裂；请检查该文件或删除后重试"
                )
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(val)
        try:
            os.chmod(str(pf), 0o600)
        except OSError:
            log.warning("store.py 异常: chmod pepper", exc_info=True)
        _PEPPER_CACHE[key] = val
        return val


def pepper_id(pepper):
    """pepper 指纹：sha256 前 8 位。与迁移脚本写入 meta["pepper_id"] 的口径一致。"""
    return hashlib.sha256(str(pepper).encode("utf-8")).hexdigest()[:8]


def _store_path_for(root=None):
    """root 给定时按「KB_ROOT/.api_tokens.json」推导，否则用 config.TOKEN_FILE。"""
    if root is None:
        return _token_file()
    return Path(root) / ".api_tokens.json"


def _check_pepper_id(pepper, root=None):
    """比对库中 meta["pepper_id"] 与当前 pepper，不一致直接报错。

    迁移脚本会把当时的 pepper 指纹写进 meta。若 .kb_pepper 丢了/挂错，或事后
    才加上 KB_TOKEN_PEPPER，get_pepper() 会静默换一个值——所有存量令牌算出的
    哈希都对不上，表现为「全站 401 且日志里什么都没有」。这里把静默失效改成
    显式故障：第一次算哈希就炸，并留下明确日志。

    meta 里没有 pepper_id（未迁移的老库）时补写当前指纹，不报错。
    """
    pid = pepper_id(pepper)
    tf = _store_path_for(root)
    key = (str(tf), pid)
    if key in _PEPPER_ID_CHECKED:
        return
    recorded = ""
    read_ok = True
    if tf.exists():
        try:
            recorded = ((json.loads(tf.read_text(encoding="utf-8")).get("meta") or {})
                        .get("pepper_id") or "")
        except Exception:
            # 锁外读失败（常见于与 save_store 并发，读到半截文件）。此时「库里有没有
            # 指纹」是未知，不是「没有」。若当成空去走补写，锁内实际有值时会只跳过
            # 写入、不做比对，随后把错误 pepper 标记为已验证——P1-2 的防护就失效了。
            read_ok = False
            log.warning("store.py 异常: 读取 pepper_id（转锁内复核）", exc_info=True)
    if recorded and recorded != pid:
        msg = (
            f"pepper 与库中记录不一致（库内 {recorded}，当前 {pid}）："
            "所有令牌将失效。请检查 KB_TOKEN_PEPPER 或 .kb_pepper "
            f"（{_pepper_file(root)}），确认用的是当初迁移时那一份"
        )
        log.error(msg)
        raise RuntimeError(msg)
    if not recorded:
        # 只给「本进程自己的令牌库」补写。root 显式给出时（迁移脚本/跨库调用）
        # 不在这里动盘，由调用方自己把 meta 写进去，避免迁移途中被旁路改写。
        if root is None:
            if getattr(_TX_ACTIVE, "on", False):
                # 已持锁（如 _issue_token 内）：不能在这里再抢锁，交给本次落盘一并写入。
                # 标记「已校验」也推迟到 _apply_pending_pepper_id 复核成功之后——
                # 否则复核抛错时 key 已在缓存里，本进程此后永不比对（fail-open）。
                _PEPPER_PENDING.pid = pid
                _PEPPER_PENDING.keys = getattr(_PEPPER_PENDING, "keys", set()) | {key}
                return
            else:
                try:
                    with store_transaction() as s:
                        meta = s.get("meta") or {}
                        actual = meta.get("pepper_id") or ""
                        if actual and actual != pid:
                            # 锁内复核：并发写盘导致锁外读失败/读空，但库里其实有指纹。
                            # 必须照常比对，不许标记为已检查。
                            msg = (
                                f"pepper 与库中记录不一致（库内 {actual}，当前 {pid}）："
                                "所有令牌将失效。请检查 KB_TOKEN_PEPPER 或 .kb_pepper "
                                f"（{_pepper_file(root)}），确认用的是当初迁移时那一份"
                            )
                            log.error(msg)
                            raise RuntimeError(msg)
                        if not actual:
                            meta["pepper_id"] = pid
                            s["meta"] = meta
                        elif not read_ok:
                            # 锁外没读到、锁内读到了正确值：这次可以认为是已校验。
                            pass
                except RuntimeError:
                    raise
                except Exception:
                    # 锁内复核也没跑成功 = 这一次等于没校验过。绝不能落到下面的
                    # add(key)，否则错误 pepper 会被永久当成「已验证」，回到
                    # 「全站 401 且日志里什么都没」的原始后果（fail-open）。
                    log.warning("store.py 异常: 写入 pepper_id（本次不标记已校验）",
                                exc_info=True)
                    return
        else:
            # root 显式给出（迁移脚本/跨库调用）：不在本地补写。
            # 锁外读失败时库中有无指纹仍未知，不许标记已校验。
            if not read_ok:
                log.warning("store.py: 读取 pepper_id 失败且为显式 root 调用，"
                            "本次不做校验，也不标记已校验")
                return
    _PEPPER_ID_CHECKED.add(key)


def token_hash(tk, root=None):
    """明文令牌 → 存储键：sha256(pepper + 明文) 的十六进制。

    确定性哈希：同一明文 + 同一 pepper 永远得到同一键，迁移后存量令牌照常可用。
    """
    if not tk:
        return ""
    pepper = get_pepper(root)
    _check_pepper_id(pepper, root)
    material = (pepper + str(tk)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def token_prefix(tk, root=None):
    """给人看的标识：哈希前 12 位。吊销按这个前缀匹配（D1 修复）。"""
    return token_hash(tk, root)[:12]


def token_record(tk, info, root=None, hashed=None):
    """补齐哈希化后新增的字段：id / prefix。返回被补过的记录（原地修改）。

    hashed 可传入已算好的哈希，避免调用方对同一明文重复计算（每次都读 pepper + 比对）。
    """
    h = token_hash(tk, root) if hashed is None else hashed
    info.setdefault("id", "tk_" + secrets.token_hex(5))
    info["prefix"] = h[:12]
    return info


# ── 文件级缓存（减少高频读磁盘）──

_CACHE = {"data": None, "ts": 0, "mtime": None}
_CACHE_TTL = 30  # 30 秒缓存
_CACHE_LOCK = threading.Lock()


log = logging.getLogger("kb.store")

def _load_store_cached():
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
            return _CACHE["data"]
    data = load_store()
    with _CACHE_LOCK:
        _CACHE["data"] = data
        _CACHE["ts"] = now
    return data


def _load_store_fresh(max_age=1.0):
    """读令牌库，但按**文件 mtime** 校验，跨进程改动立刻可见。

    为什么需要：`_load_store_cached()` 是 30 秒 TTL 的进程内缓存，另一个进程
    （kb-mcp、另一个 gunicorn worker）落盘解绑/改绑后，本进程最多 30 秒仍读到
    旧快照 —— 期间新设备会一直吃 mismatch 403，而且失败不刷新缓存，用户重试
    也解不开。设备绑定是安全相关的判定，不能建立在这种陈旧快照上。
    高频请求下用 max_age 顶一层（默认 1 秒）避免每个请求都读盘。

    mtime 变了就整份重读；没变就沿用缓存。

    依赖前提：文件系统 mtime 足够细。这里用 st_mtime_ns（整数纳秒）+ st_size 联合判定：
    用 st_mtime（float）时在 1.7e9 量级的有效精度只有约 0.24 微秒，不是纳秒；
    短时间内的两次写入可能拿到同一个 float 值而漏判。st_size 几乎零成本，
    rename 覆盖时大小大概率变化，多一层保险。
    在 NFS 或粗粒度 mtime 的文件系统上，仍可能撞同一个时间戳，
    可见性退化为 max_age 上界（默认 1 秒）——那时要把 max_age 调小或改读 sidecar 版本号。
    """
    tf = _token_file()
    try:
        _st = tf.stat()
        # 用整数纳秒 + 文件大小联合判定，不用 float 的 st_mtime（精度约 0.24 微秒）。
        mt = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        mt = None
    now = time.time()
    with _CACHE_LOCK:
        if (_CACHE["data"] is not None
                and now - _CACHE["ts"] < max_age
                and _CACHE.get("mtime") == mt):
            return _CACHE["data"]
    data = load_store()
    with _CACHE_LOCK:
        _CACHE["data"] = data
        _CACHE["ts"] = now
        _CACHE["mtime"] = mt
    return data


def _invalidate_cache():
    with _CACHE_LOCK:
        _CACHE["data"] = None
        _CACHE["ts"] = 0
        _CACHE["mtime"] = None


def load_store():
    tf = _token_file()
    s = json.loads(tf.read_text(encoding="utf-8")) if tf.exists() else {}
    s.setdefault("master", None)
    s.setdefault("tokens", {})
    s.setdefault("requests", {})
    return s


def save_store(s):
    tf = _token_file()
    # 🔒 保护 master token：从磁盘读取真实值还原，防止意外覆盖
    if tf.exists():
        try:
            current = json.loads(tf.read_text(encoding="utf-8"))
            orig_master = current.get("master")
            if orig_master and isinstance(orig_master, dict) and orig_master.get("token"):
                s["master"] = orig_master
        except Exception:
            log.warning("store.py 异常", exc_info=True)
    # 原子写入：先写临时文件，再 rename，防止写入中断导致数据损坏
    import os, tempfile
    fd, tmp_path = tempfile.mkstemp(
        dir=str(tf.parent), prefix=".api_tokens_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(tf))
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            log.warning("store.py 异常", exc_info=True)
        raise
    _invalidate_cache()


def init_master():
    # 走 store_transaction：原实现直接 save_store()，是全文唯一不走 flock 的写点，
    # 与 store_transaction() 的注释「所有写点都必须走这里」不符。
    # 启动期与令牌清理线程可能并发，这里补上同一把锁。
    with store_transaction() as s:
        if not s.get("master") or not isinstance(s["master"], dict) or not s["master"].get("token"):
            s["master"] = {
                "token": secrets.token_urlsafe(40),
                "name": "master",
                "created": datetime.now().isoformat(),
                "note": "主Token",
            }
        return s


def is_master(tk):
    m = _load_store_cached().get("master", {}).get("token")
    # 凭据比较用常量时间，避免按字符逐位比较泄露长度/前缀信息；编码成 bytes 后才能比较非 ASCII 令牌
    return bool(m) and bool(tk) and secrets.compare_digest(str(m).encode("utf-8"), str(tk).encode("utf-8"))


@contextmanager
def store_transaction():
    """独占方式读-改-写令牌库，跨进程串行化。

    save_store() 对 tokens / requests 是整体覆盖写入，而
    「load_store → 改 → save_store」在清理线程与多个鉴权路由里各写一遍，
    彼此之间没有锁。清理线程读盘后、写盘前若正好签发了新令牌，
    就会用自己那份旧数据覆盖文件，**刚签发的令牌凭空消失**。
    所有写点都必须走这里，不要再各自调用 save_store()。
    """
    tf = _token_file()
    if fcntl is None:
        s = load_store()
        _before0 = _tx_snapshot(s)
        prev = getattr(_TX_ACTIVE, "on", False)
        try:
            _TX_ACTIVE.on = True   # 放进 try：两行之间被异步异常打断会漏复原
            yield s
            keys = _apply_pending_pepper_id(s)
            if _tx_dirty(s, _before0) or keys:
                save_store(s)
            # 落盘成功之后才认「已校验」：save 失败则标记作废（下次重验）
            _PEPPER_ID_CHECKED.update(keys)
        finally:
            _TX_ACTIVE.on = prev
            _clear_pending()
        return
    tf.parent.mkdir(parents=True, exist_ok=True)
    lock_path = tf.with_suffix(tf.suffix + ".lock")
    with open(lock_path, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        prev = getattr(_TX_ACTIVE, "on", False)
        try:
            _TX_ACTIVE.on = True   # 放进 try：两行之间被异步异常打断会漏复原
            s = load_store()
            _before = _tx_snapshot(s)
            yield s
            keys = _apply_pending_pepper_id(s)
            if _tx_dirty(s, _before) or keys:
                save_store(s)
            # 落盘成功之后才认「已校验」
            _PEPPER_ID_CHECKED.update(keys)
        finally:
            _TX_ACTIVE.on = prev
            _clear_pending()
            fcntl.flock(lk, fcntl.LOCK_UN)


def _tx_snapshot(s):
    """事务开始前的快照：把整份 store 序列化成一行字符串用于比对。

    为什么要这个：save_store 是整文件覆盖 + rename，无条件写回会让**只读事务**
    （例如 _bind_device 的 same / mismatch / gone 三条不写字段的路径）在每次
    请求上全量落盘一次 —— 写放大、mtime 一直在变、备份/同步被反复触发。
    """
    # 整份序列化，不按键名枚举：按枚举写会漏掉将来新增的根键（写进去被判不脏
    # → 静默丢写）。整份比较对新增键天然安全。
    # 用 default=str 兜住非 JSON 原生的值；sort_keys 保证顺序稳定。
    return json.dumps(s, sort_keys=True, ensure_ascii=False, default=str)


def _tx_dirty(s, before):
    """事务里到底改没改业务字段。

    before 是必填参数：原来给 None 默认值并在 None 时静默返回 True，
    等于「忘传就保守写回」——看似安全，实际把「漏传」这种 bug 藏起来了。
    改成必填后，漏传直接 TypeError，不会再悄悄退化成无脑写盘。
    """
    return _tx_snapshot(s) != before


def _clear_pending():
    """清空本线程的 pending（pid + keys）。事务所有退出路径都要调，含 yield 抛异常。"""
    _PEPPER_PENDING.pid = None
    _PEPPER_PENDING.keys = set()


def _apply_pending_pepper_id(s):
    """把 _check_pepper_id 在持锁期间记下的 pid 落进本次写入。

    返回「待标记为已校验」的 key 集合——**由调用方在 save_store 成功之后**才
    update 进 _PEPPER_ID_CHECKED，避免「标记已生效但指纹没落盘」的假绿。
    """
    pid = getattr(_PEPPER_PENDING, "pid", None)
    keys = set(getattr(_PEPPER_PENDING, "keys", set()))
    # 先清再比：调用方若因不一致抛错，pending 也不许留给下一个无关事务
    # （否则 stale pid 会带进别人的落盘，把爆炸半径扩大）。
    _PEPPER_PENDING.pid = None
    _PEPPER_PENDING.keys = set()
    if not pid:
        return set()
    meta = s.get("meta") or {}
    actual = meta.get("pepper_id") or ""
    if actual and actual != pid:
        # 库里已有指纹且与当前 pepper 不符：不许静默跳过，也不许覆盖，
        # 必须显式失败（否则这一轮落盘会把错误 pepper 的无感状态固化下去）。
        msg = (
            f"pepper 与库中记录不一致（库内 {actual}，当前 {pid}）："
            "所有令牌将失效。请检查 KB_TOKEN_PEPPER 或 .kb_pepper，"
            "确认用的是当初迁移时那一份"
        )
        log.error(msg)
        raise RuntimeError(msg)
    if not actual:
        meta["pepper_id"] = pid
        s["meta"] = meta
    # 复核通过（actual == pid 或刚补写）：把「已校验」交给调用方在落盘后补上
    return keys


def is_expired(info):
    e = info.get("expires_at", "")
    return e and datetime.fromisoformat(e) < datetime.now()
