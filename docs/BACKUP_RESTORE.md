# 备份、恢复与迁移回滚

> 本文档对应的三个脚本都在 `scripts/`：`backup.sh`（备份）、`backup_drill.sh`（恢复演练）、`restore.sh`（恢复）。
> 全部脚本支持 `DRY_RUN=1` 或 `--dry-run` 预演，不会在预演时写入任何数据。

## 为什么需要它

ProductFlow 的全部有状态数据都在三个 Docker volume 里：

| Volume | 内容 | 丢了会怎样 |
|--------|------|-----------|
| `productflow-postgres-data` | 商品、会话、消息、素材元数据、迁移版本 | 全部业务数据消失 |
| `productflow-storage`（或 `STORAGE_HOST_PATH` 宿主路径） | 素材原图、生成图、缩略图、导出物 | 数据库里的引用全部指向空文件 |
| `productflow-redis-data` | Dramatiq 队列快照 | 影响很小：**DB 是权威状态**，worker 启动时会把未完成任务重新入队 |

**注意**：`docker compose down -v` 会删除这三个卷。`release.sh` 永远不会执行 `down -v`，手动操作时也不要用它。

## 日常备份

```bash
just backup                       # 等价于 bash scripts/backup.sh
BACKUP_DIR=/mnt/nas/pf just backup   # 备份到外部/网络位置（推荐）
BACKUP_RETENTION_DAYS=30 just backup # 保留 30 天（默认 14 天）
```

产物（`backups/<时间戳>/`）：

- `postgres.dump.gz` — `pg_dump -Fc` 自定义格式，可被 `pg_restore` 精确还原
- `storage.tar.gz` — media 卷完整归档
- `redis.rdb.gz` — 队列快照（锦上添花）
- `manifest.txt` — 创建时间、git commit、alembic 版本、各文件 sha256

**把备份放到机器之外**：默认目录 `./backups/` 与数据在同一块盘上，磁盘故障时会一起丢。生产部署请设置 `BACKUP_DIR` 指向 NAS/外置盘/对象存储同步目录。

建议频率：数据每天变就每天一次（可挂 Windows 任务计划或 cron 调用 `bash scripts/backup.sh`）。

## 恢复演练（定期做，验证"备份真的能用"）

```bash
just backup-drill                     # 用最新一份备份
bash scripts/backup_drill.sh backups/20260915-112508
```

演练会把备份还原到**一次性临时 Postgres 容器**（独立卷，不接入生产网络），校验表数量、迁移版本与 manifest 是否一致、关键表行数，检查存储归档文件数，然后**无论成败都清理临时容器与卷**——全程不接触生产数据。

### 演练记录

| 日期 | 备份目录 | 结果 |
|------|---------|------|
| 2026-09-15 | `backups/20260915-112508`（72M） | 通过：25 张表、迁移 `20260915_0035` 与 manifest 一致、`agent_messages` 111 行 / `asset_library` 15 行与生产吻合、存储归档 229 个文件 |

## 从备份恢复（破坏性）

```bash
bash scripts/restore.sh backups/20260915-112508 --dry-run   # 先看要做什么
bash scripts/restore.sh backups/20260915-112508              # 交互确认 yes
RESTORE_CONFIRM=yes bash scripts/restore.sh <dir>            # 自动化场景
RESTORE_TARGET=db bash scripts/restore.sh <dir>              # 只恢复数据库
RESTORE_TARGET=storage bash scripts/restore.sh <dir>         # 只恢复存储
```

脚本动作：校验 sha256 → 停 backend/worker/web → 重建数据库 schema 并 `pg_restore` → 恢复存储卷 → 重启 → 等健康检查通过 → 打印迁移版本。

**恢复后请核对**：商品列表与数量、会话与消息是否完整、素材图片能否打开预览、生成物能否下载。

## 迁移回滚

数据库迁移在 backend 容器启动时自动执行（`alembic upgrade head && uvicorn ...`）。若某次升级出问题：

```bash
# 看当前版本与历史
docker compose exec -T productflow-backend alembic current
docker compose exec -T productflow-backend alembic history | head -20

# 回退一版
docker compose exec -T productflow-backend alembic downgrade -1
```

**注意**：仓库内所有迁移都实现了 `downgrade`，但涉及删表/删列的迁移（如早期的 `0017`、`0024`）回退会**丢失那部分数据**——回退前先做一次备份。含数据语义变更的迁移要回滚，正确做法是「恢复备份」而不是「downgrade」：

```bash
bash scripts/restore.sh backups/<升级前的备份> --dry-run
```

所以标准升级流程是：

1. `just backup`（升级前）
2. `just release`
3. 核对功能
4. 出问题 → `bash scripts/restore.sh <升级前的备份>`

## 定时备份示例

Windows 任务计划（每天 03:00）：

```
程序：C:\Program Files\Git\bin\bash.exe
参数：-lc "cd /c/Users/123/Desktop/agent/projects/active/moments-poster-studio/app && BACKUP_DIR=/d/backups/productflow bash scripts/backup.sh"
```

Linux cron：

```cron
0 3 * * * cd /path/to/app && BACKUP_DIR=/mnt/nas/productflow bash scripts/backup.sh >> /var/log/productflow-backup.log 2>&1
```

每周跑一次演练（发现备份损坏）：

```cron
0 4 * * 0 cd /path/to/app && bash scripts/backup_drill.sh >> /var/log/productflow-drill.log 2>&1
```
