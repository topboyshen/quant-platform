# 核心模块与兼容边界

定时主链保持 source → research seal → notify / digest。XHS 只是按需、
本地 `DRAFT_ONLY` 展示，不是研究密封或通知的前置阶段。

| 所有者 | 责任 | 不负责 |
| --- | --- | --- |
| `src/research-artifacts.mjs` | 报告/状态校验、有界读取、不可变写入原语 | XHS 模板、发送 |
| `src/research-seal.mjs` | 冻结档案与摘要绑定、研究密封 | XHS 草稿 |
| `src/xhs-draft.mjs` | 按需草稿变换与本地保存 | 定时研究链 |
| `src/notification-contract.mjs` | 纯策略、事件身份、常量 | 文件操作、发送 |
| `src/notification-store.mjs` | 固定路径、有界读取、原子发布、同一把排队锁 | 业务证明、传输 |
| `src/notification-proof.mjs` | 报告/认领/密封/授权证明、发送确认 | 排队、调用发送器 |
| `src/notification-receipts.mjs` | 回执协议与不可变落盘 | 决定是否发送 |
| `src/notification-transport.mjs` | Hermes 子进程、超时与严格 ACK 解析 | 研究、队列与回执落盘 |
| `src/notification-queue.mjs` | 常规事件、定向发送、过期 sending 恢复 | 人工二次授权、更正流程 |
| `src/notification-recovery.mjs` | 显式授权和更正事件流程 | 定时业务入口 |

通知依赖向下指向证明、存储与纯协议，不反向导入 CLI。每日投递不导入人工
恢复模块；传输不依赖本地研究或队列。所有模块仅导入时均不写状态、启动
子进程或发送。只有显式调用相应操作才产生其原有副作用。

`scripts/hermes-notify.mjs` 仍是同名 CLI 和历史公开 API；其旧导出直接指向
各所有者函数。旧 `src/xhs-draft.mjs` 的 `buildResearchSeal` 导出保留，
但密封 CLI 直接使用独立模块。`XhsDraftIntegrityError` 的 class、name 和
instanceof 语义不变，由共享原语模块单一拥有。

此次拆分不改变事件 ID、锁路径、发送门禁、ambiguous 停止重试策略、哈希
与封存协议；不删除旧版凭据或档案 reader。新增 `src/*.mjs` 自然改变新
compiler 指纹，算法不变：旧密封档案只能沿既有跨时代“已验证基线”读取，
不能借此重新编译、重封或改写旧档。新定时 v2 的固定 08:00 截止与旧 v1
读取语义均保持原样；回退文件并不代表新状态能被老 reader 消费。

`test/core-module-boundaries.test.mjs` 守护依赖方向、兼容导出和导入无副作用。
原有研究密封、发送、回执、人工恢复和跨时代回归继续检验行为。
