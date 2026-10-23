# -*- coding: utf-8 -*-
"""T12：项目主文档自动生成/更新（五段式骨架）。

锁死不变量：
    ①  新建项目（目录下无主文档）时，第一篇 type=project 笔记触发
        `<项目名>项目.md` 生成，frontmatter 齐全、正文五段顺序固定；
    ②  同项目再写 → 只改 `updated` 与「最近笔记」，其余四段一字不动；
    ③  60 秒内连写只刷一次（模块级时间戳聚合）；
    ④  `KB_PROJECT_DOC=0` 整体停用；
    ⑤  模板缺失时 POST /notes 仍 200/201（失败不阻断写入）；
    ⑥  存量保护：项目目录或父目录已有主文档 → 不生成、不改写。

隔离方式照抄 tests/test_templates_api.py（临时 KB_ROOT + patch 路径常量）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

_PATH_CONSTS = {
    "KB_ROOT": None,
    "NOTES_DIR": None,
    "TOKEN_FILE": None,
    "LOG_FILE": None,
    "SETTINGS_FILE": None,
    "INDEX_FILE": None,
    "EMBEDDINGS_FILE": None,
    "FAISS_FILE": None,
    "INDEX_MANIFEST_FILE": None,
    "MCP_USAGE_LOG": None,
}

TPL_DOC = """---
title: 项目主文档
description: 项目主文档骨架
schema_version: 1
---

# {{title}}

## 背景
一句话说明项目为什么建立。

## 当前状态
当前进展。

## 关键发现
踩过的坑。

## 最近笔记
- 项目下最近的笔记链接

## 后续建议
下一步要做什么。
"""


def _patch_paths_everywhere(tmp: Path, notes: Path):
    values = {
        "KB_ROOT": tmp,
        "NOTES_DIR": notes,
        "TOKEN_FILE": tmp / ".api_tokens.json",
        "LOG_FILE": tmp / ".audit_log.jsonl",
        "SETTINGS_FILE": tmp / ".kb_settings.json",
        "INDEX_FILE": tmp / ".search_index.json",
        "EMBEDDINGS_FILE": tmp / ".embeddings.npy",
        "FAISS_FILE": tmp / ".faiss.index",
        "INDEX_MANIFEST_FILE": tmp / ".index_manifest.json",
        "MCP_USAGE_LOG": tmp / ".kb_mcp_usage.jsonl",
    }
    patchers = []
    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "")
        if not name.startswith("kb_lib"):
            continue
        for key, val in values.items():
            if hasattr(mod, key):
                patchers.append(mock.patch.object(mod, key, val))
    for p in patchers:
        p.start()
    return patchers


@pytest.fixture
def env():
    """临时笔记库（含 _模板/项目主文档.md）+ 隔离 Flask 应用。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbprojdoc-"))
    notes = tmp / "笔记"
    notes.mkdir()
    tdir = notes / "_模板"
    tdir.mkdir()
    (tdir / "项目主文档.md").write_text(TPL_DOC, encoding="utf-8")

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S
    from kb_lib.services import project_doc as PD

    patchers = _patch_paths_everywhere(tmp, notes)
    patchers.append(mock.patch.object(PD, "_LAST_REFRESH", {}))
    try:
        AU._init_db()
        S.init_master()
        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield c, {"Authorization": f"Bearer {master}"}, notes, tdir
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _body(tag: str) -> str:
    return (
        f"# {tag}\n\n## 背景\n" + ("背景内容" * 300) + "\n\n"
        "## 关键发现\n- 发现\n\n## 实施步骤\n- 步骤\n\n"
        "## 验证结果\n- 结果\n\n## 后续建议\n- 建议\n"
    )


def _post(c, h, title, subdir, tag):
    return c.post("/notes", headers=h, json={
        "type": "project", "title": title, "subdir": subdir,
        "content": _body(tag), "tags": ["T12"],
    })


def test_新建项目自动生成五段主文档(env):
    c, h, notes, _ = env
    r = _post(c, h, "T12第一篇", "测试项目-T12", "第一篇")
    assert r.status_code == 201, r.data[:400]
    data = r.get_json()
    assert data["project_doc"] == "10-项目/测试项目-T12/测试项目-T12项目.md", data

    doc = notes / data["project_doc"]
    assert doc.is_file()
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("---")
    for key in ("type: project", "schema_version: 1", 'content_layer: "curated"',
                "tags: [项目]", "status: 进行中", "priority: 中",
                "created:", "updated:", 'id: "proj-'):
        assert key in text, (key, text[:400])
    sections = [ln for ln in text.split("\n") if ln.startswith("## ")]
    assert sections == ["## 背景", "## 当前状态", "## 关键发现", "## 最近笔记", "## 后续建议"], sections
    assert "[[10-项目/测试项目-T12/T12第一篇.md]]" in text


