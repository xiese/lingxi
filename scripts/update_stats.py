#!/usr/bin/env python3
"""知识库统计自动更新脚本 — 从 API 拉实时数据，更新 README.md 和项目主文档。

统计口径约定：
- total_md / dashboard.total: 仪表盘可见笔记数（排除模板/隐藏等）
- indexed: /health 或 /api/index/status 的索引文档数
- total_on_disk: 文件系统 笔记/**/*.md 总数
- tags: 标签云 unique 标签数
- projects/knowledge/writing/diaries/archived: dashboard 分类计数
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import re
import sys
from datetime import datetime
from urllib.request import Request, urlopen
import logging

log = logging.getLogger("update_stats")

# 优先环境变量，否则从脚本位置推导
KB_ROOT = os.environ.get("KB_ROOT", str(Path(__file__).resolve().parent.parent))
API = os.environ.get("KB_API", "http://127.0.0.1:8899")
NOTES_DIR = Path(KB_ROOT) / "笔记"
PROJECT_NOTE = Path(KB_ROOT) / "笔记" / "10-项目" / "知识库项目.md"
README_PATH = Path(KB_ROOT) / "README.md"
DEV_PROGRESS_PATH = Path(KB_ROOT) / ".dev_progress.json"
TOKENS_FILE = Path(KB_ROOT) / ".api_tokens.json"


def _stale_text(v):
    """把 stale 渲染成给人和给机器看都对的文本。

    2026-10-18 第 18 项：stale 现在可以是 None（服务刚启动、缓存未预热，
    表示「不知道」）。直接 f-string 会写出字面 `None`，让人以为「跟得上」或
    「坏了」。这里统一渲染成「未知」。
    """
    return "未知" if v is None else str(v)


def _stale_from_health(health: dict):
    """从 /health 响应取 stale，区分「未知」与「0」。

    2026-10-18 第 18 项：/health 冷启动未预热时 stale 为 null，表示「不知道」。
    原写法（get 取 stale 默认 0 再 or 0）会把 null 和缺失都变成 0，
    在 README / 开发进度里显示成「索引完全跟得上磁盘」——这是假信息。
    口径：字段存在且为 null → None（未知）；字段缺失 → None（响应异常，同样不猜）；
    有数值 → int。调用方需能显示「未知」。
    """
    if not isinstance(health, dict):
        return None
    v = health.get("stale")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _master_token() -> str:
    env = os.environ.get("KB_MASTER_TOKEN") or os.environ.get("KB_TOKEN")
    if env:
        return env
    data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    return data["master"]["token"]


def api_get(path: str, auth: bool = False, retries: int = 2):
    """通过 Python urllib 请求 API（避免 token 暴露在 ps 命令中），带简单重试。"""
    last_err = None
    headers = {}
    if auth:
        headers["Authorization"] = f"Bearer {_master_token()}"
    for attempt in range(retries + 1):
        try:
            req = Request(f"{API}{path}", headers=headers)
            return json.loads(urlopen(req, timeout=15).read())
        except Exception as e:
            last_err = e
            log.debug("API 请求失败（第 %d/%d 次）: %s — %s", attempt + 1, retries + 1, path, e, exc_info=True)
            if attempt < retries:
                import time
                time.sleep(1)
    raise Exception(f"API请求失败({retries+1}次重试) {path}: {last_err}")


def get_dashboard_stats() -> dict:
    """获取所有实时统计。优先用公开 /health，鉴权接口失败时尽量降级。"""
    health = api_get("/health")
    indexed = int(health.get("indexed", 0) or 0)

    # 优先 index/status（鉴权）；失败则回落 health
    try:
        index = api_get("/api/index/status", auth=True)
        indexed = int(index.get("indexed", indexed) or indexed)
    except Exception:
        # 鉴权接口不可用时回退 /health 口径（两者可能差 1~2 篇，见 update_dev_progress 注释）
        log.warning("/api/index/status 不可用，回退 /health 的 indexed=%s", indexed, exc_info=True)
        index = {"indexed": indexed}

    dash = api_get("/dashboard", auth=True)
    tags = api_get("/api/tags", auth=True)

    total_md = int(dash.get("total", 0) or 0)
    projects = int(dash.get("projects", 0) or 0)
    knowledge = int(dash.get("knowledge", 0) or 0)
    writing = int(dash.get("writing", 0) or 0)
    diaries = int(dash.get("diaries", 0) or 0)
    archived = int(dash.get("archived", 0) or 0)
    tag_count = int(tags.get("total", 0) or 0)

    # 可见笔记列表 total（过滤后）；失败时用 dashboard.total
    filtered = total_md
    try:
        notes = api_get("/notes?limit=1", auth=True)
        filtered = int(notes.get("total", total_md) or total_md)
    except Exception:
        # 过滤后可见数取不到时用 dashboard.total，此时 excluded 恒为 0
        log.debug("/notes?limit=1 不可用，过滤后可见数回退为 dashboard.total", exc_info=True)

    excluded = max(total_md - filtered, 0)
    not_indexed = max(filtered - indexed, 0)

    return {
        "total_md": total_md,
        "indexed": indexed,
        "filtered": filtered,
        "excluded": excluded,
        "not_indexed": not_indexed,
        "projects": projects,
        "knowledge": knowledge,
        "writing": writing,
        "diaries": diaries,
        "archived": archived,
        "tags": tag_count,
        "version": str(health.get("version", "")),
        "stale": _stale_from_health(health),
        "jieba": bool(health.get("jieba", False)),
        "total_on_disk": 0,  # filled below
    }


def update_readme(stats: dict) -> bool:
    """更新 README.md 中的 ## 统计 段落。"""
    if not README_PATH.exists():
        print(f"ERROR: README 不存在 {README_PATH}")
        return False

    content = README_PATH.read_text(encoding="utf-8")
    new_block = (
        f"## 统计\n\n"
        f"- 笔记总数：{stats['total_md']} 篇（索引 {stats['indexed']} 篇，"
        f"文件系统 {stats['total_on_disk']} 篇含 README/模板/隐藏文件）\n"
        f"- 项目：{stats['projects']} 篇\n"
        f"- 知识：{stats['knowledge']} 篇\n"
        f"- 写作：{stats['writing']} 篇\n"
        f"- 日记：{stats['diaries']} 篇\n"
        f"- 归档：{stats['archived']} 篇\n"
        f"- 标签数：{stats['tags']}\n"
        f"- 搜索索引：BM25 + 向量检索 + jieba中文分词"
        f"（stale={_stale_text(stats['stale'])}）\n"
    )

    stats_pattern = r"## 统计\n\n(?:- .+\n)+"
    if not re.search(stats_pattern, content):
        # 2026-10-07 修：根 README 被 auto-commit 覆盖后统计段丢失，旧逻辑直接 return False，
        # 连带主档「数据状态」也不再更新（main 里 ok=False 即跳过），统计自 2026-08-18 停摆。
        # 缺段时按锚点插入；无锚点则追加到文末。插入位置：文档段之前。
        print("WARN: README 缺少 ## 统计 段落，自动插入")
        anchor = next((a for a in ("## 文档", "## 说明", "## 目录结构") if a in content), None)
        if anchor:
            content = content.replace(anchor, new_block + "\n" + anchor, 1)
        else:
            content = content.rstrip() + "\n\n" + new_block
    else:
        content = re.sub(stats_pattern, new_block, content)
    # 同步 updated frontmatter
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = re.sub(
        r"(?m)^updated: \d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?",
        f"updated: {now_str}",
        content,
        count=1,
    )
    README_PATH.write_text(content, encoding="utf-8")
    print(
        f"✅ README.md 统计已更新: {stats['total_md']}篇 / "
        f"{stats['indexed']}索引 / {stats['tags']}标签"
    )
    return True


