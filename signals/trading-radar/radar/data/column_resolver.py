"""
列名标准化工具 - 统一处理不同数据源的列名差异

问题：东财 API 返回的列名有多种格式（中文、f12/f14 字段名、英文常量），
多个模块（market_data.py、push_adapter.py、akshare_adapter.py）都有各自的 fallback 逻辑。

解决方案：集中到这里，统一探测和映射。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    import pandas as pd


# 标准列名常量
class StandardColumns:
    CODE = "code"
    NAME = "name"
    PRICE = "price"
    CLOSE = "close"
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    VOLUME = "volume"
    AMOUNT = "amount"
    CHANGE = "change"
    CHANGE_PCT = "change_pct"
    PREV_CLOSE = "prev_close"
    TURNOVER = "turnover"


# 列名别名映射：标准名 -> 可能的别名列表
COLUMN_ALIASES: Dict[str, List[str]] = {
    StandardColumns.CODE: [
        "代码", "f12", "symbol", "CODE", "code", "股票代码", "stock_code",
    ],
    StandardColumns.NAME: [
        "名称", "f14", "name", "NAME", "股票名称", "stock_name",
    ],
    StandardColumns.PRICE: [
        "最新价", "f2", "price", "PRICE", "current_price", "现价",
    ],
    StandardColumns.CLOSE: [
        "最新价", "f2", "close", "CLOSE", "收盘价", "收盘",
    ],
    StandardColumns.OPEN: [
        "今开", "f17", "open", "OPEN", "开盘价", "开盘",
    ],
    StandardColumns.HIGH: [
        "最高", "f15", "high", "HIGH", "最高价",
    ],
    StandardColumns.LOW: [
        "最低", "f16", "low", "LOW", "最低价",
    ],
    StandardColumns.VOLUME: [
        "成交量", "f5", "volume", "VOLUME", "vol", "VOL", "成交",
    ],
    StandardColumns.AMOUNT: [
        "成交额", "f6", "amount", "AMOUNT", "amt", "AMT",
    ],
    StandardColumns.CHANGE: [
        "涨跌额", "f4", "change", "CHANGE", "涨跌",
    ],
    StandardColumns.CHANGE_PCT: [
        "涨跌幅", "f3", "change_pct", "CHANGE_PCT", "pct", "PCT", "涨幅",
    ],
    StandardColumns.PREV_CLOSE: [
        "昨收", "f18", "prev_close", "PREV_CLOSE", "昨收价", "previous_close",
    ],
    StandardColumns.TURNOVER: [
        "换手率", "f8", "turnover", "TURNOVER", "换手",
    ],
}


def detect_columns(df_columns) -> Dict[str, Optional[str]]:
    """
    探测 DataFrame 列名映射

    参数:
        df_columns: DataFrame.columns 或列名列表

    返回:
        {标准列名: 实际列名}，未找到的列值为 None
    """
    cols = list(df_columns)
    col_set = set(cols)
    mapping: Dict[str, Optional[str]] = {}

    for standard, aliases in COLUMN_ALIASES.items():
        found = None
        # 优先精确匹配
        if standard in col_set:
            found = standard
        else:
            # 按别名顺序匹配
            for alias in aliases:
                if alias in col_set:
                    found = alias
                    break
        mapping[standard] = found

    return mapping


def rename_to_standard(df: "pd.DataFrame", mapping: Optional[Dict[str, Optional[str]]] = None) -> "pd.DataFrame":
    """
    将 DataFrame 列名重命名为标准列名

    参数:
        df: pandas DataFrame
        mapping: 可选，预计算的列名映射（由 detect_columns 生成）

    返回:
        重命名后的 DataFrame（原地修改）
    """

    if mapping is None:
        mapping = detect_columns(df.columns)

    rename_map = {}
    for standard, actual in mapping.items():
        if actual is not None and actual != standard and actual in df.columns:
            rename_map[actual] = standard

    if rename_map:
        df.rename(columns=rename_map, inplace=True)

    return df


def get_column(df, standard_name: str, mapping: Optional[Dict[str, Optional[str]]] = None, default=None) -> None:
    """
    安全获取列数据（支持别名）

    参数:
        df: DataFrame
        standard_name: 标准列名
        mapping: 可选，预计算的列名映射
        default: 列不存在时的默认值

    返回:
        Series 或 default
    """
    if mapping is None:
        mapping = detect_columns(df.columns)

    actual = mapping.get(standard_name)
    if actual and actual in df.columns:
        return df[actual]

    # 尝试直接访问标准名
    if standard_name in df.columns:
        return df[standard_name]

    return default


def build_column_resolver(df_columns) -> "ColumnResolver":
    """构建列名解析器（缓存映射，避免重复探测）"""
    return ColumnResolver(df_columns)


class ColumnResolver:
    """列名解析器 - 缓存映射关系，支持快速列访问"""

    def __init__(self, df_columns):
        self.mapping = detect_columns(df_columns)
        self._col_set = set(df_columns)

    def get(self, standard_name: str) -> Optional[str]:
        """获取标准列名对应的实际列名"""
        actual = self.mapping.get(standard_name)
        if actual and actual in self._col_set:
            return actual
        if standard_name in self._col_set:
            return standard_name
        return None

    def has(self, standard_name: str) -> bool:
        """检查是否存在某列（支持别名）"""
        return self.get(standard_name) is not None

    def resolve(self, df, standard_name: str, default=None) -> None:
        """从 DataFrame 中解析列值"""
        actual = self.get(standard_name)
        if actual and actual in df.columns:
            return df[actual]
        if standard_name in df.columns:
            return df[standard_name]
        return default

    def __repr__(self):
        found = [k for k, v in self.mapping.items() if v is not None]
        missing = [k for k, v in self.mapping.items() if v is None]
        return f"ColumnResolver(found={len(found)}, missing={missing})"
