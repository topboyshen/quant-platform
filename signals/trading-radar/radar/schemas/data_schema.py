#!/usr/bin/env python3
"""
统一数据 Schema (OHLCV + 复权因子 + 财务指标)
用于标准化不同数据源的数据格式
"""

from typing import List

import numpy as np
import pandas as pd

# 尝试导入 pandera（pandas 校验走 pandera.pandas，避免顶层 pandera 弃用告警）
try:
    import pandera.pandas as pa
    PANDERA_AVAILABLE = True
except ImportError:
    PANDERA_AVAILABLE = False
    pa = None


# ==================== 标准列名定义 ====================

class ColumnNames:
    """统一列名常量定义"""
    # 基础 OHLCV
    DATE = "date"
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    VOLUME = "volume"
    AMOUNT = "amount"  # 成交额

    # 派生价格 (兼容层)
    PRICE = "price"  # 与 close 等价
    VWAP = "vwap"    # 成交均价

    # 复权因子
    ADJ_FACTOR = "adj_factor"
    IS_ADJUSTED = "is_adjusted"

    # 涨跌幅
    CHANGE = "change"
    CHANGE_PCT = "change_pct"

    # 财务指标 (可选)
    PE_TTM = "pe_ttm"
    PB = "pb"
    TURNOVER = "turnover"  # 换手率
    MARKET_CAP = "market_cap"

    # 股票信息
    CODE = "code"
    NAME = "name"


# 必需列
REQUIRED_COLUMNS = [
    ColumnNames.DATE,
    ColumnNames.OPEN,
    ColumnNames.HIGH,
    ColumnNames.LOW,
    ColumnNames.CLOSE,
    ColumnNames.VOLUME,
]

# 可选列
OPTIONAL_COLUMNS = [
    ColumnNames.AMOUNT,
    ColumnNames.PRICE,  # 冗余列，与 close 等价
    ColumnNames.VWAP,
    ColumnNames.ADJ_FACTOR,
    ColumnNames.IS_ADJUSTED,
    ColumnNames.CHANGE,
    ColumnNames.CHANGE_PCT,
    ColumnNames.PE_TTM,
    ColumnNames.PB,
    ColumnNames.TURNOVER,
    ColumnNames.MARKET_CAP,
    ColumnNames.CODE,
    ColumnNames.NAME,
]

ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


# ==================== Pandera Schema ====================

if PANDERA_AVAILABLE:
    # DataFrame 级别校验函数
    def _check_high_low(df):
        return (df["high"] >= df["low"]).all()

    def _check_close_range(df):
        return (
            (df["close"] >= df["low"]) & (df["close"] <= df["high"])
        ).all()

    def _check_open_range(df):
        return (
            (df["open"] >= df["low"]) & (df["open"] <= df["high"])
        ).all()

    # 基础 OHLCV Schema
    OHLCV_SCHEMA = pa.DataFrameSchema({
        # 日期索引或列
        ColumnNames.DATE: pa.Column(pa.DateTime, nullable=False),

        # 价格列：必须为正数
        ColumnNames.OPEN: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
            pa.Check.less_than_or_equal_to(10000),  # 合理价格范围
        ], nullable=False),
        ColumnNames.HIGH: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
            pa.Check.less_than_or_equal_to(10000),
        ], nullable=False),
        ColumnNames.LOW: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
            pa.Check.less_than_or_equal_to(10000),
        ], nullable=False),
        ColumnNames.CLOSE: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
            pa.Check.less_than_or_equal_to(10000),
        ], nullable=False),

        # 成交量：非负整数
        ColumnNames.VOLUME: pa.Column(pa.Int64, checks=[
            pa.Check.greater_than_or_equal_to(0),
        ], nullable=False),

        # 派生价格列 (与 close 等价)
        ColumnNames.PRICE: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
        ], nullable=True, required=False),

        # 成交额：非负
        ColumnNames.AMOUNT: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than_or_equal_to(0),
        ], nullable=True, required=False),

        # 复权因子：默认为1
        ColumnNames.ADJ_FACTOR: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
        ], nullable=True, required=False),
        ColumnNames.IS_ADJUSTED: pa.Column(pa.Bool, nullable=True, required=False),

        # 涨跌幅
        ColumnNames.CHANGE: pa.Column(pa.Float64, nullable=True, required=False),
        ColumnNames.CHANGE_PCT: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than_or_equal_to(-30),
            pa.Check.less_than_or_equal_to(30),
        ], nullable=True, required=False),

        # 财务指标
        ColumnNames.PE_TTM: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
            pa.Check.less_than(10000),  # 排除极端值
        ], nullable=True, required=False),
        ColumnNames.PB: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than(0),
        ], nullable=True, required=False),
        ColumnNames.TURNOVER: pa.Column(pa.Float64, checks=[
            pa.Check.greater_than_or_equal_to(0),
            pa.Check.less_than_or_equal_to(100),
        ], nullable=True, required=False),  # 换手率 %

        # 股票信息
        ColumnNames.CODE: pa.Column(pa.String, nullable=True, required=False),
        ColumnNames.NAME: pa.Column(pa.String, nullable=True, required=False),

    }, strict=False,  # 允许额外列
       coerce=True,   # 自动类型转换
       checks=[
           pa.Check(_check_high_low, error="high < low 校验失败"),
           pa.Check(_check_close_range, error="close 不在 [low, high] 范围内"),
           pa.Check(_check_open_range, error="open 不在 [low, high] 范围内"),
       ]
    )
