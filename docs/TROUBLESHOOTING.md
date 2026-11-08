# 常见问题排查

## 一、服务启动

### 服务无法启动，提示端口被占用

```
OSError: [Errno 98] Address already in use
```

查看占用端口的进程：

```bash
ss -lptn 'sport = :8899'
```

改用其他端口：

```bash
KB_PORT=8900 python3 scripts/kb-server.py
```

### 启动后提示 waitress 未安装

```
⚠️ waitress 未安装，回退 Flask 开发服务器
```

说明依赖未完整安装。开发服务器为单线程，性能与稳定性均低于 waitress：

```bash
pip install -r requirements.txt
```

若安装后仍报这条，确认当前 python 就是装了依赖的那个（虚拟环境是否已激活）：

```bash
python3 -c "import waitress; print(waitress.__version__)"
```

### 缺 faiss / sklearn：启动横幅不会提示，从 `mode` 字段判断

启动横幅**不会**告诉你缺这两个包（`search.py` 里只是静默把 `_HAS_FAISS`
置为 False）。要判断当前实际用哪层检索，看检索响应里的 `mode`，
或直接查 `GET /api/index/status` 的 `has_faiss`。

`requirements-optional.txt` 未安装时**服务仍可正常使用**，
只是检索降级为纯关键词模式。需要向量检索时：

```bash
pip install -r requirements-optional.txt
```

注意区分两件事：这两个包只提供「向量索引加速」与「余弦相似度计算」，
向量检索本身还要求在「设置」里配置 `embedding_base` 与 `embedding_model`，
指向一个兼容 OpenAI `/v1/embeddings` 的 HTTP 服务。两者缺一不可。

### 按横幅端口连不上服务

横幅打印的是 `KB_PORT` 的配置值。当该值不在 1–65535，且 waitress 未安装时，
回退的 Flask 开发服务器会静默改绑一个临时端口，横幅仍显示配置值。
先查真实监听端口：

```bash
ss -tlnp | grep kb-server
```

然后按查到的端口访问，或把 `KB_PORT` 改成合法值并重启。

### 启动后访问面板返回 404

确认访问的是 `/web/` 而非根路径，并确认 `KB_ROOT` 已正确设置。面板静态资源
从代码目录读取，与 `KB_ROOT` 无关；若代码目录结构被改动，请确认
`scripts/web/` 目录完整存在。

## 二、检索

### 检索不到任何结果

按以下顺序排查：

1. **确认索引已建立**

   ```bash
   curl -H "Authorization: Bearer <令牌>" http://127.0.0.1:8899/api/index/status
   ```

   若 `indexed` 为 0，说明索引为空。检查笔记是否放在 `KB_ROOT/笔记/` 下的
   标准目录中，然后重建索引：

   ```bash
   python3 scripts/kb-cli/kb-cli.py rebuild
   ```

2. **确认查询词确实存在**

   使用全文检索交叉验证：

   ```bash
   curl -G http://127.0.0.1:8899/search \
     -H "Authorization: Bearer <令牌>" \
     --data-urlencode "q=查询词"
   ```

3. **确认索引不是陈旧的**

   `GET /api/index/status` 返回的 `dirty` 为 `true` 时，说明有笔记未纳入索引。

### 索引状态显示向量不可用

```
"has_embeddings": false, "index_consistent": false
```

说明向量数据与笔记数量不一致，系统已自动回退到纯关键词检索。可能原因：

- 嵌入服务不可用或返回异常
- 索引构建过程中断

处理方式：确认嵌入服务配置正确后重建索引。

```bash
python3 scripts/kb-cli/kb-cli.py rebuild
```

如暂不使用向量检索，可将检索模式设为 `bm25`，系统会忽略向量产物。

### 增量刷新遇到嵌入失败，新笔记进不了索引

→ 见 [`INDEXING.md`](INDEXING.md)：根因、修复（1.0.18 起文本面与向量面解耦）、
验证证据、上线与回滚步骤。

### 索引产物不一致 → 每次写入都触发全量嵌入重建（写入卡几分钟）

**现象**：单次写入要等几分钟才返回。日志两条连着出现：

