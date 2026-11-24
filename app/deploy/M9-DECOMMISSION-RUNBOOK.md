# M9 下线 Runbook（只停不删）

状态：**M9 工具与文档完成，未执行任何停止/删除**。执行下线需要在前置条件全部满足后单独授权。

当前冻结基线：`0.3.106`，冻结从 **2026-11-24** 开始，最早 Phase 2 复核
**2026-12-08**。详见 [`FREEZE-BASELINE-2026-11-24.md`](FREEZE-BASELINE-2026-11-24.md)。

## 1. 前置条件（缺一不可）

1. M3–M8 均已切换并通过验收；qp 承担全部在用职责。
2. 切换后观察 **≥2 周**，无回退旧链路的故障。当前窗口：2026-11-24 → 2026-12-08；
   2026-11-24 是 `.105`/`.106` 混合版本日，不作为完整单版本基线。
3. 用户对“停旧服务/容器”给出**单独授权**；**数据删除需再一次单独授权**。
4. `NO_BACKUP` 政策不变；qp 03:50 备份继续运行。
5. 先完成只读盘点：`/var/lib/qp/decommission-2026-11-24.json`
   （sha256 `e98f4846…`）；root 属主路径和 `ubuntu` user timers 需分别补扫。

## 2. 只读盘点（任何阶段都可先跑）

```bash
sudo -u qp /opt/qp/current/venv/bin/qp decommission inventory \
  --out /var/lib/qp/decommission-$(date +%F).json
```

- 输出：legacy system/user timers、`radar-*` 容器、关键路径大小、`sha256`。
- 工具只调用 `systemctl list-timers`、`docker ps`、`du -sb`，**不执行 stop/disable/rm**。
- root 属主路径（`/opt/research-ledger` 等）若 du 失败，需以 root 重跑对应路径。

## 3. 停服顺序（只停不删，逐单元）

**禁止按名称前缀批量停服。** 不得执行 `systemctl disable --now 'astock*'`、
`quant-*`、`trading-radar*` 或 `docker stop` 一组容器。每个 unit/容器都必须
在 [`M9-EXECUTION-CHECKLIST.md`](M9-EXECUTION-CHECKLIST.md) 有一行完整证据：
职责已接替、消费者已切换、独立监督仍有效、观察期已满足、回滚命令已测试、
审批人和时间已记录。任何一项空白即停止，不允许“先停了再补”。

候选清单（**只是盘点候选，不是执行单**）：

- user units：`astock-cninfo-precollect.timer`、`astock-xhs-source.timer`、
  `nng8-research-digest.timer`、`research-chain-alert*.timer`
- system timers：`quant-research-daily-summary.timer`、`research-digest.timer`、
  `research-ledger-pipeline-health.timer`、`research-data-health*.timer`、
  `research-index-close-cache.timer`、`quant-execution-digest.timer` 及其余盘点项
- containers：`radar-*`（先依赖方，最后 QD 前端/PG；只 stop，不 rm）

单单元执行模板（每行只替换一个 `UNIT`）：

```bash
UNIT=<exact-unit>
EVIDENCE=/var/lib/qp/decommission/evidence/$UNIT.json
[ -s "$EVIDENCE" ] || { echo "BLOCKED: missing per-unit evidence $EVIDENCE" >&2; exit 1; }
sudo systemctl disable --now "$UNIT"
systemctl is-active "$UNIT"   # expect inactive
# 记录 next business day 的 qp 行数/报表/告警，确认替换职责仍在产出
```

容器同样逐个执行；`radar-quantdinger-postgres/redis` 只有在消费者、保留期和
数据删除审批都明确后才能停，不能与前端一起批量停。

旧站点外壳与 QD 前端保留 **30 天只读回看**；根路径已由 qp 接管。

## 4. 只读归档（失败不吞）

归档只包含配置与 unit 文件（不含业务数据）。必须保留退出码和错误摘要，
并用**成员清单**核对完整性；归档文件的 sha256 只能证明“这一份压缩包没变”，
**不能证明包内文件齐全**。

```bash
set -Eeuo pipefail
ARCHIVE=/var/lib/qp/decommission-$(date +%F)-units.tgz
WORK=/var/lib/qp/decommission/$(date +%F)
mkdir -p "$WORK"
EXPECTED="$WORK/expected-files.txt"
ACTUAL="$WORK/archive-members.txt"
ERROR="$WORK/tar-error.log"

for dir in /etc/systemd/system /home/ubuntu/.config/systemd/user; do
  [ -d "$dir" ] || { echo "ARCHIVE_MISSING_DIR $dir" >&2; exit 1; }
done

find /etc/systemd/system /home/ubuntu/.config/systemd/user -maxdepth 1 -type f \
  \( -name 'trading-radar*' -o -name 'astock*' -o -name 'quant-*' \
     -o -name 'research-*' -o -name 'signal-bus*' -o -name 'qd-*' \
     -o -name 'radar-*' -o -name 'nng8-*' \) 2>"$ERROR" \
  | sed 's#^/##' | sort -u > "$EXPECTED"
[ -s "$EXPECTED" ] || { echo "ARCHIVE_NO_INPUT" >&2; exit 1; }

if ! tar -C / -czf "$ARCHIVE" -T "$EXPECTED" 2>>"$ERROR"; then
  rc=$?
  echo "ARCHIVE_FAILED rc=$rc" >&2
  sed -n '1,20p' "$ERROR" >&2
  exit "$rc"
fi

tar -tzf "$ARCHIVE" | sed 's#^\./##' | sort -u > "$ACTUAL"
if ! diff -u "$EXPECTED" "$ACTUAL"; then
  echo "ARCHIVE_INCOMPLETE: member list differs from expected" >&2
  exit 1
fi
sha256sum "$ARCHIVE" | tee "$ARCHIVE.sha256"
wc -l < "$EXPECTED"
```

- 任何 `tar` 失败、目录缺失、输入为空、成员清单不一致都必须停止；不得把
  stderr 重定向丢弃后继续。
- 数据只记录清单与 sha256（`qp decommission inventory`），**不迁移、不打包、不删除**。
- 文档：本 runbook + `M9-EXECUTION-CHECKLIST.md` + 盘点 JSON + 停服记录。

## 5. 观察与回滚

- 停服后观察 2 周；期间 qp 告警与日报为唯一来源。
- 回滚：
  ```bash
  sudo systemctl enable --now <unit> ...
  systemctl --user enable --now <unit> ...
  sudo docker start radar-...   # 镜像与容器未删
  ```
- 回滚不改 qp 数据；旧系统数据全程未被修改。

## 6. 数据清理（不在 M9 自动执行）

删除旧库、qlib/parquet、证据、Hermes ledger、AStock work 等，需要**再一次单独授权**，
并逐项列出路径、大小、保留理由与回滚可能性。M9 不提供删除命令。

## 7. 安全

- 不删除业务数据、证据、日志；不 `docker rm`、不 `rm -rf`。
- 报告/日志不出现 token、chat_id、DSN 密码。
- 停服与 qp 发布解耦：同一时段只做一件事，并记录基线（git tag + unit 备份）。