def test_同项目再写只动updated与最近笔记(env):
    c, h, notes, _ = env
    first = _post(c, h, "T12第一篇", "测试项目-T12", "第一篇")
    doc = notes / first.get_json()["project_doc"]
    before = doc.read_text(encoding="utf-8")

    # 清空聚合窗口，模拟 60 秒后再写
    from kb_lib.services import project_doc as PD
    PD._LAST_REFRESH.clear()
    doc.write_text(before.replace("updated: ", "updated: 2000-01-01 00:00 # ", 1),
                   encoding="utf-8")
    before = doc.read_text(encoding="utf-8")

    r2 = _post(c, h, "T12第二篇", "测试项目-T12", "第二篇")
    assert r2.status_code == 201, r2.data[:400]
    after = doc.read_text(encoding="utf-8")

    # 「最近笔记」多一条
    assert "[[10-项目/测试项目-T12/T12第二篇.md]]" in after
    assert "[[10-项目/测试项目-T12/T12第一篇.md]]" in after
    # updated 变了
    assert "updated: 2000-01-01 00:00" not in after

    # 「当前状态」段必须随项目改动变化（方案 T12 验收：写入后状态段变化），
    # 但只允许追加/刷新「最近更新」这一行，状态本身不许被机器改写。
    # 其余三段一字未变：逐段比对
    def seg(text):
        out, cur = {}, None
        for ln in text.split("\n"):
            if ln.startswith("## "):
                cur = ln.strip()
                out[cur] = []
            elif cur:
                out[cur].append(ln)
        return {k: "\n".join(v).strip() for k, v in out.items()}

    b, a = seg(before), seg(after)
    assert "- 最近更新：" in a["## 当前状态"], a["## 当前状态"]
    assert "状态：进行中" in a["## 当前状态"], a["## 当前状态"]
    assert b["## 当前状态"] != a["## 当前状态"]
    for name in ("## 背景", "## 关键发现", "## 后续建议"):
        assert b[name] == a[name], name
    assert "T12第二篇" not in a["## 背景"]


def test_最近笔记去重且最多10条(env):
    c, h, notes, _ = env
    from kb_lib.services import project_doc as PD
    first = _post(c, h, "T12第0篇", "测试项目-T12", "第0篇")
    doc = notes / first.get_json()["project_doc"]
    for i in range(1, 13):
        PD._LAST_REFRESH.clear()
        assert _post(c, h, f"T12第{i}篇", "测试项目-T12", f"第{i}篇").status_code == 201
    text = doc.read_text(encoding="utf-8")
    links = [ln for ln in text.split("\n") if ln.startswith("- [[10-项目/测试项目-T12/")]
    assert len(links) == 10, links
    assert len(set(links)) == 10, links


def test_60秒内连写只刷一次(env):
    c, h, notes, _ = env
    from kb_lib.services import project_doc as PD
    first = _post(c, h, "T12聚合0", "测试项目-聚合", "聚合0")
    doc = notes / first.get_json()["project_doc"]
    assert doc.is_file()
    # 首篇生成后窗口是空的；第一篇「改动刷新」记一次时间
    PD._LAST_REFRESH.clear()
    assert _post(c, h, "T12聚合1", "测试项目-聚合", "聚合1").status_code == 201
    snap = doc.read_text(encoding="utf-8")
    for i in (2, 3, 4):
        assert _post(c, h, f"T12聚合{i}", "测试项目-聚合", f"聚合{i}").status_code == 201
        assert doc.read_text(encoding="utf-8") == snap, f"第 {i} 篇不应触发刷新"
    assert "T12聚合4.md" not in snap


def test_窗口内被聚合的链接下次刷新一起补(env, monkeypatch):
    """60 秒窗口内写的那几篇不能丢链：下次刷新要一起补进「最近笔记」。"""
    c, h, notes, _ = env
    from kb_lib.services import project_doc as PD
    first = _post(c, h, "T12补链0", "测试项目-补链", "补链0")
    doc = notes / first.get_json()["project_doc"]
    PD._LAST_REFRESH.clear()
    assert _post(c, h, "T12补链1", "测试项目-补链", "补链1").status_code == 201
    for i in (2, 3):
        assert _post(c, h, f"T12补链{i}", "测试项目-补链", f"补链{i}").status_code == 201
    text = doc.read_text(encoding="utf-8")
    assert "T12补链2.md" not in text and "T12补链3.md" not in text
    # 窗口过期后（把间隔改成 0 模拟）再写一篇，之前被聚合的三篇必须补上
    monkeypatch.setattr(PD, "REFRESH_INTERVAL", 0.0)
    assert _post(c, h, "T12补链4", "测试项目-补链", "补链4").status_code == 201
    text = doc.read_text(encoding="utf-8")
    for i in (1, 2, 3, 4):
        assert f"[[10-项目/测试项目-补链/T12补链{i}.md]]" in text, (i, text)


