"""知识库 API — 文件上传/下载路由"""
import logging

from pathlib import Path
from flask import request, jsonify, send_file

from ..routes import bp, _dedup_path
from ..config import KB_ROOT, NOTES_DIR, ALLOWED_EXTENSIONS, TYPE_MAP, get_setting
from ..audit import audit_log
from ..auth import check_auth, safe, sanitize
from ..services import write_policy

# ── 敏感文件下载保护 ──
_SENSITIVE_FILE_MARKERS = (
    ".api_tokens.json", ".kb_settings.json", ".audit_log.jsonl",
    ".kb_mcp_usage.jsonl", ".search_index.pkl", ".search_index.json",
    ".search_index.json.arrays.npz", ".search_index.pkl.legacy", ".embeddings.npy",
    ".faiss.index", ".kb_auth_watch_state.json",
)
_SENSITIVE_EXTENSIONS = {".pkl", ".npy", ".npz", ".index", ".sqlite3"}
_DOWNLOAD_ALLOWED_DIRS = {"笔记", "_assets"}  # 只允许下载笔记和资源目录下的文件


log = logging.getLogger("kb.routes_files")

# 安全加固：这些类型按 inline 提供会被浏览器直接渲染/执行，存在脚本注入风险。
# 一律强制作为附件下载。
_FORCE_ATTACHMENT_EXT = {
    ".html", ".htm", ".svg", ".js", ".ts", ".css", ".xml", ".json",
}


def _is_safe_download(path: Path) -> bool:
    """检查文件路径是否可安全下载（阻止敏感文件泄露）"""
    name = path.name
    str_path = str(path)
    # 1. 阻止已知敏感文件
    if any(marker in str_path for marker in _SENSITIVE_FILE_MARKERS):
        return False
    # 2. 阻止敏感扩展名
    if path.suffix.lower() in _SENSITIVE_EXTENSIONS:
        return False
    # 3. 只允许在笔记和资源目录下下载
    try:
        rel = path.relative_to(KB_ROOT)
        top = rel.parts[0] if rel.parts else ""
        if top not in _DOWNLOAD_ALLOWED_DIRS:
            return False
    except ValueError:
        log.warning("routes_files.py 异常: _is_safe_download", exc_info=True)
        return False
    # 4. 阻止隐藏文件
    if name.startswith("."):
        return False
    return True


# ── 上传 ──

