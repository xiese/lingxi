#!/usr/bin/env python3
"""第 16 项（推送按事件勾选）反向验证。

把每处修复逐个改坏，确认 tests/test_push_events.py 报红；最后还原并复查全绿。
用法：python3 tests/revert_16.py
退出码：0 = 全部变异有约束力；1 = 有漏检
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUSH = ROOT / "scripts/kb_lib/push_qq.py"
CFG = ROOT / "scripts/kb_lib/config.py"
ADMIN = ROOT / "scripts/kb_lib/routes/routes_admin.py"

APP = ROOT / "scripts/web/app.js"
AUDIT = ROOT / "scripts/kb_lib/audit.py"
AUTH = ROOT / "scripts/kb_lib/routes/routes_auth.py"
HTML = ROOT / "scripts/web/index.html"

CASES = [
    # ── 前端：勾选界面 ──
    ("设置项里没有推送勾选", APP,
     "    {k:'qq_push_events', l:'推哪些情况', t:'checks',",
     "    {k:'__gone__', l:'推哪些情况', t:'text',"),
    ("多选渲染成单值输入", APP,
     "  if(f.t==='checks'){",
     "  if(false){"),
    ("保存不拼多选值", APP,
     "    v=picked.join(',');",
     "    v='';"),
    ("多选容器不带 data-key", APP,
     "var box='<div class=\"set-checks\" data-key=\"'+esc(f.k)+'\">';",
     "var box='<div class=\"set-checks\">';"),
    ("摘要不显示勾选数", APP,
     "    return n?n+' 项已勾选':'全部关闭';",
     "    return '';"),
    ("缺复选框样式", HTML,
     ".set-check{display:flex;",
     ".set-checkX{display:flex;"),

    ("默认不关闭（默认推全部）", PUSH,
     '    raw = s.get("qq_push_events", "") or ""',
     '    raw = s.get("qq_push_events", "") or ",".join(PUSH_EVENTS)'),
    ("不按勾选过滤（未勾选也发）", PUSH,
     '    if event and event not in enabled_events(s):',
     '    if False:'),
    ("非法事件键不过滤", PUSH,
     '    return {e for e in picked if e in PUSH_EVENTS}',
     '    return set(e for e in picked if e)'),
    ("先发请求后判断勾选", PUSH,
     '''    s = load_settings()
    payload = build_qq_payload(message, s)
    public = _public_settings(s)
    if event and event not in enabled_events(s):''',
     '''    s = load_settings()
    payload = build_qq_payload(message, s)
    public = _public_settings(s)
    if False:'''),
    ("公开设置不带勾选项", PUSH,
     '        "events": sorted(enabled_events(s)),',
     '        "events": [],'),
    ("配置默认值非空", CFG,
     '    "qq_push_events": "",',
     '    "qq_push_events": "new_device",'),
    ("判定与写入不在同一把锁", AUDIT,
     "    with _NEW_DEV_LOCK:  # LAST",
     "    if True:"),
    ("空设备名当新设备推", AUDIT,
     "        return bool(device) and n == 0",
     "        return n == 0"),
    ("锁定退回窗口累计口径", AUDIT,
     "    if login_failures_since_success(ip) < LOGIN_FAIL_MAX:\n        return False",
     "    if True:\n        return login_failures_recent(ip) >= LOGIN_FAIL_MAX"),
    # ── 第 15 项前端：设备清单与一键吊销 ──
    ("设备清单没有前端入口", APP,
     "    {k:'_devices', l:'登录设备', t:'devices'",
     "    {k:'_gone', l:'登录设备', t:'text'"),
    ("二级页不加载设备列表", APP,
     "    _setLoadDevices();\n    return;",
     "    return;"),
    ("吊销不发 DELETE", APP,
     "ap('/auth/revoke/'+encodeURIComponent(hint),'DELETE')",
     "ap('/auth/revoke/'+encodeURIComponent(hint))"),
    ("吊销前不确认", APP,
     "if(!confirm('吊销「'+device+'」的这枚令牌？该设备立即失效。'))return;",
     "if(false)return;"),
    ("设备列表不显示使用次数", APP,
     "+' · 使用 '+(it.uses||0)+' 次'",
     "+''"),
    ("后端白名单漏 qq_push_events", ADMIN,
     '        "qq_push_events",\n',
     ''),
]


def run_tests():
    a = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_push_events.py", "tests/test_login_devices.py", "-q", "-x"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    b = subprocess.run(
        ["node", "tests/js/push_events_ui.test.cjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    c = subprocess.run(
        ["node", "tests/js/login_devices_ui.test.cjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    return 0 if (a.returncode == 0 and b.returncode == 0 and c.returncode == 0) else 1


def sub_once(text, old, new):
    if old not in text:
        return None
    return text.replace(old, new, 1)


def main():
    originals = {p: p.read_text(encoding="utf-8") for p in {PUSH, CFG, ADMIN, APP, HTML, AUDIT, AUTH}}
    tmps = {}
    for p in originals:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=p.suffix)
        f.close()
        shutil.copy2(p, f.name)
        tmps[p] = f.name

    bad = 0
    try:
        for name, path, old, new in CASES:
            only_last = old.endswith("  # LAST")
            if only_last:
                old = old[:-8]
            src = originals[path]
            if only_last:
                # 只改最后一处（with _NEW_DEV_LOCK 在两个函数里都出现）
                i = src.rfind(old)
                if i < 0:
                    print(f"❌ {name}：锚点未找到，变异未生效（不算通过）")
                    bad += 1
                    continue
                mut = src[:i] + new + src[i + len(old):]
            else:
                mut = sub_once(src, old, new)
            if mut is None:
                print(f"❌ {name}：锚点未找到，变异未生效（不算通过）")
                bad += 1
                continue
            path.write_text(mut, encoding="utf-8")
            rc = run_tests()
            path.write_text(src, encoding="utf-8")
            if rc == 0:
                print(f"❌ {name}：测试仍然全绿，漏检！")
                bad += 1
            else:
                print(f"✅ {name}：报红（exit {rc}）")
    finally:
        for p, tmp in tmps.items():
            shutil.copy2(tmp, p)
            Path(tmp).unlink(missing_ok=True)

    print("-" * 60)
    rc = run_tests()
    print("还原后全量：", "全绿" if rc == 0 else f"仍有失败 exit {rc}")
    if rc != 0:
        bad += 1
    print(f"共 {len(CASES)} 条变异，漏检 {bad} 条")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
