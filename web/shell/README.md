# web/shell — 统一入口外壳（P2）

纯静态（HTML/CSS/JS，无构建依赖）。`./build.sh` 生成 `dist/`，部署到 nnG8 `/opt/quant-platform-shell/current`，由 nginx 443 `= /` 与 `/shell-assets/` 提供。

路由（nnG8 443）：`/` 外壳（basic auth，P3 前保留）· `/qd/` QuantDinger 前端（QD 账号）· `/api/` QD 后端 · `/workbench/` 研究工作台（basic auth）· `/data/` 工作台数据（basic auth）。