@bp.route("/upload", methods=["POST"])
def upload_file():
    tk, err, code = check_auth("w")
    if err:
        return err, code
    if "file" not in request.files:
        return jsonify({"error": "缺少file"}), 400
    file = request.files["file"]
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"不支持:{ext}"}), 400
    ftype = request.form.get("type", "")
    force_dir = request.form.get("dir", "")
    if force_dir:
        # 安全处理上传目录：逐段清理，防止路径穿越
        from pathlib import Path as _Path
        from ..auth import sanitize_path_segment
        segments = [sanitize_path_segment(s) for s in _Path(force_dir.strip()).parts if s and s != "."]
        if not segments or any(s == ".." for s in segments):
            return jsonify({"error": "非法上传目录"}), 400
        target = NOTES_DIR.joinpath(*segments)
        # 确保最终路径仍在 NOTES_DIR 内
        try:
            target.resolve().relative_to(NOTES_DIR.resolve())
        except ValueError:
            log.warning("routes_files.py 异常: upload_file", exc_info=True)
            return jsonify({"error": "非法上传目录"}), 400
    elif ftype in TYPE_MAP:
        target = NOTES_DIR / TYPE_MAP[ftype]
    elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        if ftype in TYPE_MAP:
            target = NOTES_DIR / TYPE_MAP[ftype] / "_assets"
        else:
            target = KB_ROOT / "_assets"
    elif ext in (".html", ".css", ".js", ".ts"):
        # 代码文件不直接放入 scripts 目录（防止代码注入），放入 _assets
        target = KB_ROOT / "_assets"
    else:
        target = KB_ROOT / "_assets"
    # ── 用户私有目录闸门（2026-09-24）──
    from ..config import USER_OWNED_DIRS, USER_OWNED_ACK
    try:
        _rt = target.resolve()
    except Exception:
        _rt = target
    _uo = [d for d in USER_OWNED_DIRS
           if _rt == (NOTES_DIR / d).resolve() or (NOTES_DIR / d).resolve() in _rt.parents]
    if _uo and request.form.get(USER_OWNED_ACK, "") != "user":
        return jsonify({
            "error": "user_owned_dir_forbidden",
            "detail": "目录「%s」为受保护的用户专属空间，AI 不得自行写入" % _uo[0],
            "hint": "经验教训与修复记录请写 10-项目/<项目名>/；确需写入请带 %s=user" % USER_OWNED_ACK,
        }), 403
    # ── 写入规范化（2026-10-16，仅 .md）──
    # 非 md 是二进制资源，落点由 _assets 规则决定，不归规范化管。
    # 任何异常都退回原逻辑，绝不阻断上传；KB_NORMALIZE_WRITES=0 可停用。
    _norm = None
    if ext == ".md":
        try:
            from ..services.note_normalizer import plan_server_write as _plan_norm
            _norm = _plan_norm(
                NOTES_DIR, title=Path(file.filename or "").stem, note_type=ftype,
                subdir=force_dir or "",
            )
        except Exception:
            log.warning("routes_files.py 写入规范化异常，退回原逻辑", exc_info=True)
            _norm = None
        if _norm:
            _nt2 = _norm["type"] or ftype
            _segs2 = [x for x in _norm["folder"].split("/") if x]
            _tp2 = str(TYPE_MAP.get(_nt2, "") or "")
            if _segs2 and _segs2[0] == _tp2:
                _segs2 = _segs2[1:]
            _g2err, _ = write_policy.validate_write_hygiene(
                note_type=_nt2, subdir="/".join(_segs2),
                title=Path(file.filename or "").stem, content="",
                notes_dir=NOTES_DIR, type_map=TYPE_MAP, sanitize_fn=sanitize,
            )
            if _g2err:
                log.warning("规范化落点被写时闸门拒绝，退回原逻辑: %s", _g2err)
                _norm = None
    if _norm:
        _tdir = NOTES_DIR / _norm["folder"]
        _tdir.mkdir(parents=True, exist_ok=True)
        _stem = Path(_norm["rel"]).stem
        dest = _dedup_path(_tdir, _stem, ext)
    else:
        target.mkdir(parents=True, exist_ok=True)
        fname = file.filename or "unnamed"
        _stem = sanitize(Path(fname).stem)
        dest = _dedup_path(target, _stem, ext)
    file.save(str(dest))
    try:
        rel = str(dest.relative_to(NOTES_DIR))
    except ValueError:
        log.warning("routes_files.py 异常: upload_file", exc_info=True)
        rel = str(dest.relative_to(KB_ROOT))
    if ext == ".md":
        # 方案 B：md 上传走统一写后出口（内部仍看 index_auto_rebuild）
        write_policy.on_note_changed(action="uploaded", force_search_refresh=False)
    audit_log("upload", rel, request.method, 201, extra=f"type={ftype or 'auto'} size={dest.stat().st_size}")
    payload = {"path": rel, "size": dest.stat().st_size, "filename": dest.name}
    if dest.stem != _stem:
        # 2026-10-11：同名文件已存在时 _dedup_path 会静默追加 _1/_2，
        # 上传方原以为覆盖、实际库里多一份。必须回传改名事实。
        payload["renamed"] = True
        payload["renamed_from"] = f"{_stem}{ext}"
    return jsonify(payload), 201


# ── 下载 ──

@bp.route("/files/<path:fp>")
def download_file(fp):
    tk, err, code = check_auth("r")
    if err:
        return err, code
    try:
        p = safe(fp)
    except Exception:
        log.warning("routes_files.py 异常: download_file", exc_info=True)
        return jsonify({"error": "路径非法"}), 400
    if not _is_safe_download(p):
        return jsonify({"error": "文件受保护，禁止下载"}), 403
    if not p.exists():
        return jsonify({"error": "不存在"}), 404
    if not p.is_file():
        # 与 _resolve_note 口径一致：目录不是文件，返 400 而非 500/404
        return jsonify({"error": "不是文件"}), 400
    # 危险类型走附件；其余维持 inline，保证图文可直接查看
    force = p.suffix.lower() in _FORCE_ATTACHMENT_EXT
    return send_file(str(p), as_attachment=force)