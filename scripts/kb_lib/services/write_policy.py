"""写路径单一真相：卫生 + 质量 + 写后索引出口。

REST / MCP 共用本模块；MCP 不再本地重复门禁。
"""
from __future__ import annotations

import os
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 与历史 MCP 默认对齐，可用环境变量覆盖
MIN_CONTENT_LEN = int(os.environ.get("KB_MCP_MIN_CONTENT_LEN", "800"))
REQUIRED_SECTIONS = [
    "## 背景",
    "## 关键发现",
    "## 实施步骤",
    "## 验证结果",
    "## 后续建议",
]

# 写后需要 force 刷搜索的动作（删除/归档会改变可检索集合）
FORCE_REFRESH_ACTIONS = frozenset(
    {
        "deleted",
        "delete",
        "archived",
        "archive",
        "batch_deleted",
        "batch_archived",
    }
)

__all__ = [
    "MIN_CONTENT_LEN",
    "REQUIRED_SECTIONS",
    "FORCE_REFRESH_ACTIONS",
    "required_sections_for",
    "validate_note_quality",
    "validate_write_hygiene",
    "on_note_changed",
]


log = logging.getLogger("kb.write_policy")


def required_sections_for(template_name: Optional[str] = None) -> List[str]:
    """按模板名取章节要求；None / 找不到 / 模板无 `## ` 标题时回退默认五节。"""
    if not template_name:
        return list(REQUIRED_SECTIONS)
    try:
        from .templates import get_template

        tpl = get_template(template_name)
    except Exception:
        log.warning("读取模板失败，回退默认章节：%s", template_name, exc_info=True)
        return list(REQUIRED_SECTIONS)
    if not tpl:
        return list(REQUIRED_SECTIONS)
    sections = [s for s in (tpl.get("sections") or []) if isinstance(s, str) and s.strip()]
    if not sections:
        return list(REQUIRED_SECTIONS)
    return sections


def validate_note_quality(
    content: str,
    note_type: str = "knowledge",
    quality_check: bool = True,
    template: Optional[str] = None,
) -> Optional[dict]:
    """质量门禁。通过返回 None；失败返回 error dict（HTTP 400 体）。

    - quality_check=False 时跳过
    - diary / writing 不强制章节与长度
    - project / knowledge 默认：≥ MIN_CONTENT_LEN + 5 必备章节
    - template 指定模板时，章节要求改用该模板的 `## ` 标题
    """
    if not quality_check:
        return None
    if note_type not in ("project", "knowledge"):
        return None

    required = required_sections_for(template)
    problems: List[str] = []
    text = (content or "").strip()
    if len(text) < MIN_CONTENT_LEN:
        problems.append(
            f"正文过短：{len(text)} 字符，最低建议 {MIN_CONTENT_LEN} 字符"
        )
    missing = [s for s in required if s not in text]
    if missing:
        problems.append("缺少必备章节：" + ", ".join(missing))
    if not problems:
        return None
    return {
        "error": "quality_check_failed",
        "problems": problems,
        "required_sections": list(required),
        "min_content_len": MIN_CONTENT_LEN,
        "hint": (
            "建议包含背景、关键发现、实施步骤、验证结果、后续建议；"
            "小型临时笔记可设置 quality_check=false（卫生硬拦仍生效）。"
        ),
    }


def ensure_inside(path, root, label: str = "写入路径"):
    """落盘前的最后一道围栏：确认 path 在 root 之内。

    返回 (error_dict|None, resolved_path|None)。调用方应在 mkdir/write 前调用，
    把「用户输入拼路径」的失误挡成 400 而不是写坏文件或 500。
    """
    try:
        rp = Path(path).resolve()
        rr = Path(root).resolve()
        rp.relative_to(rr)
        return None, rp
    except ValueError:
        log.warning("%s 逃出根目录: %s（root=%s）", label, path, root)
        return {
            "error": "path_forbidden",
            "detail": f"{label}超出笔记库范围",
            "hint": "标题/子目录不得包含 .. 或绝对路径",
        }, None
    except OSError as e:
        log.warning("%s 解析失败: %s（%s）", label, path, e)
        return {
            "error": "path_forbidden",
            "detail": f"{label}无法解析",
            "hint": "路径含非法字符",
        }, None


