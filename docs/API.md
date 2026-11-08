# 接口参考

服务默认监听 `http://127.0.0.1:8899`，共 71 个路由。

## 一、鉴权

### 令牌类型

| 类型 | 获取方式 | 权限 |
|---|---|---|
| 主令牌（master） | 首次启动自动生成于 `$KB_ROOT/.api_tokens.json` | 全部接口 |
| 设备令牌（r） | 通过授权申请流程签发 | 只读接口 |
| 设备令牌（w） | 通过授权申请流程签发 | 只读与写入接口 |

设备令牌可设置有效期，默认 43200 分钟（30 天）。

### 请求方式

在请求头中携带令牌：

```http
Authorization: Bearer <令牌>
```

### 权限标记说明

下文表格中的「权限」列：

| 标记 | 含义 |
|---|---|
| 公开 | 无需令牌 |
| `r` | 需要只读或更高权限 |
| `w` | 需要写入或更高权限 |
| `master` | 仅主令牌可用 |

### 公开接口的注意事项

以下接口无需令牌即可访问：`/health`、`/api/version`、`/`、`/favicon.ico`、
`/web/`、`/api/index/rebuild/status`、`/auth/request`、
`/auth/request/<rid>/status`。

> `/dashboard` 与 `/api/dashboard` **需要 `r` 令牌**（`check_auth("r")`，
> 无令牌返 401），不在上面的公开清单里。

若将服务暴露于公网，应在反向代理层对这些路径增加访问控制。

## 二、响应约定

- 成功响应为 JSON，`Content-Type: application/json`
- 失败响应形如 `{"error": "错误说明"}`，并带对应 HTTP 状态码
- 常见状态码：`400` 参数错误、`401` 未认证、`403` 权限不足、`404` 资源不存在、
  `409` 冲突、`500` 服务内部错误