def test_模板五段顺序不对时回落内置骨架(env):
    """骨架是契约：磁盘模板写坏了不能把功能整体关掉，回落到内置五段骨架。"""
    c, h, notes, tdir = env
    (tdir / "项目主文档.md").write_text(
        "---\ntitle: 项目主文档\n---\n\n# {{title}}\n\n## 背景\nx\n\n## 最近笔记\ny\n",
        encoding="utf-8",
    )
    r = _post(c, h, "T12错骨架", "测试项目-错骨架", "错骨架")
    assert r.status_code == 201, r.data[:400]
    doc = notes / "10-项目/测试项目-错骨架/测试项目-错骨架项目.md"
    assert doc.is_file(), "坏模板应回落内置骨架"
    got = [ln.strip() for ln in doc.read_text(encoding="utf-8").split("\n") if ln.strip().startswith("## ")]
    assert got == ["## 背景", "## 当前状态", "## 关键发现", "## 最近笔记", "## 后续建议"], got


def test_开关关闭时不生成(env, monkeypatch):
    c, h, notes, _ = env
    monkeypatch.setenv("KB_PROJECT_DOC", "0")
    r = _post(c, h, "T12关闭", "测试项目-关闭", "关闭")
    assert r.status_code == 201, r.data[:400]
    assert "project_doc" not in r.get_json()
    assert not (notes / "10-项目/测试项目-关闭/测试项目-关闭项目.md").exists()


def test_模板缺失时用内置骨架照常生成(env):
    """生产库笔记目录不进仓库，没有磁盘模板也必须能生成（否则功能永远不生效）。"""
    c, h, notes, tdir = env
    (tdir / "项目主文档.md").unlink()
    r = _post(c, h, "T12无模板", "测试项目-无模板", "无模板")
    assert r.status_code == 201, r.data[:400]
    doc = notes / "10-项目/测试项目-无模板/测试项目-无模板项目.md"
    assert doc.is_file(), "模板缺失时应回落到内置骨架，而不是静默跳过"
    text = doc.read_text(encoding="utf-8")
    # 五段骨架必须齐且顺序一致
    got = [ln.strip() for ln in text.split("\n") if ln.strip().startswith("## ")]
    assert got == ["## 背景", "## 当前状态", "## 关键发现", "## 最近笔记", "## 后续建议"], got
    # 笔记本身必须落盘
    assert (notes / "10-项目/测试项目-无模板/T12无模板.md").is_file()


def test_非project类型不生成也不刷新(env):
    """三个触发条件之一：type != project 时钩子完全不介入，已有主文档也不动。"""
    c, h, notes, _ = env
    proj = notes / "10-项目" / "类型闸门"
    proj.mkdir(parents=True)
    doc = proj / "类型闸门项目.md"
    original = "---\nid: \"gate\"\ntitle: \"类型闸门\"\ntype: project\nupdated: 2026-01-01 00:00\n---\n\n# 类型闸门\n\n## 背景\n手工。\n"
    doc.write_text(original, encoding="utf-8")

    # type=note 写进同一项目目录：既不新建主文档，也不刷新已有的
    r = c.post("/notes", headers=h, json={
        "type": "note", "title": "T12非项目", "subdir": "类型闸门",
        "content": _body("非项目"), "tags": ["T12"],
    })
    assert r.status_code in (200, 201), r.data[:400]
    assert "project_doc" not in r.get_json(), "type != project 不应返回 project_doc"
    assert doc.read_text(encoding="utf-8") == original, "type != project 不该刷新主文档"
    # 笔记本身照常落盘（type=note 走它自己的默认目录，不进 10-项目）
    assert list(notes.rglob("T12非项目.md")), "非项目笔记必须正常落盘"
    # 项目目录里不能凭空多出任何文件
    assert [x.name for x in proj.iterdir()] == ["类型闸门项目.md"]


def test_目录内同名普通笔记不被当成主文档(env):
    """用户手写的「与目录同名」普通笔记：不是主文档，一个字都不许改。"""
    c, h, notes, _ = env
    proj = notes / "10-项目" / "手写项目"
    proj.mkdir(parents=True)
    hand = proj / "手写项目.md"          # 与目录同名，但是普通笔记（无 type: project）
    hand_text = "# 手写项目\n\n我自己写的笔记，不是主文档。\n"
    hand.write_text(hand_text, encoding="utf-8")

    r = _post(c, h, "T12手写", "手写项目", "手写")
    assert r.status_code == 201, r.data[:400]
    # 手写笔记必须逐字不变
    assert hand.read_text(encoding="utf-8") == hand_text
    # 钩子应另建一份真正的主文档，而不是去改它
    assert (proj / "手写项目项目.md").is_file()
    assert "[[10-项目/手写项目/T12手写.md]]" in (proj / "手写项目项目.md").read_text(encoding="utf-8")