def _path_forbidden(detail: str, hint: str) -> dict:
    """统一的路径类硬拦响应体（error/detail/hint 键序固定）。"""
    return {
        "error": "path_forbidden",
        "detail": detail,
        "hint": hint,
    }


def _norm_write_inputs(
    subdir: str, title: str, content: str
) -> Tuple[str, str, str]:
    """归一化写入三项输入：反斜杠压平、去首尾空白与斜杠。"""
    sd_norm = (subdir or "").replace("\\", "/").strip().strip("/")
    title_s = (title or "").strip()
    content_s = content or ""
    return sd_norm, title_s, content_s


def _guard_path_traversal(title_s: str, sd_norm: str) -> Optional[dict]:
    """路径穿越硬拦（2026-09-18 修复）。

    背景：标题里的 `/` 曾被当作目录分隔符，`../` 更能逃出笔记库
    （实测可写到 KB_ROOT 外任意 .md，服务以 root 运行）。
    这里做**与落盘无关**的独立校验：只要含路径分隔符或 .. 段就拒。
    title 先于 subdir 检查，顺序即优先级。
    """
    for field_name, raw in (("title", title_s), ("subdir", sd_norm)):
        if not raw:
            continue
        if "\0" in raw:
            return _path_forbidden(
                f"{field_name} 含空字节",
                "标题/子目录不得含控制字符",
            )
        norm = raw.replace("\\", "/")
        if any(seg == ".." for seg in norm.split("/")):
            return _path_forbidden(
                f"{field_name} 含上级目录引用（..）",
                "标题只能是单段名字；子目录不得用 .. 回溯",
            )
        if field_name == "title" and re.match(r"^[/\\\uFF0F\uFF3C]", raw):
            return _path_forbidden(
                "title 不得以路径分隔符开头（绝对路径）",
                "标题是单段文件名；归类请用 subdir",
            )
        if field_name == "subdir" and re.match(r"^[/\\\uFF0F\uFF3C]", raw):
            return _path_forbidden(
                "subdir 不得以路径分隔符开头（绝对路径）",
                "subdir 用相对路径，如 项目名 或 项目名/子目录",
            )
    return None


def _guard_title_nonempty(title_s: str) -> Optional[dict]:
    """标题净化后不得为空或纯点。"""
    if not title_s.strip().strip(".").strip():
        return _path_forbidden("title 净化后为空", "请给出有效标题")
    return None


_FORBIDDEN_PATH_SUBSTRINGS = (
    "10-项目_知识库项目",
    "10-项目/10-项目_知识库项目",
)


def _guard_deprecated_path(sd_norm: str, title_s: str) -> Optional[dict]:
    """废弃路径 10-项目_知识库项目 硬拦。"""
    for bad in _FORBIDDEN_PATH_SUBSTRINGS:
        if bad in sd_norm or bad in f"{sd_norm}/{title_s}":
            return _path_forbidden(
                f"禁止写入废弃路径：{bad}",
                "请写 10-项目/知识库项目.md 或 10-项目/知识库项目/ 下；见 skill personal-knowledge-base §9",
            )
    return None


def _guard_nested_top_dir(
    note_type: str, sd_norm: str, type_map: Optional[dict]
) -> Optional[dict]:
    """嵌套撞名硬拦（2026-09-26 新增）。

    背景：2026-09-19、2026-09-26 两次出现 20-知识/10-项目/ 这类错位——
    项目笔记被写进知识区里一个叫「10-项目」的子目录。旧校验只拦废弃路径，
    不看子目录名是否撞顶层目录名，所以清完照样复发。
    判据按**最终落点**：剥掉与当前 type 同名的首段后，subdir 里不应再出现
    任何顶层目录名。70-经验 例外——那是用户私有目录，交给 owner_ack 闸门。
    archive 例外：50-档案 下按原目录结构镜像归档（如 空壳待定_20260913/20-知识/工具研究），
    那是设计如此，不是错位；archive 落根另有 archive_root_forbidden 管着。
    """
    if note_type == "archive":
        return None
    top_dirs = set((type_map or {}).values()) | {"00-索引", "_模板", "70-经验"}
    type_prefix = str((type_map or {}).get(note_type, "") or "")
    segs = [x for x in sd_norm.split("/") if x]
    if type_prefix and segs and segs[0] == type_prefix:
        segs = segs[1:]
    for seg in segs:
        if seg in top_dirs and seg != "70-经验":
            return {
                "error": "nested_top_dir_forbidden",
                "detail": f"subdir 里不能再出现顶层目录名：{seg}",
                "hint": "subdir 只填分类名（如 运维、研究）；顶层目录由 type 决定，不要手写 10-项目/ 这类前缀",
            }
    return None