def update_project_note(stats: dict) -> bool:
    """更新 知识库项目.md 中的数据状态段落 + updated 时间。"""
    if not PROJECT_NOTE.exists():
        print(f"WARN: 项目笔记不存在 {PROJECT_NOTE}")
        return False

    content = PROJECT_NOTE.read_text(encoding="utf-8")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = re.sub(
        r"(?m)^updated: \d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?",
        f"updated: {now_str}",
        content,
        count=1,
    )

    # 统一「## 数据状态」块，避免手写漂移
    ver_suffix = f"；服务版本: {stats['version']}" if stats.get("version") else ""
    data_block = (
        f"## 数据状态\n\n"
        f"- indexed: {stats['indexed']}, stale: {_stale_text(stats['stale'])}, "
        f"jieba: ON, 项目笔记: {stats['projects']}, "
        f"知识笔记: {stats['knowledge']}, 日记: {stats['diaries']}, "
        f"归档: {stats['archived']}, 写作: {stats['writing']}\n"
        f"- 仪表盘可见笔记: {stats['total_md']}；"
        f"文件系统 .md: {stats['total_on_disk']}\n"
        f"- 标签数: {stats['tags']}\n"
        f"- 搜索索引: .search_index.json (+ .arrays.npz), 嵌入: .embeddings.npy, FAISS: .faiss.index\n"
        f"- 统计同步时间: {now_str}{ver_suffix}\n"
    )

    if re.search(r"## 数据状态\n", content):
        content = re.sub(
            r"## 数据状态\n(?:.*?\n)*?(?=\n## |\Z)",
            data_block + "\n",
            content,
            count=1,
            flags=re.S,
        )
    else:
        # 插到「## 文档路径约定」前，否则追加末尾
        if "## 文档路径约定" in content:
            content = content.replace("## 文档路径约定", data_block + "\n## 文档路径约定", 1)
        else:
            content = content.rstrip() + "\n\n" + data_block

    PROJECT_NOTE.write_text(content, encoding="utf-8")
    print("✅ 项目主文档数据状态已同步")
    return True


