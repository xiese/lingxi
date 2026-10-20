# -*- coding: utf-8 -*-
"""第 7-2 项：模板插件的接口层测试（GET /api/templates、POST /notes 带 template）。

与 tests/test_templates.py 的分工：
    test_templates.py  → 纯服务层（加载、frontmatter 拆分、章节判定）
    本文件             → 真起 Flask 应用，走鉴权 + 路由 + 写盘全链路

锁死不变量：
    ①  GET /api/templates 返回 _模板/ 下的模板，含 name/body/sections；
        body 必须不含 frontmatter（前端 textarea 预填用）
    ②  _模板/ 不存在时返回内置「默认模板」，不报错
    ③  无 token → 401
    ④  POST /notes 带 template=某模板时，按该模板章节校验：
        缺模板里的章节 → 400，且 required_sections 是模板章节而非默认五节
    ⑤  不带 template 时行为不变：仍要求默认五节
    ⑥  写盘内容不含模板 frontmatter（后端自己重建 frontmatter）

隔离方式照抄 tests/test_links_resolve_batch.py。
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


def _patch_paths_everywhere(tmp: Path, notes: Path):
    values = {
        "KB_ROOT": tmp,
        "NOTES_DIR": notes,
        "TOKEN_FILE": tmp / ".api_tokens.json",
        "LOG_FILE": tmp / ".audit_log.jsonl",
        "SETTINGS_FILE": tmp / ".kb_settings.json",
        "INDEX_FILE": tmp / ".search_index.pkl",
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


TPL_PROJ = """---
id: "proj-{{timestamp}}"
title: ""
type: project
tags: []
---

# {{title}}

## 目标
一句话描述项目目标。

## 当前状态
- [ ] 里程碑 1

## 日志
- YYYY-MM-DD: 做了什么
"""


@pytest.fixture
def client():
    """临时笔记库（含 _模板/）+ 隔离 Flask 应用。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbtpl-"))
    notes = tmp / "笔记"
    notes.mkdir()
    tdir = notes / "_模板"
    tdir.mkdir()
    (tdir / "项目模板.md").write_text(TPL_PROJ, encoding="utf-8")

    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    # _patch_paths_everywhere 内部已经 start()，这里不能再 start（Python 3.12 会抛
    # RuntimeError: Patch is already started，3.10/3.11 只是碰巧没炸）
    # templates.py 在 import 时就把 TEMPLATES_DIR 定成了常量，需单独改
    from kb_lib.services import templates as T
    tpl_patcher = mock.patch.object(T, "TEMPLATES_DIR", tdir)
    tpl_patcher.start()
    patchers.append(tpl_patcher)

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


def test_列出模板_含body与章节(client):
    c, h, notes, _ = client
    r = c.get("/api/templates", headers=h)
    assert r.status_code == 200, r.data[:300]
    data = r.get_json()
    names = [x["name"] for x in data["templates"]]
    assert "项目模板" in names, names

    proj = next(x for x in data["templates"] if x["name"] == "项目模板")
    assert proj["sections"] == ["## 目标", "## 当前状态", "## 日志"]
    # 前端 textarea 预填用 body，必须已去掉 frontmatter 且首字符是 #
    assert proj["body"].startswith("#"), repr(proj["body"][:20])
    assert "type: project" not in proj["body"]


def test_无模板目录时返回内置默认(client):
    c, h, notes, tdir = client
    shutil.rmtree(tdir)
    r = c.get("/api/templates", headers=h)
    assert r.status_code == 200
    data = r.get_json()
    # 2026-10-19 T9：内置模板从 1 套扩到 5 套（默认/项目/会议/读书/日记）。
    # 本用例原意是「模板目录没了也还能拿到内置模板」，不是钉死数量。
    assert len(data["templates"]) >= 1
    assert all(t["builtin"] is True for t in data["templates"])
    default = next(t for t in data["templates"] if t["id"] == "builtin-default")
    assert "## 背景" in default["sections"]


