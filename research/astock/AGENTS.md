# AStock AI System

这是一个面向研究支持的 A 股晨报项目。工作时请遵守：

- 主工作流：node scripts/morning-report.mjs。
- 配置是事实来源；不要擅自更改股票池、模型、风险规则或数据截止时间。
- 临时数据只能写入 work/，原始接入数据写入 data/inbox/，晨报写入 reports/daily/。
- 不得把 API key、Cookie、Token 或用户 Pi auth.json 内容写入项目。
- 所有行情、资金、财务与新闻数字必须带来源和时间；数据不可用时要显式降级。
- 先运行 npm run validate 和 npm test 验证更改。
- 当前 Pi 版本没有原生 MCP 客户端；mcp/ 是标准输入/输出桥接契约，不应描述为已启用的 Pi MCP。

