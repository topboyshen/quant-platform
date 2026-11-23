# M7 前端部署与逐页验收 Runbook

状态：**M7.1–M7.6 开发完成，未部署**。部署需单独授权；页面由用户逐页验收。

前端是静态产物，不写数据库、不触发任务、不发通知；唯一运行时依赖是 `/next/api/` 反代到 `qp-api`。

## 1. 构建（本地或 CI，不在 nnG8 上构建）

```bash
cd app/frontend
npm ci
npm run build          # vue-tsc --noEmit && vite build
ls dist                # index.html + assets/
```

产物只包含 `index.html` 与 `assets/*`；不生成 sourcemap，不包含任何密钥。

## 2. 发布布局

nginx 的 `/next/` 与 `/next/assets/` 都指向 `/opt/qp/current/frontend/`
（见 `app/deploy/nginx/qp-next-locations.conf`）。因此前端随 release 一起切换：

```bash
REL=/opt/qp/releases/<新版本>
# 1) 后端 release 按既有流程准备（backend/venv/deploy 等）
# 2) 用新构建替换 frontend（保留旧目录做回滚）
sudo mv "$REL/frontend" "$REL/frontend.prev.$(date +%Y%m%dT%H%M%S)"
sudo install -d -o root -g root -m 0755 "$REL/frontend"
sudo rsync -a --delete dist/ "$REL/frontend/"
# 3) 原子切换
sudo ln -sfn "$REL" /opt/qp/current.tmp && sudo mv -T /opt/qp/current.tmp /opt/qp/current
```

nginx 无需 reload（alias 跟随 `current`）；`index.html` 为 `no-cache`，`assets/*` 为
`immutable`，新构建的文件名带 hash，不会命中旧缓存。

## 3. 上线自检

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' https://<host>/next/            # 200
curl -fsS -o /dev/null -w '%{http_code}\n' https://<host>/next/api/health # 200
```

浏览器检查：未登录跳转 `/next/login`；登录后 7 个一级导航可切换；刷新任意子路由不 404
（history 模式由 `try_files ... /next/index.html` 兜底）。

## 4. 逐页验收清单

| # | 页面 | 路径 | 验收点 |
|---|---|---|---|
| 1 | 总览 | `/next/` | 业务日/交易时段/刷新状态/版本；当前待处理；行情/信号/研究截止与缺口；日报投递与回执；近期自然运行质量 |
| 2 | 行情 · 健康 | `/next/market` | 与既有 M3 页面一致 |
| 3 | 行情 · 证券 | `/next/market/instruments` | 搜索/筛选 |
| 4 | 行情 · 日历 | `/next/market/calendar` | 三来源日历 |
| 5 | 行情 · 指数收盘 | `/next/market/index-close` | 曲线/表格 |
| 6 | 行情 · 覆盖 | `/next/market/coverage` | 覆盖率 |
| 7 | 行情 · 导入 | `/next/market/imports` | 导入对账 |
| 8 | 研究 · 回测 | `/next/research` | 列表 + 详情抽屉 |
| 9 | 研究 · 盈利 | `/next/research/profit` | 运行 + picks |
| 10 | 研究 · AI | `/next/research/ai` | 任务/对话 + 消息 |
| 11 | 研究 · 项目 | `/next/research/projects` | 项目/分支 |
| 12 | 信号 · 运行 | `/next/signals` | 运行 + 决策 |
| 13 | 信号 · 趋势 | `/next/signals/trends` | 日期查询 |
| 14 | 信号 · 结果 | `/next/signals/outcomes` | 收益/超额 |
| 15 | 信号 · 对比 | `/next/signals/compare` | 影子报告/差异 |
| 16 | 报告 · 中心 | `/next/reports` | 类型筛选 + 正文/来源 |
| 17 | 报告 · 投递 | `/next/reports/deliveries` | 发送与回执分列 |
| 18 | 运维 | `/next/ops` 等 8 页 | 既有 M2 页面 |
| 19 | 设置 | `/next/settings` | 账号/平台 |

每页检查：真实数据、空态、错误态（可临时断网或改 API 路径验证）、无敏感值。

## 5. 回滚

1. `/opt/qp/current` 切回上一版本（前端随 release 一起回滚）。
2. 或仅恢复 `frontend.prev.*` 目录后重指 `current`。
3. 前端无状态，无需数据回滚；nginx 配置未变。

## 6. 安全

- 只上传 `dist/` 静态文件；不部署 `node_modules`、源码、sourcemap。
- 前端不存 token；会话 cookie 为 HttpOnly/Secure/SameSite=Lax，路径 `/next/`。
- 不展示 Telegram token、chat_id、HMAC/DSN 等敏感值。

## 7. 变更控制

- 前端发布与后端 release 同批授权；避开 07:45–08:25、15:00–16:25、21:40–23:15。
- 页面逐页验收；任何一页不通过只回滚该页所属批次，不阻塞其他页。