def test_端到端_内置五套与字段结构(client):
    """T9 验收 1/5：GET 真走路由返回 5 套内置 + _模板/ 条目，且老字段 sections/body 仍在。"""
    c, h, notes, _ = client
    r = c.get("/api/templates", headers=h)
    assert r.status_code == 200
    ts = r.get_json()["templates"]

    builtin = [t for t in ts if t.get("builtin") is True]
    ids = {t["id"] for t in builtin}
    assert ids == {
        "builtin-default", "builtin-project", "builtin-meeting",
        "builtin-reading", "builtin-diary",
    }, ids

    # _模板/ 目录里的项目模板必须以 dir- 前缀出现
    assert any(t["id"].startswith("dir-") and t["name"] == "项目模板" for t in ts), [t["id"] for t in ts]

    # 老字段一个都不能少
    for t in ts:
        for k in ("name", "sections", "body", "builtin"):
            assert k in t, (t.get("id"), k, sorted(t))
        assert isinstance(t["sections"], list)


def test_端到端_模板目录不存在时仍返回内置五套(client):
    """T9 验收 2：模板目录没了也不能 500，内置必须还在。"""
    c, h, notes, tdir = client
    shutil.rmtree(tdir)
    r = c.get("/api/templates", headers=h)
    assert r.status_code == 200
    builtin = [t for t in r.get_json()["templates"] if t.get("builtin") is True]
    assert len(builtin) == 5, [t["id"] for t in builtin]


def test_端到端_保存自定义模板后可列出且可删除(client):
    """T9 验收：模板管理增删改。POST 保存自定义 → GET 能看到 → DELETE 能删。"""
    c, h, notes, _ = client
    body = "# 我的模板T9\n\n## 一\n\n## 二\n"
    r = c.post("/api/templates", headers=h,
               json={"id": "t9-custom", "name": "T9自定义", "body": body})
    assert r.status_code == 201, r.data[:300]

    ts = c.get("/api/templates", headers=h).get_json()["templates"]
    got = next((t for t in ts if t["id"] == "t9-custom"), None)
    assert got is not None, [t["id"] for t in ts]
    assert got["name"] == "T9自定义"
    assert got["sections"] == ["## 一", "## 二"], got["sections"]

    d = c.delete("/api/templates/t9-custom", headers=h)
    assert d.status_code == 200, d.data[:300]
    ts2 = c.get("/api/templates", headers=h).get_json()["templates"]
    assert all(t["id"] != "t9-custom" for t in ts2)


def test_端到端_内置模板不可覆盖不可删除(client):
    """T9 验收：内置受保护。"""
    c, h, notes, _ = client
    r = c.post("/api/templates", headers=h,
               json={"id": "builtin-default", "name": "x", "body": "# x\n\n## 一\n"})
    assert r.status_code == 400, r.data[:300]
    d = c.delete("/api/templates/builtin-default", headers=h)
    assert d.status_code == 400, d.data[:300]


def test_端到端_不许占用目录模板的id(client):
    """T9：dir-* id 属于 _模板/ 目录，不能被自定义模板顶掉（顶掉会导致同 id 两条、原条目取不到）。"""
    c, h, notes, _ = client
    ts = c.get("/api/templates", headers=h).get_json()["templates"]
    dir_id = next(t["id"] for t in ts if t["id"].startswith("dir-"))
    r = c.post("/api/templates", headers=h,
               json={"id": dir_id, "name": "冒名", "body": "# x\n\n## 一\n"})
    assert r.status_code == 400, r.data[:300]
    # 原目录模板还在，且没有同 id 的第二条
    ts2 = c.get("/api/templates", headers=h).get_json()["templates"]
    same = [t for t in ts2 if t["id"] == dir_id]
    assert len(same) == 1, same
    assert same[0]["name"] == "项目模板"


def test_无token列模板401(client):
    c, h, notes, _ = client
    assert c.get("/api/templates").status_code == 401


