# 命令行客户端

`kb-cli` 是知识库的命令行客户端，覆盖检索、阅读、创建、上传、索引维护等日常操作。

## 一、安装

无需单独安装，直接通过脚本调用：

```bash
python3 scripts/kb-cli/kb-cli.py <命令> [参数]
```

可设置别名简化调用：

```bash
alias kb='python3 /srv/knowledge-base/scripts/kb-cli/kb-cli.py'
```

将路径替换为实际部署路径。写入 `~/.bashrc` 可长期生效。

## 二、配置

### 配置文件

配置保存在 `~/.kb-cli.json`，包含 `api_url` 与 `token` 两项。

### 初始化

```bash
# 交互式初始化
python3 scripts/kb-cli/kb-cli.py config init

# 或分别设置
python3 scripts/kb-cli/kb-cli.py config set api_url http://127.0.0.1:8899
python3 scripts/kb-cli/kb-cli.py config set token <令牌>
```

### 查看当前配置

```bash
python3 scripts/kb-cli/kb-cli.py config
```

令牌以掩码形式显示。

### 环境变量覆盖

环境变量优先级高于配置文件：

```bash
export KB_API=http://127.0.0.1:8899
export KB_TOKEN=<令牌>
```

`config`、`version`、`help` 三条命令无需令牌即可使用。

## 三、命令参考

### 检索

```bash
# 检索笔记（默认返回 5 条）
python3 scripts/kb-cli/kb-cli.py search 检索关键词

# 指定返回条数
python3 scripts/kb-cli/kb-cli.py search 检索关键词 -limit 10

# JSON 输出，便于脚本处理
python3 scripts/kb-cli/kb-cli.py search 检索关键词 -json
```

### 阅读

```bash
python3 scripts/kb-cli/kb-cli.py read "20-知识/示例笔记.md"
python3 scripts/kb-cli/kb-cli.py read "20-知识/示例笔记.md" -json
```

### 列出笔记

```bash
python3 scripts/kb-cli/kb-cli.py list
python3 scripts/kb-cli/kb-cli.py list -dir 20-知识 -limit 20
python3 scripts/kb-cli/kb-cli.py list -tag 调研
python3 scripts/kb-cli/kb-cli.py list -type knowledge -offset 20
```

| 参数 | 说明 |
|---|---|
| `-dir` | 限定目录 |
| `-tag` | 按标签过滤 |
| `-type` | 按类型过滤 |
| `-limit` | 返回条数 |
| `-offset` | 分页偏移 |
| `-json` | JSON 输出 |

### 创建笔记

```bash
python3 scripts/kb-cli/kb-cli.py create \
  -title "笔记标题" \
  -type knowledge \
  -subdir 技术 \
  -tags "调研,示例"
```

| 参数 | 说明 |
|---|---|
| `-title` | 标题，必填 |
| `-type` | 类型，取值见下表 |
| `-subdir` | 子目录 |
| `-tags` | 标签，逗号分隔 |
| `-relations` | 关联笔记 |
| `-status` | 状态 |
| `-json` | JSON 输出 |

类型取值：`project`、`knowledge`、`writing`、`record`、`diary`、`archive`、`dream`。

> 创建 `knowledge` 与 `project` 类型笔记时默认启用质量检查，正文需达到最小长度并包含
> 规定章节。不满足时接口返回 `400`。详见 [API 文档](API.md#质量门禁)。

### 更新笔记

内容从标准输入传入：

```bash
cat 修改后的内容.md | python3 scripts/kb-cli/kb-cli.py update "20-知识/示例笔记.md"
```

### 归档笔记

```bash
python3 scripts/kb-cli/kb-cli.py delete "20-知识/示例笔记.md"
```

该命令将笔记移入 `50-档案`，而非物理删除。加 `-force` 可跳过确认提示。

### 上传文件

```bash
python3 scripts/kb-cli/kb-cli.py upload 本机文件.md
python3 scripts/kb-cli/kb-cli.py upload 本机文件.md -dir 20-知识 -type knowledge
```

该命令直连 REST 的 `/upload` 接口。

### 相关笔记与历史

```bash
python3 scripts/kb-cli/kb-cli.py related "20-知识/示例笔记.md"
python3 scripts/kb-cli/kb-cli.py history "20-知识/示例笔记.md"
```

### 标签与图谱

```bash
python3 scripts/kb-cli/kb-cli.py tags
python3 scripts/kb-cli/kb-cli.py graph
```

### 状态与健康检查

```bash
python3 scripts/kb-cli/kb-cli.py status
python3 scripts/kb-cli/kb-cli.py health
```

`status` 输出笔记总数、索引篇数、BM25 与向量索引状态、待更新数量。
`health` 请求 `/health` 接口，用于确认服务可达。

### 重建索引

```bash
python3 scripts/kb-cli/kb-cli.py rebuild
```

### 问答

```bash
python3 scripts/kb-cli/kb-cli.py ask "问题内容"
```

需要先配置 LLM 服务，未配置时返回未配置提示。

### 版本

```bash
python3 scripts/kb-cli/kb-cli.py version
```

同时输出客户端版本与服务端版本。

## 四、命令别名

| 别名 | 等价命令 |
|---|---|
| `up` | `upload` |
| `save` | `update` |
| `rm` | `delete` |

## 五、脚本化使用

配合 `-json` 与标准 Unix 工具处理输出：

```bash
# 统计含某关键词的笔记数
python3 scripts/kb-cli/kb-cli.py search 部署 -limit 20 -json | \
  python3 -c "import json,sys; print(len(json.load(sys.stdin)['items']))"

# 导出全部知识类笔记路径
python3 scripts/kb-cli/kb-cli.py list -type knowledge -limit 200 -json | \
  python3 -c "import json,sys; [print(i['path']) for i in json.load(sys.stdin)['items']]"
```
