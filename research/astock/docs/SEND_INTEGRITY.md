# 通知发送完整性边界

本文件描述发送端的本地保证，不把本地一次留痕当作 Telegram 绝无重复的证明。

## 同一事件的发布和消费

- `emitRunEvent`、`emitCorrectionEvent` 和两个 drain 共用通知锁。锁忙时发布器返回 `busy`，不会在锁外新增 pending。
- 普通批量 drain 和定向 drain 都检查完整状态集合；同时存在 pending/sent 等多份状态时保留证据并 defer，不覆盖终态。
- 已有发送回执时，即使事件被移回 pending，也不重新发送。correction 使用自己的 gate 和回执，但同样遵守这条规则。
- 普通发送回执通过不可覆盖的发布方式持久化；文件先 fsync，发布后同步目录。回执先于 sent，已接受发送后落盘或状态转移失败不会自动重试。
- 陈旧 sending 与其他状态冲突时不会覆盖另一份状态；无冲突的陈旧普通 sending 仍转 ambiguous，等待人工核对，不能为了清零删除或重发。

## 成功附件的最终证明

`src/morning-seal-proof.mjs` 是密封字段的单一校验契约。标准晨报入口和最终 outbox 消费都要求相同的 schema/kind/state、日期、报告/last-run 哈希、门禁回显；新研究密封还要求冻结截止与上下文哈希绑定。

只有新研究密封确实不存在时，才沿用旧草稿兼容路径；存在但无效不回退。手工恢复的一次性授权只替代编排器认领，不替代报告和密封完整性检查。correction 继续走独立更正 gate，不套用普通晨报密封。

## 失败和跳过通知

成功附件只接受完成的 success occurrence。失败通知允许相应的已完成失败/阻断等终态，跳过通知接受 skipped；running 不放行。失败 last-run 用真实 `failedAt` 绑定认领时间窗，不要求并不存在的 `completedAt`。日期、认领 UUID、冻结截止及时间窗校验仍然保留。

不能绑定新鲜 last-run 的异常退出仍保守留档待核对，不据旧状态自动生成成功附件；这不替代 systemd 的链路失败告警。

## 离线回归与部署限制

`test/send-integrity.test.mjs` 使用临时目录和假发送器复现发布/消费交错、旧多状态残留、回执后状态转移失败、授权后密封变化以及真实失败/跳过终态。旧发送/授权/correction/v3 套件继续覆盖原契约。

升级不迁移、不删除历史冲突或回执，不补发历史事件。发布前运行 `npm run validate`、`npm test`；平台跳过项必须单列，不能算作生产通过。
