# 索引与检索

> 本文件收录索引构建、增量刷新、向量检索相关的问题与处置。
> 本文档 2026-11-07 从 `TROUBLESHOOTING.md` 拆出独立成篇（内容一致，便于按主题查）。

### 增量刷新遇到嵌入失败，新笔记进不了索引

**现象**：新写的笔记在面板与检索里长时间看不到；`GET /api/index/status` 的
`dirty` 一直为 `true`；日志里反复出现「增量嵌入未成功（embeddings 与 paths
未对齐），回滚本次刷新」，默认每 5 分钟一次。

**根因**：`incremental_refresh()` 原先在嵌入失败时走快照回滚分支，把**已经算好
并落盘**的文本索引（paths/BM25）连同向量面一起还原。嵌入上游持续返回 429/5xx
时，文本索引永远无法前进——连与向量无关的新笔记也一并被挡住。

**修复（1.0.18 起）**：文本索引与向量嵌入解耦。嵌入调用失败或行数未对齐时，
只把 `embeddings`/`faiss_index` 置空并落盘：`vector_search_ready` 变 `false`、
检索自动退回 BM25，本次 paths/BM25 结果保持不变，不再回滚。

- 改动文件：`scripts/kb_lib/search.py`（`_apply_incremental_docs_locked()`）、
  `tests/test_incremental_refresh_embed_degrade.py`（新增三条回归用例）。
- 判据：`/api/index/status` 里 `index_consistent=true` 但 `has_embeddings=false`
  表示「文本面正常、向量面降级」；两者都为 `false` 才是文本索引自身出问题。

**验证证据（离线 A/B，未启动服务、未动生产数据）**：在临时 `KB_ROOT` 下放两篇
样本笔记，先正常建带向量的索引，再新增第三篇，把 `embedding_api` 换成抛 429 的
桩，调用 `incremental_refresh()`：

| 观测点 | 1.0.17（改前） | 1.0.18（改后） |
|---|---|---|
| 刷新是否抛错 | 抛 `RuntimeError`（未对齐，回滚本次刷新） | 不抛错 |
| 新笔记是否进内存索引 | 否 | 是 |
| 持久化索引里能否 grep 到新笔记 | 否 | 是 |
| `embeddings` 行数 | 2（旧向量，随快照一起还原） | 无（向量面置空） |
| `vector_search_ready` | `true`（等于回到刷新前） | `false`（退回 BM25） |
| 索引状态 `state` | `failed` | `clean` |
| 嵌入正常时 | 三条路径一致、向量可用 | 与改前逐项一致 |

复现方式：把仓库的 `scripts/` 复制两份，基线换成 1.0.17 的 `search.py`，用一个
临时 `KB_ROOT` 分别 import `kb_lib.search` 跑上述流程；全程不需要启动
`kb-server`、不占 8899、不写生产目录。

**上线步骤**（在生产库目录执行，脚本会下载 Release 包、自检、切 `current`
软链、重启 `kb-server`）：

```bash
# 1. 先体检当前状态（只读，不改任何东西）
bash scripts/release-deploy.sh --check

# 2. 部署 1.0.18（含下载校验、版本一致性、语法编译、启动后健康检查）
bash scripts/release-deploy.sh --version 1.0.18

# 3. 验证
curl -s http://127.0.0.1:8899/health
curl -s -H "Authorization: Bearer <令牌>" http://127.0.0.1:8899/api/index/status
```

增量刷新默认每 **5 分钟** 一轮（`search.py` 的后台周期循环 `time.sleep(300)`），
所以上线后新笔记最迟 5 分钟进索引；若嵌入上游仍未恢复，索引状态会显示文本面
正常、向量面不可用，检索走 BM25。

**回滚**：

```bash
# 由部署脚本按 .deploy-state.json 记录的上一版回滚（含索引格式适配）
bash scripts/release-deploy.sh --rollback

# 等价的手工确切断回 1.0.17（两条一起执行，切完必须重启）
ln -sfn "$KB_ROOT/releases/kb-1.0.17" "$KB_ROOT/current"
systemctl restart kb-server
```

**遗留**：本次只保证「文本面不被向量上游拖垮」，不解决嵌入额度本身。额度耗尽
期间语义检索不可用；额度恢复后下一轮增量刷新会重新尝试建立向量，也可手工执行
`python3 scripts/kb-cli/kb-cli.py rebuild` 全量重建。
