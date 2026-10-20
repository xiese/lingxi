"""pytest 全局配置。

kb_lib 在导入时会创建库根目录下的状态文件（`.api_tokens.json`、
`.kb_settings.json`、`.audit.db` 等）。为了让测试不污染仓库工作区，
这里在导入任何被测模块之前，先把库根目录指向一个临时目录。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="kbtest-root-"))
(_TMP_ROOT / "笔记").mkdir(parents=True, exist_ok=True)

# 必须在 import kb_lib 之前设置：config 在模块导入时读取该变量
os.environ["KB_ROOT"] = str(_TMP_ROOT)
os.environ.setdefault("KB_HOST", "http://127.0.0.1:8899")

# 令牌表相关变量必须与宿主环境隔离。
# 背景（2026-10-17 实测）：在导出了 KB_TOKEN 的 shell 里跑 pytest，
# `test_K1_空令牌表时build_mcp_auth必须抛错` 会失败 —— `_load_sse_auth_tokens`
# 读到了宿主令牌，令牌表非空，于是不抛 RuntimeError。
# 该用例断言的是「空令牌表必须拒绝启动」，前提就是「表是空的」；
# 环境里带着令牌时这个前提不成立，属于**测试环境污染**，不是代码缺陷。
# 修法是隔离环境（这里），不是改断言。
for _leaked in ("KB_TOKEN", "KB_MCP_SSE_TOKENS"):
    os.environ.pop(_leaked, None)
