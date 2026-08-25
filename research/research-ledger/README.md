# Research Ledger

**版本**: 0.3.0-dual-calibration  
**协议**: PostgreSQL + 本地对等认证  
**访问**: 仅本地回环 (127.0.0.1:18769)

投资研究控制平面：仅追加、本地限制的预测账本和结果审核系统。

## 核心特性

### 🔒 安全设计

- **无凭据存储**: 使用 PostgreSQL 本地对等认证
- **仅本地访问**: 监听 `127.0.0.1:18769`，不暴露公网
- **只读适配器**: 所有导入负载来自 root 所有、服务只读的源适配器
- **仅追加**: 预测一经封存不可修改，结果审核产生新版本

### 📊 预测生命周期

```
生成 → 封存 → 等待结果 → 人工审核 → 正式结算 → 校准分析
   ↓         ↓            ↓           ↓          ↓
manifest  sealed   PENDING_REVIEW  APPROVED  REVIEWED  cohort_stats
```

### 🎯 结果审核策略

结果审核**验证市场事实**，而非预测是否正确。

**批准条件**（必须全部满足）：
- ✅ 预测与封存批次唯一绑定
- ✅ 目标会话已终结
- ✅ 源快照完整无损
- ✅ 独立来源确认前/目标收盘价和交易状态
- ✅ 二元结果：目标收盘 > 前收盘 = 1，其他 = 0

**拒绝原因**（标准代码）：
```
SOURCE_UNAVAILABLE              - 来源不可用
SOURCE_HASH_MISMATCH            - 哈希不匹配
SYMBOL_BINDING_MISMATCH         - 标的绑定不一致
TARGET_SESSION_NOT_FINAL        - 目标会话未终结
PRIOR_SESSION_MISSING           - 前会话缺失
SUSPENSION_OR_ZERO_VOLUME       - 停牌或零成交
CLOSE_VALUE_MISMATCH            - 收盘价不一致
DIRECTION_MISMATCH              - 方向不一致
TIMESTAMP_OR_CUTOFF_INVALID     - 时间戳无效
FORECAST_NOT_PROSPECTIVE        - 预测非前瞻性
DUPLICATE_OR_MISSING_RECORD     - 记录重复或缺失
INDEPENDENT_SOURCE_CONFLICT     - 独立来源冲突
PROTOCOL_MISMATCH               - 协议不匹配
```

⚠️ **重要**: 错误的预测**永远不是**拒绝理由。

## 架构

### 数据库

- **PostgreSQL**: `research_ledger` 数据库
- **迁移版本**: `004_calibration_dual_cohort_v1`
- **用户**: `researchledger`（系统用户，本地对等认证）

### 源适配器

```
app/
├── ledger_app.py              # 主 HTTP API 服务器
├── ledger_worker.py           # 后台工作进程
├── astock_shadow_adapter.py   # AStock 系统适配器
├── legacy_shadow_adapter.py   # 遗留系统适配器
├── model_shadow_adapter.py    # 模型适配器
└── phase2_shadow_adapter.py   # Phase2 仪表板适配器
```

### 目录结构

```
/opt/research-ledger/          # 应用根目录
├── app/                       # 应用代码
├── migrations/                # 数据库迁移脚本
├── scripts/                   # 运维脚本
├── systemd/                   # systemd 服务配置
├── venv/                      # Python 虚拟环境
└── docs/                      # 文档

/var/lib/research-ledger/      # 数据目录（组权限：researchledger）
├── cas/                       # 内容寻址存储
├── manifests/                 # 导入清单
│   ├── astock/
│   ├── phase2/
│   └── models/
└── import-reports/            # 导入报告

/var/backups/research-ledger/  # 备份目录
└── hourly/                    # 小时备份
```

## 部署与运维

### 系统服务

```bash
# 服务状态
sudo systemctl status research-ledger.service

# 查看日志
sudo journalctl -u research-ledger.service -f

# 重启服务
sudo systemctl restart research-ledger.service
```

### 初始化部署

```bash
cd /opt/research-ledger
sudo ./setup_ledger.sh
```

脚本会：
1. 创建系统用户和组 `researchledger`
2. 设置目录权限
3. 创建 Python 虚拟环境并安装依赖
4. 创建 PostgreSQL 角色和数据库
5. 运行数据库迁移

### 健康检查

```bash
# API 健康检查
curl http://127.0.0.1:18769/health

# 运行时健康
/opt/research-ledger/scripts/research-ledger-runtime-health.sh

# 管道健康
/opt/research-ledger/scripts/research-ledger-pipeline-health.sh
```

