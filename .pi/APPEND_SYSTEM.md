# Quant Platform 开发

当前应用在 `app/`。后端是 `app/backend/qp`（FastAPI、SQLAlchemy、Alembic、PostgreSQL），前端是 `app/frontend`（Vue 3、TypeScript、Vite，部署前缀 `/next/`）。模块边界见 `app/README.md` 和 `ARCHITECTURE.md`。根目录的 `README.zh-CN.md` 说明了各个历史组件的用途。

处理当前应用需求时，从 `app/` 中的实现和相应测试入手。涉及旧组件时，先查看该目录的 README 和已有的 AGENTS.md。研究与影子信号保持只读语义；部署版本、冻结范围和发布流程以 `app/deploy/` 下的记录为准。

## 本地验证

按改动范围选用仓库 CI 的检查。依赖版本、覆盖率门槛和完整步骤见 `.github/workflows/ci.yml`。

- 后端使用 Python 3.12。在 `app/backend` 安装 `requirements.lock` 和本地包（`python -m pip install --no-deps -e .`）。运行 `python -m pytest -q`，仓库资产检查用 `python -m pytest -q --run-assets -m assets`。数据库测试按仓库的 Linux CI 环境运行，并将 `QP_TEST_PGDATA` 指向临时测试目录。
- 后端静态检查在仓库根运行 `ruff check app/backend` 和 `ruff format --check app/backend`。
- 前端使用 Node.js 22。在 `app/frontend` 用 `npm ci` 安装锁定依赖，再按需运行 `npm run lint`、`npm test`、`npm run build`。
