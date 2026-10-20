# 给 AI agent 的仓库规矩（lingxi）

这个仓库只放**代码和通用文档**。2026-10 已经因为下面的事故重建过两次仓库，请严格遵守：

1. **开发记录、复盘、验收报告、带日期的文档一律写进知识库笔记**（走知识库 API），不进本仓库。`docs/` 只放 API / ARCHITECTURE / CLI / CONFIGURATION / DEVELOPMENT / INSTALL / MCP / TROUBLESHOOTING / UPGRADE 这几份通用文档。
2. **不许出现真实 IP、令牌、key、root 用户目录下的绝对路径**。示例 IP 用 `192.0.2.x`，路径写 `$KB_ROOT`。
3. **不许 force-push，不许改 remote，不许把别的仓库的历史 merge / push 进来。** 换仓库时必须重新 clone，不能只改 remote 地址——旧 .git 里的历史会跟着推上去。
4. 改动走分支 + PR，`guard` 检查必须通过才能合入 main。
5. 不要用 `--no-verify` 绕过本地钩子。

规则由 `scripts/ci/guard.py` 强制执行（本地 pre-commit / pre-push 和 CI 共用）。