```
索引产物不一致，禁用向量检索: paths(2075) 与 embeddings(1985) 不一致
全量嵌入超出单轮预算 300.0s（已处理 320/2074），中止本轮并强制回滚
```

**根因**：向量产物（`.embeddings.npy`）与 `paths` 行数对不上（历史错位），
启动即禁用向量面；而**每次写入**的增量刷新发现对齐失败后都会退回「全量重建」，
全量重建有 `_full_embed_budget = 300.0s` 的单轮预算（防持锁太久导致检索假死，
见 `search.py` 的注释），预算内跑不完全量 → 中止回滚 → 下次写入再来一遍。
上游常态下全量本来就跑不完，所以这个循环**不会自愈**。

**处置**：在服务之外把向量一次算完，再原子落盘，最后重启服务。

```bash
# 1) 停服：重建期间不能再有写入（否则 paths 会变、又对不齐）
systemctl stop kb-server
# 2) 离线重建（脚本在仓库 scripts/ 下；KB_ROOT 必须显式给，见下面的「踩过的坑」）
KB_ROOT=<数据根> python3 scripts/kb-rebuild-embeddings.py --dry-run   # 先看一批耗时与外推总时长
KB_ROOT=<数据根> python3 scripts/kb-rebuild-embeddings.py             # 正式
# 3) 起服并验证
systemctl start kb-server
curl -s 127.0.0.1:8899/health
grep -oE '"(paths_count|embeddings_count|faiss_count|consistent)": [^,}]+' <KB_ROOT>/.index_manifest.json
```

**判据（三项都要满足，缺一不可）**：

| 检查 | 通过的样子 |
|---|---|
| manifest 三项 | `paths_count` == `embeddings_count` == `faiss_count`，且 `consistent: true` |
| 启动日志 | **没有**「索引产物不一致，禁用向量检索」 |
| 写入耗时 | 一篇常规笔记回到 10 秒级（实测 9.7s） |

**踩过的坑**：

- 别指望「在线重建」：一是 300s 预算不够，二是重建期间只要有人写，`paths` 就变，
  刚算好的向量又差一两篇，白白重来 —— **必须先停服**。
- 脚本里要先钉 `KB_ROOT`（数据根）再 `import kb_lib`：生产的代码在 `<KB_ROOT>/current/`、
  数据在 `<KB_ROOT>/`，不钉的话 `config.py` 按代码位置推算出 `current`，`load()` 读不到主索引（`paths=0`）。
- 脚本复用了 `SearchIndex` 的私有方法（`_ensure_faiss_ids` / `_build_faiss_index` / `_save_faiss_ids`）
  与 `_atomic_np_save` / `write_index_manifest`，改名了要先跑 `--dry-run` 确认。

### 相关度显示为 0%

在小规模库中，归一化后可能显示为 0%。这不影响检索结果是否正确，仅表示该结果
在本次结果集中缺少可对比的基准。

### 中文检索效果不佳

确认 jieba 分词已加载。健康检查返回的 `jieba` 字段应为 `true`：

```bash
curl http://127.0.0.1:8899/health
```

## 三、AI 功能

### 接口返回未配置提示

问答、摘要、标签生成等功能需要 LLM 服务。检查设置：

```bash
curl -H "Authorization: Bearer <主令牌>" http://127.0.0.1:8899/admin/settings
```

确认 `llm_base` 与 `llm_model` 已填写且非空。修改后无需重启服务。

### 问答返回空内容

部分推理模型会将额度消耗在思考过程上，导致正文为空。系统已内置额度阶梯重试
（逐级提升至 16000、32000）。若仍为空，可尝试：

- 更换为响应更稳定的模型
- 缩短输入内容
- 提高服务端的输出额度上限

### 模型列表为空

`GET /admin/models` 返回空列表说明 `llm_base` 或 `embedding_base` 未配置。
该接口不会返回推测的模型名，只反映外部服务实际提供的模型。

### 检索接口偶发 500 或长时间无响应

这是两件不同的事，分开判断：