else:
    OHLCV_SCHEMA = None


# ==================== 数据校验器 ====================

class DataValidator:
    """数据校验器，支持 pandera 和基础校验"""

    def __init__(self):
        self.errors: List[str] = []

    def validate(self, df: pd.DataFrame, strict: bool = False) -> bool:
        """
        校验 DataFrame 是否符合统一 Schema

        Args:
            df: 待校验的 DataFrame
            strict: 是否严格模式（检查所有列名）

        Returns:
            bool: 是否通过校验
        """
        self.errors = []

        # 使用 pandera 校验
        if PANDERA_AVAILABLE and OHLCV_SCHEMA is not None:
            try:
                OHLCV_SCHEMA.validate(df)
                return True
            except pa.errors.SchemaError as e:
                self.errors.append(f"Schema 校验失败: {e}")
                return False

        # 基础校验 (无 pandera 时)
        return self._basic_validate(df, strict)

    def _basic_validate(self, df: pd.DataFrame, strict: bool) -> bool:
        """基础校验逻辑"""
        # 检查必需列
        missing = set(REQUIRED_COLUMNS) - set(df.columns)
        if missing:
            self.errors.append(f"缺少必需列: {missing}")
            return False

        # 检查数据类型
        numeric_cols = [ColumnNames.OPEN, ColumnNames.HIGH, ColumnNames.LOW,
                        ColumnNames.CLOSE, ColumnNames.VOLUME]
        for col in numeric_cols:
            if not pd.api.types.is_numeric_dtype(df[col]):
                self.errors.append(f"列 {col} 不是数值类型")
                return False

        # 检查价格逻辑
        for idx, row in df.iterrows():
            if row[ColumnNames.HIGH] < row[ColumnNames.LOW]:
                self.errors.append(f"行 {idx}: high < low")
                return False
            if not (row[ColumnNames.LOW] <= row[ColumnNames.CLOSE] <= row[ColumnNames.HIGH]):
                self.errors.append(f"行 {idx}: close 不在 [low, high] 范围内")
                return False
            if not (row[ColumnNames.LOW] <= row[ColumnNames.OPEN] <= row[ColumnNames.HIGH]):
                self.errors.append(f"行 {idx}: open 不在 [low, high] 范围内")

        # 严格模式：检查是否有未知列
        if strict:
            unknown = set(df.columns) - set(ALL_COLUMNS)
            if unknown:
                self.errors.append(f"发现未知列: {unknown}")
                return False

        return len(self.errors) == 0


# ==================== 数据标准化器 ====================

