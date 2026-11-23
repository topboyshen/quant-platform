# M5 研究模块影子部署与验收 Runbook

状态：**开发中（M5.1–M5.6 完成），未部署**。部署需单独授权；按设计稿，M5 部署排在
M4 影子验收结论（最早 2026-11-20 08:50）之后，除非用户明确作废 M4 窗口。

本文只描述影子运行：qp 只读旧系统、只写自己的 `research` schema 与 `/var/lib/qp*`，
不写 `research_ledger` / phase2 / QuantDinger，不发 Telegram，不调用 LLM，不触发旧任务。

## 1. 部署物

| 类别 | 路径 |
|---|---|
| migration | `app/backend/migrations/versions/0005…0009`（head `0009`） |
| 只读源适配 | `qp/research/sources.py`（ledger/QD `SET TRANSACTION READ ONLY`、phase2 `mode=ro`） |
| 导入器 | `qp/research/importer.py`（台账、Phase2、回测、AI、盈利研究、项目） |
| LLM 入口 | `qp/research/llm.py`（只回放；live 调用显式拒绝并审计） |
| 对比工具 | `qp/research/compare.py` + `qp research compare`（R1–R10） |
| 任务 | `qp/jobs/research.py`：`research.import_ledger|phase2|backtests|ai|profit|projects`、`research.compare` |
| 定时器 | `app/deploy/systemd/qp-research-*.timer`（7 个，全部 `Persistent=false`） |
| 配置样例 | `app/deploy/qp.env.example`（M5 段） |

## 2. 源访问前置（需单独授权）

以下每一项都是对旧系统的**只读授权**，不是数据写入；仍属于生产变更，需用户明确批准后再执行。

### 2.1 research_ledger（宿主 PostgreSQL）

qp 以本机 peer 认证连接（OS 用户 `qp` → PG 角色 `qp`），只需 schema 只读权限：

```sql
-- 以 postgres 执行
GRANT USAGE ON SCHEMA ledger TO qp;
GRANT SELECT ON ALL TABLES IN SCHEMA ledger TO qp;
ALTER DEFAULT PRIVILEGES FOR ROLE researchledger IN SCHEMA ledger
  GRANT SELECT ON TABLES TO qp;
```

回滚：`REVOKE SELECT ON ALL TABLES IN SCHEMA ledger FROM qp; REVOKE USAGE ON SCHEMA ledger FROM qp;`

### 2.2 QuantDinger（容器 PostgreSQL）

容器未发布端口，只能经 docker 网桥 IP 访问；容器重建后 IP 会变，需同步更新 DSN。

```bash
# 1) 取容器 IP（不要打印任何密码）
sudo docker inspect radar-quantdinger-postgres \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'

# 2) 在容器内创建只读角色（密码交互输入，不写入文档/仓库）
sudo docker exec -it radar-quantdinger-postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '\password qp_ro'
sudo docker exec -it radar-quantdinger-postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  'GRANT CONNECT ON DATABASE '"$POSTGRES_DB"' TO qp_ro;
   GRANT USAGE ON SCHEMA public TO qp_ro;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO qp_ro;'
```

回滚：`REVOKE ... FROM qp_ro; DROP ROLE qp_ro;`（先确认无连接）。

### 2.3 phase2 SQLite

当前 `/var/lib/research-system-phase2` 为 `researchphase2:researchphase2 0700`，qp 无法穿越。
只加读 ACL，不改属主/权限：

```bash
sudo setfacl -m u:qp:x /var/lib/research-system-phase2
sudo setfacl -m u:qp:r /var/lib/research-system-phase2/phase2.db
```

回滚：`sudo setfacl -x u:qp /var/lib/research-system-phase2 /var/lib/research-system-phase2/phase2.db`

## 3. 目录与环境变量

```bash
sudo install -d -o qp -g qp -m 0750 /var/lib/qp/research-reports
```

`/etc/qp/qp.env`（root:qp 0640）追加；DSN 含密码时不得出现在命令输出、工单或聊天：

```ini
QP_LEDGER_DSN=postgresql+psycopg:///research_ledger?host=/var/run/postgresql
QP_PHASE2_DB=/var/lib/research-system-phase2/phase2.db
QP_QD_DSN=postgresql+psycopg://qp_ro:__PASSWORD__@<容器IP>:5432/<QD库名>
QP_LLM_SHADOW=1
QP_RESEARCH_REPORT_DIR=/var/lib/qp/research-reports
```

