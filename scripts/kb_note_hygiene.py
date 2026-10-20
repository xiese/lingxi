#!/usr/bin/env python3
"""知识库笔记卫生扫描（只报告，不删除）。

用法：
  python3 scripts/kb_note_hygiene.py
  python3 scripts/kb_note_hygiene.py --json
  python3 scripts/kb_note_hygiene.py --quiet-ok

退出码：
  0 = 无问题（或仅 info）
  1 = 有 warning/critical 问题
  2 = 扫描失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from kb_lib.config import NOTES_DIR, KB_ROOT
except Exception:
    KB_ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parent.parent)
    NOTES_DIR = KB_ROOT / "笔记"

ARCHIVE_ROOT = "50-档案"
PROJECT_ROOT = "10-项目"
KNOWLEDGE_ROOT = "20-知识"
SKIP_NAME_EXACT = {"README.md"}
POINTER_HINTS = ("指针", "权威来源", "子入口", "redirect", "重定向", "archived", "请 skill_view")
FORBIDDEN_DIR_MARKERS = (
    "10-项目_知识库项目",
    "10-项目/10-项目_知识库项目",
)
# 标准顶层目录（嵌套撞名检测用，2026-09-26 新增）
STANDARD_TOP = {
    "00-索引", "10-项目", "20-知识", "30-写作", "30-记录", "40-日记",
    "50-档案", "60-梦境", "70-经验", "_模板", "_assets",
}
# 嵌套里允许出现的名字：项目自带资源目录
NESTED_ALLOW = {"_assets"}


def body_of(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2]
    return text


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[\w\u4e00-\u9fff]{2,}", text.lower()))


def is_pointer_like(text: str, size: int) -> bool:
    if size >= 4000:
        return False
    low = text[:2000]
    return any(h in low for h in POINTER_HINTS)


def leading_h1_lines(raw: str) -> list[int]:
    """正文开头连续一级标题的行号（1-based，跳过 YAML frontmatter 与空行）。

    纯函数：只依赖入参字符串，不读写文件系统，便于单测。
    规则与 2026-09-26 的 duplicate_h1 检测保持一致：从正文第一处非空行开始，
    连续收集 `# ` 开头的行，遇到第一种其他内容即停止（第 2 处及以后的 H1 不算）。
    """
    lines = raw.split("\n")
    i = 0
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1
    h1: list[int] = []
    while i < len(lines):
        t = lines[i].strip()
        if t == "":
            i += 1
            continue
        if t.startswith("# "):
            h1.append(i + 1)
            i += 1
            continue
        break
    return h1


def _collect_files(notes_dir: Path) -> tuple[list[dict], list[dict]]:
    """收集 notes_dir 下全部 md 的元信息（纯文件收集，返回 files 与 read_error issues）。"""
    issues: list[dict] = []
    files: list[dict] = []
    for f in notes_dir.rglob("*.md"):
        if not f.is_file():
            continue
        try:
            rel = str(f.relative_to(notes_dir)).replace("\\", "/")
        except ValueError:
            continue
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            issues.append({"level": "warning", "kind": "read_error", "path": rel, "detail": str(e)})
            continue
        size = len(raw.encode("utf-8", errors="replace"))
        body = body_of(raw)
        files.append(
            {
                "path": rel,
                "name": f.name,
                "stem": f.stem,
                "size": size,
                "sha": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                "body_sha": hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest(),
                "tokens": tokenize(body),
                "raw": raw,
                "body": body,
                "in_archive": rel.startswith(ARCHIVE_ROOT + "/") or rel == ARCHIVE_ROOT,
            }
        )
    return files, issues


def scan(notes_dir: Path) -> dict:
    """编排：收集文件 → 按固定顺序跑 11 项检测 → severity sort → summary。"""
    files, issues = _collect_files(notes_dir)
    _check_exact_duplicates(files, issues)
    _check_same_basename(files, issues)
    _check_near_duplicates(files, issues)
    _check_empty_shells(files, issues)
    _check_skill_dumps(files, issues)
    _check_archive_root_litter(notes_dir, issues)
    _check_forbidden_paths(files, issues)
    _check_project_dir_forks(notes_dir, issues)
    _check_test_junk(files, issues)
    _check_nested_top_dirs(notes_dir, issues)
    _check_duplicate_h1(files, issues)

    # severity sort
    order = {"critical": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda x: (order.get(x.get("level", "info"), 9), x.get("kind", ""), str(x.get("paths", ""))))

    summary = {
        "scanned_md": len(files),
        "active_md": sum(1 for x in files if not x["in_archive"]),
        "issues": len(issues),
        "critical": sum(1 for x in issues if x["level"] == "critical"),
        "warning": sum(1 for x in issues if x["level"] == "warning"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "notes_dir": str(notes_dir),
    }
    return {"summary": summary, "issues": issues}


# 1 exact duplicates (body hash) among non-archive
def _check_exact_duplicates(files: list[dict], issues: list[dict]) -> None:
    by_body = defaultdict(list)
    for it in files:
        if it["in_archive"]:
            continue
        by_body[it["body_sha"]].append(it["path"])
    for h, paths in by_body.items():
        if len(paths) >= 2:
            issues.append(
                {
                    "level": "critical",
                    "kind": "exact_duplicate",
                    "paths": sorted(paths),
                    "detail": f"body 完全相同 x{len(paths)}",
                }
            )


# 2 same basename multi path (active only)
def _check_same_basename(files: list[dict], issues: list[dict]) -> None:
    by_name = defaultdict(list)
    for it in files:
        if it["in_archive"] or it["name"] in SKIP_NAME_EXACT:
            continue
        by_name[it["name"]].append(it)
    for name, items in by_name.items():
        if len(items) < 2:
            continue
        # allow project main + knowledge pointer
        roles = []
        for it in items:
            p = it["path"]
            if p.startswith(PROJECT_ROOT + "/"):
                roles.append("project")
            elif p.startswith(KNOWLEDGE_ROOT + "/"):
                roles.append("knowledge")
            else:
                roles.append("other")
        if set(roles) == {"project", "knowledge"} and len(items) == 2:
            k = next(x for x in items if x["path"].startswith(KNOWLEDGE_ROOT + "/"))
            if is_pointer_like(k["raw"], k["size"]):
                continue
            issues.append(
                {
                    "level": "warning",
                    "kind": "dual_main_same_name",
                    "paths": [x["path"] for x in items],
                    "detail": "同名跨 10-项目/20-知识，且知识侧不像指针",
                }
            )
            continue
        issues.append(
            {
                "level": "warning",
                "kind": "same_basename",
                "paths": [x["path"] for x in items],
                "detail": f"同名 {name} 多路径",
            }
        )


# 3 near-duplicate in same parent dir（跳过指针模板，避免 skill 指针互撞）
def _check_near_duplicates(files: list[dict], issues: list[dict]) -> None:
    by_dir = defaultdict(list)
    for it in files:
        if it["in_archive"] or it["size"] < 400:
            continue
        if is_pointer_like(it["raw"], it["size"]):
            continue
        parent = str(Path(it["path"]).parent)
        by_dir[parent].append(it)
    for parent, items in by_dir.items():
        n = len(items)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = items[i], items[j]
                if not a["tokens"] or not b["tokens"]:
                    continue
                inter = len(a["tokens"] & b["tokens"])
                union = len(a["tokens"] | b["tokens"])
                jacc = inter / union if union else 0.0
                if jacc >= 0.75:
                    issues.append(
                        {
                            "level": "critical",
                            "kind": "near_duplicate",
                            "paths": [a["path"], b["path"]],
                            "detail": f"同目录近重复 Jaccard={jacc:.2f}",
                        }
                    )


# 4 empty / tiny shells (active)
def _check_empty_shells(files: list[dict], issues: list[dict]) -> None:
    for it in files:
        if it["in_archive"]:
            continue
        body = it["body"].strip()
        if it["size"] < 120 or (len(body) < 80 and body.count("\n") < 3):
            # allow archived redirect stubs that still live in active tree intentionally
            if is_pointer_like(it["raw"], it["size"]) and it["size"] < 1500:
                continue
            issues.append(
                {
                    "level": "warning",
                    "kind": "empty_shell",
                    "paths": [it["path"]],
                    "detail": f"过短 {it['size']}B",
                }
            )


# 5 skill dumps
def _check_skill_dumps(files: list[dict], issues: list[dict]) -> None:
    for it in files:
        if it["in_archive"]:
            continue
        name_l = it["name"].lower()
        if "skill" in name_l and it["size"] >= 20000 and not is_pointer_like(it["raw"], it["size"]):
            issues.append(
                {
                    "level": "critical",
                    "kind": "skill_dump",
                    "paths": [it["path"]],
                    "detail": f"疑似 skill 整包 dump {it['size']}B",
                }
            )


# 6 archive root litter
def _check_archive_root_litter(notes_dir: Path, issues: list[dict]) -> None:
    arch_root = notes_dir / ARCHIVE_ROOT
    if arch_root.is_dir():
        for f in arch_root.iterdir():
            if f.is_file() and f.suffix == ".md":
                issues.append(
                    {
                        "level": "warning",
                        "kind": "archive_root_litter",
                        "paths": [f"{ARCHIVE_ROOT}/{f.name}"],
                        "detail": "50-档案 根上散落 md，应收进 文档收敛_*/ 子目录",
                    }
                )


# 7 forbidden legacy paths
def _check_forbidden_paths(files: list[dict], issues: list[dict]) -> None:
    for it in files:
        for marker in FORBIDDEN_DIR_MARKERS:
            if marker in it["path"]:
                issues.append(
                    {
                        "level": "critical",
                        "kind": "forbidden_path",
                        "paths": [it["path"]],
                        "detail": f"废弃路径标记: {marker}",
                    }
                )
                break


# 8 project directory forks: "foo" and "foo 部署" / "foo部署"
def _check_project_dir_forks(notes_dir: Path, issues: list[dict]) -> None:
    proj = notes_dir / PROJECT_ROOT
    if proj.is_dir():
        dirs = [d for d in proj.iterdir() if d.is_dir()]
        names = {d.name: d for d in dirs}
        for name, d in names.items():
            base = re.sub(r"[\s_-]*(部署|deploy|deployment)$", "", name, flags=re.I).strip()
            if not base or base == name:
                continue
            # find sibling with same base
            for other_name, other in names.items():
                if other_name == name:
                    continue
                other_base = re.sub(r"[\s_-]*(部署|deploy|deployment)$", "", other_name, flags=re.I).strip()
                if other_base == base or other_name == base:
                    pair = sorted([f"{PROJECT_ROOT}/{name}", f"{PROJECT_ROOT}/{other_name}"])
                    if not any(
                        i.get("kind") == "project_dir_fork" and sorted(i.get("paths", [])) == pair for i in issues
                    ):
                        issues.append(
                            {
                                "level": "warning",
                                "kind": "project_dir_fork",
                                "paths": pair,
                                "detail": "疑似同一项目双目录（含部署后缀）",
                            }
                        )


# 9 test junk names in active tree
def _check_test_junk(files: list[dict], issues: list[dict]) -> None:
    junk_re = re.compile(r"(测试渣|resolve-test|upload_smoke|kb_cli_upload_smoke)", re.I)
    for it in files:
        if it["in_archive"]:
            continue
        if junk_re.search(it["name"]) or junk_re.search(it["path"]):
            issues.append(
                {
                    "level": "warning",
                    "kind": "test_junk",
                    "paths": [it["path"]],
                    "detail": "测试/smoke 命名残留在现役目录",
                }
            )


# 10 nested dir collides with a top-level dir name（2026-09-26 新增）
# 背景：09-19、09-26 两次出现 20-知识/10-项目/ 这类错位——项目笔记被写进
# 知识区的 10-项目 子目录。旧检测只扫顶层目录，嵌套错位看不见，清完复发。
# 50-档案/ 整棵子树是历史归档镜像，里面出现同名目录属正常，不报。
def _check_nested_top_dirs(notes_dir: Path, issues: list[dict]) -> None:
    for d in notes_dir.rglob("*"):
        if not d.is_dir():
            continue
        try:
            rel = d.relative_to(notes_dir)
        except ValueError:
            continue
        if len(rel.parts) < 2:
            continue
        if d.name not in STANDARD_TOP or d.name in NESTED_ALLOW:
            continue
        if rel.parts[0] == ARCHIVE_ROOT:
            continue
        issues.append(
            {
                "level": "warning",
                "kind": "nested_top_dir",
                "paths": [str(rel).replace("\\", "/")],
                "detail": "子目录名撞上顶层目录名，疑似写错归类（用 subdir 时漏了项目名？）",
            }
        )


# 11 duplicate leading H1（2026-09-26 新增）
# 背景：REST 写入时无条件在正文前插一行 `# {title}`（routes_notes.py），
# 若客户端自己也写了标题，就会出现两个标题；反复保存会越堆越多。
# 旧检测不看正文标题，全库曾累积到 531 篇双标题。
def _check_duplicate_h1(files: list[dict], issues: list[dict]) -> None:
    for it in files:
        if it["in_archive"]:
            continue
        h1 = leading_h1_lines(it["raw"])
        if len(h1) >= 2:
            issues.append(
                {
                    "level": "warning",
                    "kind": "duplicate_h1",
                    "paths": [it["path"]],
                    "detail": "正文开头有 %d 个一级标题（行 %s），疑似重复插入"
                              % (len(h1), ",".join(str(x) for x in h1)),
                }
            )


def render_text(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# 知识库笔记卫生报告",
        f"",
        f"- 时间：{s['generated_at']}",
        f"- 扫描：{s['scanned_md']} md（现役 {s['active_md']}）",
        f"- 问题：{s['issues']}（critical {s['critical']} / warning {s['warning']}）",
        f"- 目录：`{s['notes_dir']}`",
        f"",
    ]
    if not report["issues"]:
        lines.append("无问题。保持安静即可。")
        return "\n".join(lines)

    by_kind = defaultdict(list)
    for it in report["issues"]:
        by_kind[it["kind"]].append(it)
    for kind, items in by_kind.items():
        lines.append(f"## {kind} ({len(items)})")
        for it in items:
            paths = it.get("paths") or ([it["path"]] if it.get("path") else [])
            lines.append(f"- [{it['level']}] {it.get('detail', '')}")
            for p in paths:
                lines.append(f"  - `{p}`")
        lines.append("")
    lines.append("处理：先 dry-run 清单，用户确认后再归档；默认不自动删除、不 git push。")
    lines.append("规范：skill `personal-knowledge-base` → `references/kb-doc-convergence.md`")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KB note hygiene scan (report only)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--quiet-ok", action="store_true", help="print nothing when no issues")
    ap.add_argument("--notes-dir", default=str(NOTES_DIR))
    args = ap.parse_args(argv)
    notes_dir = Path(args.notes_dir)
    if not notes_dir.is_dir():
        print(f"notes dir missing: {notes_dir}", file=sys.stderr)
        return 2
    try:
        report = scan(notes_dir)
    except Exception as e:
        print(f"scan failed: {e}", file=sys.stderr)
        return 2

    has_problem = report["summary"]["critical"] + report["summary"]["warning"] > 0
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if has_problem or not args.quiet_ok:
            print(render_text(report))
    return 1 if has_problem else 0


if __name__ == "__main__":
    raise SystemExit(main())
