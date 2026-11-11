# 📐 统一 Data Schema 设计文档

> 目标：解决不同数据源列名、索引、格式不统一的问题，建立统一的 OHLCV + 复权因子 + 财务指标数据标准。

---

## 🏗️ 架构概览

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   原始数据源     │     │   统一数据层      │     │   业务消费者     │
│  (MX Client)    │────▶│  DataNormalizer  │────▶│  RL/Backtest    │
│  Tushare        │     │  DataValidator   │     │  Monitor        │
│  AKShare        │     │  UnifiedSource   │     │  Benchmark      │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

---

## 📁 新增文件

| 文件 | 职责 |
|------|------|
| `data_schema.py` | 定义统一 Schema、列名常量、Pandera 校验规则、DataNormalizer |
| `radar/data/market_data.py` | 统一数据源入口，`MarketDataFetcher` 包装东财/腾讯/akshare，返回标准化数据 |
| `test_data_schema.py` | 测试套件，对比新旧格式、测试列名映射、校验错误捕获 |

---

## 📊 标准列名 (ColumnNames)

### 必需列 (REQUIRED_COLUMNS)

| 列名 | 类型 | 说明 |
|------|------|------|
| `date` | datetime | 交易日期，已解析为 datetime64 |
| `open` | float64 | 开盘价，必须 > 0 |
| `high` | float64 | 最高价，必须 >= max(open, close, low) |
| `low` | float64 | 最低价，必须 <= min(open, close, high) |
| `close` | float64 | 收盘价，必须 > 0 |
| `volume` | int64 | 成交量，必须 >= 0 |

### 可选列 (OPTIONAL_COLUMNS)

| 列名 | 类型 | 说明 |
|------|------|------|
| `price` | float64 | 与 `close` 等价，向后兼容 |
| `amount` | float64 | 成交额 |
| `vwap` | float64 | 成交量加权平均价 |
| `adj_factor` | float64 | 复权因子 |
| `is_adjusted` | bool | 是否已复权 |
| `change` | float64 | 涨跌额 |
| `change_pct` | float64 | 涨跌幅 % |
| `pe_ttm` | float64 | 市盈率 TTM |
| `pb` | float64 | 市净率 |
| `turnover` | float64 | 换手率 % |
| `code` | string | 股票代码 |
| `name` | string | 股票名称 |

---

## 🔧 自动修复的数据质量问题

`DataNormalizer` 在标准化过程中会自动修复常见数据质量问题：

1. **列名自动映射**
   - `开盘价` → `open`
   - `收盘价` / `最新价` / `收盘` → `close`
   - `成交量` / `vol` / `VOL` → `volume`
   - `trade_date` → `date`
   - 支持 East Money / Tushare / AKShare 等多种格式

2. **日期自动解析**
   - `2024-01-01` → datetime64
   - `20240101` → datetime64
   - `2024/01/01` → datetime64
   - 自动去时区

3. **价格逻辑修复**
   - 检测 `open` / `high` / `low` / `close` 的逻辑不一致
   - 自动修正 `high = max(ohlc)`，`low = min(ohlc)`
   - 处理 API 返回的 `high == close == low` 但 `open != close` 的情况

4. **缺失值填充**
   - `open` / `high` / `low` 缺失 → 用 `close` 填充
   - `price` 缺失 → 用 `close` 填充

5. **派生列自动计算**
   - `price = close`
   - `change_pct = close.pct_change() * 100`
   - `change = close.diff()`
   - `vwap = amount / volume`

---

## ✅ Pandera 校验规则

使用 `pandera` 进行 DataFrame 级别强校验：

### 列级校验
- `open/high/low/close` > 0 且 <= 10000
- `volume` >= 0
- `change_pct` ∈ [-30%, 30%]
- `pe_ttm` > 0 且 < 10000
- `turnover` ∈ [0%, 100%]

### DataFrame 级校验
- `high >= low`（全表）
- `close ∈ [low, high]`（全表）
- `open ∈ [low, high]`（全表）

