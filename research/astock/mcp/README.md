# A 股数据接入

本项目提供互相隔离的晨报与主动投研入口：

1. 当前自动晨报使用固定的标准输入桥 `mcp/akshare_bridge.py`。Node 工作流直接启动项目虚拟环境中的 Python，不经过 shell；请求从 stdin 读取，响应 JSON 单独写到 stdout。
2. `mcp/akshare_server.py` 把同一数据核心封装成 MCP stdio server，供未来具备 MCP 客户端能力的 Pi 或其他客户端使用。
3. `mcp/stock_research_server.py` 提供主动投研的六个细粒度工具。固定客户端 `stock_research_client.py` 只允许配置核心池，并把结果交给 `pi-research`。

当前本机 Pi Coding Agent 0.83.0 没有原生 MCP 配置入口。项目扩展通过固定 Python 客户端连接本地 stdio server；不会让 Pi 选择任意 server、tool、命令或路径。自动晨报始终使用固定 stdin 桥，不改用主动投研 MCP。

## Stock Research MCP

`stock_research_server.py` 仅在一次研究请求期间通过 stdio 启动，不监听端口、不常驻，也不具备交易、发稿或调度能力。工具为：

- `get_stock_price()`：复权日 K、MA/RSI/MACD 与量能指标。
- `get_financial_report()`：时点过滤的财务摘要、机构持股和主要股东；无披露时间的机构数据会明确降级。
- `get_news()`：带发布时间的媒体新闻，不冒充官方公告。
- `get_sector_rank()`：当前行业或概念快照。
- `get_capital_flow()`：供应方口径的个股历史或市场排行，不代表真实资金意图。
- `run_backtest()`：只读最近验证 Qlib 回测摘要和指定股票历史横截面排名；不会现场训练或运行任意配置。

随后 `kronos_bridge.py` 可从已返回的历史 K 线构建最多三只股票的本地 Kronos-mini 五日状态情景。它使用固定源码、模型、tokenizer、CPU 和交易日历路径；调用者不能指定路径或模型。失败时显式降级，不影响其他证据。

## 安装

在项目根目录执行：

```powershell
npm run data:setup
```

该命令创建项目自己的 `.venv`，并从根目录 `requirements.txt` 安装固定的 `akshare 1.18.81` 与 `mcp 2.0.0`。不会改系统 Python，也不会读取或显示 Pi/Ollama 凭据。

## 自动工作流使用的固定 stdin 桥

固定入口由 `config/workflow.json` 的 `dataBridge` 指向：

```text
.venv/Scripts/python.exe mcp/akshare_bridge.py
```

工作流把 `contract.schema.json#/$defs/request` 对应的一个完整 JSON 写入 stdin。桥必须：

1. 从 stdin 读取且只读取一个完整请求。
2. 拉取并规范化行情、资金、公告、新闻和财务数据。
3. 将一个 `contract.schema.json#/$defs/response` 响应写到 stdout。
4. 把日志写到 stderr，并以退出码 0 表示协议调用成功。
5. 为 `live`/`partial` 数据提供 `sources`，并用 `sourceIds` 关联事实字段。

stdout 不得混入日志，否则 JSON 解析会失败。桥失败时，普通晨报运行会安全降级为数据不可用；`--strict-data` 会直接失败，适合诊断。

AkShare 上游 HTTP 请求若未自行声明超时，数据核心会在单次 AkShare 调用期间补上固定的 15 秒默认超时；上游显式设置的超时不会被覆盖。某个模块超时后会按现有规则使用匹配的 last-good 缓存，或把该模块标记为不可用，其余模块继续采集。这个临时设置仅作用于当前采集线程，并会在成功或异常后恢复，不会读取、记录或输出请求凭据。

响应中的 `meta.dataCompleteness` 提供 `market`、`capital`、`news`、`financial` 和 `overall` 五类模块评分；`data.sectionCompleteness` 记录八个技术区块的评分。评分衡量本次模块的新鲜度与可用性，不代表数据绝对正确。完整响应恒定包含交易日历、隔夜市场、上一 A 股交易日、政策事件、板块、资金、股票池、公告与新闻八个区块。

每个 `sources[]` 条目都有 `sourceType`，用于区分 `trading_calendar_data`、`market_data`、`industry_market_data`、`dragon_tiger_list_aggregation`、`financial_data_aggregation`、`media_article`、`media_aggregation` 与 `official_announcement`。当 `stock_info_global_em` 或 `stock_news_em` 返回东方财富域名下的聚合页时，来源会明确标为 `media_aggregation`，并分别保留 `originalPublisher`（原媒体，若上游提供）和 `aggregator: "东方财富"`；该链接不能被描述成原媒体直链，也不能被当成交易所或上市公司官方公告。

last-good 缓存包含独立的 `cacheFormatVersion`。来源身份结构升级后，旧版本缓存会自动失效，避免旧缓存继续输出缺少分类或错误归属的来源记录。

手工把一个请求送入固定桥时，可调用 PowerShell 包装器：

```powershell
Get-Content -Raw work/morning-request.json |
  .\scripts\run-akshare-bridge.ps1
```

包装器只会调用本项目 `.venv` 中的 Python，不会回退到系统 Python。

## 验证真实数据桥

以下命令会连接数据源、校验响应并准备晨报上下文，但不会调用 DeepSeek，也不会生成正式晨报：

```powershell
npm run data:test
```

若只想验证协议、不访问真实行情，可继续使用 mock provider：

```powershell
node scripts/morning-report.mjs --dry-run --input data/samples/morning-context.example.json
```

## MCP server：为未来客户端准备

未来支持 MCP stdio 的客户端可启动：

```powershell
.\.venv\Scripts\python.exe mcp\akshare_server.py
```

它复用 stdin 桥的数据采集与标准化核心，而不是另建一套数据逻辑。MCP transport 的初始化、工具调用与通知均遵循 MCP 协议；不要把普通桥的单个 request JSON 直接写给 MCP server。

## 数据与证据边界

- AkShare 是数据访问库，不是交易所官方事实保证。上游接口可能变更、限流或返回空值；报告必须保留数据时间、来源和缺口。
- 行情、板块、龙虎榜和财务记录的 `units` 显式说明数值单位；同花顺行业一览按 AkShare 文档使用万手/亿元，`provider_native_unspecified` 则表示上游接口未给出可核验单位，禁止自行换算或补写“元/亿元/股/手”。
- “主力资金”等字段依赖数据供应方口径，不能被当作资金真实意图。
- 北向资金盘中实时净买入若没有可核验数据，应明确写“北向资金实时净流入不可用”。不得用历史值、成交额或媒体估算替代。
- 新闻影响方向、涨跌原因、主题受益关系属于推断，不属于源数据事实。
- 不要把密码、Cookie、API key、Token 或 Pi 的 `auth.json` 放入请求、响应、日志或项目文件。
