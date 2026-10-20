# 安装与部署

## 环境要求

| 项目 | 要求 |
|---|---|
| Python | 3.10 或更高版本（MCP SSE 服务建议 3.11 及以上） |
| 操作系统 | Linux 或 macOS；Windows 建议使用 WSL |
| 磁盘 | 视笔记规模而定，代码与依赖约需 500 MB |
| 内存 | 建议 1 GB 以上；启用向量检索时建议 2 GB 以上 |

## 一、安装

### 1. 获取代码

```bash
git clone https://github.com/xiese/lingxi.git knowledge-base
cd knowledge-base
```

### 2. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows 环境下激活命令为 `.venv\Scripts\activate`。

### 3. 依赖分三份，按需安装

| 文件 | 内容 | 是否必需 |
|---|---|---|
| `requirements.txt` | Flask、waitress、numpy、rank-bm25、jieba、fastmcp | **必需**。装完即可启动，默认走 BM25 关键词检索，不需要任何外部服务 |
| `requirements-optional.txt` | faiss-cpu、scikit-learn | 可选。装了后可用向量检索；不装也能跑，检索自动降级为纯关键词模式 |
| `requirements-dev.txt` | pytest | 可选。跑测试用 |

```bash
# 需要向量检索时再装（同时要在「设置」里配置 embedding_base / embedding_model）
pip install -r requirements-optional.txt

# 需要跑测试时
pip install -r requirements-dev.txt
```

注意：即使装了 `requirements-optional.txt`，向量检索仍然要求你在「设置」里把
`embedding_base` 指向一个兼容 OpenAI `/v1/embeddings` 的 HTTP 服务。
本仓库不含本地推理引擎，`requirements-optional.txt` 只提供索引加速与相似度计算。

### 4. 确认依赖安装结果

```bash
python3 -c "import flask, waitress; print('ok')"
```

### 5. 判断缺了什么包

启动横幅会明确告诉你是哪一层在兜底。三种常见提示与含义：

| 启动横幅提示 | 含义 | 影响 | 处理 |
|---|---|---|---|
| `⚠️ waitress 未安装，回退 Flask 开发服务器` | 生产 WSGI 不可用 | 能用，但开发服务器是单线程，并发一高就排队 | `pip install waitress` |
| （无此横幅） | — | 启动横幅**不会**提示缺 faiss/sklearn；要判断当前用哪层检索，看检索响应里的 `mode` 字段或 `GET /api/index/status` 的 `has_faiss` | `pip install -r requirements-optional.txt` |
| 启动直接报 `ModuleNotFoundError` | 缺必需依赖 | 服务起不来 | `pip install -r requirements.txt` |

`requirements-optional.txt` 里的两个包是 try/except 兜底依赖：
缺少时服务不启动失败，只在启动横幅和数据里说明降级到哪一层。

## 二、配置知识库根目录

知识库根目录（`KB_ROOT`）用于存放笔记与运行期状态文件。该目录与代码目录相互独立，
建议放在独立路径，便于单独备份。

```bash
export KB_ROOT=/srv/knowledge-base
mkdir -p "$KB_ROOT/笔记"
```

若未设置 `KB_ROOT`，系统默认使用仓库根目录，此时运行期状态文件会生成在仓库内。
建议显式设置，避免状态文件与代码混放。

### 目录权限

服务进程需要对 `KB_ROOT` 具备读写权限：

```bash
sudo chown -R "$USER":"$USER" "$KB_ROOT"
```

## 三、启动服务

```bash
python3 scripts/kb-server.py
```

默认监听 `127.0.0.1:8899`。可通过环境变量调整：

```bash
KB_HOST=127.0.0.1 KB_PORT=8899 python3 scripts/kb-server.py
```

首次启动时会自动完成以下初始化：

1. 生成主令牌，写入 `$KB_ROOT/.api_tokens.json`（权限 `0600`）
2. 创建默认设置文件 `$KB_ROOT/.kb_settings.json`
3. 创建审计数据库 `$KB_ROOT/.audit.db`
4. 构建初始检索索引

### 验证启动结果

```bash
curl http://127.0.0.1:8899/health
```

正常返回示例：

```json
{"status": "ok", "version": "1.0.0", "indexed": 0, "jieba": true, "stale": 0}
```

### 获取主令牌

```bash
cat "$KB_ROOT/.api_tokens.json"
```

主令牌用于管理操作与令牌签发，请妥善保管，不要提交到版本库或写入代码。

## 四、部署为系统服务

### systemd（推荐）

`scripts/kb-server.service` 为单元文件模板。**使用前必须修改其中的路径**：

```ini
[Service]
WorkingDirectory=/srv/knowledge-base
ExecStart=/usr/bin/python3 /srv/knowledge-base/scripts/kb-server.py
Environment=KB_ROOT=/srv/knowledge-base
Environment=KB_HOST=127.0.0.1
```

将 `/srv/knowledge-base` 替换为实际路径，并确认 `ExecStart` 中的 Python 解释器
路径正确（虚拟环境部署时应指向 `.venv/bin/python3`），然后安装：

```bash
sudo cp scripts/kb-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kb-server
sudo systemctl status kb-server
```

查看日志：

```bash
journalctl -u kb-server -f
```

> 启动横幅打印的是 `KB_PORT` 的配置值。若该值不在 1–65535，且 waitress 未安装
> （回退 Flask 开发服务器），Werkzeug 会静默改绑一个临时端口，横幅仍显示原值。
> 按横幅连不上时，用 `ss -tlnp | grep kb-server` 查真实监听端口。

### 反向代理与 HTTPS

`scripts/nginx-kb.conf` 提供了 Nginx 配置示例。其中包含域名与证书路径占位符，
使用前需替换为实际值：

```bash
sudo cp scripts/nginx-kb.conf /etc/nginx/conf.d/kb.conf
# 编辑其中的 server_name 与证书路径
sudo nginx -t && sudo systemctl reload nginx
```

### 对外提供服务时的注意事项

服务默认仅监听回环地址。若需对外提供访问，请注意：

1. 设置 `KB_HOST=0.0.0.0` 后，**必须**通过反向代理配置 HTTPS
2. 部分健康检查类接口无需令牌即可访问，不应直接暴露于公网
3. 建议在反向代理层增加访问控制或限流

## 五、升级

升级方式取决于当初怎么获取的代码：

**通过 git clone 获取的**：

```bash
cd knowledge-base
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart kb-server
```

**通过压缩包获取的**：压缩包不含版本库，无法用 `git pull` 合并。请按
[升级指南](UPGRADE.md) 操作，其中说明了数据备份位置与旧目录清理步骤。

升级后如需重建索引：

```bash
python3 scripts/kb-cli/kb-cli.py rebuild
```

## 六、卸载

1. 停止并移除服务：`sudo systemctl disable --now kb-server`
2. 删除单元文件：`sudo rm /etc/systemd/system/kb-server.service`
3. 删除代码目录
4. `KB_ROOT` 目录包含全部笔记数据，如需保留请单独备份后再删除

## 下一步

- [配置说明](CONFIGURATION.md)：启用向量检索与 LLM 功能
- [接口参考](API.md)：对接自有系统
- [MCP 接入](MCP.md)：接入 AI 客户端
