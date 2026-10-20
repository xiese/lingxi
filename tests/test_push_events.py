"""第 16 项：消息推送按事件勾选。

验收口径（17:34 C 节 + 17:47 评审）：
    ① 设置页可以逐个勾选「什么情况推给我」
    ② 全部默认关闭（新装/未配置时一条都不推）
    ③ 没勾上的事件不发，且不发「跳过通知」刷屏
    ④ 手动测试推送不受勾选限制
    ⑤ 勾选存的是逗号分隔字符串，非法事件键被忽略（不静默污染配置）
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import kb_lib.push_qq as pq  # noqa: E402


def _fake_settings(**kw):
    base = {
        "qq_push_enabled": True,
        "qq_push_webhook_url": "https://example.invalid/hook",
        "qq_push_webhook_secret": "",
        "qq_push_target": "home",
        "qq_push_payload_mode": "generic",
        "qq_push_events": "",
    }
    base.update(kw)
    return base


@pytest.fixture
def patch_settings(monkeypatch):
    def _set(**kw):
        s = _fake_settings(**kw)
        monkeypatch.setattr(pq, "load_settings", lambda: s)
        return s

    return _set


# ① 事件清单存在且是「键 → 中文名」
def test_事件清单完整():
    assert isinstance(pq.PUSH_EVENTS, dict)
    assert len(pq.PUSH_EVENTS) >= 5
    for k, v in pq.PUSH_EVENTS.items():
        assert isinstance(k, str) and k.islower(), f"事件键必须是小写英文：{k}"
        assert isinstance(v, str) and v, f"事件 {k} 缺中文名"


def test_事件键不含逗号或空格():
    """存的是逗号分隔字符串，键里出现逗号会把一个事件拆成两个。"""
    for k in pq.PUSH_EVENTS:
        assert "," not in k and " " not in k, f"事件键非法：{k!r}"


# ② 默认全部关闭
def test_默认全部关闭(patch_settings):
    patch_settings()
    assert pq.enabled_events() == set()
    for k in pq.PUSH_EVENTS:
        assert pq.is_event_enabled(k) is False


def test_未配置键时也当作全部关闭(patch_settings):
    s = patch_settings()
    s.pop("qq_push_events", None)
    assert pq.enabled_events() == set()


# ③ 勾选解析
def test_勾选解析逗号分隔(patch_settings):
    patch_settings(qq_push_events="new_device,login_failed")
    assert pq.enabled_events() == {"new_device", "login_failed"}


def test_勾选解析容忍空格和空项(patch_settings):
    patch_settings(qq_push_events=" new_device , ,login_failed ,")
    assert pq.enabled_events() == {"new_device", "login_failed"}


def test_非法事件键被忽略(patch_settings):
    patch_settings(qq_push_events="new_device,__evil__,,login_failed")
    assert pq.enabled_events() == {"new_device", "login_failed"}


def test_也接受列表形式(patch_settings):
    patch_settings(qq_push_events=["new_device", "todo_due"])
    assert pq.enabled_events() == {"new_device", "todo_due"}


# ④ 未勾选不发，且不刷屏
def test_未勾选的事件不发送(patch_settings):
    patch_settings(qq_push_events="new_device")
    r = pq.send_qq_message("测试", event="token_revoked")
    assert r["sent"] is False
    assert r.get("skipped") is True
    assert r["event"] == "token_revoked"


def test_未勾选时不产生任何网络请求(patch_settings, monkeypatch):
    """skipped 必须在构造请求之前返回，不能先发再判断。"""
    patch_settings(qq_push_events="")
    called = []
    monkeypatch.setattr(pq.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    pq.send_qq_message("测试", event="login_failed")
    assert called == []


def test_勾选的事件会真的发(patch_settings, monkeypatch):
    patch_settings(qq_push_events="new_device")

    class R:
        status = 200

        def read(self, n=0):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = req.data
        return R()

    monkeypatch.setattr(pq.urllib.request, "urlopen", fake_urlopen)
    r = pq.send_qq_message("新设备登录了", event="new_device")
    assert r["sent"] is True
    assert seen["url"] == "https://example.invalid/hook"
    assert "新设备登录了" in seen["body"].decode("utf-8")


# ⑤ 手动测试推送不受勾选限制
def test_手动推送不受勾选限制(patch_settings, monkeypatch):
    patch_settings(qq_push_events="")

    class R:
        status = 200

        def read(self, n=0):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pq.urllib.request, "urlopen", lambda *a, **k: R())
    r = pq.send_qq_message("手动测试", event="")
    assert r["sent"] is True
    assert "skipped" not in r


# ⑥ 总开关仍然有效：勾了事件但总开关关着，也不发
def test_总开关关闭时勾选也不发(patch_settings):
    patch_settings(qq_push_enabled=False, qq_push_events="new_device")
    r = pq.send_qq_message("x", event="new_device")
    assert r["sent"] is False
    assert r.get("disabled") is True


# ⑦ 对外暴露的 settings 里带上勾选项
def test_公开设置包含勾选项(patch_settings):
    patch_settings(qq_push_events="todo_due,new_device")
    r = pq.send_qq_message("x", dry_run=True)
    assert r["settings"]["events"] == ["new_device", "todo_due"]


# ⑧ 后端白名单必须含 qq_push_events，否则保存被静默丢弃
def test_后端白名单含勾选键():
    src = (ROOT / "scripts/kb_lib/routes/routes_admin.py").read_text(encoding="utf-8")
    i = src.index("float_keys")
    j = src.index("for k in [", i)
    k = src.index("]:", j)
    block = src[j:k]
    assert '"qq_push_events"' in block, "routes_admin 保存白名单缺 qq_push_events，勾选会被丢掉"


# ⑨ 配置默认值必须是空（默认全关）
def test_配置默认值为空():
    src = (ROOT / "scripts/kb_lib/config.py").read_text(encoding="utf-8")
    assert '"qq_push_events": ""' in src, "默认值必须为空字符串（全部默认关闭）"

# ─── 真实调用点必须带事件名（否则勾选是死的）───────────────────────────
# DS 审查原话：功能 16 是惰性的——没有任何调用方传 event，勾选框不影响真实推送。
# 这几条直接检查生产代码里每个 send_qq_message 调用都带了 event。

import re as _re

SCRIPTS = ROOT / "scripts"


def _strip_comment_lines(text):
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def test_每个真实推送调用都带事件名():
    """scripts/ 下所有 send_qq_message 调用，除手动测试接口外都必须传 event=。"""
    calls = []
    for p in SCRIPTS.rglob("*.py"):
        if p.name == "push_qq.py":
            continue  # 定义文件本身
        src = _strip_comment_lines(p.read_text(encoding="utf-8"))
        for m in _re.finditer(r"send_qq_message\(", src):
            # 取到配对右括号
            i = m.end()
            depth = 1
            while i < len(src) and depth:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            calls.append((p, src[m.start():i]))
    assert calls, "没找到任何 send_qq_message 调用，测试失效"
    for p, call in calls:
        # 手动测试推送（routes_admin 的测试接口）允许不传 event
        if p.name == "routes_admin.py":
            continue
        assert "event=" in call, f"{p.name} 的推送没带 event=，设置页勾选对它不起作用：{call[:120]}"


def test_申请授权与首次登录都接了推送():
    """两条真实业务推送必须各自带对应事件名。"""
    a = (SCRIPTS / "kb_lib/routes/routes_auth.py").read_text(encoding="utf-8")
    assert 'event="auth_request"' in a, "有人申请授权没接推送事件"
    assert 'event="new_device"' in a, "新设备登录没接推送事件"
    w = (SCRIPTS / "kb_auth_watch.py").read_text(encoding="utf-8")
    assert 'event="auth_request"' in w, "授权轮询推送没接事件名"


def test_申请登录不写失败记录():
    """「发起申请」不是失败，写进 login_log(False) 会让正常申请被算进 5 次锁定。"""
    a = (SCRIPTS / "kb_lib/routes/routes_auth.py").read_text(encoding="utf-8")
    i = a.index('audit_log("auth_request"')
    seg = a[max(0, i - 400):i]
    assert "login_log(False" not in seg, "申请登录被记成失败，会误触发限速"


def test_新设备判定只看成功记录():
    sys.path.insert(0, str(SCRIPTS))
    import kb_lib.audit as audit
    assert hasattr(audit, "is_new_device"), "缺 is_new_device，新设备推送无法判断"
    src = (SCRIPTS / "kb_lib/audit.py").read_text(encoding="utf-8")
    i = src.index("def is_new_device(")
    body = src[i:i + 700]
    assert "ok=1" in body, "新设备判定没过滤成功记录，失败登录会被当成新设备"