def update_dev_progress(stats: dict) -> bool:
    """同步 .dev_progress.json 的 stats 段（只改统计字段，不动 completed/next 等）。

    口径与 README/主档保持一致：
    - indexed     ：优先 /api/index/status（鉴权口径，含未公开条目），
                    与 /health 的 indexed 可能差 1~2 篇，属正常差异；
    - notes_total ：仪表盘可见笔记数（dashboard.total，排除模板/隐藏/归档散页）；
    - stale       ：/health 的 stale（>0 表示索引落后于磁盘）；
    - 磁盘 .md 总数见主档「文件系统 .md」，此处不重复记录。
    """
    data: dict = {}
    if DEV_PROGRESS_PATH.exists():
        try:
            loaded = json.loads(DEV_PROGRESS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            log.warning(".dev_progress.json 解析失败，按空结构重建统计字段", exc_info=True)
            data = {}
    st = data.get("stats")
    if not isinstance(st, dict):
        st = {}
        data["stats"] = st
    st["indexed"] = stats["indexed"]
    st["notes_total"] = stats["total_md"]
    st["stale"] = stats["stale"]
    st["service"] = "running"
    st["jieba"] = bool(stats.get("jieba", False))
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        DEV_PROGRESS_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except Exception:
        log.warning("写入 .dev_progress.json 失败: %s", DEV_PROGRESS_PATH, exc_info=True)
        return False
    print(
        f"✅ .dev_progress.json 统计已同步: indexed={st['indexed']} / "
        f"notes_total={st['notes_total']} / stale={_stale_text(st['stale'])}"
    )
    return True


def main() -> None:
    try:
        stats = get_dashboard_stats()
        if NOTES_DIR.is_dir():
            stats["total_on_disk"] = sum(1 for _ in NOTES_DIR.rglob("*.md"))
        else:
            stats["total_on_disk"] = 0
            log.warning("笔记目录不存在，磁盘 .md 数按 0 记录: %s", NOTES_DIR)
        print(
            f"实时数据: {stats['total_md']}篇可见 / {stats['total_on_disk']}篇磁盘 / "
            f"{stats['indexed']}篇索引 / {stats['tags']}标签 / stale={_stale_text(stats['stale'])}"
        )
        print(
            f"        项目{stats['projects']} 知识{stats['knowledge']} "
            f"写作{stats['writing']} 日记{stats['diaries']} 归档{stats['archived']}"
        )

        # 三个统计目标分别更新，任一失败都如实报告（不再因 README 失败连坐主档）
        results = {
            "README.md 统计段": update_readme(stats),
            "项目主文档数据状态": update_project_note(stats),
            ".dev_progress.json": update_dev_progress(stats),
        }
        print("\n完成！定时任务建议：每 6 小时运行一次")
        failed = [name for name, ok in results.items() if not ok]
        for name, ok in results.items():
            print(f"  {'✅' if ok else '❌'} {name}")
        if failed:
            print(f"❌ 未更新: {', '.join(failed)}")
            sys.exit(1)
        sys.exit(0)
    except Exception as e:
        print(f"❌ 失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
