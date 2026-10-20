"""P1: 索引内容级抽检——paths/contents 内容错位必须被抓到。

背景：09-29 复发过「health 报 clean 但实际错位」（数量一致、内容错位），
既有 check_artifact_consistency 只比数量。本测试覆盖 search.py 新增的
sample_content_digests / verify_content_samples / _save_index 挂点：

1. 抽检下标均匀、带边界；
2. 索引内容与磁盘一致时不误报；
3. 整体错位一格（09-29 事故形态）、单篇被污染、文件丢失都能抓到；
4. _save_index 真实落盘路径：错位时 index_consistent=False，manifest 记
   consistent=False / reason / content_samples。

不依赖 Flask 服务；所有文件写临时目录，不碰生产索引。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if "pytest" not in sys.modules:
    pytest_stub = types.ModuleType("pytest")
    pytest_stub.fixture = lambda *a, **k: (lambda fn: fn)
    sys.modules["pytest"] = pytest_stub

import kb_lib.config as config  # noqa: E402
import kb_lib.search as search_mod  # noqa: E402

N_FILES = 25


def _mk_notes(root: Path, n: int = N_FILES):
    """造 n 篇带 frontmatter 的笔记，返回 (notes_dir, bodies)。

    bodies 是「剥掉 frontmatter、统一换行后的正文」——与 _collect_docs 产出同构，
    用来验证 _note_body_from_disk 的归一化规则与索引侧逐字一致。
    """
    notes = root / "笔记"
    notes.mkdir(parents=True, exist_ok=True)
    bodies = []
    for i in range(n):
        rel = "n%02d.md" % i
        body = "标题%d\n正文第%d段。关键词%d" % (i, i, i)
        (notes / rel).write_text(
            "---\ntitle: t%d\ntags: [a]\n---\n%s\n" % (i, body), encoding="utf-8"
        )
        # 磁盘正文 = 剥 frontmatter 后的内容（含文件自带的尾部换行），
        # 与 _collect_docs 的产出逐字一致，否则抽检会误报。
        bodies.append(body + "\n")
    return notes, bodies


def _paths(n: int = N_FILES):
    return ["n%02d.md" % i for i in range(n)]


def test_sample_indices_even_and_capped():
    assert search_mod._even_sample_indices(0) == []
    assert search_mod._even_sample_indices(5, 0) == []
    assert search_mod._even_sample_indices(3, 10) == [0, 1, 2]
    idx = search_mod._even_sample_indices(101, 20)
    assert len(idx) == 20
    assert idx[0] == 0 and idx[-1] == 100
    assert idx == sorted(idx) and len(set(idx)) == len(idx)


def test_sample_capped_at_index_size():
    # n=1 < CONTENT_SAMPLE_SIZE(20) 时抽满 1 篇
    s = search_mod.sample_content_digests(["a.md"], ["body"], None, size=20)
    assert len(s) == 1 and s[0]["path"] == "a.md"
    # paths/contents 长度不一致：无记录
    assert search_mod.sample_content_digests(["a.md"], ["x", "y"], None) == []


def test_verify_passes_when_index_matches_disk(tmp_path):
    notes, bodies = _mk_notes(tmp_path)
    samples = search_mod.sample_content_digests(_paths(), bodies, notes)
    assert samples and len(samples) == 20  # 25 篇按 20 抽
    ok, reason = search_mod.verify_content_samples(samples, notes)
    assert ok is True and reason == "ok"


def test_verify_passes_on_frontmatter_normalization(tmp_path):
    """磁盘文件带 frontmatter + CRLF，索引存的是剥后的 LF 正文——不得误报。"""
    notes = tmp_path / "笔记"
    notes.mkdir(parents=True)
    body = "正文内容甲\n第二行"
    (notes / "crlf.md").write_bytes(
        "---\r\ntitle: t\r\n---\r\n".encode("utf-8") + body.replace("\n", "\r\n").encode("utf-8")
    )
    samples = [{"path": "crlf.md", "sha256": search_mod._sha256_text(body)}]
    ok, reason = search_mod.verify_content_samples(samples, notes)
    assert ok is True, reason


def test_verify_catches_shifted_contents(tmp_path):
    """整体错位一格（09-29 事故形态：数量一致、内容互相错位）必须抓到。"""
    notes, bodies = _mk_notes(tmp_path)
    shifted = bodies[1:] + [bodies[0]]
    samples = search_mod.sample_content_digests(_paths(), shifted, notes)
    ok, reason = search_mod.verify_content_samples(samples, notes)
    assert ok is False
    assert "内容抽检不一致" in reason
    assert any(p in reason for p in _paths())


def test_verify_catches_single_note_mismatch(tmp_path):
    notes, bodies = _mk_notes(tmp_path)
    bad = list(bodies)
    bad[11] = "被污染的内容"  # 11 在 25 篇抽 20 的均匀下标里
    samples = search_mod.sample_content_digests(_paths(), bad, notes)
    ok, reason = search_mod.verify_content_samples(samples, notes)
    assert ok is False
    assert "n11.md" in reason and "内容抽检不一致" in reason


def test_verify_flags_missing_file(tmp_path):
    notes, bodies = _mk_notes(tmp_path)
    (notes / "n00.md").unlink()
    samples = search_mod.sample_content_digests(_paths(), bodies, notes)
    ok, reason = search_mod.verify_content_samples(samples, notes)
    assert ok is False and "无法读取" in reason


def test_verify_empty_samples_not_flagged(tmp_path):
    ok, reason = search_mod.verify_content_samples([], tmp_path)
    assert ok is True and reason == "ok"


def _iso_paths(tmp_path, monkeypatch):
    pkl = tmp_path / ".search_index.pkl"
    manifest = tmp_path / ".index_manifest.json"
    monkeypatch.setattr(search_mod, "INDEX_FILE", pkl, raising=False)
    monkeypatch.setattr(search_mod, "INDEX_MANIFEST_FILE", manifest, raising=False)
    monkeypatch.setattr(search_mod, "NOTES_DIR", tmp_path / "笔记", raising=False)
    return pkl, manifest


def test_save_index_flags_content_mismatch(tmp_path, monkeypatch):
    """_save_index 真实路径：索引内容与磁盘不符 → consistent=False 且 manifest 留痕。"""
    notes, bodies = _mk_notes(tmp_path)
    pkl, manifest = _iso_paths(tmp_path, monkeypatch)

    si = search_mod.SearchIndex()
    bad = list(bodies)
    bad[3] = "错位内容甲"  # 3 在均匀抽样下标里
    with si.lock:
        si.paths = _paths()
        si.contents = bad
        si.bm25 = None
        si._save_index()

    assert si.index_consistent is False
    assert "内容抽检不一致" in si.consistency_reason
    assert pkl.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["consistent"] is False
    assert "内容抽检不一致" in data["reason"]
    assert isinstance(data.get("content_samples"), list) and data["content_samples"]


def test_save_index_clean_case_and_manifest_selfcheck(tmp_path, monkeypatch):
    notes, bodies = _mk_notes(tmp_path)
    pkl, manifest = _iso_paths(tmp_path, monkeypatch)

    si = search_mod.SearchIndex()
    with si.lock:
        si.paths = _paths()
        si.contents = bodies
        si.bm25 = None
        si._save_index()

    assert si.index_consistent is True
    assert si.consistency_reason == "ok"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["consistent"] is True
    assert data["paths_count"] == N_FILES
    # manifest 里记的抽样哈希，抽出来回验磁盘应通过（自洽性）
    ok, reason = search_mod.verify_content_samples(data["content_samples"], notes)
    assert ok is True, reason
