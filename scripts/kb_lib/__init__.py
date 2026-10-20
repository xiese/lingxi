"""知识库 REST API — 应用工厂（FAISS + BM25 检索；版本见 config.py VERSION）"""

import threading, time
import logging
from flask import Flask, jsonify

from .config import (
    HOST, PORT, WEB_DIR, SETTINGS_FILE, DEFAULT_SETTINGS,
    KB_ROOT, NOTES_DIR, HAS_JIEBA, load_settings, save_settings,
)
from .store import init_master, load_store, store_transaction, is_expired
from .audit import log_request
from .search import _init_index
from .routes import bp, start_rate_cleanup
from .routes.routes_auth import start_auth_limit_cleanup


log = logging.getLogger("kb.__init__")

# ── 安全响应头（CSP / 点击劫持 / 嗅探 / Referrer）──
#
# 全站此前没有任何安全响应头，浏览器只能按默认策略处理：
#   · 无 CSP       → 一旦有内容绕过转义进入页面，可任意加载外部脚本
#   · 无 frame-ancestors / X-Frame-Options → 可被任意站点 iframe 嵌套做点击劫持
#   · 无 nosniff   → 上传的 .txt/.md 可能被嗅探成 HTML 执行
# 下面逐项补上。
#
# 关于 script-src 的 'unsafe-inline'：index.html 里有 73 个内联事件处理器
# （onclick="rn('...')" 等），app.js 还会动态生成更多。CSP 的 nonce 机制不覆盖
# 事件处理器属性，要彻底去掉 'unsafe-inline' 必须先把这批处理器改成
# addEventListener —— 属于独立重构，本次不做。当前策略仍能挡住主要攻击面：
#   · default-src 'self' 且不列任何外部主机 → 无法加载 //evil.com/x.js
#   · object-src 'none' / base-uri 'none'  → 无法用 <object>/<base> 改写脚本来源
#   · frame-ancestors 'none'               → 无法被嵌套
# 配合 app.js 中已加固的 marked 渲染器（转义原始 HTML、限制链接协议），
# 已能阻断「笔记内容里塞 <img onerror> / javascript: 链接」这类注入。
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def security_headers():
    """Flask after_request 钩子：补齐安全响应头"""
    def hook(response):
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response

    return hook


def create_app():
    """应用工厂：注册蓝图 + 审计钩子 + 安全响应头"""
    app = Flask(__name__, static_folder=None)
    # 安全加固：原实现没有任何请求体大小上限，RATE_LIMIT 的 upload
    # 档只限 20 次/60 秒、不限单次大小，一个写令牌即可把磁盘写满。
    # 这里设 32MB 总请求体上限，超限由 Flask 直接回 413。
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

    @app.errorhandler(413)
    def _too_large(e):
        return jsonify({
            "error": "payload_too_large",
            "detail": "请求体超过 32MB 上限",
            "hint": "上传大文件请分批；索引三产物由服务端自行生成，无需上传",
        }), 413

    app.register_blueprint(bp)
    app.after_request(log_request())
    app.after_request(security_headers())
    return app


# ── 后台启动动作（2026-10-07 修复：import 即起线程）──
#
# 原实现在模块导入时无条件执行 init_master() + _init_index + 清理线程：
# 任何 import kb_lib 的进程（CLI、测试 collect，以及 kb_auth_watch / kb_eval /
# kb_note_hygiene / kb_compile_wiki 这些只碰 kb_lib.config 等子模块、同样会
# 触发包初始化的脚本）都会拉真实索引加载、写令牌文件与索引文件。现在 import
# 只做声明，启动动作收进 start_background()，由服务入口显式调用；重复调用幂等。
#
# 调用方清单（漏一个 = 线上少干一件事）：
#   · kb-server.py        —— REST/Web 主服务，必须调（已加）。
#   · kb_mcp_sse.py       —— 经 kb_mcp_server 走 HTTP，不导入 kb_lib，无需调。
#   · kb-cli / kb_eval / kb_auth_watch / kb_note_hygiene / kb_compile_wiki
#                         —— 不消费索引与清理循环，保持 import-only。
master_store = None
_bg_started = False
_bg_lock = threading.Lock()


def cleanup_loop():
    """Token 过期清理循环：每 60 秒清理一次过期令牌（store_transaction 串行化）。"""
    while True:
        time.sleep(60)
        try:
            with store_transaction() as s:
                expired = [k for k, v in s.get("tokens", {}).items() if is_expired(v)]
                for k in expired:
                    del s["tokens"][k]
        except Exception:
            log.warning("__init__.py 异常", exc_info=True)


def start_background():
    """显式启动后台任务：设置文件兜底、主令牌初始化、索引加载、令牌/限流清理循环。

    只在服务入口调用（kb-server.py）；测试与 CLI 不要调用。
    幂等：重复调用不会叠加线程。
    """
    global master_store, _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True

    # 库根目录兜底：KB_ROOT 指向的路径可能尚不存在（首次部署、或换了新路径
    # 但忘了先建目录）。此前只在写设置文件时才暴露，报的是临时文件名 +
    # FileNotFoundError，看不出「根目录不存在」。这里统一先建好，并给出明确提示。
    if not KB_ROOT.exists():
        KB_ROOT.mkdir(parents=True, exist_ok=True)
        log.warning("库根目录不存在，已自动创建: %s", KB_ROOT)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    # 设置文件兜底：仅缺失时创建，不覆盖已有配置
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS)

    master_store = init_master()

    # 后台启动索引加载（load → 缺失则 build → 增量刷新 + 5 分钟周期刷新）
    threading.Thread(target=_init_index, daemon=True, name="kb-index-init").start()

    # Token 过期清理循环
    threading.Thread(target=cleanup_loop, daemon=True, name="kb-token-cleanup").start()

    # 限流记录清理循环（原在 routes/__init__.py 模块级起线程，一并收编到这里）
    start_rate_cleanup()

    # 鉴权请求限流记录清理循环（原在 routes/routes_auth.py:40 模块级起线程，
    # 2026-10-07 第二轮收编；线程职责/300s 间隔/退出条件不变）
    start_auth_limit_cleanup()
