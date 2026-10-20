# MCP 服务端接入

MCP（Model Context Protocol）服务端让支持该协议的 AI 客户端能够直接检索、读取与
写入知识库。系统提供两种传输方式：

| 传输方式 | 脚本 | 适用场景 |
|---|---|---|
| stdio | `scripts/kb_mcp_server.py` | 客户端与服务端在同一台机器 |
| SSE | `scripts/kb_mcp_sse.py` | 客户端通过网络远程连接 |

两种方式共用同一套工具实现。

## 一、环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_HOST` | `http://127.0.0.1:8899` | REST API 地址 |
| `KB_ROOT` | 仓库根目录 | 知识库根目录 |
| `KB_TOKEN_FILE` | `$KB_ROOT/.api_tokens.json` | 令牌文件路径 |
| `KB_TOKEN` | 无 | 直接指定令牌，优先于令牌文件 |
| `KB_CLIENT_NAME` | `unknown` | 客户端标识，用于调用记录 |
| `KB_MCP_READONLY` | 无 | 设为 `1` 禁用所有写入类工具 |
| `KB_MCP_USAGE_LOG` | `$KB_ROOT/.kb_mcp_usage.jsonl` | 调用记录文件 |
| `KB_MCP_UPLOAD_MAX_BYTES` | `5242880` | 上传大小上限（字节） |

SSE 方式另有：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_MCP_SSE_HOST` | `127.0.0.1` | 监听地址 |
| `KB_MCP_SSE_PORT` | `8898` | 监听端口 |
| `KB_MCP_SSE_AUTH` | `1` | 设为 `0` 关闭鉴权，不建议 |
| `KB_MCP_SSE_TOKENS` | 无 | 额外的允许令牌，逗号分隔 |

## 二、stdio 方式

适用于客户端与服务端同机部署的场景。客户端配置示例：

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "python3",
      "args": ["/srv/knowledge-base/scripts/kb_mcp_server.py"],
      "env": {
        "KB_HOST": "http://127.0.0.1:8899",
        "KB_ROOT": "/srv/knowledge-base",
        "KB_CLIENT_NAME": "my-client"
      }
    }
  }
}
```

将路径替换为实际部署路径。若服务端运行在虚拟环境中，`command` 应指向该环境的
Python 解释器。

## 三、SSE 方式

### 启动服务

```bash
export KB_ROOT=/srv/knowledge-base
python3 scripts/kb_mcp_sse.py
```

默认监听 `127.0.0.1:8898`，端点路径为 `/sse`。

### 客户端配置

```json
{
  "mcpServers": {
    "knowledge-base": {
      "url": "http://127.0.0.1:8898/sse",
      "headers": {
        "Authorization": "Bearer <令牌>"
      }
    }
  }
}
```

### 对外提供服务

```bash
KB_MCP_SSE_HOST=0.0.0.0 python3 scripts/kb_mcp_sse.py
```

> 对外提供时**必须保留鉴权**（`KB_MCP_SSE_AUTH` 保持默认值 `1`），
> 并通过反向代理配置 HTTPS。

## 四、工具清单

| 工具 | 参数 | 说明 |
|---|---|---|
| `kb_search` | `query`, `limit`, `mode` | 检索知识库，返回相关笔记 |
| `kb_dual_search` | `query`, `limit` | 记忆与知识库双重检索 |
| `kb_mentions` | `needle`, `limit`, `offset` | 全库精确扫描某字符串的提及位置 |
| `kb_read` | `path` | 读取指定笔记 |
| `kb_list` | `directory`, `limit`, `offset` | 列出笔记 |
| `kb_create` | `title`, `content`, `note_type`, `subdir`, `tags`, `status`, `quality_check` | 创建或更新笔记 |
| `kb_create_project_log` | `project_name`, `goal`, `changed_files`, `key_implementation`, `verification`, `issues`, `next_steps`, `tags`, `status` | 按固定模板创建项目开发记录 |
| `kb_upload` | `file_path`, `target_dir`, `note_type`, `content`, `content_base64`, `filename` | 上传文件至知识库 |
| `kb_ask` | `question` | 基于知识库问答 |
| `kb_dashboard` | 无 | 查看统计概览 |
| `kb_index_status` | 无 | 查看索引状态 |
| `kb_index_rebuild` | 无 | 重建检索索引 |
| `kb_note_hygiene` | `quiet_ok` | 扫描笔记卫生问题，仅报告不修改 |
| `kb_memory_boundary_check` | `summary`, `task_type` | 判断内容应写入知识库还是长期记忆 |
| `memory_search` | `query`, `limit` | 检索外部长期记忆（可选集成） |

### 只读模式

设置 `KB_MCP_READONLY=1` 后，写入类工具（`kb_create`、`kb_upload`、
`kb_index_rebuild` 等）将被禁用，适合仅需检索能力的客户端。

### 上传大小限制

`kb_upload` 默认上限为 5 MB，可通过 `KB_MCP_UPLOAD_MAX_BYTES` 调整。

### 调用记录

每次工具调用会追加记录到 `$KB_ROOT/.kb_mcp_usage.jsonl`，可在 Web 面板查看，
或通过 `GET /api/mcp-usage` 查询。

### 可选集成

`memory_search` 与 `kb_dual_search` 依赖外部长期记忆存储，默认读取
`~/.kb-memory/memory.sqlite3`，或由 `KB_MEMORY_FILES` 指定的 Markdown 文件。
未配置时这两个工具返回空结果，不影响其余工具。
