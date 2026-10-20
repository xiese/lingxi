"""import kb_lib 零副作用回归测试（2026-10-07）。

背景：kb_lib/__init__.py 原在模块导入时执行 init_master() + _init_index +
清理线程，任何 import（CLI、测试 collect，以及 kb_auth_watch / kb_eval /
kb_note_hygiene / kb_compile_wiki 等只碰 kb_lib 子模块、同样会触发包初始化的
脚本）都会拉真实索引加载、写令牌文件与索引文件。现已收编为显式
start_background()，import 只做声明。

本文件用全新解释器子进程验证：
  A import kb_lib 不起线程、不写任何状态文件；
  B 显式 start_background() 后正常起线程、建主令牌、落索引文件，且调用幂等。

历史遗留项已清零：kb_lib/routes/routes_auth.py:40 原在模块级起「鉴权请求限流
记录清理」线程，2026-10-07 第二轮已按同一模式收编为 start_auth_limit_cleanup()
并由 start_background() 显式调用；本测试的已知遗留排除列表随之改回空。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
CHILD_ENV_DROP = ("KB_BIND", "KB_API_BASE", "KB_HOST")
# 已知遗留的 import 级线程（历史排除项，routes_auth 已收编，现为空，见模块 docstring）
KNOWN_LEFTOVER_THREADS = ()


def _run_child(script: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for k in CHILD_ENV_DROP:
        env.pop(k, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env, capture_output=True, text=True, timeout=300, cwd=str(SCRIPTS),
    )


_IMPORT_CHILD = r"""
import os, sys, threading, tempfile, pathlib
root = pathlib.Path(tempfile.mkdtemp(prefix="kb-import-clean-"))
(root / "\u7b14\u8bb0").mkdir()
os.environ["KB_ROOT"] = str(root)
sys.path.insert(0, %(scripts)r)
known = %(known)r
before = threading.active_count()
import kb_lib
import kb_lib.routes
import kb_lib.search
after = threading.active_count()
names = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
extra = [n for n in names if not any(k in n for k in known)]
assert not extra, f"import 后出现未被收编的后台线程: {extra}"
assert after == before + len(known), (
    f"线程数不符合预期（应仅多出 {len(known)} 个已知遗留线程 {known}）: {before} -> {after}"
)
for mine in ("kb-index-init", "kb-token-cleanup", "kb-rate-cleanup"):
    assert mine not in names, f"import 不应启动 {mine}"
created = sorted(p.name for p in root.iterdir())
assert created == ["\u7b14\u8bb0"], f"import 产生了状态文件: {created}"
assert getattr(kb_lib, "master_store", None) is None, "import 不应初始化 master_store"
assert not (root / ".search_index.pkl").exists(), "import 不应加载/构建索引"
print("IMPORT_CLEAN_OK")
"""


def test_import_kb_lib_不起线程不写文件():
    script = _IMPORT_CHILD % {"scripts": str(SCRIPTS), "known": KNOWN_LEFTOVER_THREADS}
    r = _run_child(script)
    assert r.returncode == 0, f"import 副作用回归:\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-1500:]}"
    assert "IMPORT_CLEAN_OK" in r.stdout


_BG_CHILD = r"""
import os, sys, threading, tempfile, pathlib, time
root = pathlib.Path(tempfile.mkdtemp(prefix="kb-bg-"))
(root / "\u7b14\u8bb0").mkdir()
os.environ["KB_ROOT"] = str(root)
sys.path.insert(0, %(scripts)r)
import kb_lib

before = threading.active_count()
kb_lib.start_background()

# 索引初始化线程（kb-index-init）是瞬时的：空库 build 毫秒级完成即退出，
# 因此等可观测终态——索引文件落盘 + 周期刷新循环线程在场，而不是抢瞬时线程名。
deadline = time.time() + 120
while time.time() < deadline:
    names = [t.name for t in threading.enumerate()]
    if (root / ".search_index.pkl").exists() and any("_periodic_refresh_loop" in n for n in names):
        break
    time.sleep(0.2)
assert (root / ".search_index.pkl").exists(), "索引文件未落盘（_init_index 未执行）"

names = [t.name for t in threading.enumerate()]
for want in ("kb-token-cleanup", "kb-rate-cleanup", "kb-auth-limit-cleanup"):
    assert want in names, f"start_background 未启动 {want}: {sorted(names)}"
assert threading.active_count() > before, "start_background 未起任何线程"
assert (root / ".api_tokens.json").exists(), "start_background 未初始化主令牌"
assert kb_lib.master_store and kb_lib.master_store.get("master", {}).get("token")

def stable_count(timeout=60):
    last, same = -1, 0
    end = time.time() + timeout
    while time.time() < end:
        c = threading.active_count()
        if c == last:
            same += 1
            if same >= 5:
                return c
        else:
            same = 0
        last = c
        time.sleep(0.2)
    return last

n_before = stable_count()
kb_lib.start_background()
time.sleep(0.5)
assert threading.active_count() == n_before, (
    f"重复调用 start_background 叠加了线程: {n_before} -> {threading.active_count()}"
)
print("START_BACKGROUND_OK")
"""


def test_start_background_显式启动且幂等():
    script = _BG_CHILD % {"scripts": str(SCRIPTS)}
    r = _run_child(script)
    assert r.returncode == 0, f"start_background 回归:\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-1500:]}"
    assert "START_BACKGROUND_OK" in r.stdout