class DataNormalizer:
    """
    数据标准化器：将不同数据源的数据转换为统一 Schema
    """

    # 列名映射表：支持多种中文/英文列名变体
    COLUMN_MAPPINGS = {
        # 日期
        ColumnNames.DATE: ["date", "日期", "time", "timestamp", "Date", "交易日期", "trade_date"],

        # 价格
        ColumnNames.OPEN: ["open", "开盘价", "Open", "今开", "开盘", "开盘"],
        ColumnNames.HIGH: ["high", "最高价", "High", "最高", "highest"],
        ColumnNames.LOW: ["low", "最低价", "Low", "最低", "lowest"],
        ColumnNames.CLOSE: ["close", "收盘价", "Close", "最新价", "现价", "close_price", "收盘"],
        ColumnNames.PRICE: ["price", "Price", "价格", "current_price", "最新价"],

        # 成交量额
        ColumnNames.VOLUME: ["volume", "成交量", "Volume", "vol", "VOL", "成交股数", "成交量(股)"],
        ColumnNames.AMOUNT: ["amount", "成交额", "Amount", "amt", "AMT", "成交金额"],

        # 复权
        ColumnNames.ADJ_FACTOR: ["adj_factor", "复权因子", "adjust_factor", "factor"],

        # 涨跌幅
        ColumnNames.CHANGE: ["change", "涨跌额", "change_amount", "涨跌"],
        ColumnNames.CHANGE_PCT: ["change_pct", "涨跌幅", "change_percent", "pct_change", "涨跌%"],

        # 财务
        ColumnNames.PE_TTM: ["pe_ttm", "市盈率_TTM", "PE_TTM", "pe", "PE"],
        ColumnNames.PB: ["pb", "市净率", "PB", "pb_ratio"],
        ColumnNames.TURNOVER: ["turnover", "换手率", "turnover_rate", "换手%"],

        # 信息
        ColumnNames.CODE: ["code", "股票代码", "Code", "symbol", "stock_code", "ts_code"],
        ColumnNames.NAME: ["name", "股票名称", "Name", "stock_name", "股票简称"],
    }

    def __init__(self):
        self.validator = DataValidator()
        self.warnings: List[str] = []

    def normalize(self, df: pd.DataFrame, source: str = "unknown") -> pd.DataFrame:
        """
        标准化数据：转换为统一 Schema

        Args:
            df: 原始 DataFrame
            source: 数据来源标识 (如 "eastmoney", "tushare", "akshare")

        Returns:
            标准化后的 DataFrame
        """
        self.warnings = []
        df = df.copy()

        # 1. 标准化列名
        df = self._normalize_columns(df)

        # 2. 标准化日期
        df = self._normalize_date(df)

        # 3. 确保必需列存在
        df = self._ensure_required_columns(df)

        # 4. 数据清洗
        df = self._clean_data(df)

        # 5. 添加派生列
        df = self._add_derived_columns(df)

        # 6. 排序和去重
        df = self._sort_and_dedup(df)

        # 7. 校验
        if not self.validator.validate(df, strict=False):
            raise ValueError(f"数据校验失败: {self.validator.errors}")

        return df

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化列名"""
        # 反向映射：从各种变体找到标准名
        rename_map = {}
        used_standards = set()

        for col in df.columns:
            col_lower = col.lower().strip()
            for standard, variants in self.COLUMN_MAPPINGS.items():
                if col in variants or col_lower in [v.lower() for v in variants]:
                    # 如果标准列已存在，保留原列
                    if standard in used_standards:
                        continue
                    rename_map[col] = standard
                    used_standards.add(standard)
                    break

        # 执行重命名
        df = df.rename(columns=rename_map)

        # 检查缺失的必需列
        missing = set(REQUIRED_COLUMNS) - set(df.columns)
        if missing:
            self.warnings.append(f"缺失列，将使用默认值: {missing}")
            for col in missing:
                if col == ColumnNames.DATE:
                    df[col] = pd.NaT
                elif col == ColumnNames.VOLUME:
                    df[col] = 0
                else:
                    df[col] = np.nan

        return df

    def _normalize_date(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化日期列"""
        if ColumnNames.DATE in df.columns:
            # 尝试多种日期格式
            date_col = df[ColumnNames.DATE]

            # 如果是字符串，尝试解析
            if date_col.dtype == object:
                # 先尝试自动推断
                try:
                    df[ColumnNames.DATE] = pd.to_datetime(date_col, errors="coerce")
                    # 如果全部解析成功，直接返回
                    if df[ColumnNames.DATE].notna().all():
                        pass
                    else:
                        # 有失败，尝试特定格式
                        failed_mask = df[ColumnNames.DATE].isna()
                        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%m/%d/%Y"]:
                            try:
                                parsed = pd.to_datetime(date_col[failed_mask], format=fmt, errors="coerce")
                                df.loc[failed_mask, ColumnNames.DATE] = parsed
                                failed_mask = df[ColumnNames.DATE].isna()
                                if not failed_mask.any():
                                    break
                            except (ValueError, TypeError):
                                continue
                except (ValueError, TypeError):
                    df[ColumnNames.DATE] = pd.to_datetime(date_col, errors="coerce")
            else:
                df[ColumnNames.DATE] = pd.to_datetime(date_col)

            # 确保时区是本地时间
            if df[ColumnNames.DATE].dt.tz is not None:
                df[ColumnNames.DATE] = df[ColumnNames.DATE].dt.tz_localize(None)

        return df

    def _ensure_required_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """确保所有必需列都存在且逻辑一致"""
        # close -> price 映射 (如果 price 缺失但 close 存在)
        if ColumnNames.PRICE not in df.columns and ColumnNames.CLOSE in df.columns:
            df[ColumnNames.PRICE] = df[ColumnNames.CLOSE]

        # price -> close 映射 (如果 close 缺失但 price 存在)
        if ColumnNames.CLOSE not in df.columns and ColumnNames.PRICE in df.columns:
            df[ColumnNames.CLOSE] = df[ColumnNames.PRICE]

        # 如果只有 close，用 close 填充其他价格列
        if ColumnNames.CLOSE in df.columns:
            for col in [ColumnNames.OPEN, ColumnNames.HIGH, ColumnNames.LOW]:
                if col not in df.columns or df[col].isna().all():
                    df[col] = df[ColumnNames.CLOSE]

        # 修复价格逻辑不一致问题（API 数据质量问题）
        # 确保: low <= min(open, close) <= max(open, close) <= high
        if all(col in df.columns for col in [ColumnNames.OPEN, ColumnNames.HIGH, ColumnNames.LOW, ColumnNames.CLOSE]):
            # 计算每个时间点的合理 low 和 high
            price_matrix = df[[ColumnNames.OPEN, ColumnNames.HIGH, ColumnNames.LOW, ColumnNames.CLOSE]]

            # 找出当前 high < 四个价格中最大值 的行
            actual_high = price_matrix.max(axis=1)
            actual_low = price_matrix.min(axis=1)

            needs_fix = (df[ColumnNames.HIGH] < actual_high) | (df[ColumnNames.LOW] > actual_low)
            if needs_fix.any():
                self.warnings.append(
                    f"发现 {needs_fix.sum()} 行价格逻辑不一致 (open/high/low/close)，已自动修正"
                )
                df.loc[needs_fix, ColumnNames.HIGH] = actual_high[needs_fix]
                df.loc[needs_fix, ColumnNames.LOW] = actual_low[needs_fix]

        # high >= low 兜底检查
        if ColumnNames.HIGH in df.columns and ColumnNames.LOW in df.columns:
            invalid = df[ColumnNames.HIGH] < df[ColumnNames.LOW]
            if invalid.any():
                self.warnings.append(f"发现 {invalid.sum()} 行 high < low，已交换")
                df.loc[invalid, [ColumnNames.HIGH, ColumnNames.LOW]] = \
                    df.loc[invalid, [ColumnNames.LOW, ColumnNames.HIGH]].values

        return df

    def _clean_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """数据清洗"""
        # 删除全空行
        df = df.dropna(how="all")

        # 数值列转换为 float
        numeric_cols = [ColumnNames.OPEN, ColumnNames.HIGH, ColumnNames.LOW,
                        ColumnNames.CLOSE, ColumnNames.VOLUME, ColumnNames.AMOUNT]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 删除关键列全为空的行
        key_cols = [col for col in [ColumnNames.CLOSE, ColumnNames.VOLUME] if col in df.columns]
        if key_cols:
            df = df.dropna(subset=key_cols, how="all")

        # 处理无穷值
        df = df.replace([np.inf, -np.inf], np.nan)

        return df

    def _add_derived_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加派生列"""
        # 如果存在 close 但不存在 price，添加 price
        if ColumnNames.CLOSE in df.columns and ColumnNames.PRICE not in df.columns:
            df[ColumnNames.PRICE] = df[ColumnNames.CLOSE]

        # 计算涨跌幅
        if ColumnNames.CHANGE_PCT not in df.columns and ColumnNames.CLOSE in df.columns:
            df[ColumnNames.CHANGE_PCT] = df[ColumnNames.CLOSE].pct_change() * 100

        if ColumnNames.CHANGE not in df.columns and ColumnNames.CLOSE in df.columns:
            df[ColumnNames.CHANGE] = df[ColumnNames.CLOSE].diff()

        # 计算 VWAP (成交量加权平均价)
        if (ColumnNames.VWAP not in df.columns and
            ColumnNames.AMOUNT in df.columns and
            ColumnNames.VOLUME in df.columns):
            with np.errstate(divide="ignore", invalid="ignore"):
                df[ColumnNames.VWAP] = df[ColumnNames.AMOUNT] / df[ColumnNames.VOLUME]
            df[ColumnNames.VWAP] = df[ColumnNames.VWAP].replace([np.inf, -np.inf], np.nan)

        # 复权标记
        if ColumnNames.IS_ADJUSTED not in df.columns:
            df[ColumnNames.IS_ADJUSTED] = ColumnNames.ADJ_FACTOR in df.columns

        return df

    def _sort_and_dedup(self, df: pd.DataFrame) -> pd.DataFrame:
        """排序和去重"""
        if ColumnNames.DATE in df.columns:
            df = df.sort_values(ColumnNames.DATE)
            df = df.drop_duplicates(subset=[ColumnNames.DATE], keep="last")
            df = df.reset_index(drop=True)

        return df


# ==================== 便捷函数 ====================

def normalize_dataframe(df: pd.DataFrame, source: str = "unknown") -> pd.DataFrame:
    """便捷函数：标准化 DataFrame"""
    normalizer = DataNormalizer()
    return normalizer.normalize(df, source)


def validate_dataframe(df: pd.DataFrame, strict: bool = False) -> tuple:
    """便捷函数：校验 DataFrame

    Returns:
        (is_valid: bool, errors: list)
    """
    validator = DataValidator()
    is_valid = validator.validate(df, strict)
    return is_valid, validator.errors


# ==================== 测试 ====================

if __name__ == "__main__":
    # 测试数据
    test_data = {
        "日期": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "开盘价": [100.0, 101.0, 102.0],
        "最高价": [102.0, 103.0, 104.0],
        "最低价": [99.0, 100.0, 101.0],
        "收盘价": [101.0, 102.0, 103.0],
        "成交量": [10000, 15000, 20000],
    }

    df = pd.DataFrame(test_data)
    print("原始数据:")
    print(df)
    print("\n列名:", df.columns.tolist())

    normalizer = DataNormalizer()
    df_norm = normalizer.normalize(df, source="test")

    print("\n标准化后:")
    print(df_norm)
    print("\n列名:", df_norm.columns.tolist())
    print("\n警告:", normalizer.warnings)

    # 校验
    is_valid, errors = validate_dataframe(df_norm)
    print(f"\n校验结果: {is_valid}")
    if errors:
        print("错误:", errors)
