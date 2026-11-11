---
description: 说明晨报职责边界（不触发工作流或发稿）
argument-hint: "[YYYY-MM-DD]"
---
不要运行 `scripts/morning-report.mjs`，不要读取 `work/last-run.json`，也不要生成或发布报告。

1. 如果用户要交互式板块/个股研究，请让其退出当前会话后运行 `pi-research`，再提出研究问题；该入口只有只读研究工具。
2. 如果用户要开发功能，请使用 `pi-dev`，完成读代码、修改和测试；不要触发生产晨报。
3. 如果用户确实要人工发稿，说明完整工作流必须由操作员在 Pi 之外直接运行 `npm run morning`（指定日期使用既有 runner），Pi 不代为执行。
4. `$1` 只可作为说明中的目标日期，不得拼接到命令或 shell。
