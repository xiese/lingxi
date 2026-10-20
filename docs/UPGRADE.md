# 升级指南

## 从 5.9.3 升级到 1.0.0

### 关于版本号

本次对外发布版本号重置为 **1.0.0**。功能是 5.9.3 的延续与增强，版本号重新
计数**不代表功能回退**。升级后 `/health` 与 `/api/version` 返回的版本号会从
`5.9.3` 变为 `1.0.0`，这是预期行为。若有外部监控按版本号做判断，请同步更新。

### 为什么采用全新部署

旧版本以压缩包形式分发，包内含版本库目录（`.git`）。新版包**不再包含版本库**，
两者无法通过 `git pull` 合并——直接在原目录拉取会导致历史分叉。

因此本指南采用**全新部署**方式：新目录装新版，旧目录整体删除。这样旧版本库
彻底清除，不会残留。

### 升级前必读：笔记数据在哪里

**这是本次升级最容易出问题的地方。**

笔记与运行期状态文件存放在「库根目录」（`KB_ROOT`）：

- 若启动时**设置了** `KB_ROOT` 环境变量 → 数据在 `$KB_ROOT/笔记`
- 若**没有设置** `KB_ROOT` → 数据在**代码目录内**的 `笔记/`

先确认属于哪种情况，再决定后面怎么备份。查法：

```bash
# 看服务是怎么启动的（systemd 用户）
systemctl cat kb-server 2>/dev/null | grep -i kb_root

# 或在旧代码目录里看是否有笔记
ls -d <旧代码目录>/笔记 2>/dev/null && echo "笔记在代码目录内，务必先备份"
```

### 升级步骤

#### 1. 停服务

```bash
sudo systemctl stop kb-server
# 若不是 systemd 托管，手动结束 kb-server 进程
```

#### 2. 备份数据（不可跳过）

把笔记与运行期状态文件复制到安全位置：

```bash
# 情况 A：设了 KB_ROOT
cp -a "$KB_ROOT" ~/kb-backup-$(date +%F)

# 情况 B：没设 KB_ROOT，数据在旧代码目录内
cp -a <旧代码目录>/笔记 ~/kb-backup-$(date +%F)-笔记
cp -a <旧代码目录>/.api_tokens.json ~/kb-backup-$(date +%F)-tokens.json
cp -a <旧代码目录>/.kb_settings.json ~/kb-backup-$(date +%F)-settings.json
```

`笔记/` 是全部笔记内容，`.api_tokens.json` 是主令牌，`.kb_settings.json` 是
设置（含外部服务地址与模型名）。这三样丢了要重新配置。

#### 3. 部署新版

解压新版包到**新目录**：

```bash
tar xzf knowledge-base-1.0.0.tar.gz
cd knowledge-base

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### 4. 恢复数据

```bash
# 情况 A：继续用原 KB_ROOT（推荐）
# 数据本来就在那里，无需搬动，跳过本步

# 情况 B：数据原在旧代码目录内，搬进新位置
export KB_ROOT=/srv/knowledge-base          # 换成你要的路径
mkdir -p "$KB_ROOT"
cp -a ~/kb-backup-*-笔记 "$KB_ROOT/笔记"
cp -a ~/kb-backup-*-tokens.json "$KB_ROOT/.api_tokens.json"
cp -a ~/kb-backup-*-settings.json "$KB_ROOT/.kb_settings.json"
```

建议从此显式设置 `KB_ROOT`，让数据与代码分离，以后升级只换代码目录。

#### 5. 删除旧目录

确认新版启动正常**之后**再删。

```bash
rm -rf <旧代码目录>
```

旧目录里含旧版本库（`.git`），整体删除即可，无需单独清理。

#### 6. 启动并验证

```bash
export KB_ROOT=/srv/knowledge-base
python3 scripts/kb-server.py
```

另开一个终端验证：

```bash
curl http://127.0.0.1:8899/health
# 期望：{"status":"ok","version":"1.0.0",...}
```

打开 `http://127.0.0.1:8899/web/`，确认笔记列表与检索正常。

#### 7. 重建索引

笔记数据是从旧版搬过来的，索引需要重建一次：

```bash
python3 scripts/kb-cli/kb-cli.py rebuild
```

### 升级后检查清单

- [ ] `/health` 返回 `version: 1.0.0`
- [ ] Web 面板能打开，笔记列表完整
- [ ] 检索有结果
- [ ] 主令牌未变（`$KB_ROOT/.api_tokens.json` 仍是原文件）
- [ ] 设置未丢（外部服务地址、模型名仍在）
- [ ] 旧代码目录已删除
- [ ] 笔记备份保留至少一周

### 常见问题

**Q：升级后笔记不见了**

检查服务启动时的 `KB_ROOT` 指向。若新版没设 `KB_ROOT`，它会去代码目录里找
`笔记/`，而你的数据在别处。设置 `KB_ROOT` 指向正确路径后重启。

**Q：检索返回空**

笔记搬动后需要重建索引，执行第 7 步。

**Q：能不能不删旧目录，直接覆盖代码**

不推荐。旧目录里的版本库目录会残留，且新旧代码混放后难以判断哪些文件属于
哪个版本。全新部署更干净。

**Q：旧目录里的 `.git` 一定要删吗**

是。本包不再包含版本库，两者不是同一条历史线。留着它，后续任何 `git` 操作
都会产生混乱。整体删除旧目录即可。
