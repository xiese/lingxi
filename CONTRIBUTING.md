# 贡献指南

感谢你有兴趣参与本项目。本文说明提交代码、报告问题与提 PR 的基本约定。

## 开发环境

```bash
git clone https://github.com/xiese/lingxi.git knowledge-base
cd knowledge-base

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-optional.txt   # 可选：向量检索
pip install -r requirements-dev.txt        # 测试
```

启动本地服务：

```bash
export KB_ROOT="$PWD"      # 笔记与运行期状态文件放在仓库根目录
mkdir -p 笔记
python3 scripts/kb-server.py
```

服务默认监听 `127.0.0.1:8899`，Web 面板在 `http://127.0.0.1:8899/web/`。

## 三处代码位置（最重要）

本项目在三个地方存在，职责严格分开。**改代码只允许在开发库。**

| 位置 | 角色 | 允许做什么 |
|---|---|---|
| 本机 `<开发库目录>` | **开发库** | 唯一可以改代码、commit、push 的地方 |
| GitHub `xiese/lingxi` 的 `main` | **中转** | 代码单向流入生产，不在这里编辑 |
| `远程服务器:$KB_ROOT` | **生产库** | 维护者日常使用、写笔记、传文件；代码只能通过 `scripts/deploy.sh` 从 GitHub 拉 |

生产库的规矩，一条都不能破：

- 不改代码、不 commit、不 push。
- 不热修。线上紧急问题也走：开发库改 → pytest → push → 生产 `deploy.sh`。
- 生产库 `core.hooksPath` 指向仓库外的钩子目录，`pre-commit` / `pre-push` 会直接 `exit 1` 拦住。
- 生产库根目录的 `.kb-env` 内容必须是 `prod`；开发库是 `dev`。该文件不入库。

`笔记/` 只在生产库存在，被 `.gitignore` 排除，合库、部署全程不碰。备份走生产库的
`scripts/备份笔记.sh`（每天 3:30 打包 `笔记/` + `00-索引/` 到 `~/kb-backups`），
再由本机 `拉取备份.sh` 拉走。

### 笔记是数据，不进代码库

**笔记只住生产库（远程服务器）。写、改笔记一律走知识库 API，不用 git 处理笔记。**

开发库克隆下来后，先跑一次初始化：

```bash
bash scripts/setup-dev.sh
```

它做两件事：设 `core.hooksPath=.githooks`、写 `.kb-env=dev`。

之后 `.githooks/pre-commit` 会拦住任何把 `笔记/` 提交进代码库的操作——**包括 `git add -f 笔记/...`**（`-f` 能绕过 `.gitignore`，但绕不过钩子）：

```
❌ 拒绝提交：暂存区里有 笔记/ 下的文件
   被拦下的文件：
     笔记/30-记录/xxx.md
```

误把笔记提交进去了，用 `git rm --cached <文件>` 移出索引（磁盘文件保留），
**不要**用 `git rm`（那会连磁盘文件一起删）。

**项目文档要进代码库的，放 `docs/`，不放 `笔记/`。**

钩子只拦「新增/修改/改名/复制」，放行「删除」——因为把误跟踪的笔记移出索引，
本身就是一个删除动作。

## 提交前检查

**基线命令：在仓库根目录跑 `pytest -q`。**

```bash
pytest -q
```

当前基线：**274 passed, 2 skipped**。

必须是仓库根目录，不要写成 `pytest tests`——`scripts/test_version_consistency.py` 里有 5 个
版本一致性测试，只在根目录收集时会跑到。写成 `pytest tests` 会静默漏掉这 5 个（269 passed），
让人误以为测试变少了。

2 个 skip 是预期的，都不算失败：

- `tests/test_dedup_template_boilerplate.py`：索引为空时跳过端到端打分
- `tests/test_note_normalizer.py`：缺 `笔记/.kb/rules.yaml` 时跳过（开发库没有 `笔记/`）。
  要跑这一组，设置 `KB_RULES=/path/to/rules.yaml`。

另外两项也要通过：

```bash
python3 -m compileall -q scripts tests
python3 scripts/test_version_consistency.py
```

CI 会在 Python 3.10 / 3.11 / 3.12 上重跑一遍，并额外做启动冒烟测试。

## 代码约定

- **Python 风格**：标准库优先，第三方依赖新增前请先说明理由——本项目刻意保持零配置可启动，
  新增必需依赖会破坏这一点。
- **路径安全**：任何接收用户路径的入口都必须经过 `kb_lib.auth` 的校验函数，
  不要自己拼 `NOTES_DIR / user_input`。历史上的路径穿越漏洞就是从这类拼接来的。
- **错误语义**：区分「没有结果」和「检索/服务失败」。前者返回空列表，后者必须显式标记
  （见 `degraded` 字段与 `LLMUnavailable` → 503 的约定），不要把失败伪装成正常空结果。
- **前端**：`scripts/web/app.js` 中任何把后端数据放进 `innerHTML` 的地方，
  都必须先过 `esc()`（HTML 转义）或 `jss()`（内联事件处理器参数）。新增 Markdown 渲染逻辑
  不要绕过 `hardenMarked()` 注册的渲染器。
- **注释**：解释「为什么」而不是「做了什么」。修复缺陷时写明原来错在哪、会有什么后果。
- **版本号**：改动后同步更新 `scripts/kb_lib/config.py` 的 `VERSION`，
  以及 `scripts/web/index.html` / `sw.js` 中的引用，`test_version_consistency.py` 会校验。

## 提交信息

用中文描述，格式建议：

```
<动词> <对象>：<一句话说明>

可选：为什么这样改、影响范围
```

例：

```
修复登录页在规则命中时误显示绿色校验反馈

原实现按格式匹配就给绿色，会被误读为「密码正确」。
改为只在服务端确认后提示，格式提示统一用中性色。
```

## 报告问题

请附上：

1. 复现步骤（越具体越好，含请求路径与参数）
2. 期望结果与实际结果
3. 版本号（`curl http://127.0.0.1:8899/health` 或 `GET /api/version`）
4. 相关日志（注意先抹掉令牌等凭据）

安全相关问题请不要开公开 issue，见 [SECURITY.md](SECURITY.md)。

## 许可

提交的贡献按 [MIT License](LICENSE) 授权。
