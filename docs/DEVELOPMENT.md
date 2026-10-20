# 开发说明

## 一、开发环境准备

```bash
git clone https://github.com/xiese/lingxi.git knowledge-base
cd knowledge-base
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

运行测试：

```bash
python3 -m pytest tests/ -q
```

## 二、代码组织

```
scripts/
├── kb-server.py            API 服务入口
├── kb_lib/                 核心实现
│   ├── config.py           常量、目录映射、设置读写
│   ├── auth.py             鉴权、路径校验、frontmatter 解析
│   ├── store.py            令牌存储
│   ├── search.py           索引与检索
│   ├── embed.py            向量化与精排
│   ├── llm.py              模型调用
│   ├── audit.py            审计日志
│   ├── graph.py            关系图谱
│   ├── push_qq.py          Webhook 推送
│   ├── routes/             HTTP 路由
│   └── services/           领域服务
├── kb_mcp_server.py        MCP 服务端（stdio）
├── kb_mcp_sse.py           MCP 服务端（SSE）
├── kb_mcp_memory.py        MCP 记忆检索辅助
├── kb-cli/                 命令行客户端
└── web/                    Web 面板
```

## 三、编码约定

### 配置读取

配置统一通过 `config.py` 的 `get_setting()` 与 `load_settings()` 读取，
不要在模块导入时把配置值绑定到模块级变量。

```python
# 推荐：运行时读取，配置变更即时生效
from .config import get_setting
value = get_setting("some_key")

# 避免：导入时绑定，配置变更后不生效
from .config import SOME_KEY
```

同理，依赖运行时状态的路径（如令牌文件、审计库）应通过函数延迟求值，
而不是在导入时固化为常量。这样测试可以通过环境变量重定向这些路径。

### 路径处理

所有来自请求的文件路径必须经过 `auth.py` 的安全校验函数处理后再使用，
不得直接拼接。

### 错误处理

- 面向用户的错误返回明确的 JSON 说明与恰当的状态码
- 内部异常记录到日志（`log.warning(..., exc_info=True)`），不向客户端暴露堆栈
- 外部服务调用失败时应降级而非中断主流程

### 注释

注释说明「为什么」而非「做什么」。涉及权衡或历史决策的地方，记录当时的
判断依据与已知限制，便于后续维护。

## 四、测试

### 测试隔离

`tests/conftest.py` 在任何 `kb_lib` 模块导入前设置环境变量，将知识库根目录
指向临时目录：

```python
os.environ["KB_ROOT"] = tempfile.mkdtemp(prefix="kbtest-root-")
```

这依赖 `config.py` 在运行时读取环境变量。新增依赖运行时路径的模块时，
应沿用同一模式，避免测试污染真实数据目录。

### 运行指定测试

```bash
python3 -m pytest tests/test_search_embed.py -v
python3 -m pytest tests/ -k "traversal" -v
```

### 版本一致性检查

```bash
python3 scripts/test_version_consistency.py
```

该脚本校验版本号在服务端、Web 面板、缓存标识等位置保持一致。

### 测试数据

测试中使用的项目名、路径等一律使用通用占位符（如 `示例项目`、`/srv/kb`），
不要写入真实的个人或业务标识。

## 五、新增接口

1. 在 `scripts/kb_lib/routes/` 下找到对应功能域的文件，或新建路由文件
2. 使用 `@bp.route` 注册路由
3. 按需添加鉴权：`check_auth("r")`、`check_auth("w")` 或 `@require_master`
4. 若新建文件，在 `routes/__init__.py` 中导入以完成蓝图注册
5. 补充测试

## 六、新增 MCP 工具

在 `scripts/kb_mcp_server.py` 中以装饰器注册：

```python
@mcp.tool()
def kb_example(query: str, limit: int = 5) -> str:
    """工具说明，会展示给调用方。"""
    ...
    return result
```

函数签名与文档字符串会被客户端用于工具发现，应准确描述用途与参数。

## 七、版本发布

版本号定义于 `scripts/kb_lib/config.py` 的 `VERSION` 常量。发布新版本时：

1. 更新 `VERSION`
2. 同步更新 Web 面板缓存标识（`scripts/web/sw.js` 与 `app.js` 中的引用）
3. 运行 `python3 scripts/test_version_consistency.py` 确认一致
4. 更新 `CHANGELOG.md`
5. 运行完整测试

## 八、提交约定

- 提交信息说明变更内容与原因
- 不要提交笔记内容、令牌、密钥、索引产物等运行期数据（`.gitignore` 已覆盖）
- 提交前确认工作区未混入临时文件

## 九、文档维护

代码变更导致以下内容失效时，应在同一次提交中同步更新：

| 变更类型 | 需更新的文档 |
|---|---|
| 新增或修改接口 | `docs/API.md`、`README.md` 接口表 |
| 新增配置项 | `docs/CONFIGURATION.md`、`.env.example` |
| 新增 MCP 工具 | `docs/MCP.md` |
| 新增命令行参数 | `docs/CLI.md` |
| 目录结构变化 | `docs/ARCHITECTURE.md`、`README.md` |
| 版本号变化 | `CHANGELOG.md` |
