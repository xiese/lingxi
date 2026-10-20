#!/usr/bin/env python3
"""
知识库 CLI 工具 (kb-cli) — Python 版，无需编译
用法: python kb-cli.py <命令> [参数]
      或设置别名: alias kb-cli='python3 /path/to/kb-cli.py'

实现已拆分到同目录 kb_cli/ 包：client.py / commands.py / main.py
"""
import sys, os, io

# Windows 下强制 stdin/stdout/stderr 使用 UTF-8，避免 GBK 编码错误
if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kb_cli.main import main

if __name__ == "__main__":
    main()
