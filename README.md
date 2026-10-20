# 灵犀 · lingxi

> one thread, both ends. —— 一根线，两头通。

本地优先的个人知识库系统。以纯 Markdown 存储笔记，提供混合检索、REST API、
Web 管理面板、MCP 服务端与命令行客户端。

笔记以文件形式存放，不被专有数据库锁定，可随时用任意编辑器读写或迁移。

## 特性

| 模块 | 说明 |
|---|---|
| 混合检索 | BM25 关键词检索与向量检索（FAISS）结果融合；向量检索可选，未配置外部嵌入服务时自动仅用关键词检索 |
| 精确定位 | `mentions` 接口对全库做确定性扫描，回答「哪些笔记提到了某字符串」，与相关性排序互补 |
| REST API | Flask + waitress，共 70 个路由，覆盖笔记读写、检索、索引、鉴权、审计、集成 |
| Web 面板 | 单页应用，支持检索、阅读、编辑、上传、关系图谱、问答与 PWA 离线访问 |
| MCP 服务 | 15 个工具，支持 stdio 与 SSE 两种传输方式 |
| 命令行 | `kb-cli`，覆盖检索、阅读、创建、上传、重建索引等日常操作 |
| 索引一致性 | 维护内容、向量、FAISS 三产物的计数清单，支持增量刷新与陈旧检测 |
| 笔记治理 | frontmatter 分层、标签治理、断链检查、重复与空壳扫描（仅报告，不修改） |
| 鉴权与审计 | 主令牌与设备令牌（可设有效期）两级，含设备申请审批流程；SQLite 审计库 |

## 环境要求

- Python 3.10 或更高版本
- Linux 或 macOS；Windows 建议使用 WSL
- 必需依赖见 [requirements.txt](requirements.txt)（Flask / waitress / numpy / rank-bm25 / jieba / fastmcp）
- 可选依赖见 [requirements-optional.txt](requirements-optional.txt)：`faiss-cpu` 与 `scikit-learn`
  用于向量检索加速（缺失时自动降级，仅慢一些）

默认零配置即可启动，检索走 BM25 关键词模式，**不需要任何外部服务**。
如需向量检索，在「设置」里配置 `embedding_base` 与 `embedding_model`，指向一个兼容
OpenAI `/v1/embeddings` 的 HTTP 服务即可——本仓库不含本地推理引擎，不会在安装阶段下载模型。

## 快速开始

```bash
# 1. 获取代码并安装依赖
git clone <你的仓库地址> knowledge-base
cd knowledge-base
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 指定知识库根目录（笔记与运行期状态文件的存放位置）
export KB_ROOT=/srv/knowledge-base
mkdir -p "$KB_ROOT/笔记"

# 3. 启动 API 服务（默认仅监听 127.0.0.1:8899）
python3 scripts/kb-server.py

# 4. 读取主令牌（首次启动时自动生成）
cat "$KB_ROOT/.api_tokens.json"
```

服务启动后访问 `http://127.0.0.1:8899/web/` 打开 Web 面板。

> **安全提示**
> 默认仅监听本机回环地址。若需对外提供服务，请设置 `KB_HOST=0.0.0.0`
> **并同时配置反向代理、HTTPS 与访问令牌**。请勿将未配置鉴权的服务直接暴露于公网。

## 配置

所有配置均可通过环境变量提供，完整清单见 [.env.example](.env.example)。
该文件只是清单示例，程序不会自动读取 `.env`；请用 `export` 或 systemd 的
`EnvironmentFile` 传入，详见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_ROOT` | 仓库根目录 | 笔记与运行期状态文件的存放位置 |
| `KB_HOST` | `127.0.0.1` | API 监听地址 |
| `KB_PORT` | `8899` | API 监听端口 |
| `KB_API` | `http://127.0.0.1:8899` | 客户端连接地址 |
| `KB_TOKEN` | 无 | 客户端访问令牌 |
| `KB_MCP_SSE_HOST` | `127.0.0.1` | MCP SSE 监听地址 |
| `KB_MCP_SSE_PORT` | `8898` | MCP SSE 监听端口 |
| `KB_MCP_SSE_URL` | `http://127.0.0.1:8898/sse` | 面板展示用的 MCP SSE 地址 |

### 关于检索模式

系统默认使用 **BM25 关键词检索**，无需任何外部服务即可工作。

如需启用向量检索（可提升语义相近但用词不同的检索效果），需配置任一所支持的
嵌入服务，并将检索模式切换为混合模式。配置方法见
[docs/CONFIGURATION.md](docs/CONFIGURATION.md)。

LLM 相关功能（问答、摘要、标签生成、智能整理）为可选能力，未配置时对应接口会返回
明确的未配置提示，不影响其余功能。

## 笔记目录结构

`KB_ROOT` 下按笔记类型分目录，目录名与笔记的 `type` 字段一一对应：

```
$KB_ROOT/
├── 笔记/
│   ├── 10-项目/       type: project
│   ├── 20-知识/       type: knowledge
│   ├── 30-写作/       type: writing
│   ├── 30-记录/       type: record
│   ├── 40-日记/       type: diary
│   ├── 50-档案/       type: archive
│   ├── 60-梦境/       type: dream
│   └── 70-经验/       受保护目录，写入须显式携带 owner_ack=user
└── 00-索引/           自动生成的标签索引与最近更新
```

笔记使用 YAML frontmatter：

