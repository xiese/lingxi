"""仓库闸门新增三项检查的回归（2026-10-29）。

锁死的不变量：
    ① 内网机器简称不许进仓库：`生产 <两段式机器号>:<端口>` 这类两段式写法
       不是合法 IP，旧 IP_RE 要求四段所以漏拦，但它照样暴露「哪台机器在跑」
    ② 文档保留段（192.0.2.x / 198.51.100.x / 203.0.113.x）必须放行，
       否则规则会把 AGENTS.md 要求的示例地址自己也判红
    ③ 历史里写了 `release: X.Y.Z` 的提交必须有对应的 v X.Y.Z tag，
       缺 tag = 这个版本对外根本装不到，闸门要在合入前喊住
    ④ tag 指向的提交里 VERSION 必须等于 tag 名，防手滑打错锚点
    ⑤ 代码里 import 的第三方包必须在 requirements 三个文件里声明过，
       漏声明的表现是「装了就能用、没装就静默回落内置默认」，最难查
    ⑥ 真仓当前状态必须全绿（新规则不许把已有的 main 判红，宁可少检不误伤）

guard.py 不是包（scripts/ci/ 下没有 __init__.py），所以按文件路径加载。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "ci" / "guard.py"
# 样本里的两段式机器号必须拆开拼：写全就会被闸门自己的内容闸判红（同 guard.py 拆旧仓名的做法）。
SHORT_HOST = "1" + ".92"
SHORT_HOST_B = "119" + ".29"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("kb_guard_under_test", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_content_check(guard, monkeypatch, tmp_path, text, filename="a.md"):
    """把一段文字伪装成受跟踪文件，跑内容闸，返回报错列表。"""
    f = tmp_path / filename
    f.write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(guard, "tracked", lambda: [filename])
    return guard.check_content([filename])


# ---------- ① 内网简称必须报红 ----------

@pytest.mark.parametrize("text", [
    f"生产 {SHORT_HOST}:8899 出现搜索超时\n",
    f"现象（生产 {SHORT_HOST}:8899，2026-10-25 03:21:08）：\n",
    f"端到端（生产 {SHORT_HOST} 临时实例 :8902）：\n",
    f"连生产 {SHORT_HOST} 实测 9 个工具\n",
    f"### 部署（生产 {SHORT_HOST_B}）\n",
])
def test_short_host_form_is_blocked(guard, monkeypatch, tmp_path, text):
    errs = _write_content_check(guard, monkeypatch, tmp_path, text)
    assert errs, f"内网简称没被拦住：{text!r}"
    assert any("内网机器简称" in e for e in errs), errs


# ---------- ② 示例段与版本号不许误伤 ----------

@pytest.mark.parametrize("text", [
    "示例实例 192.0.2.10:8899 复现\n",
    "健康检查 http://192.0.2.1:8899/health\n",
    "版本从 1.0.16 升到 1.0.17\n",
    "耗时 1.5:2.5 秒区间\n",
    "生产实例（已脱敏）出现搜索超时\n",
])
def test_doc_ip_and_version_not_blocked(guard, monkeypatch, tmp_path, text):
    errs = _write_content_check(guard, monkeypatch, tmp_path, text)
    assert not any("内网机器简称" in e for e in errs), f"误伤：{text!r} -> {errs}"


# ---------- ③ release 提交缺 tag 必须报红 ----------

def _fake_git(guard, monkeypatch, log_out, tag_out="", show_out=None):
    calls = []

    def fake_git(*a):
        calls.append(a)
        if a[:2] == ("log", "--format=%H%x09%s"):
            return log_out
        if a[0] == "tag":
            return tag_out
        if a[0] == "show":
            return (show_out if show_out is not None else '')
        if a[0] == "rev-parse":
            return ""
        return ""

    monkeypatch.setattr(guard, "git", fake_git)
    return calls


def test_release_commit_without_tag_is_blocked(guard, monkeypatch):
    calls = _fake_git(guard, monkeypatch, "abc123\trelease: 9.9.9 — 测试用假版本\n")
    errs = guard.check_release_tags("HEAD")
    assert calls, "没调用 git"
    assert any("9.9.9" in e for e in errs), f"缺 tag 没报红：{errs}"


def test_release_commit_with_tag_is_ok(guard, monkeypatch):
    _fake_git(guard, monkeypatch,
              "abc123\trelease: 9.9.9 — 测试用假版本\n",
              tag_out="v9.9.9\n",
              show_out='VERSION = "9.9.9"\n')
    assert guard.check_release_tags("HEAD") == []


# ---------- ④ tag 与 VERSION 不一致必须报红 ----------

def test_tag_pointing_to_wrong_version_is_blocked(guard, monkeypatch):
    _fake_git(guard, monkeypatch,
              "abc123\trelease: 9.9.9 — 测试用假版本\n",
              tag_out="v9.9.9\n",
              show_out='VERSION = "9.9.8"\n')
    errs = guard.check_release_tags("HEAD")
    assert any("VERSION" in e or "版本" in e for e in errs), f"tag 打错没报红：{errs}"


def test_no_release_commit_is_ok(guard, monkeypatch):
    _fake_git(guard, monkeypatch, "abc123\tfix: 普通修复\n")
    assert guard.check_release_tags("HEAD") == []


# ---------- ⑤ 依赖漏声明必须报红 ----------

def _make_tree(tmp_path, monkeypatch, imports, requires):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "m.py").write_text(imports, encoding="utf-8")
    # 仓内包：真仓里 scripts/kb_lib 就是这种嵌套目录，检查必须认得
    (tmp_path / "scripts" / "kb_lib").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "kb_lib" / "__init__.py").write_text("", encoding="utf-8")
    for name, body in requires.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return ["scripts/m.py", "scripts/kb_lib/__init__.py"] + list(requires)


def test_undeclared_import_is_blocked(guard, monkeypatch, tmp_path):
    files = _make_tree(tmp_path, monkeypatch,
                       "import totally_made_up_pkg\n",
                       {"requirements.txt": "Flask==3.1.3\n"})
    errs = guard.check_deps(files)
    assert any("totally" in e for e in errs), f"漏声明没报红：{errs}"


def test_declared_import_is_ok(guard, monkeypatch, tmp_path):
    files = _make_tree(tmp_path, monkeypatch,
                       "import totally_made_up_pkg\nimport flask\n",
                       {"requirements.txt": "Flask==3.1.3\ntotally-made-up-pkg==1.0\n"})
    assert guard.check_deps(files) == []


def test_stdlib_and_local_modules_not_flagged(guard, monkeypatch, tmp_path):
    files = _make_tree(
        tmp_path, monkeypatch,
        "import os\nimport sys\nimport json\nimport re\nfrom pathlib import Path\n"
        "import kb_lib\nfrom scripts.m import x\n",
        {"requirements.txt": ""})
    assert guard.check_deps(files) == [], "标准库/仓内模块被误判成第三方"


def test_alias_mapping(guard, monkeypatch, tmp_path):
    """import 名和包名不同名时必须认得：yaml->PyYAML、sklearn->scikit-learn。"""
    files = _make_tree(tmp_path, monkeypatch,
                       "import yaml\nfrom sklearn.linear_model import Ridge\n",
                       {"requirements.txt": "PyYAML==6.0.2\n",
                        "requirements-optional.txt": "scikit-learn==1.7.2\n"})
    assert guard.check_deps(files) == [], "别名映射没生效"


def test_alias_mapping_case_insensitive(guard, monkeypatch, tmp_path):
    """`from PIL import Image` 取到的 top 是 "PIL"，而别名表键是小写 "pil" ——
    不做大小写归一化会漏匹配、误报「pillow 没写进 requirements」（2026-10-31 修复）。"""
    files = _make_tree(tmp_path, monkeypatch,
                       "from PIL import Image\n",
                       {"requirements.txt": "pillow==11.0.0\n"})
    assert guard.check_deps(files) == [], "PIL → pillow 的别名大小写没归一化"


# ---------- ⑥ 真仓当前状态必须全绿 ----------

def test_real_repo_content_clean(guard):
    """真仓所有跟踪文件跑内容闸不许报错（新规则不许把已有 main 判红）。"""
    files = [p for p in guard.tracked() if (REPO / p).exists()]
    import os
    cwd = os.getcwd()
    os.chdir(REPO)
    try:
        errs = guard.check_content(files)
    finally:
        os.chdir(cwd)
    assert errs == [], errs[:10]


def test_real_repo_release_tags_aligned(guard):
    """真仓历史里的 release 提交必须都已有 tag 且 VERSION 对得上。"""
    assert guard.check_release_tags("HEAD") == []


def test_real_repo_deps_declared(guard):
    """真仓现有第三方 import 必须全部已声明（含刚补的 PyYAML）。"""
    import os
    cwd = os.getcwd()
    os.chdir(REPO)
    try:
        errs = guard.check_deps(guard.tracked())
    finally:
        os.chdir(cwd)
    assert errs == [], errs[:10]