def _guard_project_dir_fork(note_type: str, sd_norm: str) -> Optional[dict]:
    """项目 subdir 末段部署/deploy 分叉硬拦。"""
    if note_type != "project" or not sd_norm:
        return None
    last = sd_norm.split("/")[-1]
    if re.search(r"(部署|deploy|deployment)\s*$", last, re.I):
        return {
            "error": "project_dir_fork_forbidden",
            "detail": f"禁止新建带部署后缀的项目子目录：{last}",
            "hint": "并入同一项目目录（如 grokcli-2api/），不要 foo/ + foo 部署/ 双开",
        }
    return None


def _guard_archive_root(note_type: str, sd_norm: str) -> Optional[dict]:
    """archive 禁止落到 50-档案 根目录。"""
    if note_type != "archive":
        return None
    parts = [p for p in sd_norm.split("/") if p and p != "50-档案"]
    if len(parts) == 0:
        return {
            "error": "archive_root_forbidden",
            "detail": "禁止直接写到 50-档案 根目录",
            "hint": "请 subdir 指定 文档收敛_YYYY-MM-DD/子目录 或其它归档子路径",
        }
    return None


def _project_subdir_segments(note_type: str, sd_norm: str) -> Optional[List[str]]:
    """把 project 的 subdir 归一成「项目名 / 可选子目录」两段语义。

    create_note 允许 subdir 带顶层目录前缀（如 "10-项目/某某项目"），
    这段前缀不是层级，计数前必须剥掉；非 project 类型直接返回 None（不适用）。
    """
    if note_type != "project" or not sd_norm:
        return None
    segs = [p for p in sd_norm.split("/") if p]
    if segs and re.match(r"^\d{2}-", segs[0]):
        segs = segs[1:]
    return segs


def _guard_project_dir_action_word(note_type: str, sd_norm: str) -> Optional[dict]:
    """项目目录名以「动作词」结尾硬拦 —— 2026-10-26 新增。

    背景：实测 10-项目/ 下 69 个目录里，21 个是「一篇笔记一个目录」，
    目录名就是那次任务的标题（<项目>-整合服务 / <项目>-算法池总览UI /
    <长任务名>与动态结算 …）。根因是调用方把任务标题当 subdir 传进来，
    而 plan_server_write 一旦拿到 subdir 且落点父目录存在就会接管并建目录。

    项目目录应当是**名词**（一个真实存在的东西），不是**动作**（一次任务）。
    「整合服务」不是一个东西，是某个项目的一次动作 → 应归该项目目录。

    判定用两种模式，覆盖实测全部 21 个坏目录：
      A. 分隔符 + 动作词结尾：<项目>-整合服务 / dsh-会话与插件治理
      B. 无分隔符但整名以动作词结尾：<长任务名>以动作词收尾
    """
    segs = _project_subdir_segments(note_type, sd_norm)
    if not segs:
        return None
    first = segs[0]
    ACTION_TAIL = (
        r"整合服务|总览UI|开发记录|回测|采集|接入|移植|修复|审查|审计|"
        r"整改|排障|定稿|上线|落地|实现|验证|复盘|调研|方案|设计|"
        r"结论|锁定|账本|成绩单|服务器运维|治理|升级|改造|重构|清理|收敛|"
        r"统计走势图|新架构|数据源|结算|部署|迁移|运维|维护|排查|诊断|"
        r"测试库|测试机"
    )
    pat_a = re.compile(r"[-—_·](" + ACTION_TAIL + r")\s*$", re.I)
    pat_b = re.compile(r"(" + ACTION_TAIL + r")\s*$", re.I)
    hit = pat_a.search(first) or (pat_b.search(first) if len(first) >= 5 else None)
    if hit:
        return {
            "error": "project_dir_action_word_forbidden",
            "detail": f"项目目录名不能以动作词结尾：{first}",
            "hint": (
                "项目目录应是名词（一个真实存在的东西），不是一次任务。"
                "把这篇文档放进所属项目目录，例如 subdir=\"算法库\" 而不是 "
                "subdir=\"算法库-整合服务\"。新建项目请先在 "
                "10-项目/README.md 项目表登记。"
            ),
        }
    return None