def test_目录内已有主文档时只刷updated不改正文(env):
    """项目目录里已有本模块生成的主文档：不再生成第二份，正文骨架一字不动。"""
    c, h, notes, _ = env
    old_dir = notes / "10-项目" / "存量项目"
    old_dir.mkdir(parents=True)
    legacy = old_dir / "存量项目项目.md"
    body_text = (
        "# 存量项目\n\n## 背景\n人工写的背景。\n\n## 当前状态\n- 状态：进行中\n"
        "\n## 关键发现\n人工写的发现。\n\n## 最近笔记\n\n## 后续建议\n人工写的建议。\n"
    )
    legacy_text = (
        "---\nid: \"proj-old\"\ntitle: \"存量项目\"\ntype: project\n"
        "tags: [项目]\ncreated: 2026-01-01\nupdated: 2026-01-01 00:00\n---\n\n" + body_text
    )
    legacy.write_text(legacy_text, encoding="utf-8")

    r = _post(c, h, "T12存量", "存量项目", "存量")
    assert r.status_code == 201, r.data[:400]
    after = legacy.read_text(encoding="utf-8")
    # 背景 / 关键发现 / 后续建议 逐字不动；状态段只多一行「最近更新」
    assert "人工写的背景。" in after and "人工写的发现。" in after and "人工写的建议。" in after
    assert "updated: 2026-01-01 00:00" not in after
    # 没有凭空多出第二份主文档
    assert sorted(x.name for x in old_dir.iterdir() if x.name.endswith("项目.md")) == ["存量项目项目.md"]

    # 再写一篇也不能凭空多文件
    PD = __import__("kb_lib.services.project_doc", fromlist=["x"])
    PD._LAST_REFRESH.clear()
    _post(c, h, "T12存量2", "存量项目", "存量2")
    assert sorted(x.name for x in old_dir.iterdir() if x.name.endswith("项目.md")) == ["存量项目项目.md"]


def test_存量项目平铺在根时只刷不动正文(env):
    """生产口径：19 份存量主文档平铺在 `10-项目/` 根，项目正文在子目录。"""
    c, h, notes, _ = env
    root = notes / "10-项目"
    root.mkdir(parents=True, exist_ok=True)
    flat = root / "示例集项目.md"
    flat.write_text(
        "---\nid: \"btc268\"\ntitle: \"示例\"\ntype: project\n"
        "created: 2026-01-01\nupdated: 2026-01-01 00:00\n---\n\n"
        "# 示例\n\n## 目标\n人工写的目标。\n\n## 当前状态\n人工写的状态。\n",
        encoding="utf-8",
    )
    proj = root / "示例集 走势监控"
    proj.mkdir(parents=True)

    # 存量项目不回补：即使目录里没主文档，也不许在根目录/子目录凭空新建
    _post(c, h, "T12存量A", "示例集 走势监控", "存量A")
    assert flat.read_text(encoding="utf-8") == (
        "---\nid: \"btc268\"\ntitle: \"示例\"\ntype: project\n"
        "created: 2026-01-01\nupdated: 2026-01-01 00:00\n---\n\n"
        "# 示例\n\n## 目标\n人工写的目标。\n\n## 当前状态\n人工写的状态。\n"
    ), "人工写的存量主文档一个字都不许改"
    assert sorted(x.name for x in proj.iterdir()) == ["T12存量A.md"]
    assert sorted(x.name for x in root.iterdir() if x.name.endswith("项目.md")) == ["示例集项目.md"]
    # 也不能因为「目录里没有主文档」就在目录内回补一份
    assert not (proj / "示例集 走势监控项目.md").exists(), "存量项目被回补了主文档"


def test_主文档自身写入不触发再生成(env):
    """落盘文件就是本项目主文档时，钩子既不生成也不刷新（只走正常覆盖写）。"""
    c, h, notes, _ = env
    first = _post(c, h, "T12第一篇", "测试项目-自写", "第一篇")
    doc = notes / first.get_json()["project_doc"]
    from kb_lib.services import project_doc as PD
    PD._LAST_REFRESH.clear()
    r = c.post("/notes", headers=h, json={
        "type": "project", "title": "测试项目-自写项目", "subdir": "测试项目-自写",
        "content": _body("自写"), "tags": ["T12"],
    })
    assert r.status_code in (200, 201), r.data[:400]
    assert "project_doc" not in r.get_json()
    text = doc.read_text(encoding="utf-8")
    # 钩子没介入：正文是本次提交的内容，不是骨架的「（待补充）」
    assert "（待补充）" not in text
    assert "[[10-项目/测试项目-自写/" not in text
