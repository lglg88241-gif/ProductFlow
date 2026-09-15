# 批次 B 方案：独立账号与数据私有（迁移与回滚先行）

> 状态：**方案待确认**。本文档确认前，实现只允许"纯新增"部分（见 §3），
> 不回填、不加约束、不改任何现有端点的行为。

## 1. 目标（对应审计要求）

独立账号、用户数据默认私有、管理员一次性邀请、服务端会话可吊销。
涉及审计项：S3/S7/S8 的根治、批次 B 全部、以及"20 人本地验收"的身份前提。

## 2. 数据模型与迁移

### 新表（迁移 `20260916_0036`，纯新增）

- `users`：id、username（唯一）、password_hash（Argon2id）、role（`admin`/`member`）、
  display_name、is_active、created_at
- `user_invites`：id、token_hash（sha256，明文只出现一次）、created_by、note、
  expires_at（默认 24h）、used_at/used_by、revoked_at
- `user_sessions`：id、user_id、token_hash、created_at、last_seen_at、
  absolute_expires_at（7d）、idle_expires_at（24h）、revoked_at

### 业务表归属（同一迁移，仅加可空列）

`products` / `agent_sessions` / `image_sessions` / `asset_library` / `copy_reports`
增加 `owner_id`（可空外键 → users.id，建索引）。
**不加 NOT NULL、不加约束校验、不回填**——全部在 §4 步骤里分步做。

## 3. 第一批实现（纯新增，本文档确认前即可开工）

| 项 | 内容 | 回退方式 |
|----|------|---------|
| 0036 迁移 | 新表 + 可空 owner 列 | alembic downgrade -1（无数据损失） |
| 密码/令牌 | argon2-cffi；邀请/会话令牌只存 sha256 | 删依赖 |
| CLI | `python -m productflow_backend.initialization create-admin`（本地建首个管理员） | 无副作用 |
| 新端点 | 邀请创建/列表/撤销（admin）、邀请兑换、用户登录/登出/me（新增 router，不动现有 admin 会话） | 删路由 |
| 服务端会话 | 独立 cookie `pf_user_session`（httpOnly）；闲置 24h/绝对 7d；登出/禁用即吊销 | 现有流程不受影响 |
| CSRF 最小防线 | 状态变更端点要求自定义头 + Origin 同源校验 | 单独可撤 |
| 旧共存 | 现有 admin-key 门禁与所有现有端点**行为不变**；旧口令仅管理员用，不作为普通用户登录 | — |

## 4. 现有数据迁移（确认后单独一批，按此顺序）

1. `just backup`（升级前快照 + 演练 `backup_drill.sh`）
2. CLI 建初始管理员
3. 回填：现有非内置数据 `owner_id = 初始管理员`（内置模板保持全体可读）
4. 校验脚本：无 NULL owner 的业务行、计数对账（迁移前后每表行数一致）
5. 加约束：NOT NULL（分迁移，可逐表回退）
6. 启用归属过滤（所有列表/详情/搜索/下载/导出/删除/工具/worker 按用户上下文过滤）
7. 双账号交叉验证（审计要求的逐路径验证）

**回滚**：步骤 1 的备份恢复；代码层面每步一个提交，可逐个 revert。
归属过滤启用前的任何时点，旧"管理员可见全部"行为仍在，不存在锁死。

## 5. 明确不做 / 后置

- 不接邮件服务；不自动发邀请
- 不宣称对服务器运营者的加密隔离（运营者有 DB/磁盘权限）
- `SET NULL` 语义：删除会话不连带删用户收藏（素材溯源外键）
- 品牌档案、向量检索维持后置

## 6. 验收标准（本批完成后）

- [ ] 新表/新端点测试全绿；现有 512+ 测试不回退
- [ ] CLI 能建管理员；邀请兑换一次性、过期/撤销/禁用全部生效
- [ ] 会话闲置/绝对过期、登出吊销有测试证据
- [ ] 现有数据零改动（迁移前后行数对账=0 差异，仅新增空列）
- [ ] 双账号隔离验证**留待回填批**，不在本批宣称完成
