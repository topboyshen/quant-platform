# 服务器部署运维手册

AStock_AI_System 在 Ubuntu 云服务器上的部署、定时晨报、远程驱动说明。
当前定时入口以第 3 节、仓库 `systemd/` 与经只读核对的生效单元为准；
模块边界见 [CORE_MODULES.md](docs/CORE_MODULES.md)。第 1—2、4—6、8 节保留
早期 Pi 部署和模型实验资料，不是当前 executor、模型或健康状态证明，不能
据此更改配置或调用模型。现役配置以 `config/workflow.json` 和离线 validate
为准；运行任务、联网采集、真实推理、外发和生产部署均须相应授权。
本地项目路径（Windows）：`C:\Users\ipjan\Documents\Codex\2026-08-02\referenced-chatgpt-conversation-this-is-an\outputs\AStock_AI_System`
远程控制器路径：`C:\Users\ipjan\Documents\Codex\Pi_Unified_History\pi-remote*.cmd|.sh`

---

## 1. 服务器

| 项 | 值 |
|----|----|
| 主机 | `ubuntu@150.158.46.28:22` |
| SSH 密钥 | `C:\Users\ipjan\Documents\quant.pem` |
| 系统 | Ubuntu 24.04.4 LTS，4 vCPU / 3.6Gi RAM / 60G，无 GPU |
| 时区 | Asia/Shanghai (CST +0800) —— cron 时间直接用，无需换算 |
| 项目根 | `~/AStock_AI_System` |

### 1.1 关键环境陷阱：本地 Mihomo 代理

服务器上有本地代理 `127.0.0.1:7890`(HTTP) / `7891`(SOCKS)，会**打断**与 `ollama.com` 和 PyPI 的直连（SSL_ERROR_SYSCALL / 502）。处理方式：

- **pi / ollama-cloud**：在 `~/.bashrc` 和 `~/bin/pi-rpc` 中设置 `NO_PROXY="localhost,127.0.0.1,ollama.com,registry.npmjs.org"` 并 `unset HTTP_PROXY HTTPS_PROXY ALL_PROXY ...`。ollama.com 直连 <1s 即可。
- **pip 安装**：`~/.pip/pip.conf` 和 `~/.config/pip/pip.conf` 硬编码了坏代理 + 腾讯镜像。**必须**用 `PIP_CONFIG_FILE=/dev/null` 绕过，再配合腾讯内部 PyPI 镜像安装。
- **数据桥子进程**：`src/config.mjs` 的 `buildMinimalBridgeEnvironment` 已按设计不继承代理变量，AkShare 直连正常。

### 1.2 Node / pi

- Node 通过 **nvm v22.23.2** 安装，软链到 `/usr/local/bin/{node,npm,npx,pi}`，非交互 shell / cron 均可访问。
- pi v0.83.0 全局安装。
- **cron 的 PATH 必须含 `/usr/local/bin`**（cron 默认 PATH 极小），`run-morning-cron.sh` 已 pin。

### 1.3 pi 配置

- 全局：`~/.pi/agent/{settings.json,models.json,auth.json}` —— ollama-cloud 密钥；**默认模型 glm-5.2**（通用编码助手）。
- 项目：`~/AStock_AI_System/.pi/settings.json` —— 锁定 **`ollama-cloud/deepseek-v4-flash`**（晨报用），`thinking=high`。
- `src/config.mjs` 对模型做**硬校验**：`workflow.model !== "deepseek-v4-flash"` 会直接 throw。换模型必须同时改 `config/workflow.json` + 放宽该校验。
- **RPC 模式项目信任**：`--mode rpc` 默认不加载项目 `.pi/settings.json`（非交互 `defaultProjectTrust=ask`→忽略）。`~/bin/pi-rpc` 已加 `--approve` 信任项目设置；`npm run morning` 内部 `runMorningAgent` 也带 `--approve`。
- 已装扩展：pi-web-access、@vigolium/piolium、@hypabolic/pi-hypa(filtered)、pi-turn-limit(项目本地)。

---

## 2. 数据层 / MCP / SQLite

