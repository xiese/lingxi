# 配置说明

系统配置分为三类：环境变量、设置文件、令牌文件。

| 类别 | 位置 | 用途 | 是否入库 |
|---|---|---|---|
| 环境变量 | shell / systemd | 部署相关路径与端口 | 否 |
| 设置文件 | `$KB_ROOT/.kb_settings.json` | 运行期可调参数、外部服务接入 | 否 |
| 令牌文件 | `$KB_ROOT/.api_tokens.json` | 令牌与设备授权申请 | 否 |

**运行期设置可在 Web 面板「设置」页面直接修改**，无需重启服务。
环境变量则需重启进程生效。

## 一、环境变量

完整清单见仓库根目录的 [.env.example](../.env.example)（该文件只是清单与注释示例，
**程序不会自动读取 `.env`**，需要你用下列任一方式把变量真正传进进程）：

```bash
# 方式一：临时（当前 shell 会话）
export KB_ROOT=/srv/knowledge-base
python3 scripts/kb-server.py

# 方式二：systemd（推荐用于常驻服务）
#   把变量写进单元文件的 [Service] 段，或单独放一个环境文件：
#   EnvironmentFile=/etc/kb/kb.env
sudo systemctl restart kb-server
```

> `.env.example` 里 `KEY=value` 的写法可以直接作为 `EnvironmentFile` 的内容使用。

### 路径与监听

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_ROOT` | 仓库根目录 | 笔记与运行期状态文件的存放位置 |
| `KB_HOST` | `127.0.0.1` | API 服务监听地址。**默认只绑定本机回环**，公网与局域网都连不上；要对公网提供服务时改 `0.0.0.0`，此时必须自行配置鉴权、反向代理与 HTTPS |
| `KB_PORT` | `8899` | API 服务监听端口。取值范围 1–65535；超出范围时若本机未装 waitress（回退 Flask 开发服务器），Werkzeug 会静默改绑一个临时端口，而启动横幅仍打印你设的值 |
| `KB_TOKEN_FILE` | `$KB_ROOT/.api_tokens.json` | 令牌文件路径 |
| `KB_TRUSTED_PROXIES` | 空 | 可信反向代理 IP，逗号分隔。只有来自这些地址的请求才采信 `X-Forwarded-For` / `X-Real-IP`，否则一律用 TCP 对端地址 |

### 客户端连接

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_API` | `http://127.0.0.1:8899` | 命令行与脚本使用的 API 地址 |
| `KB_URL` | `http://127.0.0.1:8899` | 检索评估脚本使用的 API 地址 |
| `KB_TOKEN` | 无 | 访问令牌（优先级高于配置文件） |
| `KB_MASTER_TOKEN` | 无 | 脚本中使用的管理令牌 |

### MCP 服务

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_MCP_SSE_HOST` | `127.0.0.1` | SSE 服务监听地址 |
| `KB_MCP_SSE_PORT` | `8898` | SSE 服务监听端口 |
| `KB_MCP_SSE_URL` | `http://127.0.0.1:8898/sse` | 面板展示用的对外 SSE 地址 |
| `KB_MCP_SSE_AUTH` | `1` | 设为 `0` 可关闭 SSE 鉴权，不建议 |
| `KB_MCP_SSE_TOKENS` | 空 | 额外的 SSE 令牌白名单，逗号分隔；用于给客户端单独签发 MCP 令牌 |
| `KB_MCP_READONLY` | 空（视为可写） | 设为 `1`/`true`/`yes`/`on` 时，MCP 只暴露只读工具，写入类调用直接返回提示 |
| `KB_MCP_UPLOAD_MAX_BYTES` | `5242880`（5 MiB） | MCP 上传单文件大小上限 |
| `KB_MCP_MIN_CONTENT_LEN` | `800` | MCP 写入笔记时正文最小字符数，低于此值按质量校验拒绝 |
| `KB_MCP_USAGE_LOG` | `$KB_ROOT/.kb_mcp_usage.jsonl` | MCP 调用统计的落盘位置 |
| `KB_CLIENT_NAME` | 平台标识 | 客户端名称，仅出现在 MCP 调用统计行里 |

### 其他