- **响应很慢**：嵌入服务不可达时，HTTP 层有重试预算（约 90 秒）兜底，
  到点即失败并降级，不会无限重试。配额等待则受 `KB_EMB_WAIT_MAX` 限制
  （默认 120 秒）。仍觉得慢就把 `embedding_type` 切回 `bm25`。
- **返回 500**：`search.py` 已把向量面（嵌入、精排）的所有异常统一转成
  `SearchUnavailable`，表现为 `degraded: true` + `reason`，而不是 500。
  若仍看到 500，说明异常发生在别的环节（令牌库、索引文件权限等），
  直接看 `journalctl -u kb-server -f` 的 traceback。

`mode` 字段里出现 `bm25(fallback: ...)` 与 `degraded` 是两个独立信号：
前者说明向量面因缺依赖或配置未启用而走了关键词兜底，此时 `degraded` 为 false，
因为关键词检索本身是成功的。只判断 `degraded` 的客户端会漏掉这一类。

## 四、笔记写入

### 创建笔记返回 400 quality_check_failed

这是质量门禁的正常行为。创建 `knowledge` 与 `project` 类型笔记时要求：

- 正文长度不少于 800 字符
- 包含以下章节：`## 背景`、`## 关键发现`、`## 实施步骤`、`## 验证结果`、`## 后续建议`

按模板补全章节即可通过。如确需跳过，在请求体中传入 `"quality_check": false`。

### 写入 70-经验 返回 403

该目录为受保护目录，设计为仅由人工写入。如需写入，在请求体中显式携带
`owner_ack=user`。

### 文件名中的特殊字符被替换

标题中的斜杠、空格等字符会被替换为下划线，以避免生成非法路径。这是预期行为。

## 五、鉴权

### 返回 401

令牌缺失、错误或已过期。确认请求头格式：

```http
Authorization: Bearer <令牌>
```

设备令牌默认有效期为 30 天，过期后需重新申请。

### 返回 403

令牌权限不足。只读令牌无法执行写操作，写操作与管理接口需要更高权限。

### 忘记主令牌

主令牌存储于 `$KB_ROOT/.api_tokens.json`：

```bash
cat "$KB_ROOT/.api_tokens.json"
```

若该文件丢失，需停止服务后删除该文件并重启，系统会重新生成主令牌。注意：
**此操作会使所有已签发的设备令牌失效**。



#### 已确认但不在当轮修复范围的鉴权问题

以下问题已确认存在、已评估影响，但**不在当轮修复范围**。每条写清现象、影响面、
规避方式与解除条件，避免下次排查时重复分析。

### KB-KNOWN-01 · `CF-Connecting-IP` 与 `X-Forwarded-For` 共用同一个受信列表

**状态**：待办（未修）

**现象**：`client_ip()` 判断是否采信 `CF-Connecting-IP` 时，用的是 `KB_TRUSTED_PROXIES`。
`X-Forwarded-For` 也读同一个列表。

**为什么是问题**：`XFF` 是链式头，取最右段（上游代理追加），客户端自造的值会被挤到左边；
`CF-Connecting-IP` 是**单值头**，没有这个保护。一旦 `KB_TRUSTED_PROXIES` 里混进任何
非 Cloudflare 的代理（例如本机 nginx、或 cloudflared 回连的 `127.0.0.1`），
对端就能自带 `CF-Connecting-IP` 直接伪造来源。

**影响面**：`last_ip` 失真（设备页显示的最近 IP 不可信）；
`/auth/request` 的每 IP 限流被换桶绕过（每次换一个头值就是新计数桶）。

**触发时机**：示例服务器 生产部署时若配置了受信代理即生效。当前开发库单测环境不受影响。

**规避方式**：`KB_TRUSTED_PROXIES` 只填 Cloudflare / cloudflared 的回连地址，
不要往里加任何其它代理。代码已对非法 IP 值做丢弃（fail-closed），但不防"合法格式的伪造值"。

**解除条件**：拆出独立配置项 `KB_TRUSTED_CF_PROXIES`，只有对端在该列表内才采信
`CF-Connecting-IP`；`XFF` 继续用 `KB_TRUSTED_PROXIES`。需要改 示例服务器 环境变量与
cloudflared 侧配置，属生产鉴权行为变更，需单独评审。