def test_按模板章节校验_缺节400(client):
    c, h, notes, _ = client
    body = "# 测试项目\n\n" + "内容" * 500 + "\n\n## 目标\n做点事\n\n## 当前状态\n进行中\n"
    r = c.post("/notes", headers=h, json={
        "type": "project", "title": "模板校验测试", "content": body,
        "template": "项目模板",
    })
    assert r.status_code == 400, r.data[:300]
    err = r.get_json()
    assert err["required_sections"] == ["## 目标", "## 当前状态", "## 日志"]
    assert any("## 日志" in p for p in err["problems"])


def test_不带模板仍要求默认五节(client):
    c, h, notes, _ = client
    body = "# 测试\n\n" + "内容" * 500 + "\n\n## 目标\n做点事\n"
    r = c.post("/notes", headers=h, json={
        "type": "project", "title": "默认口径测试", "content": body,
    })
    assert r.status_code == 400
    err = r.get_json()
    assert "## 背景" in err["required_sections"]


def test_模板章节齐了能建成_且不含模板frontmatter(client):
    c, h, notes, _ = client
    body = ("# 模板落盘测试\n\n" + "内容" * 500 +
            "\n\n## 目标\n做点事\n\n## 当前状态\n进行中\n\n## 日志\n- 2026-10-18: 建了\n")
    r = c.post("/notes", headers=h, json={
        "type": "project", "title": "模板落盘测试", "content": body,
        "template": "项目模板",
    })
    assert r.status_code == 201, r.data[:400]
    path = r.get_json()["path"]
    text = (notes / path).read_text(encoding="utf-8")
    # 只应有一份 frontmatter（后端重建的那份）
    assert text.count("\n---\n") >= 1
    assert text.startswith("---")
    assert "proj-{{timestamp}}" not in text, "模板占位符被写进正文"
    assert text.count("type: project") == 1, text[:400]
    # 章节都落进去了
    for sec in ("## 目标", "## 当前状态", "## 日志"):
        assert sec in text


def test_客户端把模板原文含frontmatter整篇送上来_只留一份frontmatter(client):
    """真实场景：前端直接把模板文件全文（--- 开头）塞进 textarea 提交。

    后端必须剥掉模板自带的那份 frontmatter，否则文件里会出现两个 --- 块，
    YAML 解析直接坏掉；{{timestamp}} 也必须替换掉，不能原样落进正文。
    """
    c, h, notes, _ = client
    tpl_text = (
        "---\n"
        'id: "proj-{{timestamp}}"\n'
        'title: ""\n'
        "type: project\n"
        "tags: []\n"
        "---\n\n"
        "# {{title}}\n\n"
        "## 目标\n做点事\n\n"
        "## 当前状态\n进行中\n\n"
        "## 日志\n- 2026-10-18: 建了\n\n"
        + "内容" * 500
    )
    r = c.post("/notes", headers=h, json={
        "type": "project", "title": "整篇模板测试", "content": tpl_text,
        "template": "项目模板",
    })
    assert r.status_code == 201, r.data[:400]
    path = r.get_json()["path"]
    text = (notes / path).read_text(encoding="utf-8")
    # 文件里只能有一份 frontmatter（开头那段）
    assert text.startswith("---")
    assert text.count("\n---\n") == 1, f"出现多份 frontmatter：{text[:500]}"
    # 模板里的占位符与重复 title 字段不能漏进正文
    assert "{{timestamp}}" not in text
    assert "proj-{{timestamp}}" not in text
    # 正文里的 {{title}} 应被替换成真实标题
    assert "{{title}}" not in text
    assert "整篇模板测试" in text


def test_模板名不存在时400而不是静默回退(client):
    """选了个不存在的模板名，必须明确 400。

    静默回退默认五节的话，前端显示「已选模板 X」、后端按别的章节校验，
    用户看到的报错会跟实际原因对不上。
    """
    c, h, notes, _ = client
    body = "# 随便\n\n" + "内容" * 500 + "\n\n## 目标\n做点事\n"
    r = c.post("/notes", headers=h, json={
        "type": "project", "title": "非法模板测试", "content": body,
        "template": "根本不存在的模板",
    })
    assert r.status_code == 400
    assert "模板不存在" in r.get_json()["error"]