| 变量 | 说明 |
|---|---|
| `KB_ALERT_WEBHOOK` | 健康检查脚本的告警回调地址（`scripts/健康检查.sh`） |
| `KB_BACKUP_DIR` | 笔记备份输出目录（`scripts/备份笔记.sh`） |
| `KB_BACKUP_KEEP_DAYS` | 备份保留天数，默认 `14`（`scripts/备份笔记.sh`） |
| `KB_MEMORY_FILES` | 外部记忆文件路径，多个用 `:` 分隔 |
| `KB_MEMORY_DB` | 外部记忆数据库路径（可选集成） |
| `KB_SKILLS_DIR` | 技能目录，`/api/skills` 读取其中的 `SKILL.md`，默认 `~/.kb-skills` |
| `KB_URL` | 检索评估脚本使用的 API 地址（`scripts/kb_eval.py`） |
| `KB_GOLDEN` | 检索评估使用的 golden set 路径，默认 `tests/golden_search.json`，缺失时自动生成用例 |
| `KB_EMB_WAIT_MAX` | 单次嵌入请求等待配额的上限（秒），默认 `120`；超时本轮降级为关键词检索 |

## 二、检索模式

系统支持两种检索模式，通过设置项 `embedding_type` 切换。

### BM25 关键词检索（默认）

```
embedding_type = bm25
```

基于 `rank_bm25` 与 `jieba` 中文分词，完全本地运行，无需任何外部服务。
适合笔记规模在数千篇以内的场景。

### 检索行为

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KB_EMB_WAIT_MAX` | `120` | 等待嵌入服务的单次最长秒数。超时本轮降级为关键词检索，不会无限阻塞请求 |

### 混合检索

```
embedding_type = llm
```

在关键词检索基础上叠加向量检索，通过倒数排名融合（RRF）合并结果。
可提升「语义相近但用词不同」的检索效果。启用前提：

1. 已配置可用的嵌入服务（见下文）
2. 已安装 `numpy`（在 `requirements.txt`）与 `faiss-cpu`
   （在 **`requirements-optional.txt`**，需另行安装；缺了只会降级到 BM25，不会报错）
3. 已完成索引重建

切换模式后需重建索引：

```bash
python3 scripts/kb-cli/kb-cli.py rebuild
```

> 从混合模式切回 `bm25` 时，已有的向量文件不会被自动删除，可在切换后手动清理
> `$KB_ROOT/.embeddings.npy` 与 `$KB_ROOT/.faiss.index`。

## 三、外部服务接入

LLM 与嵌入服务均为可选。系统按 OpenAI 兼容接口调用，可对接任意符合该协议的服务，
包括本地推理服务与云端 API。

### 配置位置

在 Web 面板「设置」页面填写，或直接编辑 `$KB_ROOT/.kb_settings.json`。

> **请勿将密钥写入代码或提交到版本库。** `config.py` 中的默认值一律为空，
> 真实密钥只应存在于 `$KB_ROOT/.kb_settings.json`（该文件已被 `.gitignore` 忽略）
> 或同名环境变量中。

### 嵌入服务（启用向量检索所需）

| 设置项 | 说明 |
|---|---|
| `embedding_base` | 接口基础地址，形如 `http://host:port/v1` |
| `embedding_key` | 接口密钥，无鉴权时留空 |
| `embedding_model` | 模型名称，由服务方提供 |

可选调优项：

| 设置项 | 说明 |
|---|---|
| `embedding_tokens_per_min` | 每分钟令牌上限，用于限速；`0` 表示不限速 |
| `embedding_request_gap` | 请求间隔秒数，未设限速时生效 |
| `embedding_proxy` | 代理地址，需要经由代理访问时填写 |

### LLM 服务（启用问答与智能整理所需）

| 设置项 | 说明 |
|---|---|
| `llm_base` | 接口基础地址，形如 `http://host:port/v1` |
| `llm_key` | 接口密钥 |
| `llm_model` | 模型名称 |

依赖 LLM 的功能：问答（`/api/llm/ask`）、摘要、标签生成、文档整理、
智能上传、质量评估。未配置时这些接口会返回明确的未配置提示，其余功能不受影响。

### 精排服务（可选）

精排对检索结果做二次排序，可提升准确度，但会引入额外的响应延迟。

| 设置项 | 说明 |
|---|---|
| `rerank_enabled` | 是否启用，默认 `false` |
| `rerank_model` | 模型名称，留空时自动跳过精排 |

### 密钥的环境变量兜底

若设置文件中未填写密钥，系统会尝试读取同名的大写环境变量：