```markdown
---
title: "标题"
type: knowledge
created: 2026-01-01
tags: [调研, MCP]
---

正文内容
```

## 命令行客户端

```bash
# 初始化配置（交互式）
python3 scripts/kb-cli/kb-cli.py config init

# 或直接指定
python3 scripts/kb-cli/kb-cli.py config set api_url http://127.0.0.1:8899
python3 scripts/kb-cli/kb-cli.py config set token <令牌>

# 日常操作
python3 scripts/kb-cli/kb-cli.py search 关键词
python3 scripts/kb-cli/kb-cli.py read "20-知识/示例.md"
python3 scripts/kb-cli/kb-cli.py list -dir 20-知识 -limit 20
python3 scripts/kb-cli/kb-cli.py upload 笔记.md
python3 scripts/kb-cli/kb-cli.py rebuild
python3 scripts/kb-cli/kb-cli.py health
```

也可使用环境变量替代配置文件：

```bash
export KB_API=http://127.0.0.1:8899
export KB_TOKEN=<令牌>
```

详细用法见 [客户端接入指南](scripts/kb-cli/客户端接入指南.md)。

## MCP 接入

服务端提供 stdio 与 SSE 两种传输方式。

**stdio（本机 AI 客户端）**

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "python3",
      "args": ["/srv/knowledge-base/scripts/kb_mcp_server.py"],
      "env": {
        "KB_HOST": "http://127.0.0.1:8899",
        "KB_ROOT": "/srv/knowledge-base"
      }
    }
  }
}
```

**SSE（远程接入）**

```bash
export KB_ROOT=/srv/knowledge-base
python3 scripts/kb_mcp_sse.py     # 默认监听 127.0.0.1:8898
```

提供的工具：`kb_search`、`kb_dual_search`、`kb_mentions`、`kb_read`、`kb_list`、
`kb_create`、`kb_create_project_log`、`kb_ask`、`kb_upload`、`kb_dashboard`、
`kb_index_status`、`kb_index_rebuild`、`kb_note_hygiene`、
`kb_memory_boundary_check`、`memory_search`。

## 主要接口

完整路由清单见 [docs/API.md](docs/API.md)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/version` | 版本信息 |
| GET | `/api/dashboard` | 看板统计 |
| GET | `/search` | 全文检索 |
| GET | `/api/search/semantic` | 混合检索（相关性排序） |
| GET | `/api/search/mentions` | 全库精确提及扫描 |
| GET / POST | `/notes` | 列出 / 创建笔记 |
| GET / POST / DELETE | `/notes/<path>` | 读取 / 更新 / 删除笔记 |
| POST | `/upload` | 上传文件 |
| POST | `/api/index/rebuild` | 重建索引 |
| GET | `/api/index/status` | 索引状态 |
| GET | `/api/graph` | 笔记关系图谱 |
| GET | `/api/tags` | 标签列表 |

## 运行测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

测试通过 `tests/conftest.py` 将知识库根目录指向临时目录，不会读写真实的笔记库。

## 部署

### systemd

`scripts/kb-server.service` 为单元文件模板。使用前请将其中的 `/srv/knowledge-base`
替换为实际部署路径：

```bash
sudo cp scripts/kb-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kb-server
```

### 反向代理

`scripts/nginx-kb.conf` 提供了 Nginx 反向代理配置示例，含 HTTPS 与域名占位符。

### 运维脚本

`scripts/` 下提供若干辅助脚本，覆盖健康检查、索引更新、备份、断链检查等场景，
均可通过 `bash scripts/<脚本名>.sh` 调用。各脚本头部注释说明了用法。

## 目录结构

```
├── requirements.txt                必需依赖
├── requirements-optional.txt       向量检索可选依赖
├── requirements-dev.txt            测试依赖
├── scripts/
│   ├── kb-server.py               REST API 入口
│   ├── kb-server.service          systemd 单元文件模板
│   ├── nginx-kb.conf              反向代理配置示例
│   ├── kb_lib/                    API 核心实现
│   │   ├── config.py              配置与常量
│   │   ├── auth.py                鉴权与路径校验
│   │   ├── store.py               令牌存储
│   │   ├── search.py              混合检索与索引
│   │   ├── embed.py               向量化与精排调用
│   │   ├── audit.py               审计日志
│   │   ├── graph.py               关系图谱
│   │   ├── llm.py                 LLM 调用
│   │   ├── routes/                路由（按功能域拆分）
│   │   └── services/              领域服务
│   ├── kb_mcp_server.py           MCP 服务端（stdio）
│   ├── kb_mcp_sse.py              MCP 服务端（SSE）
│   ├── kb-cli/                    命令行客户端
│   ├── web/                       Web 面板
│   └── *.sh                       运维脚本
└── tests/                         测试套件
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | 安装与部署 |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | 全部配置项与外部服务接入 |
| [docs/API.md](docs/API.md) | 接口参考 |
| [docs/MCP.md](docs/MCP.md) | MCP 服务端接入 |
| [docs/CLI.md](docs/CLI.md) | 命令行客户端 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构与数据流 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 常见问题排查 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 开发与测试 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [SECURITY.md](SECURITY.md) | 安全策略与漏洞报告 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 行为准则 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

## 说明

- 笔记内容属于个人数据，默认不纳入版本控制，详见 `.gitignore`。
- 运行期状态文件（令牌、索引、审计库）在首次启动时自动生成于 `KB_ROOT`，
  不在仓库内产生。

## 许可

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。
