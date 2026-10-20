#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb-cli 命令实现（各子命令）。"""
import sys, os, json, argparse, urllib.request, urllib.parse, urllib.error, io

from .client import *


def cmd_search(c, args):
    p = argparse.ArgumentParser(prog="kb-cli search", description="语义搜索笔记")
    p.add_argument("query", nargs="+", help="搜索关键词")
    p.add_argument("-limit", type=int, default=5, help="返回结果数")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)
    q = " ".join(opts.query)
    path = f"/api/search/semantic?q={urllib.parse.quote(q)}&limit={opts.limit}"
    raw, code, err = c.get(path)
    if err:
        die(f"❌ 搜索失败: {err}")
    if code in (401, 403):
        die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
    e = check_response(raw, code)
    if e:
        die(f"❌ 搜索失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    print(f'🔍 搜索 "{r.get("query", q)}" — 找到 {r.get("count", 0)} 条结果\n')
    for i, item in enumerate(r.get("items", []), 1):
        print(f'{i}. [{item.get("type", "?")}] {item.get("title", "?")}')
        print(f'   📄 {item.get("path", "")}')
        ds = item.get("display_score", item.get("score", 0))
        print(f'   📊 相关度: {ds*100:.1f}%')
        print(f'   📝 {truncate(item.get("snippet", ""))}\n')

def cmd_read(c, args):
    p = argparse.ArgumentParser(prog="kb-cli read", description="读取笔记内容")
    p.add_argument("path", nargs="+", help="笔记路径")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)
    note_path = " ".join(opts.path)
    path = "/notes/" + encode_note_path(note_path)
    raw, code, err = c.get(path)
    if err:
        die(f"❌ 读取失败: {err}")
    if code == 404:
        die(f"❌ 笔记不存在: {note_path}")
    if code in (401, 403):
        die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
    e = check_response(raw, code)
    if e:
        die(f"❌ 读取失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    print(f'📄 {r.get("path", note_path)}')
    meta = r.get("meta", {})
    if meta.get("title"):
        print(f'📌 标题: {meta["title"]}')
    tags = meta.get("tags", [])
    if tags:
        print(f'🏷  标签: {", ".join(tags)}')
    print("─" * 60)
    print(r.get("content", ""))

def cmd_list(c, args):
    p = argparse.ArgumentParser(prog="kb-cli list", description="列出笔记")
    p.add_argument("-limit", type=int, default=50, help="返回数量")
    p.add_argument("-offset", type=int, default=0, help="偏移量")
    p.add_argument("-dir", default="", help="目录过滤")
    p.add_argument("-tag", default="", help="标签过滤")
    p.add_argument("-type", dest="note_type", default="", help="类型过滤")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)
    params = {"limit": opts.limit, "offset": opts.offset}
    if opts.dir:
        params["dir"] = opts.dir
    if opts.tag:
        params["tag"] = opts.tag
    if opts.note_type:
        params["type"] = opts.note_type
    path = "/notes?" + urllib.parse.urlencode(params)
    raw, code, err = c.get(path)
    if err:
        die(f"❌ 列表失败: {err}")
    if code in (401, 403):
        die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
    e = check_response(raw, code)
    if e:
        die(f"❌ 列表失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    print(f'📋 笔记列表 (显示 {r.get("count", 0)} 条，共 {r.get("total", 0)} 条)\n')
    for item in r.get("items", []):
        tags = ", ".join(item.get("tags", []))
        if len(tags) > 40:
            tags = tags[:37] + "..."
        modified = item.get("modified", "")[:10]
        print(f'  [{item.get("type", "?")}] {item.get("title", "?"):<40} 📄 {item.get("path", "")} ({modified})')
        if tags:
            print(f"         🏷 {tags}")

def cmd_create(c, args):
    p = argparse.ArgumentParser(prog="kb-cli create", description="创建笔记")
    p.add_argument("-title", required=True, help="笔记标题")
    p.add_argument(
        "-type", dest="note_type", default="knowledge",
        help="笔记类型: project/knowledge/writing/record/diary/archive/dream",
    )
    p.add_argument("-subdir", default="", help="子目录")
    p.add_argument("-tags", default="", help="标签（逗号分隔）")
    p.add_argument("-relations", default="", help="关联笔记（逗号分隔）")
    p.add_argument("-status", default="draft", help="状态: draft/active/archived")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    p.add_argument("content", nargs="*", help="笔记内容（也可从 stdin 传入）")
    opts = p.parse_args(args)

    # 从 stdin 或参数读取内容
    content = ""
    if opts.content:
        content = " ".join(opts.content)
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    if not content:
        die("❌ 内容不能为空（通过参数或 stdin 传入）")

    body = {"title": opts.title, "content": content, "type": opts.note_type, "status": opts.status}
    if opts.subdir:
        body["subdir"] = opts.subdir
    if opts.tags:
        body["tags"] = [t.strip() for t in opts.tags.split(",") if t.strip()]
    if opts.relations:
        body["relations"] = [r.strip() for r in opts.relations.split(",") if r.strip()]

    raw, code, err = c.post("/notes", body)
    if err:
        die(f"❌ 创建失败: {err}")
    if code in (401, 403):
        die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
    e = check_response(raw, code, allowed=(200, 201))
    if e:
        die(f"❌ 创建失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    print(f'✅ {r.get("action", "done")}: {r.get("path", "")}')


def cmd_upload(c, args):
    """本机文件直传 REST /upload，不走 MCP。"""
    p = argparse.ArgumentParser(
        prog="kb-cli upload",
        description="上传本机文件到知识库（直连 REST /upload，不要套 MCP）",
    )
    p.add_argument("files", nargs="+", help="要上传的本地文件路径（可多个）")
    p.add_argument("-type", dest="note_type", default="", help="类型: knowledge/project/writing/diary/record")
    p.add_argument("-dir", dest="target_dir", default="", help="目标目录，如 20-知识/工具研究")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)

    ok_n = 0
    fail_n = 0
    results = []

    for fp in opts.files:
        path = os.path.expanduser(fp)
        if not os.path.isfile(path):
            msg = f"❌ 文件不存在: {fp}"
            if opts.json:
                results.append({"file": fp, "error": "missing"})
            else:
                print(msg)
            fail_n += 1
            continue

        with open(path, "rb") as f:
            content = f.read()
        filename = os.path.basename(path)
        fields = {}
        if opts.note_type:
            fields["type"] = opts.note_type
        if opts.target_dir:
            fields["dir"] = opts.target_dir

        raw, code, err = c.post_multipart(
            "/upload",
            fields=fields,
            files=[("file", filename, content, None)],
        )
        if err:
            if opts.json:
                results.append({"file": fp, "error": err})
            else:
                print(f"❌ 上传失败 {filename}: {err}")
            fail_n += 1
            continue
        if code in (401, 403):
            die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
        e = check_response(raw, code, allowed=(200, 201))
        if e:
            if opts.json:
                results.append({"file": fp, "error": e, "raw": raw.decode("utf-8", errors="replace")[:500]})
            else:
                print(f"❌ 上传失败 {filename}: {e}")
            fail_n += 1
            continue

        r = parse_json(raw)
        if opts.json:
            results.append({"file": fp, "ok": True, **r})
        else:
            print(f'✅ 已上传: {r.get("path", filename)}  ({r.get("size", len(content))} bytes)')
        ok_n += 1

    if opts.json:
        print(json.dumps({"ok": ok_n, "failed": fail_n, "results": results}, ensure_ascii=False, indent=2))
    else:
        if len(opts.files) > 1:
            print(f"\n完成: 成功 {ok_n}，失败 {fail_n}")
    if fail_n and ok_n == 0:
        sys.exit(1)

def cmd_update(c, args):
    p = argparse.ArgumentParser(prog="kb-cli update", description="更新笔记内容（从 stdin 传入）")
    p.add_argument("path", help="笔记路径")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)

    if sys.stdin.isatty():
        die("❌ 请通过 stdin 传入笔记内容\n   示例: cat note.md | python kb-cli.py update 10-项目/xxx.md")
    content = sys.stdin.read()
    if not content:
        die("❌ 内容不能为空")

    body = {"content": content}
    path = "/notes/" + encode_note_path(opts.path)
    raw, code, err = c.post(path, body)
    if err:
        die(f"❌ 更新失败: {err}")
    if code == 404:
        die(f"❌ 笔记不存在: {opts.path}")
    if code in (401, 403):
        die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
    e = check_response(raw, code)
    if e:
        die(f"❌ 更新失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    print(f'✅ {r.get("action", "saved")}: {r.get("path", opts.path)}')

def cmd_delete(c, args):
    p = argparse.ArgumentParser(prog="kb-cli delete", description="归档笔记（移入 50-档案）")
    p.add_argument("path", help="笔记路径")
    p.add_argument("-force", action="store_true", help="跳过确认")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)

    if not opts.force:
        resp = input(f'⚠️  确认归档笔记 "{opts.path}"? (y/N): ')
        if resp.lower() != "y":
            print("已取消")
            return

    path = "/notes/" + encode_note_path(opts.path)
    raw, code, err = c.delete(path)
    if err:
        die(f"❌ 删除失败: {err}")
    if code == 404:
        die(f"❌ 笔记不存在: {opts.path}")
    if code in (401, 403):
        die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
    e = check_response(raw, code)
    if e:
        die(f"❌ 删除失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    print(f'✅ {r.get("action", "archived")} → {r.get("to", "")}')

def cmd_related(c, args):
    p = argparse.ArgumentParser(prog="kb-cli related", description="查看相关笔记")
    p.add_argument("path", help="笔记路径")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)
    path = "/api/notes/" + encode_note_path(opts.path) + "/related"
    raw, code, err = c.get(path)
    if err:
        die(f"❌ 获取失败: {err}")
    if code == 404:
        die(f"❌ 笔记不存在: {opts.path}")
    e = check_response(raw, code)
    if e:
        die(f"❌ 获取失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    print(f'🔗 相关笔记: {r.get("path", opts.path)}\n')
    for i, item in enumerate(r.get("related", []), 1):
        ds = item.get("display_score", item.get("score", 0))
        print(f'{i}. [{item.get("type", "?")}] {item.get("title", "?")}')
        print(f'   📄 {item.get("path", "")}  (相关度: {ds*100:.1f}%)')

def cmd_history(c, args):
    p = argparse.ArgumentParser(prog="kb-cli history", description="查看版本历史")
    p.add_argument("path", help="笔记路径")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)
    path = "/api/notes/" + encode_note_path(opts.path) + "/history"
    raw, code, err = c.get(path)
    if err:
        die(f"❌ 获取失败: {err}")
    if code == 404:
        die(f"❌ 笔记不存在: {opts.path}")
    e = check_response(raw, code)
    if e:
        die(f"❌ 获取失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    commits = r.get("commits", [])
    print(f'📜 版本历史: {r.get("path", opts.path)} ({len(commits)} 次提交)\n')
    for i, commit in enumerate(commits, 1):
        h = commit.get("hash", "")[:8]
        t = commit.get("time", "")[:19]
        msg = commit.get("message", "")
        author = commit.get("author", "")
        print(f"{i}. {h}  {t}  {msg}")
        print(f"   作者: {author}")

def cmd_status(c, args):
    raw, code, err = c.get("/api/index/status")
    if err:
        die(f"❌ 获取状态失败: {err}")
    e = check_response(raw, code)
    if e:
        die(f"❌ 获取状态失败: {e}")
    idx = parse_json(raw)

    raw2, _, _ = c.get("/dashboard")
    dash = parse_json(raw2)

    print("📊 知识库状态")
    print("═" * 40)
    print(f'  📁 笔记总数: {dash.get("total", 0)} (项目 {dash.get("projects", 0)} / 知识 {dash.get("knowledge", 0)} / 日记 {dash.get("diaries", 0)} / 归档 {dash.get("archived", 0)})')
    print(f'  🔍 索引笔记: {idx.get("indexed", 0)}')
    print(f'  🧠 BM25 索引: {yn(idx.get("has_index", False))}')
    print(f'  📐 向量嵌入: {yn(idx.get("has_embeddings", False))}')
    print(f'  ⚡ FAISS 加速: {yn(idx.get("has_faiss", False))}')
    print(f'  🔄 待更新: {yn(idx.get("dirty", False))}')

def cmd_health(c, args):
    raw, code, err = c.get("/health")
    if err:
        die(f"❌ 健康检查失败: {err}")
    e = check_response(raw, code)
    if e:
        die(f"❌ 健康检查失败: {e}")
    h = parse_json(raw)
    print("🏥 健康检查")
    print("═" * 40)
    print(f'  状态: {h.get("status", "?")}')
    print(f'  服务: {h.get("service", "?")}')
    print(f'  版本: v{h.get("version", "?")}')
    print(f'  索引: {h.get("indexed", 0)} 篇')
    print(f'  分词: {yn(h.get("jieba", False))}')
    print(f'  时间: {h.get("time", "?")}')

def cmd_rebuild(c, args):
    raw, code, err = c.post("/api/index/rebuild")
    if err:
        die(f"❌ 重建失败: {err}")
    e = check_response(raw, code)
    if e:
        die(f"❌ 重建失败: {e}")
    print("✅ 索引重建已触发，数秒后生效")

def cmd_tags(c, args):
    raw, code, err = c.get("/api/tags")
    if err:
        die(f"❌ 获取标签失败: {err}")
    e = check_response(raw, code)
    if e:
        die(f"❌ 获取标签失败: {e}")
    r = parse_json(raw)
    print(f'🏷 标签云 (共 {r.get("total", 0)} 个)\n')
    for t in r.get("tags", []):
        print(f'  {t.get("name", ""):<30} {t.get("count", 0)} 篇')

def cmd_ask(c, args):
    if not args:
        die("用法: kb-cli ask <问题>")
    q = " ".join(args)
    raw, code, err = c.post("/api/llm/ask", {"q": q})
    if err:
        die(f"❌ 问答失败: {err}")
    if code in (401, 403):
        die(f"❌ 认证失败 (HTTP {code}): Token 可能已过期或未设置")
    e = check_response(raw, code)
    if e:
        die(f"❌ 问答失败: {e}")
    r = parse_json(raw)
    print(f'❓ 问题: {r.get("question", q)}\n')
    print("💬 回答:")
    print(r.get("answer", ""))
    sources = r.get("sources", [])
    if sources:
        print("\n📚 来源:")
        for s in sources:
            print(f"  • {s}")

def cmd_graph(c, args):
    p = argparse.ArgumentParser(prog="kb-cli graph", description="查看知识图谱")
    p.add_argument("-json", action="store_true", help="输出 JSON 格式")
    opts = p.parse_args(args)
    raw, code, err = c.get("/api/graph")
    if err:
        die(f"❌ 获取图谱失败: {err}")
    e = check_response(raw, code)
    if e:
        die(f"❌ 获取图谱失败: {e}")
    if opts.json:
        print(raw.decode("utf-8"))
        return
    r = parse_json(raw)
    meta = r.get("meta", {})
    nodes = r.get("nodes", [])
    print(f'🕸 知识图谱: {meta.get("node_count", len(nodes))} 节点, {meta.get("link_count", 0)} 连接\n')
    # PageRank Top 10
    ranked = sorted(nodes, key=lambda n: n.get("pagerank", 0), reverse=True)
    limit = min(10, len(ranked))
    print("📈 PageRank Top 10:")
    for i in range(limit):
        n = ranked[i]
        print(f'  {i+1}. {truncate(n.get("title", ""), 40):<42} PR={n.get("pagerank", 0):.6f}  度={n.get("degree", 0)}')

def cmd_config(c, args):
    if not args:
        cfg = load_config()
        print("⚙️  当前配置:")
        print("═" * 40)
        print(f'  API 地址: {cfg["api_url"]}')
        print(f'  Token:   {mask_token(cfg.get("token", ""))}')
        print(f'  配置文件: {CONFIG_PATH}')
        return

    sub = args[0]
    if sub == "set":
        if len(args) < 3:
            die("用法: kb-cli config set <key> <value>\n  key: api_url, token")
        key, value = args[1], args[2]
        cfg = load_config()
        if key in ("api_url", "api"):
            cfg["api_url"] = value
        elif key == "token":
            cfg["token"] = value
        else:
            die(f"❌ 未知配置项: {key} (支持: api_url, token)")
        save_config(cfg)
        print("✅ 配置已保存")
    elif sub == "init":
        cfg = load_config()
        api = input(f'API 地址 [默认: {cfg["api_url"]}]: ').strip()
        if api:
            cfg["api_url"] = api
        token = input("Token: ").strip()
        if token:
            cfg["token"] = token
        save_config(cfg)
        print(f"✅ 配置已初始化到: {CONFIG_PATH}")
    else:
        die(f"❌ 未知子命令: {sub} (支持: set, init)")

def cmd_version(c, args):
    print(f"kb-cli v{CLIVERSION}")
    raw, code, _ = c.get("/api/version")
    if code == 200:
        v = parse_json(raw)
        print(f'服务端: v{v.get("server", "?")} (skill: v{v.get("skill", "?")})')

# ── 帮助 ──

def print_usage():
    print(f"""📚 知识库 CLI 工具 (kb-cli v{CLIVERSION}) — Python 版，无需编译

用法:
  python kb-cli.py <命令> [参数]

命令:
  search   <关键词>              语义搜索笔记
  read     <路径>                读取笔记内容
  list     [-limit N] [-dir D]   列出笔记
  create   -title "标题"          创建笔记（内容从 stdin 或参数传入）
  upload   <文件...>             上传本机文件（直连 REST /upload，不走 MCP）
  update   <路径>                更新笔记内容（从 stdin 传入）
  delete   <路径>                归档笔记（移入 50-档案）
  related  <路径>                查看相关笔记
  history  <路径>                查看版本历史
  status                         查看索引和统计状态
  health                         健康检查
  rebuild                        重建搜索索引
  tags                           查看标签云
  graph                          查看知识图谱
  ask      <问题>                基于知识库问答
  config   [set <key> <value>]   查看/设置配置
  version                        查看版本信息

配置:
  配置文件: ~/.kb-cli.json
  环境变量: KB_API (API 地址), KB_TOKEN (认证 Token)
  优先级: 环境变量 > 配置文件 > 默认值

路径选择：
  本机上传/创建/更新  →  kb-cli / REST 直连 8899
  AI 自动检索入库    →  MCP（Cursor/Claude 等支持 MCP 的客户端）
  不要: 本机 CLI → MCP → kb_upload

示例:
  python kb-cli.py config init                     # 首次使用，交互式配置
  python kb-cli.py config set token <your-token>   # 设置 Token
  python kb-cli.py search 知识库开发
  python kb-cli.py search -limit 10 -json Go 语言  # JSON 格式输出（适合脚本）
  python kb-cli.py read 10-项目/示例项目.md
  python kb-cli.py list -limit 20 -tag Go
  python kb-cli.py create -title "测试笔记" -type knowledge -tags 测试,Go
  echo "内容..." | python kb-cli.py create -title "标题"
  python kb-cli.py upload 笔记.md                  # 本机文件直传
  python kb-cli.py upload a.md b.pdf -type knowledge
  python kb-cli.py upload 笔记.md -dir 20-知识/工具研究
  cat note.md | python kb-cli.py update 10-项目/xxx.md
  python kb-cli.py delete 20-知识/旧笔记.md
  python kb-cli.py related 10-项目/知识库项目/README.md
  python kb-cli.py history 10-项目/知识库项目/README.md
  python kb-cli.py graph -json                     # 图谱数据（用于可视化）
  python kb-cli.py ask 知识库用了什么搜索技术""")

# ── 主入口 ──