### 降级方案
如果 `pandera` 未安装，自动回退到 `DataValidator._basic_validate()`，保持基础校验能力。

---

## 🚀 快速使用

### 1. 获取标准化日线数据

```python
from radar.data.market_data import MarketDataFetcher

fetcher = MarketDataFetcher(logger=logger, use_akshare=True)
df = fetcher.get_historical_data('300059', days=120)

# df 列名: ['date', 'open', 'high', 'low', 'close', 'volume', 'price', 'change_pct', ...]
```

### 2. 获取标准化实时数据

```python
rt = ds.get_realtime_data('300059', '东方财富')
# {'code': '300059', 'name': '东方财富', 'close': 19.97, 'open': 20.19, ...}
```

### 3. 手动标准化任意 DataFrame

```python
from data_schema import normalize_dataframe

df_norm = normalize_dataframe(df_raw, source="tushare")
```

### 4. 数据校验

```python
from data_schema import validate_dataframe

is_valid, errors = validate_dataframe(df)
if not is_valid:
    print("数据异常:", errors)
```

---

## 🔄 迁移状态

### 已迁移模块
- ✅ `run_benchmark.py` — 使用 `UnifiedDataSource.get_daily_data()`
- ✅ `rl_features.py` — 兼容统一 Schema（`price` / `close` 等价）
- ✅ `rl_strategy.py` — Live 模式兼容统一实时数据格式
- ✅ `backtest_pro.py` — 兼容统一 Schema
- ✅ `radar/core/monitor_engine.py` — 真实持仓加载已对接

### 已迁移模块
- ✅ `radar/data/market_data.py` — 底层行情获取器已返回统一 Schema（英文列名 + Pandera 校验）
- ✅ `radar/data/market_data_concurrent.py` — 并发行情获取器已适配英文列名缓存
- ✅ `run_benchmark.py` — 使用 `UnifiedDataSource.get_daily_data()`
- ✅ `rl_features.py` — 兼容统一 Schema（`price` / `close` 等价）
- ✅ `rl_strategy.py` — Live 模式兼容统一实时数据格式
- ✅ `backtest_pro.py` — 兼容统一 Schema
- ✅ `radar/core/monitor_engine.py` — 真实持仓加载已对接统一接口

### 待迁移模块
- ⏳ `mx_client.py` — 作为低层客户端保留，建议上层统一走 `radar/data/market_data.py`

---

## 📈 效果对比

### 旧格式 (mx_client.py)
```python
# 列名混合中文/英文
['date', 'open', 'close', '是否涨停', 'high', 'low', 'volume', 'price']

# date 是字符串
df['date'].dtype  # object

# 需要手动处理缺失列
df['high'] = df.get('high', df['close'])
```

### 新格式 (统一 Schema)
```python
# 纯英文标准列名
['date', 'open', 'high', 'low', 'close', 'volume', 'price', 
 'change_pct', 'change', 'is_adjusted']

# date 是 datetime64
df['date'].dtype  # datetime64[us]

# 自动填充缺失列、校验数据质量
# 直接可用，无需额外处理
```

---

## 🧪 运行测试

```bash
cd /home/ubuntu/trading_radar
source venv/bin/activate
python test_data_schema.py
```

测试覆盖：
1. 新旧数据格式对比
2. East Money / Tushare / AKShare 列名映射
3. 实时数据标准化
4. 数据校验错误捕获（high<low、负数价格、close越界）

---

## 💡 后续扩展建议

1. **多数据源支持**
   在 `radar/data/market_data.py` 中增加 `TushareDataSource`、`AKShareDataSource` 适配器

2. **复权数据接入**
   在 `DataNormalizer` 中增加前复权/后复权计算逻辑

3. **财务指标扩展**
   在 Schema 中增加 `ps_ttm`、`roe`、`net_profit_growth` 等指标

4. **缓存策略优化**
   `CachedDataSource` 已内置 TTL 缓存，可扩展为本地 parquet 持久化