---

### KB-KNOWN-02 · 令牌库的时间字段都是本地时间、无时区

**状态**：已知（不修）

**现象**：`created` / `expires_at` / `bound_at` 等字段统一用 `datetime.now().isoformat()`，
不带时区偏移。

**为什么暂不修**：`is_expired()` 按同一口径解析，全库自洽。单独给某一个字段（比如 `bound_at`）
补时区，会让库里两种时间格式并存，反而更容易出错。

**影响面**：服务跨时区迁移、或与外部系统对时间时会错位；单机同区域部署没有影响。

**解除条件**：统一改全库时间口径（带时区落盘 + 解析侧兼容旧值），属独立改造。

---

### KB-KNOWN-03 · 设备绑定判定每请求读一次令牌库

**状态**：已知（有意的取舍）

**现象**：`authenticate()` 里的设备绑定判定走 `_load_store_fresh()`，不是 30 秒 TTL 缓存。

**代价**：实测 6.2 微秒/次（含 1 次 `stat` + 1 秒内缓存命中），对比纯缓存 1.0 微秒。

**为什么不改回去**：只做"403 前再复核"只能解开误拒的一个方向；另一个方向——缓存读到
"未绑定"就放行时，别的进程刚把设备绑上——会绕过绑定校验。两个方向都要最新。

**前提**：依赖文件系统 mtime 粒度（ext4/xfs 纳秒级）。NFS 或粗粒度 mtime 的文件系统上，
可见性退化为 `max_age` 上界（默认 1 秒），需调小 `max_age` 或改用 sidecar 版本号。

---

### KB-KNOWN-04 · 抢绑失败方（`device_mismatch`）没有独立展示入口

**状态**：已知（有意取舍，不是遗漏）

**现象**：设备页（`/auth/devices`）把 `device_bind` / `device_mismatch` 两类事件整体排除，
否则 `uses` 会虚增、抢绑失败方的设备会凭空出现在"用过系统的设备"列表里。

**代价**：抢绑失败方目前**只在审计库里有记录**，任何汇总界面都看不到它。

**为什么暂不补**：新增展示面属于产品决策（放设备页、还是单独的安全事件页、
要不要告警），不在本次改动范围内。需要时按独立需求走。

**当前可用**：直接查审计库
`SELECT time, device, ip FROM audit_log WHERE action='device_mismatch' ORDER BY time DESC`。

## 六、MCP

### 客户端连接失败

1. 确认 REST 服务已在运行：`curl http://127.0.0.1:8899/health`
2. 确认 `KB_ROOT` 在 MCP 进程环境中已正确设置
3. SSE 方式需确认端口可达，且请求头携带正确的令牌

### SSE 方式返回 401

SSE 服务默认启用鉴权。在客户端配置的 `headers` 中加入令牌，或在环境变量中
设置 `KB_TOKEN`。

### 工具调用提示只读模式

环境变量 `KB_MCP_READONLY` 被设为 `1`，所有写入类工具已禁用。取消该变量后
重启 MCP 服务。

## 七、部署

### 通过反向代理访问时接口返回 404

确认代理配置中的路径重写规则正确。面板与接口均需完整转发，不应只转发根路径。

### 对外提供服务后出现异常访问

确认已完成以下配置：

1. 反向代理已启用 HTTPS
2. 公开接口已在代理层增加访问控制
3. 服务未直接暴露于公网

查看审计日志确认是否有异常操作：

```bash
curl -H "Authorization: Bearer <主令牌>" http://127.0.0.1:8899/admin/audit
```

## 八、诊断信息收集

排查问题时，以下信息通常有帮助：

```bash
# 健康状态与版本
curl http://127.0.0.1:8899/health
curl http://127.0.0.1:8899/api/version

# 索引状态
curl -H "Authorization: Bearer <令牌>" http://127.0.0.1:8899/api/index/status

# 当前设置（密钥字段已脱敏）
curl -H "Authorization: Bearer <主令牌>" http://127.0.0.1:8899/admin/settings

# 服务日志
journalctl -u kb-server -n 200
```
