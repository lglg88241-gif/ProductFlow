# 批次 B 方案：独立账号与数据私有（迁移与回滚先行）

> 状态：**第一批已落地；§4 回填已按用户确认执行（见下方执行记录）。
> 归属过滤与双账号交叉验证是下一批，未开始。**

## 执行记录（2026-09-16）

| 步骤 | 结果 |
|------|------|
| 回填前备份 `backup.sh` | 通过（三件产物 + sha256 manifest） |
| 演练 `backup_drill.sh` | 通过：28 张表、迁移 `20260916_0036` 与 manifest 一致、行数与生产吻合 |
| 管理员创建 | `lgs`（role=admin），登录实测通过 |
| 回填（`scripts/backfill_owner.py`，先 dry-run 后 apply） | 39 行归属给初始管理员：agent_sessions 15、image_sessions 12、asset_library 12（非内置）；**内置模板 3 行保持 NULL=全体可读**；逐表对账行数前后一致 |
| 密码策略 | 按产品决定降为最低 6 位 |

回填脚本幂等（`WHERE owner_id IS NULL`），可在部署窗口重复执行核对。

### 归属过滤启用记录（2026-09-16）

隔离开关 `DATA_ISOLATION_ENABLED` 已在本机实例**开启**并完成真实双账号交叉验证：

| 验证项 | lgs（admin） | tester（member） | 结论 |
|--------|-------------|-----------------|------|
| Agent 会话列表 | 16（含回填的 15） | **0** | 私有隔离生效 |
| 越权访问他人会话（详情/删除） | — | **404 / 404** | 与"不存在"同文案，不泄漏存在性 |
| 用量统计（会话/轮次） | 16 / 43 | **0 / 0 / 0** | 统计按用户 |
| 素材库 | 15（3 内置 + 12 私有） | **3（全部内置）** | 内置模板全局可读、私有素材隔离 |
| 匿名访问业务端点 | — | **401 请先登录** | 未登录不可读 |

覆盖的垂直（每个都有双账号测试）：商品与工作流、图片会话与下载、画廊、生成队列、
Agent 会话域、素材库（列表/下载/分格导出/删除）、文案报告（列表/下载）、用量度量。

**启用过程中发现并修复的真实缺陷**：
1. **compose 环境变量未注入**（审计 O4 的具体实例）：`DATA_ISOLATION_ENABLED` 加进
   `.env` 与 config 后，因 compose 用显式 `environment:` 清单而没进容器——开关看似打开
   实则无效（`/api/auth/session` 仍报 false、匿名仍 200）。已同时注入 backend 与 worker，
   并加 `docker compose config --quiet` 校验。
2. **归属继承漏洞**：文案报告与 `save_asset` 保存的素材在创建时未继承会话归属，
   落成 `owner_id = NULL`——按"NULL = 全局可读"的语义，**任何人可下载他人报告与素材**。
   已改为沿会话继承，并由跨用户下载测试锁住（该测试在修复前确实失败）。

**当前状态**：隔离已启用，属主为 lgs；`tester` 为验证用 member 账号。
回退方式：`.env` 设 `DATA_ISOLATION_ENABLED=false` 重启（或恢复 `.env.bak-pre-isolation`）。



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
