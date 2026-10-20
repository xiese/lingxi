"""安全与回归不变量测试。

鉴权分级、`degraded` 契约、限流 429、「每个写入口必须过闸门」等跨端点不变量
在早期版本里零覆盖，缺陷可以长期绿着存在。本文件把这些不变量钉住，
防止后续改动把已修的边界悄悄退回去。

分组：
  A 鉴权与权限分级
  B degraded 降级契约
  C 限流 429
  D 写闸门跨端点不变量
  E 控制字符 / frontmatter 注入
  F JSON 体类型混淆
  G 目录当文件路径
  H parse_fm 三种 tags 写法
  I 请求体大小上限
  J sync.sh 不再把正文拼进 python -c
  K SSE 空令牌表拒绝启动
  L /files 危险类型强制附件
  M 增量刷新持锁组装
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:  # pragma: no cover
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *a, **k: (lambda fn: fn)
    stub.raises = None
    sys.modules["pytest"] = stub

import pytest  # noqa: E402

_LONG = "这是用于绕过质量门禁的正文内容，需要超过八百字符。" * 40

# 需要在 fixture 里改写的路径类常量（各 kb_lib 模块在 import 时绑了这些名字，
# 只 patch config 不够——routes_files / routes_llm 等模块持有自己的引用）。
_PATH_CONSTS = {
    "KB_ROOT": None,          # 占位，运行时填
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
    """把 tmp 下的路径常量 patch 进**所有已加载**的 kb_lib 模块。

    为什么不能只 patch config：各路由模块是 `from ..config import KB_ROOT`
    的写法，import 时就把值绑到了自己的命名空间，之后改 config 不影响它们。
    """
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


@pytest.fixture
def client():
    """用临时笔记库起一个隔离的 Flask 应用（不碰生产数据）。"""
    tmp = Path(tempfile.mkdtemp(prefix="kbsec-"))
    notes = tmp / "笔记"
    notes.mkdir()

    # 先 import 齐所有子模块，再统一 patch 路径常量
    from kb_lib import create_app  # noqa: F401  触发全部路由模块 import
    import kb_lib.audit as AU
    import kb_lib.store as S

    patchers = _patch_paths_everywhere(tmp, notes)
    try:
        AU._init_db()          # 在新库里建审计表，避免 warning 噪音
        S.init_master()        # 在隔离目录里生成 master token

        app = create_app()
        app.config["TESTING"] = True
        c = app.test_client()
        master = json.loads((tmp / ".api_tokens.json").read_text(encoding="utf-8"))["master"]["token"]
        yield c, {"Authorization": f"Bearer {master}"}, notes, tmp
    finally:
        for p in reversed(patchers):
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _issue_device_token(tmp: Path, permission: str) -> str:
    """在隔离库里签发一枚设备令牌，返回明文。

    C2 起 `store["tokens"]` 的键是 sha256(pepper + 明文)，不能再拿明文当键写，
    否则 authenticate() 算哈希后查不到（会变成假红）。
    """
    import kb_lib.store as S

    tk = f"dev-{permission}"
    with S.store_transaction() as s:
        rec = {
            "name": f"dev-{permission}",
            "permission": permission,
            "device": "pytest",
            "created": "2026-10-05T00:00:00",
            "expires_at": "2099-01-01T00:00:00",
            "status": "active",
        }
        S.token_record(tk, rec)
        s["tokens"][S.token_hash(tk)] = rec
    return tk


# ────────────────────────── A 鉴权与权限分级 ──────────────────────────


def test_A1_无令牌访问受保护接口不得放行(client):
    """check_auth 未认证回 401；require_master 未认证回 403（既有行为）。"""
    c, _, _, _ = client
    expect_401 = ["/notes", "/dashboard", "/api/dashboard", "/api/tags"]
    expect_403 = ["/admin/settings", "/auth/sessions", "/admin/debug-auth"]
    for path in expect_401:
        assert c.get(path).status_code == 401, f"GET {path} 未认证应 401"
    for path in expect_403:
        assert c.get(path).status_code == 403, f"GET {path} 未认证应 403"


def test_A2_只读令牌不能写(client):
    c, _, _, tmp = client
    h = {"Authorization": f"Bearer {_issue_device_token(tmp, 'r')}"}
    cases = [
        ("post", "/notes", {"title": "x", "content": "y"}),
        ("post", "/api/notes/batch", {"paths": ["a.md"], "action": "tag", "tags": ["t"]}),
        ("post", "/api/smart-upload", {"text": "hello"}),
        ("delete", "/notes/a.md", None),
    ]
    for method, path, body in cases:
        r = getattr(c, method)(path, json=body, headers=h)
        assert r.status_code == 403, f"{method.upper()} {path} 只读令牌应 403，实得 {r.status_code}"
    # /upload 是 multipart，单独发
    r = c.post("/upload", data={"file": (io.BytesIO(b"# s\n"), "s.md")},
               headers=h, content_type="multipart/form-data")
    assert r.status_code == 403, f"POST /upload 只读令牌应 403，实得 {r.status_code}"


def test_A3_设备令牌不能碰管理接口(client):
    c, _, _, tmp = client
    for perm in ("r", "rw"):
        h = {"Authorization": f"Bearer {_issue_device_token(tmp, perm)}"}
        assert c.get("/admin/settings", headers=h).status_code == 403
        assert c.get("/auth/sessions", headers=h).status_code == 403


def test_A4_主令牌可写(client):
    c, h, _, _ = client
    r = c.post("/notes", json={"title": "主令牌写入", "type": "knowledge",
                               "content": _LONG, "quality_check": False,
                               "subdir": "20-知识"}, headers=h)
    assert r.status_code == 201, r.get_data(as_text=True)[:200]


def test_A5_非ASCII令牌不炸且判定正确(client):
    """中文/混合令牌走 is_master 与全链路鉴权都不得抛 TypeError（2026-10-17 生产事故：
    compare_digest 只吃 ASCII，外部请求带中文令牌即 500）。"""
    from kb_lib.store import is_master
    # 单元面：真 master（ASCII）对非 ASCII 令牌常量时间比较不得抛异常
    assert not is_master("中文令牌测试")
    assert not is_master("token中英混合abc")
    # 请求面：带中文令牌打受保护接口应得 401（认证失败），而不是 500
    c, h, _, tmp = client
    r = c.get("/notes", headers={"Authorization": "Bearer 中文令牌测试"})
    assert r.status_code == 401, f"中文令牌应 401，实得 {r.status_code}"
    r = c.get("/notes", headers={"Authorization": "Bearer token中英混合abc"})
    assert r.status_code == 401, f"混合令牌应 401，实得 {r.status_code}"
    # 正确性回归：ASCII 真令牌仍放行
    assert c.get("/notes", headers=h).status_code == 200


def test_A6_中文主令牌可认证(client):
    """master 本身就是中文时也要能正常认证。save_store() 有 master 保护
    （写回时用磁盘值还原 master），所以这里直接改令牌文件再刷缓存。"""
    import kb_lib.store as S
    c, _, _, tmp = client
    (tmp / ".api_tokens.json").write_text(
        json.dumps({"master": {"token": "主令牌测试中文九八七", "name": "master",
                               "created": "2026-10-17T00:00:00", "note": "pytest 中文主令牌"},
                    "tokens": {}, "requests": {}}, ensure_ascii=False),
        encoding="utf-8")
    S._invalidate_cache()
    h = {"Authorization": "Bearer 主令牌测试中文九八七"}
    r = c.get("/notes", headers=h)
    assert r.status_code == 200, f"中文主令牌应放行，实得 {r.status_code}"
    # 错误的中文令牌仍拒绝
    r = c.get("/notes", headers={"Authorization": "Bearer 主令牌测试中文九八八"})
    assert r.status_code == 401


# ────────────────────────── B degraded 降级契约 ──────────────────────────


def test_B1_向量面异常检索层转为降级而非抛出():
    """search.py:1299 把向量面任意异常转成 SearchUnavailable。"""
    import kb_lib.search as S

    si = S.search_index
    si.paths, si.contents = ["a.md"], ["甲内容"]
    si.embeddings = None
    si.bm25 = None

    def _boom(*a, **kw):
        raise OSError("模拟嵌入服务不可达")

    with mock.patch.object(S, "get_setting",
                           lambda k, d=None: "llm" if k == "embedding_type" else d), \
         mock.patch.object(si, "is_vector_search_ready", lambda: True), \
         mock.patch.object(si, "_vec_search", _boom):
        results, degraded = si.search("测试", 5, with_status=True)
    assert degraded, f"向量面异常未转为降级: results={results}"
    assert results == [], "降级时不应返回结果"


def test_B2_端点层透传degraded且不变成500(client):
    c, h, _, _ = client
    import kb_lib.routes.routes_search as RS

    with mock.patch.object(RS.search_index, "search",
                           lambda *a, **k: ([], "向量检索不可用: 模拟")):
        r = c.get("/api/search/semantic?q=测试", headers=h)
    assert r.status_code == 200, f"降级不应 500，实得 {r.status_code}"
    body = r.get_json()
    assert body.get("degraded") is True, f"未标记 degraded: {body}"
    assert body.get("reason"), "降级时必须给出 reason"


def test_B3_问答上下文检索不可用时如实标注():
    import kb_lib.routes as R

    with mock.patch.object(R.search_index, "search",
                           lambda *a, **k: ([], "检索不可用")):
        ctx, sources = R.build_qa_context("测试", top=3)
    assert "检索不可用" in ctx, f"未如实标注: {ctx[:80]!r}"


# ────────────────────────── C 限流 429 ──────────────────────────


def test_C_匿名申请端点有IP限流(client):
    """/auth/request 是匿名端点，必须限流（routes_auth 每 IP 5 次/5 分钟）。"""
    c, _, _, _ = client
    codes = [c.post("/auth/request", json={"name": "spam", "permission": "r"}).status_code
             for _ in range(8)]
    assert 429 in codes, f"连发 8 次未出现 429：{codes}"
    assert codes[-1] == 429, f"最后一次应为 429：{codes}"


# ────────────────────────── D 写闸门跨端点不变量 ──────────────────────────


@pytest.mark.parametrize("label,smart_subdir,note_extra,expect", [
    # 注意：两个入口的 subdir 语义不同 —— smart-upload 以笔记库为根，
    # /notes 是追加到 type 目录之后。所以「同一类违规结构」要用各自等价
    # 的构造来表达，不能简单复用同一个字符串（这正是写闸门不一致的根源之一）。
    ("档案根", "50-档案", {"type": "archive", "subdir": ""}, 400),
    ("嵌套顶层", "20-知识/10-项目/x",
     {"type": "knowledge", "subdir": "10-项目/x"}, 400),
])
def test_D_smart_upload与notes闸门一致(client, label, smart_subdir, note_extra, expect):
    """两个写入口必须对同一类违规结构给出一致裁决。"""
    c, h, notes, _ = client
    smart = c.post("/api/smart-upload", json={
        "text": _LONG, "title": "闸门探针", "subdir": smart_subdir,
        "type": "knowledge", "tags": ["t"],
    }, headers=h)
    note = c.post("/notes", json={
        "title": "闸门探针", "content": _LONG, "quality_check": False,
        **note_extra,
    }, headers=h)
    assert smart.status_code == expect, \
        f"smart-upload [{label}] 应 {expect}，实得 {smart.status_code} {smart.get_json()}"
    assert note.status_code == expect, \
        f"/notes [{label}] 应 {expect}，实得 {note.status_code} {note.get_json()}"


def test_D0_用户私有目录_smart_upload必须被拦(client):
    """smart-upload 的 subdir 以笔记库为根，70-经验 只能靠 USER_OWNED 闸门拦。"""
    c, h, notes, _ = client
    r = c.post("/api/smart-upload", json={
        "text": _LONG, "title": "私有目录探针", "subdir": "70-经验",
        "type": "knowledge", "tags": ["t"],
    }, headers=h)
    assert r.status_code == 403, f"应被 USER_OWNED 拦下，实得 {r.status_code} {r.get_json()}"
    assert r.get_json().get("error") == "user_owned_dir_forbidden"
    assert not (notes / "70-经验").exists(), "私有目录被创建了"


def test_D2_owner_ack是唯一的人工放行通道(client):
    c, h, notes, _ = client
    r = c.post("/api/smart-upload", json={
        "text": _LONG, "title": "人工写入", "subdir": "70-经验",
        "type": "knowledge", "tags": ["t"], "owner_ack": "user",
    }, headers=h)
    assert r.status_code == 201, "带 owner_ack=user 应允许写入用户私有目录"
    assert (notes / "70-经验").is_dir()


def test_D3_合法路径不受闸门影响(client):
    c, h, _, _ = client
    for subdir in ("20-知识/运维", "30-写作", "10-项目/知识库系统", "40-日记/2026/10"):
        r = c.post("/api/smart-upload", json={
            "text": _LONG, "title": f"正常_{subdir.replace('/', '_')}", "subdir": subdir,
            "type": "knowledge", "tags": ["t"],
        }, headers=h)
        assert r.status_code == 201, f"合法 subdir={subdir} 被误伤: {r.get_json()}"


# ────────────────────────── E 控制字符 / frontmatter 注入 ──────────────────────────


def test_E1_sanitize_title清掉控制字符():
    from kb_lib.auth import sanitize_title

    for raw in ("a\rb", "a\nb", "a\tb", "a\r\nb", "a b", "a\x00b"):
        got = sanitize_title(raw)
        assert not any(ch in got for ch in "\r\n\t\x00"), \
            f"{raw!r} → {got!r} 仍含控制字符"


def test_E2_quote_yaml把换行转成字面量():
    from kb_lib.routes import _quote_yaml

    out = _quote_yaml("a\nb\rc\td")
    assert not any(ch in out for ch in "\n\r\t"), f"仍有真实控制字符: {out!r}"
    assert "\\n" in out, f"未转义成字面量: {out!r}"


def test_E3_标题含CR不得注入frontmatter(client):
    c, h, notes, _ = client
    r = c.post("/notes", json={
        "title": "标题甲\rstatus: approved\rx_injected: pwned",
        "type": "knowledge", "content": _LONG, "quality_check": False,
        "subdir": "注入目录",
    }, headers=h)
    assert r.status_code == 201, r.get_data(as_text=True)[:200]
    fp = notes / "20-知识" / "注入目录" / r.get_json()["path"].split("/")[-1]
    raw = fp.read_text(encoding="utf-8")
    assert not any(ch in fp.name for ch in "\r\n\t"), f"文件名带控制符: {fp.name!r}"
    assert "\r" not in raw, "文件里仍残留 CR"
    injected = [ln for ln in raw.splitlines()
                if ln.startswith(("status: approved", "x_injected:"))]
    assert not injected, f"frontmatter 被注入: {injected}"
    keys = {ln.split(":")[0] for ln in raw.splitlines()
            if ln and not ln.startswith(("---", "#", "[")) and ":" in ln}
    allowed = {
        "id", "title", "type", "schema_version", "content_layer", "tags",
        "canonical_tags", "aliases", "relations", "compiled_from", "created",
        "updated", "status", "last_edited_by", "last_edit_source_type",
        "priority", "source_client", "uploaded_by", "token_name", "platform",
        "project_scope", "source_type",
    }
    assert keys <= allowed, f"frontmatter 出现约定外字段: {sorted(keys - allowed)}"


def test_E4_relations含换行不得注入(client):
    c, h, notes, _ = client
    r = c.post("/api/smart-upload", json={
        "text": _LONG, "title": "关系注入", "subdir": "20-知识",
        "type": "knowledge", "tags": ["t"],
        "relations": ["甲]\nid: evil-injected\nstatus: approved"],
    }, headers=h)
    assert r.status_code == 201
    fp = notes / "20-知识" / r.get_json()["path"].split("/")[-1]
    raw = fp.read_text(encoding="utf-8")
    injected = [ln for ln in raw.splitlines()
                if ln.startswith(("id: evil", "status: approved"))]
    assert not injected, f"relations 注入成功: {injected}"


def test_E5_batch标签不得注入frontmatter(client):
    c, h, notes, _ = client
    (notes / "20-知识").mkdir(parents=True, exist_ok=True)
    target = notes / "20-知识" / "被注入.md"
    target.write_text("---\ntitle: 被注入\ntags: [旧]\n---\n\n正文", encoding="utf-8")
    r = c.post("/api/notes/batch", json={
        "paths": ["20-知识/被注入.md"], "action": "tag",
        "tags": ['a"', '"b', "c]\nid: evil2"],
    }, headers=h)
    assert r.status_code == 200
    lines = target.read_text(encoding="utf-8").splitlines()
    bad = [ln for ln in lines if ln.startswith("id:") and "kb-" not in ln]
    assert not bad, f"batch tags 注入了 id 字段: {bad}"


def test_E6_rewrite_tags_block剔除危险字符():
    from kb_lib.services.tags import rewrite_tags_block

    out, changed = rewrite_tags_block(
        "---\ntitle: t\ntags: [旧]\n---\n\n正文",
        ['a"', '"b', "c]\nid: evil", "正常", "ok-tag"],
    )
    assert changed, "应发生替换"
    tags_line = next(ln for ln in out.splitlines() if ln.startswith("tags:"))
    assert tags_line.count("[") == 1 and tags_line.count("]") == 1, \
        f"tags 行结构被破坏: {tags_line!r}"
    assert "\n" not in tags_line, "tags 行被换行撑成多行"
    kept = [x.strip() for x in tags_line[len("tags: ["):-1].split(",")]
    assert "正常" in kept and "ok-tag" in kept, f"正常标签被误丢: {kept}"
    assert not any("id" in x or "evil" in x or "]" in x for x in kept), \
        f"危险标签未被整条丢弃: {kept}"


# ────────────────────────── F JSON 体类型混淆 ──────────────────────────


@pytest.mark.parametrize("body", ['[1,2]', '"str"', '123', 'true'])
def test_F_匿名端点非dict请求体不得500(client, body):
    """get_json(force=True) or {} 对 list/str/number/bool 原样返回 → 500。"""
    c, _, _, _ = client
    r = c.post("/auth/request", data=body, content_type="application/json")
    assert r.status_code != 500, f"body={body} 触发了 500"


def test_F2_需令牌端点同样不得500(client):
    c, h, _, _ = client
    for body in ('[1,2]', '"str"', '123', 'true'):
        r = c.post("/notes", data=body, content_type="application/json", headers=h)
        assert r.status_code != 500, f"POST /notes body={body} 触发 500"


def test_F3_合法JSON仍可解析(client):
    c, h, _, _ = client
    r = c.post("/notes", json={"title": "t", "type": "knowledge", "content": _LONG,
                               "quality_check": False, "subdir": "20-知识"}, headers=h)
    assert r.status_code == 201, "合法 JSON 被误伤"


# ────────────────────────── G 目录当文件路径 ──────────────────────────


def test_G1_目录路径返回400而非500(client):
    c, h, notes, _ = client
    (notes / "10-项目" / "proj").mkdir(parents=True, exist_ok=True)
    (notes / "10-项目" / "proj" / "子.md").write_text("# 子\n", encoding="utf-8")
    for path in ("10-项目", "10-项目/proj"):
        r = c.get(f"/notes/{path}", headers=h)
        assert r.status_code == 400, f"GET /notes/{path} 应 400，实得 {r.status_code}"
        r = c.get(f"/files/笔记/{path}", headers=h)
        assert r.status_code == 400, f"GET /files/笔记/{path} 应 400，实得 {r.status_code}"
        r = c.get(f"/notes/{path}", headers=h)
        assert r.status_code == 400


def test_G2_删除目录不会把整棵搬进档案(client):
    c, h, notes, _ = client
    proj = notes / "10-项目" / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "子.md").write_text("# 子\n", encoding="utf-8")
    r = c.delete("/notes/10-项目/proj", headers=h)
    assert r.status_code == 400, f"DELETE 目录应 400，实得 {r.status_code}"
    assert proj.is_dir(), "目录被搬走了"
    assert not (notes / "50-档案" / "proj").exists(), "整棵目录被搬进了档案"


# ────────────────────────── H parse_fm 三种 tags 写法 ──────────────────────────


@pytest.mark.parametrize("fm,expect", [
    ("tags: [运营, 部署]", ["运营", "部署"]),
    ("tags: 运营, 部署", ["运营", "部署"]),
    ("tags:\n  - 运营\n  - 部署", ["运营", "部署"]),
])
def test_H_三种tags写法统一返回list(fm, expect):
    from kb_lib.auth import parse_fm

    meta, _ = parse_fm(f"---\ntitle: t\n{fm}\n---\n\n正文")
    assert isinstance(meta.get("tags"), list), f"{fm!r} → {meta.get('tags')!r} 不是 list"
    assert meta["tags"] == expect, f"{fm!r} → {meta.get('tags')!r}"


def test_H2_标签云不再把无括号tags切成单字(client):
    c, h, notes, _ = client
    (notes / "20-知识").mkdir(parents=True, exist_ok=True)
    (notes / "20-知识" / "nobracket.md").write_text(
        "---\ntitle: nb\ntags: 运营, 部署\n---\n\n正文", encoding="utf-8")
    names = [x["name"] for x in c.get("/api/tags", headers=h).get_json()["tags"]]
    assert "运营" in names and "部署" in names, f"标签云未收录完整标签: {names}"
    assert "运" not in names and "营" not in names, f"标签仍被逐字符切碎: {names}"


# ────────────────────────── I 请求体大小上限 ──────────────────────────


def test_I_超大请求体返回413(client):
    c, h, _, _ = client
    from kb_lib import create_app

    limit = create_app().config["MAX_CONTENT_LENGTH"]
    assert limit and limit > 0, "未设置 MAX_CONTENT_LENGTH"
    big = b"x" * (limit + 1024)
    r = c.post("/upload", data={"file": (io.BytesIO(big), "big.md")},
               headers=h, content_type="multipart/form-data")
    assert r.status_code == 413, f"超限请求应 413，实得 {r.status_code}"
    assert r.get_json().get("error") == "payload_too_large"


def test_I2_正常小文件不受影响(client):
    c, h, _, _ = client
    r = c.post("/upload", data={"file": (io.BytesIO(b"# s\n"), "small.md")},
               headers=h, content_type="multipart/form-data")
    assert r.status_code == 201, f"小文件被误伤: {r.status_code} {r.get_data(as_text=True)[:120]}"


# ────────────────────────── J sync.sh 不再把正文拼进 python -c ──────────────────────────


def _sync_sh_snippet() -> str:
    """从 sync.sh 里实抽那段 python，保证测的就是产品代码（不会漂移）。"""
    src = (ROOT / "scripts" / "sync.sh").read_text(encoding="utf-8")
    m = re.search(r"<<'PY'[^\n]*\n(.*?)\nPY\n", src, re.S)
    assert m, "sync.sh 里找不到 python heredoc"
    return m.group(1)


def _run_sync_snippet(fm_head: str):
    import subprocess

    return subprocess.run(
        [sys.executable, "-", "knowledge", "t", "b", "p", fm_head],
        input=_sync_sh_snippet(), capture_output=True, text=True, timeout=30,
    )


def test_J1_sync_sh不得再用三引号拼源码():
    """RCE 特征是把外部变量拼进三引号字符串。"""
    src = (ROOT / "scripts" / "sync.sh").read_text(encoding="utf-8")
    assert "'''" not in src, "sync.sh 仍存在三引号拼接（RCE 特征）"
    assert "sys.argv" in src, "sync.sh 未改为经 argv 传值"
    # 其余 python3 -c 必须都是 stdin 传入、不拼外部变量
    for line in src.splitlines():
        if "python3 -c" not in line:
            continue
        body = line.split("python3 -c", 1)[1]
        assert "$" not in body, f"该行把 shell 变量拼进了 python 源码: {line.strip()}"


def test_J2_恶意tags行不再执行命令():
    marker_dir = Path(tempfile.mkdtemp())
    marker = marker_dir / "pwned"
    payload = f"__import__('os').system('touch {marker}')"
    try:
        out = _run_sync_snippet(f"tags: {payload}")
        assert not marker.exists(), "恶意 tags 行仍被执行（RCE 复现）"
        assert out.returncode == 0, out.stderr[:200]
        data = json.loads(out.stdout)
        assert data["tags"] == [payload], f"恶意串应被当普通标签: {data['tags']!r}"
    finally:
        shutil.rmtree(marker_dir, ignore_errors=True)


def test_J3_正常中文tags可正确解析():
    out = _run_sync_snippet('tags: [标签甲, 标签乙, "丙"]')
    assert json.loads(out.stdout)["tags"] == ["标签甲", "标签乙", "丙"]


def test_J4_块序列tags也能解析():
    """原先只 grep `^tags:` 一行，块序列写法的标签永远同步不过去。"""
    out = _run_sync_snippet("tags:\n  - 丁一\n  - 丁二")
    assert json.loads(out.stdout)["tags"] == ["丁一", "丁二"]


def test_J5_无括号tags也能解析():
    out = _run_sync_snippet("tags: 运营, 部署")
    assert json.loads(out.stdout)["tags"] == ["运营", "部署"]


# ────────────────────────── K SSE 空令牌表拒绝启动 ──────────────────────────


def test_K1_空令牌表时build_mcp_auth必须抛错():
    """原先返回 None → kb_mcp_sse 无鉴权裸奔启动。"""
    import kb_mcp_server as M

    absent = Path(tempfile.mkdtemp()) / "absent.json"
    with mock.patch.object(M, "TOKEN_FILE", absent), \
         mock.patch.dict(os.environ, {"KB_MCP_SSE_AUTH": "1"}):
        with pytest.raises(RuntimeError):
            M.build_mcp_auth()


def test_K2_显式关闭仍返回None():
    import kb_mcp_server as M

    absent = Path(tempfile.mkdtemp()) / "absent.json"
    with mock.patch.object(M, "TOKEN_FILE", absent), \
         mock.patch.dict(os.environ, {"KB_MCP_SSE_AUTH": "0"}):
        assert M.build_mcp_auth() is None, "显式 KB_MCP_SSE_AUTH=0 应允许关闭"


def test_K3_有令牌时返回校验器():
    import kb_mcp_server as M

    tf = Path(tempfile.mkdtemp()) / "tokens.json"
    tf.write_text(json.dumps({"master": {"token": "m" * 40}, "tokens": {}}), encoding="utf-8")
    with mock.patch.object(M, "TOKEN_FILE", tf), \
         mock.patch.dict(os.environ, {"KB_MCP_SSE_AUTH": "1"}):
        assert M.build_mcp_auth() is not None, "有令牌表时应启用鉴权"


# ────────────────────────── L /files 危险类型强制附件 ──────────────────────────


def test_L_危险类型强制附件_图片维持inline(client):
    c, h, _, tmp = client
    assets = tmp / "_assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "evil.html").write_text("<script>x</script>", encoding="utf-8")
    (assets / "evil2.md.html").write_text("<script>x</script>", encoding="utf-8")
    (assets / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    for name in ("evil.html", "evil2.md.html"):
        r = c.get(f"/files/_assets/{name}", headers=h)
        cd = r.headers.get("Content-Disposition", "")
        assert "attachment" in cd, f"{name} 未强制附件: {cd!r}"
    r = c.get("/files/_assets/pic.png", headers=h)
    assert "attachment" not in r.headers.get("Content-Disposition", ""), \
        "图片被强制成附件，影响正文插图"


# ────────────────────────── M 增量刷新持锁组装 ──────────────────────────


def _prep_index(si):
    import numpy as np

    si.paths, si.contents = ["a.md", "b.md", "c.md"], ["甲", "乙", "丙"]
    si.embeddings = np.eye(3)
    si.mtimes, si.bm25 = {}, None
    si._collect_docs = lambda diff_only=True: (["d.md"], ["丁"], ["a.md"], {})


def test_M_嵌入组装时必须已持有索引锁():
    """原先嵌入组装在 self.lock 之外，并发刷新会静默错位。"""
    import numpy as np

    import kb_lib.search as S

    si = S.search_index
    _prep_index(si)

    seen = {}

    def hook(batch, task=None):
        seen["acquired"] = si.lock.acquire(blocking=False)
        if seen["acquired"]:
            si.lock.release()
        return np.array([[1.0, 1.0, 1.0] for _ in batch])

    with mock.patch.object(S, "get_setting",
                           lambda k, d=None: "llm" if k == "embedding_type" else d), \
         mock.patch.object(S, "embedding_api", hook):
        si.incremental_refresh()

    assert seen.get("acquired") is False, \
        "embedding_api 被调用时未持有索引锁（并发刷新会错位）"
    assert np.allclose(si.embeddings[0], [0, 1, 0]), f"b.md 行错位: {si.embeddings[0]}"
    assert np.allclose(si.embeddings[1], [0, 0, 1]), f"c.md 行错位: {si.embeddings[1]}"
    assert np.allclose(si.embeddings[2], [1, 1, 1]), f"d.md 行错位: {si.embeddings[2]}"
    assert len(si.embeddings) == len(si.paths), "embeddings 与 paths 行数不一致"


def test_M2_两个刷新线程并发后仍对齐():
    import threading

    import numpy as np

    import kb_lib.search as S

    si = S.search_index
    _prep_index(si)

    def embed(batch, task=None):
        import time

        time.sleep(0.05)
        return np.array([[1.0, 1.0, 1.0] for _ in batch])

    with mock.patch.object(S, "get_setting",
                           lambda k, d=None: "llm" if k == "embedding_type" else d), \
         mock.patch.object(S, "embedding_api", embed):
        ts = [threading.Thread(target=si.incremental_refresh) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]

    assert np.allclose(si.embeddings[0], [0, 1, 0]), f"并发后 b.md 错位: {si.embeddings[0]}"
    assert np.allclose(si.embeddings[1], [0, 0, 1]), f"并发后 c.md 错位: {si.embeddings[1]}"
    assert len(si.embeddings) == len(si.paths)


# ────────────────────────── N 第四轮补充 ──────────────────────────


def _prep_bm25(si, paths, contents):
    from rank_bm25 import BM25Okapi

    import kb_lib.search as S

    si.paths, si.contents = list(paths), list(contents)
    si.bm25 = BM25Okapi([S._tokenize(c) for c in si.contents])
    si.embeddings = None
    si._token_sets = None


def test_N1_删光笔记后索引与检索一并清空():
    """search.py 两处早退原先只结束状态机，已删内容仍可被搜到、重启后依旧。"""
    import kb_lib.search as S

    si = S.search_index
    _prep_bm25(si, ["仅此一篇.md"], ["唯一内容 hello world"])
    si.mtimes = {"仅此一篇.md": 1.0}
    si._collect_docs = lambda diff_only=True: ([], [], ["仅此一篇.md"], {})

    si.incremental_refresh()

    assert si.paths == [], f"删光后 paths 未清空: {si.paths}"
    assert si.contents == [], f"删光后 contents 未清空: {si.contents}"
    assert si.bm25 is None, "bm25 未清空"
    results, degraded = si.search("hello", 5, with_status=True)
    assert results == [] and not degraded, f"仍能搜到已删内容: {results}"


def test_N2_bm25命中不得500_np类型泄漏():
    """_bm25_search 的 idx 来自 np.argsort（np.int64），原样进 dict 会让
    Flask jsonify 抛 TypeError —— 纯 BM25 部署下一命中就 500。"""
    import kb_lib.search as S

    si = S.search_index
    _prep_bm25(si, ["a.md", "b.md"], ["向量 检索 测试内容", "无关 内容"])
    out = si._bm25_search("向量", 5)
    assert out, "应有命中"
    for r in out:
        assert isinstance(r.get("idx"), int) and not hasattr(r["idx"], "dtype"), \
            f"idx 仍是 numpy 标量: {type(r['idx'])}"
        json.dumps(r)  # 必须可被 json 序列化


def test_N3_bm25不再每请求全量分词():
    import kb_lib.search as S

    si = S.search_index
    _prep_bm25(si, ["a.md", "b.md"], ["向量 检索 测试内容", "无关 内容"])
    calls = {"n": 0}
    orig = S._tokenize

    def counting(x):
        calls["n"] += 1
        return orig(x)

    S._tokenize = counting
    try:
        si._bm25_search("向量", 5)
        first = calls["n"]
        si._bm25_search("检索", 5)
        second = calls["n"] - first
    finally:
        S._tokenize = orig
    assert second <= 1, f"第二次查询仍触发 {second} 次全库分词（应命中缓存）"


def test_N4_xff取链最右段():
    """配了受信代理时，最左段是客户端自造值，取它等于让调用方自证身份。"""
    import kb_lib.auth as A
    from unittest import mock

    req = type("R", (), {
        "remote_addr": "192.0.2.1",
        "headers": {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"},
    })()
    with mock.patch.object(A, "TRUSTED_PROXIES", {"192.0.2.1"}), \
         mock.patch.object(A, "request", req):
        assert A.client_ip() == "5.6.7.8", \
            "仍取最左段（可被伪造，IP 限流与审计都会失真）"


def test_N5_sync_sh支持块序列tags():
    import subprocess

    src = (ROOT / "scripts" / "sync.sh").read_text(encoding="utf-8")
    assert "sys.argv" in src, "sync.sh 未走 argv 传值"
    assert chr(39) * 3 not in src, "仍存在三引号拼接"
    assert "fm_head" in src, "未改为提取 frontmatter 头部（块序列 tags 会漏）"