## 三、通用

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/` | 公开 | 跳转至 Web 面板 |
| GET | `/health` | 公开 | 健康检查，返回版本、索引篇数、陈旧数（轻量，不扫盘） |
| GET | `/api/version` | 公开 | 版本与特性列表 |
| GET | `/dashboard` | `r` | 跳转至面板看板 |
| GET | `/api/dashboard` | `r` | 看板统计（见下方字段说明） |
| GET | `/api/projects` | `r` | 项目列表 |
| GET | `/api/graph` | `r` | 笔记关系图谱 |
| GET | `/api/mcp-usage` | `r` | MCP 调用记录 |
| GET | `/favicon.ico` | 公开 | 站点图标 |
| GET | `/web/` | 公开 | Web 面板入口 |
| GET | `/web/<path:fn>` | 公开 | 面板静态资源 |

**`/health` 字段说明**（2026-10-18 第 18 项起）

`/health` 是高频探活接口，**不再扫描文件系统**，只读内存里的 stale 缓存（TTL 15 秒）。

| 字段 | 类型 | 含义 |
|---|---|---|
| `stale` | int \| null | 索引落后于磁盘的文件数。**null 表示「未知」**（服务刚启动、缓存还没预热），不等于 0 |
| `stale_cached` | bool | 本次值是否来自新鲜缓存。`false` 时该值可能是过期旧值，或 `stale` 为 null |
| `indexed` | int | 已索引篇数 |

调用方注意：

- **不要把 `stale: null` 显示成 0**，那会让人误以为「索引完全跟得上磁盘」。要显示「未知」。
- `stale_cached: false` 且 `stale` 非 null 时，服务已在后台线程预热，稍后重查即得新值。
- 老版本服务端没有 `stale_cached` 字段，按「值可信」处理即可。

**`/api/dashboard` 字段说明**（2026-10-18 起）

六张卡片统一口径：**主数字 = 该分类目录下的一级子目录个数，小字 = 该分类下的笔记篇数**。

| 字段 | 含义 |
|---|---|
| `projects` / `knowledge` / `writing` / `records` / `diaries` / `archived` | 该分类的**一级子目录个数**（不递归、不数文件，排除 `.` 与 `_` 开头的目录） |
| `projects_notes` / `knowledge_notes` / `writing_notes` / `records_notes` / `diaries_notes` / `archived_notes` | 该分类的**笔记篇数**（原 `projects` 等字段的含义，规则未变：递归 `.md`，排除 `_assets`、`README.md`、`.` 开头与 `{{` 开头文件） |
| `total` | 六类笔记篇数合计（含义未变，老调用方可继续用） |
| `total_dirs` | 六类一级子目录个数合计 |
| `dir_fallback` | 每类一个布尔值；为 `true` 表示该分类下没有任何一级子目录，此时主数字退化为篇数，前端据此在小字注明「无子目录，按篇数显示」 |

**`/api/graph` 参数说明**（2026-10-18 起）

| 参数 | 默认 | 说明 |
|---|---|---|
| `min_sim` | `0.8` | 语义边的余弦相似度下限 |
| `max_links` | `8` | 每个节点最多连出的语义边数 |
| `include_semantic` | `1` | 是否计算语义边。置 `0` 时只返回 wiki 引用边，速度快很多（生产上语义边明显多于 wiki 边） |
| `include_wiki` | `1` | 是否返回 wiki 引用边 |

返回体为 `{nodes, links, meta}`。`nodes[].degree` 是该节点连边数，前端按它降序裁剪主干；`meta.node_count` / `meta.wiki_count` / `meta.semantic_count` 是全量真实总数，前端裁剪后用它标注「已裁剪，原 wiki X / 语义 Y」。

**前端裁剪口径**（2026-10-18 起，接口本身不裁，仍返回全量）

| 项 | 口径 |
|---|---|
| 默认节点上限 | **150**（原 300），按 `degree` 降序；勾「显示全部」时不裁 |
| 裁剪优先级 | **有边节点优先**：先取 `degree>=1` 的，再用 `degree=0` 的孤立点补足。全库千级节点里绝大多数是孤立点，不这样排会把有连线的节点挤出视图 |
| 标签显示 | 只给大节点贴（手机 30 / 桌面 40，按度数取且必须有边），外加悬停/触摸的节点 |
| 标签文本 | 同前缀组先剥公共前缀与公共尾部，再从尾部保留区分段；标题完全同名的改用路径第一个分歧段 |

前端默认只画 `degree` 前 300 个节点、语义边默认关闭、wiki 边默认打开——千级节点全铺开是一团看不清的蜘蛛网。

> **破坏性变更提醒**：`projects` 等六个字段的语义从「篇数」变为「目录数」。要篇数的调用方请改用 `*_notes`。

## 四、笔记

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/notes` | `r` | 列出笔记，支持按目录、标签、类型过滤与分页 |
| POST | `/notes` | `w` | 创建笔记 |
| GET | `/notes/<path:np>` | `r` | 读取笔记 |
| POST | `/notes/<path:np>` | `w` | 更新笔记 |
| DELETE | `/notes/<path:np>` | `w` | 归档笔记（移入 `50-档案`） |
| POST | `/api/notes/batch` | `w` | 批量操作 |
| GET | `/api/notes/<path:np>/history` | `r` | 版本历史 |
| GET | `/api/notes/<path:np>/diff` | `r` | 版本差异 |
| GET | `/api/notes/<path:np>/related` | `r` | 相关笔记 |
| GET | `/api/notes/tree` | `r` | 全量笔记目录树，供前端折叠展示 |
| GET | `/api/tags` | `r` | 标签列表 |
| GET | `/api/tags/governance` | `r` | 标签治理信息 |

### 创建笔记

```bash
curl -X POST http://127.0.0.1:8899/notes \
  -H "Authorization: Bearer <令牌>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "示例笔记",
    "type": "knowledge",
    "content": "正文内容",
    "tags": ["示例"]
  }'
```

`type` 取值与目标目录的对应关系：

| `type` | 目录 |
|---|---|
| `project` | `10-项目` |
| `knowledge` | `20-知识` |
| `writing` | `30-写作` |
| `record` | `30-记录` |
| `diary` | `40-日记` |
| `archive` | `50-档案` |
| `dream` | `60-梦境` |

### 受保护目录

`70-经验` 为受保护目录，写入时须在请求体中显式携带 `owner_ack=user`，
否则返回 `403`。该目录设计为仅由人工写入，避免自动化流程覆盖其中的内容。

### 质量门禁

创建 `knowledge` 与 `project` 类型笔记时默认启用质量检查，要求正文达到最小长度
并包含规定章节（`## 背景`、`## 关键发现`、`## 实施步骤`、`## 验证结果`、`## 后续建议`）。
不满足时返回 `400` 与 `quality_check_failed`。

如需跳过检查，在请求体中传入 `"quality_check": false`。

## 五、检索与索引

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/search` | `r` | 全文检索，返回命中位置上下文 |
| GET | `/api/search/semantic` | `r` | 混合检索，按相关性排序返回前 N 条 |
| GET | `/api/search/mentions` | `r` | 全库精确扫描某字符串的提及位置 |
| POST | `/api/index/refresh` | `w` | 触发增量刷新 |
| POST | `/api/index/rebuild` | `w` | 重建索引（增量语义） |
| GET | `/api/index/status` | `r` | 索引状态与一致性信息 |
| GET | `/api/index/stale` | `r` | 陈旧笔记列表 |

### 全文检索

```bash
curl -G http://127.0.0.1:8899/search \
  -H "Authorization: Bearer <令牌>" \
  --data-urlencode "q=检索关键词"
```

返回结果含 `path`、`title`、`tags`、`matches`（命中行号与上下文）。

### 混合检索

```bash
curl -G http://127.0.0.1:8899/api/search/semantic \
  -H "Authorization: Bearer <令牌>" \
  --data-urlencode "q=检索关键词" \
  --data-urlencode "limit=5"
```

参数：`q` 必填，`limit` 默认 5、上限 20。

响应中的 `mode` 字段说明实际使用的检索模式：

| `mode` 取值 | 含义 |
|---|---|
| `semantic(bm25)` | 纯关键词检索 |
| `semantic(llm)` | 关键词与向量混合检索 |
| `bm25(fallback: ...)` | 配置为混合模式但向量不可用，已降级 |

检索不可用（索引损坏、向量库读取失败等）时，响应体额外带两个字段：

```json
{"count": 0, "items": [], "query": "关键词", "degraded": true, "reason": "具体失败原因"}
```

`degraded: true` 表示**检索本身失败**，空结果不等于「确实没有匹配」。
客户端应据此提示用户，而不是显示「无结果」。`/api/notes/<路径>/related` 同样带这两个字段。

### 全库精确提及扫描

```bash
curl -G http://127.0.0.1:8899/api/search/mentions \
  -H "Authorization: Bearer <令牌>" \
  --data-urlencode "q=某标识符" \
  --data-urlencode "limit=50"
```

参数：`q` 必填（匹配字符串），`limit` 默认 50、上限 200，支持 `offset` 分页。

该接口做确定性全库扫描，返回所有提及位置，适合「哪些笔记提到了某字符串」类问题。
与按相关性排序、只返回前 N 条的混合检索形成互补。

### 双链批量解析

```bash
curl -X POST http://127.0.0.1:8899/api/links/resolve-batch \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"from": "20-知识/来源笔记.md", "targets": ["目标甲", "目标乙"]}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `from` | 否 | 当前笔记相对路径。传了则同目录同名优先命中；不传按全库唯一匹配 |
| `targets` | 是 | 目标原文数组（`[[...]]` 里的内容）。最多 500 个，超出截断 |

响应：

```json
{"resolved": {"目标甲": "20-知识/目标甲.md", "目标乙": null}, "count": 2}
```

`resolved` 的 key 是目标原文，值是可点击的相对路径；**`null` 表示断链**（目标笔记不存在），前端据此标红。

- `targets` 不是数组 → `400`
- 空数组或全空白 → `200` + `resolved: {}`（不报错，前端不该为空格弹错误）
- 重复目标自动去重，`count` 是去重后条数
- 无 token → `401`

单篇笔记几十个双链时**必须用这个接口**：逐个调解析接口会重复构建全库链接索引，手机端会卡。

### 正文绝对路径探测

```bash
curl -X POST http://127.0.0.1:8899/api/path-probe \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"paths": ["~/示例/docs/a.md", "~/不存在.md"]}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `paths` | 是 | 绝对路径数组。最多 300 个，超出截断；非绝对路径（不以 `/` 开头）直接判 `kind=remote` |

响应：

```json
{
  "probed": {
    "~/示例/docs/a.md": {"path": "~/示例/docs/a.md", "clean": "~/示例/docs/a.md", "tail": "", "kind": "local_note", "note": "10-项目/示例/docs/a.md", "server": ""},
    "~/不存在.md": {"path": "~/不存在.md", "clean": "~/不存在.md", "tail": "", "kind": "remote", "note": null, "server": ""}
  },
  "count": 2
}
```

| 返回字段 | 说明 |
|---|---|
| `path` | 原样回传的入参，前端按它做 key 回填 |
| `clean` | 剥掉行号/章节后缀后的真实路径 |
| `tail` | 被剥掉的尾巴（`:12`、`#L9`、` 第 2~18 章`） |
| `kind` | `local_note` / `local_file` / `remote` / `invalid` |
| `note` | 仅 `local_note` 有值，库内相对路径，供前端跳转 |
| `server` | 命中 `other_server_prefixes` 时的服务器名 |

`kind` 四种：

| 值 | 含义 | 前端表现 |
|---|---|---|
| `local_note` | 本机存在，且落在知识库根目录内 → `note` 给相对路径 | 绿色实线、可点跳转 |
| `local_file` | 本机存在，但在库外（含目录、无读权限） | 绿色虚线、不可点（标「本机文件」） |
| `remote` | 本机 stat 不到 | 灰色、不可点（标「其他服务器」） |
| `invalid` | 空串、相对路径、隐藏路径、形状不符 | 不渲染，保持纯文字 |

**只认绝对路径**：`./docs/a.md`、`a.md` 这类相对写法不识别，正文里原样显示不包徽标。
服务端只做 `os.stat`，不按文件名在库里搜 —— 库里搜到同名笔记 ≠ 本机真有这个文件。

**隐藏路径不探测**：任何一段以 `.` 开头的路径（`~/.ssh/id_rsa`、`~/.kb_settings.json`）
直接返回 `invalid`，**根本不 stat**。原因：本接口持有 `r` 权限 token 即可调用，
若什么路径都 stat，等于对外提供了一个「服务器上任意绝对路径存不存在」的探针。
密钥、配置、token 文件全是隐藏文件，拦掉这一类即可，`docs/`、`笔记/` 等正常路径不受影响。

**形状校验**：`clean` 必须整体匹配 `_ABS_PATH_RE`（含空格、引号、反引号的字符串不算路径），
不匹配直接 `invalid`，不 stat。

`server` 由设置项 `other_server_prefixes` 决定（一行一个 `前缀|服务器名`），只影响徽标文字，不影响 `kind`。

- `paths` 不是数组 → `400`
- 空数组 → `200` + `probed: {}`
- 超过 300 个 → 只处理前 300 个，其余不在 `probed` 里（前端按 300 一批分多次请求，并给缺结果的项降级为「未探测」）
- 无 token → `401`

### 模板列表

```bash
curl http://127.0.0.1:8899/api/templates -H "Authorization: Bearer ***"
```

数据来自 `笔记/_模板/*.md`。目录不存在或为空时返回一条内置「默认模板」（= 默认五节）。

```json
{
  "templates": [
    {
      "name": "项目模板",
      "title": "项目模板",
      "sections": ["## 目标", "## 当前状态", "## 日志"],
      "body": "# {{title}}\n\n## 目标\n...",
      "frontmatter": "---\nid: \"proj-{{timestamp}}\"\n...\n---\n",
      "content": "---\nid: ...\n---\n\n# {{title}}\n...",
      "builtin": false
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `name` | 模板名（= 文件名去 `.md`），`POST /notes` 的 `template` 字段填这个 |
| `sections` | 模板里的二级标题（`## ...`）列表，决定写策略要求哪些章节 |
| `body` | **去掉 frontmatter 后的正文**，前端新建笔记时预填这个 |
| `frontmatter` | 模板自带的 frontmatter 原文（可为空字符串） |
| `content` | 完整原文 = `frontmatter` + `body` |
| `builtin` | `true` 表示回退的内置默认模板 |

> 前端预填必须用 `body` 不能用 `content`：`body` 首字符是 `#`，新建弹窗靠这个判据决定「直接建笔记」；用 `content` 时首字符是 `-`，会走成智能标签分支，笔记建不出来。

### 新建笔记时套用模板

`POST /notes` 增加可选字段 `template`：

```bash
curl -X POST http://127.0.0.1:8899/notes \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"type":"project","title":"新项目","content":"# 新项目\n\n## 目标\n...","template":"项目模板"}'
```

- 传了 `template` 且模板存在 → 按**该模板的二级标题**校验，缺节返回 `400`，响应里 `required_sections` 是模板章节
- 不传 / 模板不存在 / 模板没有二级标题 → 回退默认五节（`## 背景` / `## 目标` / `## 结论` / `## 详情` / `## 相关`，以 `REQUIRED_SECTIONS` 为准）
- 落盘时 frontmatter 由服务端 `_build_frontmatter()` 重建，**不要**把模板自带的 `---` 头写进 `content`，否则会出现双 frontmatter

### 功能插件

插件是纯前端扩展：一个目录 = 一个插件，放在 `scripts/web/plugins/<name>/`。

```bash
curl http://127.0.0.1:8899/api/plugins -H "Authorization: Bearer ***"
```

```json
{
  "plugins": [
    {
      "name": "note-wordcount",
      "title": "笔记字数",
      "version": "1.0.0",
      "description": "在笔记详情底部显示本篇正文字数",
      "mount": "note-detail",
      "url": "./plugins/note-wordcount/plugin.js",
      "enabled": true
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `name` | 插件 id，**必须与目录名一致**，只允许字母数字下划线短横线 |
| `mount` | 挂载点，只认 `dash` / `note-detail` / `settings` 三个 |
| `url` | 入口脚本地址，前端据此注入 `<script>` |
| `enabled` | 是否启用；关掉的插件仍在清单里（设置页要显示），但不出现在 `/api/plugins/enabled` |

`GET /api/plugins/enabled` 只返回已启用的，前端启动时用它决定挂载哪些。

`POST /api/plugins/<name>/toggle`，body `{"enabled": false}`，不带 `enabled` 时按当前状态取反：

```json
{"name": "note-wordcount", "enabled": false}
```

- 插件名不存在 → `404`，且**不写状态文件**（不留脏记录）
- 插件目录不存在 / manifest 坏 / 入口文件缺失 → 跳过该插件，其余照常返回，接口始终 `200`
- 启停状态存在库根 `.kb_plugins.json` 的 `disabled` 数组里，删掉该文件 = 恢复全部启用

## 六、文件与上传

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/upload` | `w` | 上传文件至指定目录 |
| GET | `/files/<path:fp>` | `r` | 读取库内文件 |

## 七、AI 功能

以下接口需要先配置 LLM 服务，否则返回未配置提示。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/llm/summarize` | `r` | 生成摘要 |
| POST | `/api/llm/tags` | `r` | 生成标签 |
| POST | `/api/llm/ask` | `r` | 基于库内容问答 |
| POST | `/api/llm/ask/stream` | `r` | 问答（流式） |
| POST | `/api/llm/analyze-doc` | `r` | 文档分析 |
| POST | `/api/llm/reorganize` | `w` | 文档整理 |
| POST | `/api/llm/quality` | `w` | 质量评估 |

**LLM 不可用时的统一约定**：当 LLM 服务未配置或调用失败，`/api/llm/summarize`、
`/api/llm/tags`、`/api/llm/ask`、`/api/llm/reorganize`、`/api/llm/quality` 一律返回

```json
{"error": "LLM 服务不可用", "detail": "..."}
```

状态码 **503**，`detail` 截断到 200 字符。客户端应把 503 与「正常文本回答」区分处理——
早期版本会把错误字符串直接当成答案返回（HTTP 200），导致界面显示一段报错当回答。

上面三类接口之外的其余路由：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/smart-upload` | `w` | 智能上传（自动归类） |
| GET | `/api/skills` | `r` | 技能列表 |
| GET | `/api/skills/~<dir_name>` | `r` | 技能详情 |

## 八、集成

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/integrations` | `r` | 集成信息总览 |
| GET | `/api/integrations/openai-tools-schema` | `r` | OpenAI 工具定义 |
| GET | `/api/integrations/mcp-config` | `r` | MCP 客户端配置 |
| GET | `/api/integrations/share/manifest` | `r` | 分享清单 |
| GET | `/api/integrations/share/config-preview` | `r` | 分享配置预览 |
| GET | `/api/integrations/share/scan` | `r` | 敏感信息扫描 |
| GET | `/api/integrations/share/download` | `r` | 打包下载 |
| GET | `/api/integrations/download/kb_mcp_server.py` | `r` | 下载 MCP 服务端脚本 |

## 九、管理

以下接口均需主令牌。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/settings` | 读取设置，密钥类字段以 `<REDACTED>` 返回 |
| POST | `/admin/settings` | 修改设置（可写字段见下方说明） |
| GET | `/admin/models` | 探测外部服务可用模型 |
| GET | `/admin/audit` | 查询审计日志（支持 `page`/`limit`/`action`/`device`/`category`） |
| GET | `/admin/audit/stats` | 审计统计（含 `categories` 分类条数） |
| GET | `/admin/audit/cleanup/preview` | 清理预览：`?days=N` 返回 `will_delete` |
| POST | `/admin/audit/cleanup` | 手动清理。`mode` 取 `days` 或 `all`；`days` 模式须 `days>=1`，`all` 模式须带 `confirm=true`（漏传 400）。`backup=true` 才先把待删记录导出到 `_assets/audit-backup-<时间戳>.json` 再删，不传就不落文件。返回 `deleted`/`backup_path`/`total` |
| GET | `/admin/debug-auth` | 鉴权调试信息 |
| POST | `/admin/scan-new` | 扫描新增笔记 |
| POST | `/admin/rebuild-index` | 全量重建索引 |
| POST | `/api/index/rebuild/async` | 异步重建索引 |
| GET | `/api/index/rebuild/status` | 重建进度（公开） |
| POST | `/admin/push/qq/test` | 测试推送通道 |

**`POST /admin/settings` 可写字段**（共 22 项，与 `DEFAULT_SETTINGS` 一致；不在下表内的键提交后会被静默忽略，不报错）：

| 分组 | 字段 |
|---|---|
| 大模型 | `llm_base` `llm_key` `llm_model` `embedding_base` `embedding_key` `embedding_model` `embedding_type` `rerank_enabled` `rerank_model` |
| 检索 | `hybrid_alpha` `dedup_threshold` `decay_tau_days` `index_auto_rebuild` |
| 系统 | `token_ttl` `max_log_days` `auth_watch_interval_seconds` `theme` `qq_push_enabled` `qq_push_webhook_url` `qq_push_webhook_secret` `qq_push_target` `qq_push_payload_mode` |

布尔字段（`index_auto_rebuild`、`qq_push_enabled`、`rerank_enabled`）接受字符串 `"true"/"1"/"yes"`，其余按 false 处理。

## 十、令牌管理

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/auth/request` | 公开 | 提交设备授权申请 |
| GET | `/auth/request/<rid>/status` | 公开 | 查询申请状态 |
| GET | `/auth/requests` | `master` | 申请列表 |
| POST | `/auth/approve/<rid>` | `master` | 批准申请并签发令牌 |
| POST | `/auth/reject/<rid>` | `master` | 拒绝申请 |
| POST | `/auth/token` | `master` | 手动签发令牌 |
| GET | `/auth/ttl-presets` | `master` | 有效期预设 |
| GET | `/auth/sessions` | `master` | 在线会话 |
| DELETE | `/auth/revoke/<hint>` | `master` | 吊销令牌 |
| GET | `/auth/notifications` | `master` | 通知列表 |

### 设备授权流程

1. 客户端向 `/auth/request` 提交申请，获得申请 ID
2. 管理员在面板或通过 `/auth/requests` 看到申请
3. 管理员调用 `/auth/approve/<rid>` 批准，签发令牌
4. 客户端轮询 `/auth/request/<rid>/status` 获取令牌

### 有效期预设

| 预设 | 等效分钟数 |
|---|---|
| `90d` | 129600 |
| `1y` | 525600 |