| 组件 | 说明 |
|------|------|
| Python venv | `~/AStock_AI_System/.venv`，akshare 1.18.81 + mcp 2.0.0 |
| 数据桥 | `mcp/akshare_bridge.py`，经 `.venv/bin/python` 运行；每次采集后**非破坏性**写入 SQLite |
| SQLite | `mcp/storage.py` → `data/astock.db`（WAL 模式），表 `runs`，仅追加历史 |
| MCP server | `mcp/akshare_server.py`，`MCPServer` + `ToolAnnotations`，2 只读工具：`get_a_share_morning_context`、`query_morning_history` |
| 跨平台 | `config.mjs` 的 `ALLOWED_DATA_BRIDGES` 同时认 `.venv/scripts/python.exe`(Win) 和 `.venv/bin/python`(Linux)；`scripts/doctor.mjs` 平台感知 |

**注意**：MCP server 的 `tools/call` 需 stdin 保持打开，过早 EOF 会切断响应。

---

## 3. AStock user timers、研究密封与按需 XHS 草稿

当前 VPS 不在 crontab 中调度 AStock。canonical unit 位于仓库 `systemd/`，安装到 `~/.config/systemd/user/`；`ubuntu` 用户已启用 linger，退出 SSH 后 timers 仍可运行。

| 时间/触发 | 单元 | 预期行为 |
|---|---|---|
| 工作日 07:50 | `astock-cninfo-precollect.timer` | 预采集官方披露，与发送隔离 |
| 工作日 08:00 | `astock-xhs-source.timer` | 生成当天正式报告和冻结档案；source 固定禁止最终外发 |
| 交易日 source 成功后 | `astock-research-seal.service` | 校验同次档案并生成不可变研究密封 |
| 密封成功后 | `astock-morning-notify.service` / `nng8-research-digest.service` | 分别校验并投递既有报告 / relay 摘要 |
| 明确的人工按需请求 | `astock-xhs-draft.service` 或草稿 CLI | 本地不可变 `DRAFT_ONLY` 草稿；不触发 notify / relay |

source timer 是 `Persistent=false`，主机关机错过 08:00 不会自动追补。source
失败时不会触发密封成功链。确认休市时关闭 source → seal 日报分支，不生成
日报或每日 Telegram；独立定时 relay 仍可发出签名的 `research_skip` 回执。
XHS 不再是自动阶段，未执行按需草稿不是每日故障。

`astock-xhs-draft.timer` 与 `astock-morning-notify.timer` 没有触发时间，不能
作为 inactive 模板安装；仓库只在 `docs/retired-systemd/*.timer.txt` 保存
历史参考。对已有部署，另行授权后归档并 mask 这两个 timer，保留相应
service 和成功链，不触发研究或发送。这份文件不证明现网已执行退役。

相同草稿按 SHA 命中时保持内容、hash 和 mtime；同日报告内容变化生成新的
hash 版本并保留旧版。`work/xhs/last-run.json` 仅记录最近一次按需调用；
`noop` 不是失败，`held` 通常也退出 0，均不能只用 systemd success 验收。

### 只读验收

```bash
systemctl --user list-timers --all 'astock-*'
systemctl --user status astock-xhs-source.service astock-research-seal.service astock-morning-notify.service --no-pager
journalctl --user -u astock-xhs-source.service -u astock-research-seal.service -u astock-morning-notify.service --since today --no-pager
```

交易日应逐层核对同一目标日的 occurrence、last-run、封存档案、研究密封、
发送事件及平台回执；未出现草稿不能否定已密封研究。休市须核对合法日历
skip 和签名 `research_skip`，不能把没有日报视作失败。具体通知门禁、
只读检查和人工恢复边界见 [MORNING_DELIVERY.md](docs/MORNING_DELIVERY.md)。

草稿没有发布能力：source unit 固定 `ASTOCK_FINAL_DELIVERY=disabled`；draft
unit 只允许 `AF_UNIX`，只能写 `work/xhs`；manifest 固定为 `DRAFT_ONLY`、
`publishEligible=false`、`humanApprovalRequired=true`。canonical 通知单元是
`astock-morning-notify.service`，从密封成功链触发，没有单独通知 timer。
历史 `astock-hermes-notify` 不是现役入口；Research 的 `FROZEN` 也不能替代
source、seal 和 notify 各自的门禁。

通知采用双门授权：`config/notifications.json` 必须允许已批准的 Telegram
路由，且 send-capable 进程必须精确设置 `ASTOCK_FINAL_DELIVERY=telegram`。
source runner 永远不负责发送；不得把旧 runner 重新当作 Telegram 入口。
显式投递、drain、correction 和人工二次授权都属于业务/发布操作，必须有
相应授权；不要用它们做健康检查，也不要重置 ambiguous 或批量排空历史队列。