`QP_LLM_SHADOW=1` 是硬性影子开关：LLM 入口只回放，`live` 调用会被拒绝并写入 `research.llm_call`。

## 4. systemd

```bash
sudo install -m 0644 app/deploy/systemd/qp-research-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  qp-research-ledger.timer qp-research-phase2.timer qp-research-backtests.timer \
  qp-research-ai.timer qp-research-profit.timer qp-research-projects.timer \
  qp-research-compare.timer
```

| timer | 时点（CST） | job |
|---|---|---|
| `qp-research-ledger.timer` | 工作日 09:40、16:40 | `research.import_ledger` |
| `qp-research-phase2.timer` | 工作日 16:45 | `research.import_phase2` |
| `qp-research-backtests.timer` | 每日 17:30 | `research.import_backtests` |
| `qp-research-ai.timer` | 每日 17:40 | `research.import_ai` |
| `qp-research-profit.timer` | 每日 17:45 | `research.import_profit` |
| `qp-research-projects.timer` | 每日 17:50 | `research.import_projects` |
| `qp-research-compare.timer` | 每日 18:10 | `research.compare` |

所有 timer `Persistent=false`：停机不补跑，不产生补算或补发。

## 5. 上线自检

```bash
# 1) migration 与任务注册
sudo -u qp env QP_APP_DIR=/opt/qp/current/backend /opt/qp/current/venv/bin/qp db upgrade
sudo -u qp /opt/qp/current/venv/bin/qp job list | grep '^research\.'

# 2) 逐个源导入（只读；不触发旧任务）
for j in research.import_ledger research.import_phase2 research.import_backtests \
         research.import_ai research.import_profit research.import_projects; do
  sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
    /opt/qp/current/venv/bin/qp job run '"$j"
done

# 3) 行数与最近导入记录
sudo -u qp psql -d qp -At -F '|' -c \
  "select source,status,counts from research.import_run order by id desc limit 8;"

# 4) R1–R10 对比（写 /var/lib/qp/research-reports）
sudo -u qp bash -c 'set -a; . /etc/qp/qp.env; set +a; \
  /opt/qp/current/venv/bin/qp research compare --out /var/lib/qp/research-reports'
```

期望：`overall=PASS`（或仅来源差异 WARN）；任何 FAIL 先查 `research.import_run.error` 与
报告中的 `diffs`，不得直接改数据或重跑旧任务。

## 6. 每日对账

`research.compare`（18:10）输出 `<date>.research-compare.json/.md`：

- R1 台账行数、R2 状态分布、R2P phase2 血缘行数
- R3 回测计数与权益/盈亏汇总、R4 盈利研究、R5 AI 记录、R6 项目结构
- R7 LLM 仅回放、R8 源只读与导入状态、R9 自然键唯一、R10 live 通知仅限已批准的信号/报告生产者，研究导入保持 shadow

判定：全 PASS 记通过；WARN 只允许来源/文本差异；FAIL 需人工裁决；BLOCKED（源未配置/不可达）
最多 2 天，连续 2 天视为未通过。

## 7. 安全不变量

- 不写 `research_ledger`、phase2.db、QD PG；连接只读（`SET TRANSACTION READ ONLY` / `mode=ro`）。
- 不触发旧任务、不重跑回测/结算/盈利研究，不改旧 timer。
- 不发 Telegram、不 POST dedirock、不调用券商接口、不调用真实 LLM。
- 日志、报告、工单不出现 token、chat_id、密码、DSN 密码段。

## 8. 回滚

1. `sudo systemctl disable --now qp-research-*.timer`。
2. `/opt/qp/current` 切回上一版本，重启 `qp-api`。
3. 保留 `research` schema 与报告；不删表、不删证据。
4. 如已授权源访问：按 §2 的 REVOKE/ACL 回滚；旧系统数据全程未被修改。

## 9. 变更控制

- M5 部署、migration、timer、`qp.env` 与源授权均需用户明确批准。
- 避开 07:45–08:25、15:00–16:25、21:40–23:15；部署放在 16:30 之后。
- 窗口内代码冻结在 `RELEASE_SHA`；任何修复都要结束当前窗口并重新冻结。
- 禁止手工重跑、补算、补发旧任务；`Persistent=false` 已确保不补跑。