def _guard_project_dir_depth(note_type: str, sd_norm: str) -> Optional[dict]:
    """project 的 subdir 有效层级 > 2 段硬拦 —— 2026-10-26 新增。

    合法形态只有三种：""、"<项目名>"、"<项目名>/<配置|更新|专题>"。
    出现 3 段说明调用方在造嵌套层级，长期会把库结构搞成迷宫。

    只约束 project：archive 的 50-档案 下镜像原结构（20-知识/工具研究）是设计如此，
    knowledge 的分类层级同理，都不该被这条拦掉。
    """
    segs = _project_subdir_segments(note_type, sd_norm)
    if segs is None:
        return None
    if len(segs) > 2:
        return {
            "error": "project_dir_depth_forbidden",
            "detail": f"项目 subdir 层级过深（{len(segs)} 段）：{sd_norm}",
            "hint": "只允许 <项目名> 或 <项目名>/<配置|更新|专题>，最多两段",
        }
    return None


def _guard_skill_dump(
    note_type: str, title_s: str, content_s: str
) -> Optional[dict]:
    """knowledge 下 skill 整包 dump 硬拦（≥20k 且无指针关键词）。"""
    if note_type != "knowledge":
        return None
    title_l = title_s.lower()
    if "skill" not in title_l or len(content_s) < 20000:
        return None
    pointer_ok = any(
        x in content_s[:1500]
        for x in ("权威来源", "指针", "skill_view", "~/.kb-skills")
    )
    if not pointer_ok:
        return {
            "error": "skill_dump_forbidden",
            "detail": "禁止把 skill 整包 dump 写入 20-知识（正文过长）",
            "hint": "skill 只放 ~/.kb-skills/；笔记侧最多写 ~600B 指针",
        }
    return None


def _same_basename_warnings(
    note_type: str,
    title_s: str,
    notes_dir: Optional[Path],
    sanitize_fn: Optional[Callable[[str], str]],
) -> List[dict]:
    """同名 basename 已存在 → warning（可选，依赖 notes_dir；不硬拦）。

    任何异常只降级为日志，不影响写入判定。
    """
    warnings: List[dict] = []
    try:
        if notes_dir is None:
            return warnings
        if sanitize_fn is None:
            from ..auth import sanitize as sanitize_fn  # type: ignore
        safe_title = sanitize_fn(title_s).replace(" ", "_")
        if safe_title and not (note_type == "project" and title_s == "README"):
            matches = []
            for f in notes_dir.rglob(f"{safe_title}.md"):
                rel = str(f.relative_to(notes_dir)).replace("\\", "/")
                if rel.startswith("50-档案/"):
                    continue
                matches.append(rel)
            if matches:
                warnings.append(
                    {
                        "kind": "same_basename_exists",
                        "paths": matches[:8],
                        "hint": "若 10-项目已有主文，20-知识只写指针/摘要；勿双主文",
                    }
                )
    except Exception:
        log.warning("write_policy.py 异常", exc_info=True)
    return warnings