---

## 4. 远程驱动（本机 → 服务器）

`Pi_Unified_History\` 下的 SSH+RPC 控制器，通过 `~/bin/pi-rpc` 驱动服务器端 pi。

| 启动器 | 作用 |
|--------|------|
| `pi-remote.cmd` / `.sh` | 通用，需 `--cwd` 指定远程目录 |
| `pi-remote-astock.cmd` / `.sh` | **一键**，自动 `--cwd ~/AStock_AI_System`（加载项目 deepseek-v4-flash + AGENTS.md） |

`pi-remote.js` 支持 `PI_REMOTE_CWD` 环境变量做默认 cwd。

```cmd
:: Windows
cd C:\Users\ipjan\Documents\Codex\Pi_Unified_History
pi-remote-astock                 :: 进 REPL，用 deepseek-v4-flash 在服务器项目上干活
pi-remote-astock --session       :: 持久会话
```

REPL 内：`/state` 看模型、`/models` 切模型、`/exit` 退出。提示流式输出。

---

## 5. 模型选择依据（A/B 实测）

用同一份 130KB 真实晨报数据包、`--thinking high --no-tools`，只换模型对比：

| 维度 | GLM-5.2 | DeepSeek V4 Flash |
|------|---------|-------------------|
| 上下文窗口 | 204,800 | **1,000,000** |
| `[事实]/[推断]/[情景]/[数据缺失]` 规则完整度 | **更全**（含 [情景] + 显式 [数据缺失]） | 漏 [情景] |
| 来源追溯 / 时间戳 | ✅ 带时间戳 | ⚠️ 部分 |
| 推断克制度 | 高 | 中（略激进） |
| 速度 | 11s | 14s |
| 不编造目标价/止损/技术位 | ✅ | ✅ |

**结论**：小上下文下 GLM 更"听话"；但**完整晨报全链路**（70 来源 + 长 research prompt + 候选稿 + 最多 3 轮审校反馈）累计逼近/超 20 万 token，GLM 会触发压缩丢来源，**DeepSeek 100 万窗口能保全程证据链**——这是项目锁定 DeepSeek 的核心理由。故：

- 晨报 / 大上下文 → **DeepSeek V4 Flash**（保持现状）
- 单点小任务 / 严格规则 → GLM-5.2 略优
- 通用编码助手 → GLM-5.2（全局默认）

A/B 原始输出：服务器 `work/ab-glm.txt` / `work/ab-ds.txt`。

---

## 6. 验证状态

| 检查 | 命令 | 结果 |
|------|------|------|
| 配置一致性 | `npm run validate` | ✅ |
| 单元测试 | `npm test` | ✅ 39/39 |
| 体检 | `npm run doctor` | ✅ 全过 |
| 体检+真实推理 | `npm run doctor:live` | ✅ DeepSeek V4 Flash 返回正常 |
| 数据桥冒烟 | `npm run data:test` | ✅ 70 来源 / overall 91 / loadedFrom=.venv/bin/python |
| MCP JSON-RPC | initialize / tools/list / tools/call | ✅ |
| SQLite | `python mcp/storage.py info` | ✅ 记录多次 run |

---

## 7. 同步本地改动到服务器

本地改了项目文件后，用 tar-over-ssh 增量同步（排除 .venv/node_modules/.git）：

```bash
cd "<本地项目根>"
tar czf - --exclude=.venv --exclude=node_modules --exclude=.git \
  scripts mcp src config agents test package.json \
  | ssh -i C:/Users/ipjan/Documents/quant.pem ubuntu@150.158.46.28 \
  "cd ~/AStock_AI_System && tar xzf -"
```

---

## 8. 想换晨报模型（例如换回 GLM 做实验）

1. 改 `config/workflow.json` 的 `provider`/`model`。
2. 放宽 `src/config.mjs` 里对 `deepseek-v4-flash` 的硬校验（或改成接受目标模型）。
3. 改 `~/AStock_AI_System/.pi/settings.json` 的 `defaultModel`。
4. `npm run validate` 确认一致 → `npm run doctor:live` 确认新模型可用。
5. 实验完务必还原（DeepSeek 是验证过的默认）。

> ⚠️ 不建议长期改：DeepSeek 的 1M 上下文是全链路证据链完整性的保障。
