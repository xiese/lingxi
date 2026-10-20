#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb-cli 主入口：命令分发。"""
import sys, os

from .client import *
from .commands import *


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    # 不需要 Token 的命令
    no_auth_cmds = {"config", "version", "help", "-h", "--help", "-v", "--version"}
    if cmd not in no_auth_cmds:
        cfg = load_config()
        if not cfg.get("token"):
            die("❌ Token 未设置！请先配置：\n   python kb-cli.py config init\n   或: python kb-cli.py config set token <your-token>\n   或: 设置环境变量 KB_TOKEN")
    else:
        cfg = load_config()

    client = Client(cfg)

    dispatch = {
        "search": cmd_search,
        "read": cmd_read,
        "list": cmd_list,
        "create": cmd_create,
        "upload": cmd_upload,
        "up": cmd_upload,
        "update": cmd_update,
        "save": cmd_update,
        "delete": cmd_delete,
        "rm": cmd_delete,
        "related": cmd_related,
        "history": cmd_history,
        "status": cmd_status,
        "health": cmd_health,
        "rebuild": cmd_rebuild,
        "tags": cmd_tags,
        "graph": cmd_graph,
        "ask": cmd_ask,
        "config": cmd_config,
        "version": cmd_version,
    }

    if cmd in ("help", "-h", "--help"):
        print_usage()
        return
    if cmd in ("version", "-v", "--version"):
        cmd_version(client, args)
        return

    handler = dispatch.get(cmd)
    if not handler:
        die(f"❌ 未知命令: {cmd}\n  运行 'python kb-cli.py help' 查看帮助")

    handler(client, args)
