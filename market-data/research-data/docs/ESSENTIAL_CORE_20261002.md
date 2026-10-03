# 核心记录候选与前向回退

本文件描述隔离候选，不是部署或自然验收记录。活动 `config/snapshot-contract.json`
未创建。拟首次生效日为 2026-10-08：现网已有绑定日历报告下一交易日为该日；
启用前仍须重核实际日历、仓库身份、锁和运行状态。不得改变 10-07 及更早归属。

## 保存什么，明确放弃什么

core 是 `research.core-market.v1`，`fullSnapshot=false`、
`includesCompletePriceArchive=false`，保留 Qlib 覆盖、涨跌家数、有界排名/涨跌停摘要，
不承诺逐证券完整价格归档。独立 `core-market.db/core_market`、独立 core-market
状态目录、规范 JSON 哈希和 insert-only 冲突规则不变。原 full 数据库只读保留；
只在将来明确 full 区间才恢复原 full 写入。不会补写旧缺口或合并两库。

不再强制每日留存 15 类研究/市场附录；新闻、公告、资金流、板块、估值等若未请求，
状态就是 not_requested，不视为采集成功。显式附录仍单独存储、绑定同日 core 哈希，
按原规则记录失败/未审校；没有新增定时采集。它不是当前夜间 core 的依赖。

## 无供应商核心与监督边界

- `qlib_summary.py`：纯本地 Qlib 扫描；与 full 兼容入口共用原二进制对齐、
  四参考股、有效率 >=0.995 / eligible >=5000、前后 generation 一致性校验。
- `snapshot_runtime.py`：标准库运行身份、严格 JSON、锁和原子状态写入。
- `core_storage.py`：独立 SQLite，写前/事务内/提交前再次检查当日与软截止。
- core/supervisor 导入不再载入 daily_snapshot、requests、bs4，不修改代理变量。
  full 模式的证据 reconciliation 才在有界子进程里延迟载入 full 兼容代码。
- Qlib 尚为旧日时，core 仅在可信当前开市日历下按 30 秒/max20 等待。
  首次日历日期/hash 绑定后续尝试，缺失/改变不放行；不伪造 AkShare stage。
  原 runId、锁、22:40 最后启动、22:40:30 软截止、22:42/11分钟硬截止不重置。
  假日不扫描 Qlib、不写行；结构错误、未来日、日期翻转不是可重试成功。
- 本地 AStock v2 日历证据使用冻结 08:00 截止，不把实际延迟启动当成截止；
  state/last 必须共享规范截止，实际运行窗包住 context，文件与持久记录哈希全链绑定。
  success、failed 及真实 blocked/blocked/exit2 终态不混同发布成功；合法已采日历可独立使用。
  旧 v1 保持截止等于实际启动的历史含义；未知版本、降级和不完整绑定仍拒绝。
- 仅覆盖 RESEARCH_DATA_STATE_DIR 时，full stage 仍留在该隔离状态目录；
  core worker 单独覆盖自身目录前，由 supervisor 保留 full 状态目录身份。

## v2 区间与安全恢复 full

旧二字段 policy 仍可读；无 core 历史时缺失仍默认 full。已有 core 行时，policy
缺失、退回默认 full、矛盾或不可验证的历史都阻断 writer/review，audit 明确 critical，
不会隐藏 core 行而将旧 full 重新解释成成功。v2 格式为：

```json
{"schema":"research.contract-policy.v2","transitions":[
  {"activeContract":"research.core-market.v1","effectiveTradeDate":"2026-10-08"},
  {"activeContract":"research.full-snapshot.v1","effectiveTradeDate":"2026-10-09"}
]}
```

上面第二条只是回退边界示例，不是已批准的部署日期。首条之前默认 full；
每条日期包含自身、到下一条日期之前结束。最多16条，日期严格递增，合同必须交替。
10-08 core 行始终按 core 验证，10-09 才要求新 full，不能把10-08重新报为full缺口。
audit 保留 core ledger/rowHashes，即使 activeContract 已恢复 full。contractIntervals
及 legacyFullSnapshot.intervals 为区间依据；多次切换时旧 scheduledBefore 字段置 null。

只使用 `contract_transition.py` 追加边界，不人工重写或删除 policy：

1. 读取并确认当前 policy SHA（不存在时为 missing）、实际已安装候选模块及两库路径。
2. 选择严格晚于当前上海日期、且晚于已有最后边界的未来日期；按可信日历单独审批。
3. transition 同时持有 full/core 两个原 writer 锁，以旧 SHA 比较交换；验证现有行日期
   与旧/新合同归属不变，core 内容/存储哈希仍有效；只原子更新 policy，不写库。
4. 把返回 receipt 的 policy/哈希及两库 rowHashes 纳入部署证据，重核实际wrapper与audit
   使用同一policy；启用前没有正在运行的旧解释器。切换器不是timer，也不执行collector。

示意命令（仅说明，不由本轮运行；所有实参须现场复核）：

```text
python contract_transition.py --policy /home/ubuntu/research-data/config/snapshot-contract.json \
  --expected-sha256 <现有SHA或missing> --contract research.core-market.v1 \
  --effective-date 2026-10-08 --full-db /home/ubuntu/research-data/snapshot.db \
  --core-db /home/ubuntu/research-data/core-market.db \
  --state-root /home/ubuntu/.local/state/research-data
```

恢复 full 使用相同命令与 `research.full-snapshot.v1`，日期必须是新批准的未来日。
旧版代码不理解v2，因此**不能在已有core状态后回退到旧解析器**；保留本批区间解析/
审计版本，前向追加full边界。保留全部core/full/附录状态和receipt，不删除core、不改旧full。
旧full依赖包/采集实现也须保留，否则没有功能完整的full恢复路径。

## 消费者与部署清单

必需新增运行时：snapshot_runtime.py、qlib_summary.py、core_storage.py、contract_transition.py。
必需同时更新：core_market.py、daily_snapshot.py、snapshot_supervisor.py、snapshot_contract.py、
data_health.py、research_appendix.py、review.py、core_contract.py、calendar_evidence.py、
scripts/run_daily_snapshot_with_alert.sh。其余原Qlib/日历/契约/监督模块仍必需。
新 requirements-core.txt 说明最小依赖，不是删除旧依赖授权。

review.py 按目标日合同只读选择，不把旧full当新core，也不再建议历史回填；screen/backtest
直接读Qlib，与snapshot合同独立。wrapper及health unit已有明确policy与双库/状态参数。
本地AStock/signal未发现snapshot库消费者；dashboard需同批接受contractIntervals并保留
结束后的core历史。独立项目只做引用盘点，不改变其配置或运行方式。

## 验证与剩余发布门槛

结果以 ../evidence/DATA_RESULT.json 为准。定向测试不是自然验收；本机旧全量套件的
requests/bs4缺失不能通过改依赖或替换验证器掩盖。启用要求最终精确字节的Linux全量/
进程隔离测试、dashboard兼容与历史hash复核、合适维护窗口和明确部署授权。
政策文件只是候选，当前未启用新合同。
