"""
MXDataClient — 妙想金融数据查询客户端主类。

从 mx_client.py 拆分出来的核心类，保留网络请求与高层封装。
解析逻辑已迁移到 ``mx_client.parsers``。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from mx_quota import acquire_mx_call
from radar.data.mx_feature_cache import mx_cached

from .parsers import (
    ensure_ohlcv_columns,
    normalize_column_name,
    parse_history_data,
    parse_numeric_value,
    parse_realtime_data,
)


class MXDataClient:
    """妙想金融数据查询客户端"""

    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("MX_APIKEY")
        if not self.api_key:
            raise ValueError(
                "MX_APIKEY 环境变量未设置，请先设置环境变量：\n"
                "export MX_APIKEY=***\n"
                "或者在初始化时传入 api_key 参数"
            )

    def query(self, tool_query: str) -> Dict[str, Any]:
        """查询金融数据"""
        acquire_mx_call()
        headers = {"Content-Type": "application/json", "apikey": self.api_key}
        data = {"toolQuery": tool_query}
        response = requests.post(self.BASE_URL, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_stock_history(self, code: str, name: str = None, days: int = 150) -> Optional[pd.DataFrame]:
        """获取股票历史数据"""
        try:
            full_code = code.zfill(6)
            stock_name = name or self._get_stock_name(full_code)
            query = f"{stock_name}{full_code}日K数据最近{days}天"
            result = self.query(query)
            df = parse_history_data(result)
            if df is not None and len(df) > 0:
                df["price"] = df["close"].astype(float)
                df["volume"] = df["volume"].astype(int)
                return df
            return None
        except Exception as e:
            print(f"妙想API获取历史数据失败 {code}: {e}")
            return None

    def get_stock_realtime(self, code: str, name: str = None) -> Optional[Dict[str, Any]]:
        """获取股票实时行情"""
        try:
            full_code = code.zfill(6)
            stock_name = name or self._get_stock_name(full_code)
            if full_code.startswith("920"):
                return self._get_bse_realtime(full_code, stock_name)
            query = f"{stock_name}{full_code}最新价 涨跌幅 成交量 开盘价 最高价 最低价 昨收"
            result = self.query(query)
            return parse_realtime_data(result, full_code, stock_name)
        except Exception as e:
            print(f"妙想API获取实时数据失败 {code}: {e}")
            return None

    def _get_bse_realtime(self, code: str, name: str) -> Optional[Dict[str, Any]]:
        """使用东方财富API获取北交所股票实时数据"""
        try:
            import time

            url = "http://push2.eastmoney.com/api/qt/stock/get"
            params = {
                "secid": f"0.{code}",
                "fields": "f43,f44,f45,f46,f47,f48,f60",
                "_": str(int(time.time() * 1000)),
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "http://quote.eastmoney.com/",
            }
            response = requests.get(url, params=params, headers=headers, timeout=10)
            data = response.json()
            if data.get("data"):
                d = data["data"]
                return {
                    "code": code,
                    "name": name,
                    "price": d.get("f43", 0) / 100,
                    "open": d.get("f44", 0) / 100,
                    "high": d.get("f45", 0) / 100,
                    "low": d.get("f46", 0) / 100,
                    "volume": d.get("f47", 0),
                    "prev_close": d.get("f60", 0) / 100,
                }
            return None
        except Exception as e:
            print(f"获取北交所股票 {code} 实时数据失败: {e}")
            return None

    def get_all_stocks_realtime(self) -> Optional[pd.DataFrame]:
        """获取全部 A 股实时行情（妙想API不支持批量获取）"""
        return None

    def _get_stock_name(self, code: str) -> str:
        """根据代码获取股票名称（简单映射）"""
        return ""