def validate_write_hygiene(
    note_type: str,
    subdir: str,
    title: str,
    content: str,
    notes_dir: Optional[Path] = None,
    type_map: Optional[dict] = None,
    sanitize_fn: Optional[Callable[[str], str]] = None,
) -> Tuple[Optional[dict], List[dict]]:
    """写时卫生硬拦。返回 (error_dict|None, warnings)。

    规则与历史 routes_notes._hygiene_path_guards / MCP validate_write_hygiene 对齐：
    - 废弃路径 10-项目_知识库项目
    - 项目 subdir 末段部署/deploy 分叉
    - 项目目录名以动作词结尾（2026-10-26 新增，止「一篇一目录」）
    - 项目 subdir 深度 > 2 段（2026-10-26 新增）
    - archive 禁止落到 50-档案根
    - knowledge 下 skill 整包 dump（≥20k 且无指针关键词）
    - 同名 basename 已存在 → warning（不硬拦）

    本函数只做薄编排：校验规则拆到各 _guard_* / _same_basename_warnings，
    下面的调用顺序即校验优先级，与拆分前逐行一致；任何改动顺序前先看各函数 docstring。
    """
    warnings: List[dict] = []
    sd_norm, title_s, content_s = _norm_write_inputs(subdir, title, content)

    err = _guard_path_traversal(title_s, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_title_nonempty(title_s)
    if err is not None:
        return err, warnings

    err = _guard_deprecated_path(sd_norm, title_s)
    if err is not None:
        return err, warnings

    err = _guard_nested_top_dir(note_type, sd_norm, type_map)
    if err is not None:
        return err, warnings

    err = _guard_project_dir_fork(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_project_dir_action_word(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_project_dir_depth(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_archive_root(note_type, sd_norm)
    if err is not None:
        return err, warnings

    err = _guard_skill_dump(note_type, title_s, content_s)
    if err is not None:
        return err, warnings

    warnings.extend(
        _same_basename_warnings(note_type, title_s, notes_dir, sanitize_fn)
    )
    return None, warnings


def on_note_changed(
    action: str = "updated",
    force_search_refresh: bool = False,
    _hooks: Optional[Dict[str, Any]] = None,
) -> None:
    """统一写后出口：缓存失效 + 索引刷新 +（可选）mark dirty。

    - create / save / upload / smart_upload / batch_tag：默认 update_indexes（冷却）
    - delete / archive / batch_delete/archive / 显式 force：trigger_index_refresh(force=True)
    - 测试可注入 _hooks，避免碰生产索引
    """
    hooks = _hooks
    if hooks is None:
        # 延迟导入，避免循环依赖
        from ..routes import trigger_index_refresh, _update_indexes
        from ..routes.routes_notes import _invalidate_notes_cache
        from ..routes.routes_general import _invalidate_graph_cache

        hooks = {
            "invalidate_notes_cache": _invalidate_notes_cache,
            "invalidate_graph_cache": _invalidate_graph_cache,
            "update_indexes": _update_indexes,
            "trigger_index_refresh": trigger_index_refresh,
        }
        # 生产路径：尽量挂上 SearchIndex.mark_dirty（A2）
        try:
            from ..search import search_index

            hooks["mark_index_dirty"] = lambda: search_index.mark_dirty(reason=action)
        except Exception:
            log.warning("write_policy.py 异常", exc_info=True)

    inv_notes = hooks.get("invalidate_notes_cache")
    if callable(inv_notes):
        inv_notes()

    inv_graph = hooks.get("invalidate_graph_cache")
    if callable(inv_graph):
        inv_graph()

    mark_dirty = hooks.get("mark_index_dirty")
    if callable(mark_dirty):
        mark_dirty()

    force = bool(force_search_refresh) or action in FORCE_REFRESH_ACTIONS
    if force:
        # 与旧 delete 行为对齐：关闭 index_auto_rebuild 时不刷搜索
        auto = True
        try:
            from ..config import get_setting

            auto = bool(get_setting("index_auto_rebuild"))
        except Exception:
            log.warning("write_policy.py 异常: on_note_changed", exc_info=True)
            auto = True
        if auto or _hooks is not None:
            # 测试注入 hooks 时不读配置，直接 force 触发以便断言
            trig = hooks.get("trigger_index_refresh")
            if callable(trig):
                trig(force=True)
        return

    upd = hooks.get("update_indexes")
    if callable(upd):
        upd()
