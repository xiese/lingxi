"""第 7-4 项：功能插件机制（services/plugins.py）单测。

覆盖维护者定稿的口径：
  · 一个目录 + manifest.json + 入口文件 = 一个插件，扫描即得
  · manifest 坏 / name 与目录名不一致 / mount 非法 / 入口不存在 → 跳过该插件，
    但**其余插件照常返回**（一个坏插件不许拖垮整份清单）
  · 启停状态默认全部启用，只有显式关掉的才进 disabled
  · 关不存在的插件名要报错，不能写脏记录
"""
import json
import os
from pathlib import Path

import pytest

from scripts.kb_lib.services import plugins as P


def _mk(root: Path, name: str, *, manifest=None, entry="plugin.js", entry_text="// ok"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (d / "manifest.json").write_text(
            manifest if isinstance(manifest, str) else json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8")
    if entry:
        (d / entry).write_text(entry_text, encoding="utf-8")
    return d


def _good(name="hello", mount="dash", **kw):
    m = {"name": name, "title": "示例 " + name, "version": "1.0.0",
         "description": "demo", "mount": mount, "entry": "plugin.js", "permissions": []}
    m.update(kw)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    """插件目录 + 状态文件都挪到 tmp，绝不碰真实库。"""
    pdir = tmp_path / "web" / "plugins"
    pdir.mkdir(parents=True)
    monkeypatch.setattr(P, "PLUGINS_DIR", pdir)
    monkeypatch.setattr(P, "state_file", lambda: tmp_path / ".kb_plugins.json")
    return pdir


# ── 扫描与清单 ──

def test_一个合法插件能被扫出来(env):
    _mk(env, "hello", manifest=_good("hello"))
    items = P.list_plugins()
    assert [x["name"] for x in items] == ["hello"]
    one = items[0]
    assert one["title"] == "示例 hello"
    assert one["mount"] == "dash"
    assert one["url"] == "./plugins/hello/plugin.js"
    # 默认启用
    assert one["enabled"] is True


def test_目录不存在时返回空列表而不是抛错(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PLUGINS_DIR", tmp_path / "nope")
    monkeypatch.setattr(P, "state_file", lambda: tmp_path / ".kb_plugins.json")
    assert P.list_plugins() == []
    assert P.load_enabled() == []


def test_坏插件被跳过但好插件照常返回(env):
    _mk(env, "good", manifest=_good("good"))
    _mk(env, "badjson", manifest="{ 这不是 json")
    _mk(env, "wrongname", manifest=_good("另一个名字"))
    _mk(env, "badmount", manifest=_good("badmount", mount="sidebar"))
    _mk(env, "noentry", manifest=_good("noentry"), entry=None)
    _mk(env, "nomanifest", manifest=None)
    names = [x["name"] for x in P.list_plugins()]
    assert names == ["good"], names


def test_name必须与目录名一致(env):
    # 名字不一致时前端拼 ./plugins/<目录>/<entry> 与后端启停 id 会对不上
    _mk(env, "dir1", manifest=_good("other"))
    assert P.list_plugins() == []
    assert P.parse_manifest(json.dumps(_good("other")), "dir1") is None


def test_非法目录名被跳过(env):
    _mk(env, "hello", manifest=_good("hello"))
    d = env / "bad name"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(_good("bad name")), encoding="utf-8")
    (d / "plugin.js").write_text("// x", encoding="utf-8")
    assert [x["name"] for x in P.list_plugins()] == ["hello"]


def test_下划线与点开头的目录被跳过(env):
    _mk(env, "ok", manifest=_good("ok"))
    _mk(env, "_hidden", manifest=_good("_hidden"))
    _mk(env, ".git", manifest=_good(".git"))
    assert [x["name"] for x in P.list_plugins()] == ["ok"]


def test_入口文件不存在时跳过(env):
    # 入口不存在 → 前端注入 404 脚本会静默失效，必须在扫描阶段就剔掉
    _mk(env, "hello", manifest=_good("hello"), entry="plugin.js")
    (env / "hello" / "plugin.js").unlink()
    assert P.list_plugins() == []


def test_入口路径不允许跳出目录(env):
    _mk(env, "hello", manifest=_good("hello", entry="../../evil.js"))
    assert P.list_plugins() == []


def test_标题缺失时用插件名兜底(env):
    m = _good("hello")
    m.pop("title")
    _mk(env, "hello", manifest=m)
    assert P.list_plugins()[0]["title"] == "hello"


# ── 启停 ──

def test_默认全部启用_状态文件不存在时(env):
    _mk(env, "a", manifest=_good("a"))
    _mk(env, "b", manifest=_good("b", mount="settings"))
    assert [x["name"] for x in P.load_enabled()] == ["a", "b"]
    assert not P.state_file().exists()


def test_关掉之后不出现在已启用清单里(env):
    _mk(env, "a", manifest=_good("a"))
    _mk(env, "b", manifest=_good("b", mount="settings"))
    P.set_enabled("a", False)
    assert [x["name"] for x in P.load_enabled()] == ["b"]
    # 设置页要能看到它，只是 enabled=false
    all_items = {x["name"]: x for x in P.list_plugins()}
    assert all_items["a"]["enabled"] is False
    assert all_items["b"]["enabled"] is True


def test_重新打开后回到已启用清单(env):
    _mk(env, "a", manifest=_good("a"))
    P.set_enabled("a", False)
    assert P.load_enabled() == []
    P.set_enabled("a", True)
    assert [x["name"] for x in P.load_enabled()] == ["a"]


def test_关一个不存在的插件报错而不是写脏记录(env):
    _mk(env, "a", manifest=_good("a"))
    with pytest.raises(ValueError):
        P.set_enabled("nope", False)
    assert not P.state_file().exists()


def test_插件名非法直接拒绝(env):
    _mk(env, "a", manifest=_good("a"))
    for bad in ["../etc", "a/b", "", "a b", None]:
        with pytest.raises(ValueError):
            P.set_enabled(bad, False)


def test_状态文件坏掉时按全部启用处理(env):
    _mk(env, "a", manifest=_good("a"))
    sf = P.state_file()
    sf.write_text("{ 坏掉的 json", encoding="utf-8")
    assert [x["name"] for x in P.load_enabled()] == ["a"]


def test_状态文件里的脏值被丢弃(env):
    _mk(env, "a", manifest=_good("a"))
    sf = P.state_file()
    sf.write_text(json.dumps({"disabled": ["a", "../evil", 123, None]}), encoding="utf-8")
    assert P.load_enabled() == []
    # 写回后只剩合法 id
    P.set_enabled("a", True)
    data = json.loads(sf.read_text(encoding="utf-8"))
    assert data["disabled"] == []


def test_状态文件原子写_不留下tmp文件(env):
    _mk(env, "a", manifest=_good("a"))
    P.set_enabled("a", False)
    left = list(P.state_file().parent.glob(".kb_plugins.json*"))
    assert [p.name for p in left] == [".kb_plugins.json"]


def test_单个插件扫描抛异常时其余插件照常返回(env, monkeypatch):
    """容错口径的核心：坏一个插件不许拖垮整份清单。

    2026-10-18 审查点出的真问题——原实现把 try 包在整个循环外面，
    任一插件抛非预期异常（入口名带 \\0、权限错、断掉的符号链接）
    就 `return []`，所有插件一起消失。这里直接让第二个插件的 is_dir()
    抛异常，断言第一个和第三个仍然返回。
    """
    _mk(env, "a", manifest=_good("a"))
    _mk(env, "b", manifest=_good("b"))
    _mk(env, "c", manifest=_good("c"))

    orig_is_dir = Path.is_dir

    def boom(self):
        if self.name == "b":
            raise OSError("模拟权限错误")
        return orig_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", boom)
    names = [x["name"] for x in P.list_plugins()]
    assert names == ["a", "c"]


def test_插件目录本身列不出来时返回空表而不是抛错(env, monkeypatch):
    _mk(env, "a", manifest=_good("a"))

    def boom(self, *a, **kw):
        raise OSError("模拟目录不可读")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert P.list_plugins() == []


def test_重复关同一个插件不会重复记录(env):
    _mk(env, "a", manifest=_good("a"))
    P.set_enabled("a", False)
    P.set_enabled("a", False)
    data = json.loads(P.state_file().read_text(encoding="utf-8"))
    assert data["disabled"] == ["a"]


def test_删掉状态文件等于恢复默认全启用(env):
    _mk(env, "a", manifest=_good("a"))
    P.set_enabled("a", False)
    P.state_file().unlink()
    assert [x["name"] for x in P.load_enabled()] == ["a"]


# ── 挂载点 ──

def test_只认三个约定挂载点():
    assert set(P.MOUNTS) == {"dash", "note-detail", "settings"}


def test_挂载点非法时插件被跳过(env):
    for bad in ["sidebar", "", "dash ", None]:
        _mk(env, "x", manifest=_good("x", mount=bad))
        assert P.list_plugins() == [], bad
        import shutil
        shutil.rmtree(env / "x")