### 备份

```bash
# 手动备份
/opt/research-ledger/scripts/research-ledger-backup.sh

# 备份位置
ls -lh /var/backups/research-ledger/hourly/
```

### 测试数据库安全边界

无状态单元测试可以直接运行。有状态集成测试会提交夹具，因此每个脚本必须使用一个独立、
全新的一次性空库，并显式传入 `LEDGER_TEST_DSN`；数据库名必须以 `_test` 结尾。测试脚本
不会继承生产默认库名，也会拒绝 `research_ledger`。

以下流程已在 PostgreSQL 16 上验证。先由管理员创建两个空库，并将所有者限制为非超级用户
`researchledger`：

```bash
sudo -u postgres createdb --owner=researchledger --template=template0 \
  research_ledger_integration_test
sudo -u postgres createdb --owner=researchledger --template=template0 \
  research_ledger_revision_test
```

分别运行两个脚本，不要复用同一个数据库：

```bash
sudo -u researchledger env \
  LEDGER_TEST_DSN='dbname=research_ledger_integration_test host=/var/run/postgresql' \
  /opt/research-ledger/venv/bin/python \
  /opt/research-ledger/tests/test_ledger_integration.py

sudo -u researchledger env \
  LEDGER_TEST_DSN='dbname=research_ledger_revision_test host=/var/run/postgresql' \
  /opt/research-ledger/venv/bin/python \
  /opt/research-ledger/tests/test_phase2_import_revisions.py
```

测试结束且确认没有活动连接后，删除这两个精确测试库：

```bash
sudo -u postgres dropdb research_ledger_integration_test
sudo -u postgres dropdb research_ledger_revision_test
```

PostgreSQL 16 的 `pg_dump` 会为每次导出生成随机的 `\restrict` / `\unrestrict` 标记，不能
直接把原始 dump 的 SHA-256 当作稳定 schema 指纹。需要比较时，应排除这两类行：

```bash
sudo -u researchledger pg_dump --schema-only --no-owner --no-privileges research_ledger \
  | grep -v -E '^\\(un)?restrict ' \
  | sha256sum
```

## API 端点

服务仅监听 loopback。健康与校准状态无需 capability；其余治理端点通过
`X-Ledger-Capability` 执行角色或管理员权限检查。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康、版本和迁移状态 |
| GET | `/v1/calibration/status` | 双队列校准状态 |
| GET | `/v1/review-rounds/{uuid}/submissions` | 按 capability 返回当前角色可见的盲审提交 |
| POST | `/v1/cases` | 创建研究 case（ADMIN） |
| POST | `/v1/review-rounds` | 创建盲审轮次（ADMIN） |
| POST | `/v1/review-submissions` | 提交 BULL/BEAR 等角色评审 |
| POST | `/v1/risk-vetoes` | 提交风险否决 |
| POST | `/v1/daily-candidate-runs` | 导入执行上限为 0 的每日候选包 |

AStock、Phase2 与模型 manifest 通过受控 CLI 适配器导入，不提供旧版
`/import/*` HTTP 路由。

## 正式化门槛

系统使用以下阈值判断预测集是否达到正式化标准：

```python
FORMAL_MIN_FORECASTS = 300   # 最少预测数
FORMAL_MIN_BATCHES = 10      # 最少批次数
FORMAL_MIN_BIN = 20          # 每个概率桶最少样本数
```

## 校准分队列

系统维护两个独立的校准分队列：

- **Cohort A**: 用于模型训练/验证的历史数据
- **Cohort B**: 用于前瞻性测试的保留集

详见 `docs/calibration-cohorts.md`

## 依赖

- Python 3.12+
- PostgreSQL 14+
- psycopg[binary] 3.2.10

## 相关项目

- **AStock_AI_System**: A 股晨报系统，生成研究候选
- **candidate-engine**: 策略候选生成引擎（当前冻结）
- **research-system-phase2**: Phase2 仪表板，结果审核界面

## 安全注意事项

1. ⚠️ **仅本地访问**: 服务绑定 `127.0.0.1`，不可远程访问
2. **独立低权限运行**: 服务以 `researchledger:researchledger` 运行；systemd 将文件系统设为只读，仅 `/var/lib/research-ledger` 与 `/run/research-ledger` 可写
3. ⚠️ **数据完整性**: 所有预测和结果经 SHA-256 哈希验证
4. ⚠️ **审计日志**: 所有操作记录时间戳和来源

## 许可与免责

本系统用于**研究与学习**，不构成投资建议。所有预测和结果仅用于回顾性分析和模型校准。市场有风险，决策需独立核验。
