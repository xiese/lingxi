#!/usr/bin/python3
"""知识库 API — 轻量入口（版本号见 kb_lib/config.py 的 VERSION）"""

import sys, os

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

# 确保包在当前目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kb_lib import create_app, HOST, PORT, HAS_JIEBA, start_background
from kb_lib.config import VERSION

app = create_app()

# 2026-10-07：后台启动动作（索引加载 / 主令牌初始化 / 令牌与限流清理循环）已从
# import 副作用收编为 start_background()，服务入口必须显式调用，否则线上不起
# 索引、不清令牌。
start_background()

if __name__ == "__main__":
    print("=" * 60)
    print("  📚 知识库 API v" + VERSION)
    print("=" * 60)
    print(f"  🌐 {HOST}:{PORT}  |  🖥 /web/")
    print(f"  📋 审计日志  |  ⚙ 管理设置")
    print(f"  🧠 jieba分词: {'ON' if HAS_JIEBA else 'OFF'}")
    print("=" * 60)
    try:
        from waitress import serve
        print("  🚀 waitress (生产级 WSGI, threads=8)")
        serve(app, host=HOST, port=PORT, threads=8, channel_timeout=120)
    except ImportError:
        print("  ⚠️ waitress 未安装，回退 Flask 开发服务器")
        app.run(host=HOST, port=PORT, debug=False)