```bash
export EMBEDDING_KEY=<密钥>
export LLM_KEY=<密钥>
```

## 四、其他设置项

| 设置项 | 默认值 | 说明 |
|---|---|---|
| `token_ttl` | `43200` | 设备令牌默认有效期（分钟） |
| `index_auto_rebuild` | `true` | 写入笔记后是否自动刷新索引 |
| `max_log_days` | `30` | 审计日志保留天数 |
| `theme` | `light` | 面板主题，可选 `light` / `dark` |
| `hybrid_alpha` | `0.6` | 混合检索中向量结果的权重（0 到 1） |
| `dedup_threshold` | `0.3` | 重复检测阈值 |
| `auth_watch_interval_seconds` | `300` | 设备授权申请轮询间隔（秒） |
| `decay_tau_days` | `90` | 检索时间衰减半衰期（天）。`0` = 关闭。详见下节 |
| `qq_push_enabled` | `false` | 是否启用 Webhook 推送 |
| `qq_push_webhook_url` | 空 | 推送回调地址 |
| `qq_push_webhook_secret` | 空 | 推送签名密钥 |
| `qq_push_target` | 空 | 推送目标标识 |
| `qq_push_payload_mode` | `generic` | 推送载荷格式 |

### 设备令牌有效期

`token_ttl` 以分钟为单位，支持以下简写：

| 简写 | 等效值 |
|---|---|
| `90d` | `129600` |
| `1y` | `525600` |

### 检索时间衰减（decay_tau_days）

新写的笔记往往比半年前的老笔记更贴近你当下的问题，但纯相关性排序不会体现这一点。
本设置给每条检索结果按「笔记日期」加一个时间权重，让新笔记适度靠前。

公式（`decay = 1 + 1 / (1 + age_days / tau)`）：

- `age_days` = 今天距笔记 frontmatter 里 `updated` / `created` / `date` 的天数
- `tau` = `decay_tau_days`；`age=0` 时权重约 2.0，`age=tau` 时约 1.5，越老越接近 1.0
- 最终排序分 = 相关性分 × decay，所以**只重排、不改变候选集**，也不会把不相关的笔记顶上来

取值建议：

| 值 | 效果 |
|---|---|
| `0` | 关闭（完全不改排序） |
| `30` | 衰减最猛，新笔记冲得最前，老笔记被压得较狠 |
| `90` | **默认**，实测综合最优 |
| `180` | 温和，老笔记影响小 |
| `365` 以上 | 几乎无感（接近关闭） |

**日期来源是 frontmatter，不是文件 mtime** —— 多篇不同月份的笔记 mtime 会被同步工具刷成同一天，
用 mtime 会让衰减完全失效。笔记没写 frontmatter 日期时该条 decay=1（不惩罚）。

实测数据（40 组查询 / 240 条结果 / 200 篇笔记）：新笔记（<30 天）Top3 命中率 22% → 29%，
整体平均名次仅从 7.10 变到 7.30（即老笔记几乎不受影响）。

修改方式：面板「设置」页，或

```bash
curl -X POST -H "Authorization: Bearer ***" -H "Content-Type: application/json" \
  -d '{"decay_tau_days": 90}' http://127.0.0.1:8899/admin/settings
```

配置非法（如填了字母）时自动回落默认值 90，不会影响检索。

## 五、运行期状态文件

以下文件由服务自动生成于 `KB_ROOT`，均不应纳入版本控制：

| 文件 | 说明 |
|---|---|
| `.api_tokens.json` | 主令牌、设备令牌、授权申请记录 |
| `.kb_settings.json` | 运行期设置 |
| `.audit.db` | 审计数据库（SQLite） |
| `.audit_log.jsonl` | 审计日志（追加写） |
| `.search_index.pkl` | BM25 检索索引 |
| `.embeddings.npy` | 向量数据 |
| `.faiss.index` | FAISS 索引 |
| `.index_manifest.json` | 索引一致性清单 |
| `.kb_mcp_usage.jsonl` | MCP 调用记录 |
| `.kb_auth_watch_state.json` | 授权申请推送去重状态 |

## 六、配置校验

查看当前生效的设置：

```bash
curl -H "Authorization: Bearer <主令牌>" http://127.0.0.1:8899/admin/settings
```

查看索引状态：

```bash
curl -H "Authorization: Bearer <令牌>" http://127.0.0.1:8899/api/index/status
```
