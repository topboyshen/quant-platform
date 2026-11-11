"""
Radar Pro — 市场数据获取模块。

从 radar_pro.py 拆分出来的数据获取层，封装东方财富 API 调用，
支持 A 股批量行情、单股实时、北交所、历史 K 线。
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Dict, Optional

import pandas as pd
import requests


# 东方财富妙想 API 配置（从环境变量读取，不再硬编码）
MKT_API_KEY = os.getenv("MKT_API_KEY", "")


class MarketDataFetcher:
    """市场数据获取器（东方财富 API）"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._cache_data: Optional[pd.DataFrame] = None
        self._cache_time = 0.0
        self._cache_ttl = 5

    # ------------------------------------------------------------------
    # 静态安全转换
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_float(val, default=0.0):
        if val is None or val == "" or val == "-":
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_int(val, default=0):
        if val is None or val == "" or val == "-":
            return default
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return default

    # ------------------------------------------------------------------
    # 批量 A 股行情
    # ------------------------------------------------------------------
    def fetch_all(self) -> Optional[pd.DataFrame]:
        """获取全部 A 股实时行情。"""
        now = time.time()
        if self._cache_data is not None and (now - self._cache_time) < self._cache_ttl:
            return self._cache_data

        try:
            url = "http://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": "1",
                "pz": "5000",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": "m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f14,f2,f3,f4,f5,f6,f17,f18,f15,f16,f8",
                "_": str(int(time.time() * 1000)),
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "http://quote.eastmoney.com/",
                "Authorization": f"Bearer {MKT_API_KEY}",
            }

            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("data") and data["data"].get("diff"):
                stocks = data["data"]["diff"]
                rows = [
                    {
                        "代码": stock.get("f12", ""),
                        "名称": stock.get("f14", ""),
                        "最新价": self._safe_float(stock.get("f2")),
                        "涨跌幅": self._safe_float(stock.get("f3")),
                        "涨跌额": self._safe_float(stock.get("f4")),
                        "成交量": self._safe_int(stock.get("f5")),
                        "成交额": self._safe_float(stock.get("f6")),
                        "今开": self._safe_float(stock.get("f17")),
                        "昨收": self._safe_float(stock.get("f18")),
                        "最高": self._safe_float(stock.get("f15")),
                        "最低": self._safe_float(stock.get("f16")),
                        "换手率": self._safe_float(stock.get("f8")),
                    }
                    for stock in stocks
                ]
                df = pd.DataFrame(rows)
                self._cache_data = df
                self._cache_time = now
                return df
            else:
                self.logger.error(f"东方财富API返回异常数据: {data}")
                return None

        except Exception as e:
            self.logger.error(f"获取行情失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 单股实时数据
    # ------------------------------------------------------------------
    def get_stock_data(self, code: str, name: str = None) -> Optional[Dict]:
        """获取单只股票完整实时数据。"""
        df = self.fetch_all()
        if df is not None:
            matches = df[df["代码"].str.contains(code, na=False)]
            if not matches.empty:
                row = matches.iloc[0]
                return {
                    "code": code,
                    "name": row["名称"],
                    "price": float(row["最新价"]),
                    "volume": int(row.get("成交量", 0)),
                    "open": float(row.get("今开", 0)),
                    "high": float(row.get("最高", 0)),
                    "low": float(row.get("最低", 0)),
                    "prev_close": float(row.get("昨收", 0)),
                }

        # 北交所
        if code.zfill(6).startswith("920"):
            return self._get_bse_realtime(code.zfill(6), name)

        # 其他（新三板等）
        return self._get_single_stock_realtime(code.zfill(6), name)

    def _get_bse_realtime(self, code: str, name: str) -> Optional[Dict]:
        """北交所实时数据。"""
        try:
            url = "http://push2.eastmoney.com/api/qt/stock/get"
            params = {
                "secid": f"0.{code}",
                "fields": "f43,f44,f45,f46,f47,f48,f60,f14",
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
                    "name": d.get("f14", name),
                    "price": d.get("f43", 0) / 100,
                    "open": d.get("f44", 0) / 100,
                    "high": d.get("f45", 0) / 100,
                    "low": d.get("f46", 0) / 100,
                    "volume": d.get("f47", 0),
                    "prev_close": d.get("f60", 0) / 100,
                }
            return None
        except Exception as e:
            self.logger.warning(f"东方财富获取北交所股票 {code} 失败: {e}")
            return None

    def _get_single_stock_realtime(self, code: str, name: str) -> Optional[Dict]:
        """通用单股实时数据。"""
        try:
            if code.startswith("6"):
                secid = f"1.{code}"
            elif code.startswith("0") or code.startswith("3"):
                secid = f"0.{code}"
            else:
                secid = f"0.{code}"

            url = "http://push2.eastmoney.com/api/qt/stock/get"
            params = {
                "secid": secid,
                "fields": "f43,f44,f45,f46,f47,f48,f60,f14",
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
                    "name": d.get("f14", name),
                    "price": d.get("f43", 0) / 100,
                    "open": d.get("f44", 0) / 100,
                    "high": d.get("f45", 0) / 100,
                    "low": d.get("f46", 0) / 100,
                    "volume": d.get("f47", 0),
                    "prev_close": d.get("f60", 0) / 100,
                }
            return None
        except Exception as e:
            self.logger.warning(f"东方财富获取股票 {code} 失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 历史 K 线
    # ------------------------------------------------------------------
    def get_historical_data(self, code: str, days: int = 60) -> pd.DataFrame:
        """获取历史 K 线数据用于回测。"""
        try:
            full_code = code.zfill(6)
            if full_code.startswith("6"):
                secid = f"1.{full_code}"
            else:
                secid = f"0.{full_code}"

            url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "1",
                "end": datetime.datetime.now().strftime("%Y%m%d"),
                "lmt": str(days + 30),
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "http://quote.eastmoney.com/",
                "Authorization": f"Bearer {MKT_API_KEY}",
            }

            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("data") and data["data"].get("klines"):
                klines = data["data"]["klines"]
                rows = [
                    {
                        "date": parts[0],
                        "open": float(parts[1]),
                        "close": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                        "volume": int(float(parts[5])),
                    }
                    for line in klines
                    if (parts := line.split(","))
                ]
                df = pd.DataFrame(rows)
                if len(df) >= 30:
                    df["price"] = df["close"].astype(float)
                    df["volume"] = df["volume"].astype(int)
                    df["date"] = pd.to_datetime(df["date"])
                    return df

        except Exception as e:
            self.logger.error(f"获取历史数据失败 {code}: {e}")
        return pd.DataFrame()
