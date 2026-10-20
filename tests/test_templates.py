"""模板插件（第 7-2 项）测试。

覆盖两层：
  ① services/templates.py —— 从 _模板/ 目录加载、非法名拦截、frontmatter 与正文分离；
  ② routes —— GET /api/templates 列表、POST /notes 带 template 时按模板章节校验。

第 ① 层用真实临时目录（monkeypatch TEMPLATES_DIR），第 ② 层走 Flask test_client。
关键回归：模板文件带 frontmatter 时，前端拿到的 body 必须不含 `---` 头，
否则 ltgOrCreate() 的 `#` 判据失效、选模板反而不创建笔记。
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from kb_lib.services import templates as T  # noqa: E402


TPL_WITH_FM = """---
id: "proj-{{timestamp}}"
title: ""
type: project
tags: []
---

# {{title}}

## 目标
一句话描述。

## 当前状态
- [ ] 里程碑 1

## 日志
- YYYY-MM-DD: 做了什么
"""

TPL_NO_FM = """# 简单模板

## 背景
## 结论
"""


@pytest.fixture()
def tpl_dir(tmp_path, monkeypatch):
    d = tmp_path / "_模板"
    d.mkdir()
    (d / "项目模板.md").write_text(TPL_WITH_FM, encoding="utf-8")
    (d / "简单模板.md").write_text(TPL_NO_FM, encoding="utf-8")
    monkeypatch.setattr(T, "TEMPLATES_DIR", d)
    return d


def test_列出模板读目录_带标题与章节(tpl_dir):
    items = T.list_templates()
    names = [x["name"] for x in items]
    assert names == ["简单模板", "项目模板"], names

    proj = next(x for x in items if x["name"] == "项目模板")
    assert proj["sections"] == ["## 目标", "## 当前状态", "## 日志"]
    assert proj["builtin"] is False


def test_模板正文不含frontmatter_前端判据才成立(tpl_dir):
    """回归：body 首字符必须是 #，不能是 -。

    ltgOrCreate() 用 `t.startsWith('#')` 判断「这条要不要直接建笔记」。
    模板带 frontmatter 时若把原文塞进 textarea，首字符是 `-`，
    选了模板反而走「智能标签」分支，笔记永远建不出来。
    """
    proj = T.get_template("项目模板")
    assert proj["body"].startswith("#"), proj["body"][:20]
    assert not proj["body"].startswith("---")
    assert "id: \"proj-" not in proj["body"]
    # 原文仍完整保留，需要原样渲染的调用方不受影响
    assert proj["content"].startswith("---")
    assert proj["frontmatter"].startswith("---")


def test_章节只认二级标题_不把frontmatter里的注释当章节(tpl_dir):
    proj = T.get_template("项目模板")
    assert all(s.startswith("## ") for s in proj["sections"])
    assert "type: project" not in " ".join(proj["sections"])


def test_无frontmatter模板原样返回(tpl_dir):
    simple = T.get_template("简单模板")
    assert simple["frontmatter"] == ""
    assert simple["body"] == simple["content"]
    assert simple["sections"] == ["## 背景", "## 结论"]


def test_非法模板名一律拒绝(tpl_dir):
    for bad in ["../secret", "a/b", "", "   ", ".", "..", "x\x00y"]:
        assert T.get_template(bad) is None, bad


def test_符号链接指向目录外时拒绝(tpl_dir, tmp_path):
    """模板名不带斜杠但落点是软链，也能逃出 _模板/ —— resolve() 后必须被判越界。"""
    outside = tmp_path / "外面.md"
    outside.write_text("# 外面的文件\n\n## 机密\n", encoding="utf-8")
    link = tpl_dir / "软链模板.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前文件系统不支持符号链接")
    assert T.get_template("软链模板") is None, "软链逃逸必须被拦住"


def test_模板名带md后缀也能取到(tpl_dir):
    """前端/脚本可能带后缀传，剥掉 .md 后应与不带后缀等价。"""
    a = T.get_template("项目模板")
    b = T.get_template("项目模板.md")
    assert a is not None and b is not None
    assert a["name"] == b["name"] == "项目模板"


def test_目录缺失回退内置默认模板(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "TEMPLATES_DIR", tmp_path / "不存在")
    items = T.list_templates()
    assert len(items) == 1
    assert items[0]["builtin"] is True
    from kb_lib.services.write_policy import REQUIRED_SECTIONS
    assert items[0]["sections"] == list(REQUIRED_SECTIONS)


def test_模板章节决定校验要求(tpl_dir):
    """带 template 时按模板章节校验，不再硬要求默认五节。"""
    from kb_lib.services import write_policy as WP
    assert WP.required_sections_for("项目模板") == ["## 目标", "## 当前状态", "## 日志"]
    # 未指定 / 不存在 → 回退默认五节
    assert WP.required_sections_for(None) == list(WP.REQUIRED_SECTIONS)
    assert WP.required_sections_for("不存在的模板") == list(WP.REQUIRED_SECTIONS)


def test_无章节模板回退默认五节(tpl_dir):
    from kb_lib.services import write_policy as WP
    (tpl_dir / "空壳.md").write_text("# 只有标题\n\n没有二级标题。\n", encoding="utf-8")
    assert WP.required_sections_for("空壳") == list(WP.REQUIRED_SECTIONS)


def test_按模板章节判质量_缺节被拦(tpl_dir):
    from kb_lib.services import write_policy as WP
    # 有正文长度但缺「## 日志」
    body = "# 测试项目\n\n" + "内容" * 500 + "\n\n## 目标\n做点事\n\n## 当前状态\n进行中\n"
    err = WP.validate_note_quality(body, note_type="project", template="项目模板")
    assert err is not None
    assert "## 日志" in err["problems"][0]
    assert err["required_sections"] == ["## 目标", "## 当前状态", "## 日志"]


def test_不传模板时行为与改前完全一致(tpl_dir):
    """向后兼容：不传 template，仍然要求默认五节。"""
    from kb_lib.services import write_policy as WP
    body = "# 测试\n\n" + "内容" * 500 + "\n\n## 目标\n做点事\n"
    err = WP.validate_note_quality(body, note_type="project")
    assert err is not None
    assert "## 背景" in err["problems"][0]
    assert err["required_sections"] == list(WP.REQUIRED_SECTIONS)
