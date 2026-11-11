#!/usr/bin/env python3
"""
RL 外部特征提供者
从 mx_client 获取市场情绪和资金流数据

同类请求在 MX_FEATURE_CACHE_TTL_SEC 内走进程内缓存，减少妙想 query 次数。
"""

import logging
from typing import Dict, Optional

import pandas as pd

from radar.data.mx_feature_cache import mx_cached

logger = logging.getLogger(__name__)


class ExternalFeatureProvider:
    """
    外部特征提供者 - 从 mx_client 获取市场情绪和资金流数据

    同类请求在 MX_FEATURE_CACHE_TTL_SEC 内走进程内缓存，减少妙想 query 次数（配合 mx_quota）。

    支持：
    - 北向资金历史数据（按日期）
    - 指数历史涨跌幅（按日期）
    - 融资融券余额历史（按日期）
    - 当日市场情绪（涨停/跌停家数）
    - 当日个股资金流/行业涨幅
    """

    @staticmethod
    def get_northbound_history() -> Optional[pd.DataFrame]:
        """
        获取北向资金历史净流入数据（按日期）

        Returns:
            DataFrame with columns: ['date', 'northbound_net']
            date format: '2026-04-13'
            northbound_net unit: 亿元
        """
        return mx_cached(
            "mx_feat",
            ("northbound",),
            ExternalFeatureProvider._northbound_history_raw,
        )

    @staticmethod
    def _northbound_history_raw() -> Optional[pd.DataFrame]:
        try:
            from mx_client import MXDataClient

            client = MXDataClient()

            result = client.query("北向资金净流入")
            if result.get("status") != 0:
                return None

            data = result.get("data", {}).get("data", {})
            dto_list = data.get("searchDataResultDTO", {}).get("dataTableDTOList", [])
            if not dto_list:
                return None

            dto = dto_list[0]
            table = dto.get("table", {})

            rows = []
            for key, values in table.items():
                if key == "headName" or not values:
                    continue
                if len(values) >= 2:
                    sh_net = float(values[0]) if values[0] else 0
                    sz_net = float(values[1]) if values[1] else 0
                    total_net = (sh_net + sz_net) / 10000
                    rows.append({"date": key, "northbound_net": total_net})

            df = pd.DataFrame(rows)
            name_map = dto.get("nameMap", {})
            if name_map:
                date_map = {k: v for k, v in name_map.items() if k != "headNameSub"}
                df["date"] = df["date"].map(date_map)

            df = df.dropna(subset=["date"])
            return df
        except Exception as e:
            logger.warning("获取北向资金历史数据失败: %s", e)
            return None

    @staticmethod
    def get_index_history(index_code: str = "000001.SH", days: int = 5) -> Optional[pd.DataFrame]:
        """获取指数历史涨跌幅"""
        return mx_cached(
            "mx_feat",
            ("index", index_code, int(days)),
            lambda: ExternalFeatureProvider._index_history_raw(index_code, days),
        )

    @staticmethod
    def _index_history_raw(index_code: str, days: int) -> Optional[pd.DataFrame]:
        try:
            from mx_client import get_index_history_mx

            return get_index_history_mx(index_code, days)
        except Exception as e:
            logger.warning("获取指数历史数据失败 %s: %s", index_code, e)
            return None

    @staticmethod
    def get_margin_total_history(days: int = 5) -> Optional[pd.DataFrame]:
        """获取两市融资融券余额历史"""
        return mx_cached(
            "mx_feat",
            ("margin_total", int(days)),
            lambda: ExternalFeatureProvider._margin_total_history_raw(days),
        )

    @staticmethod
    def _margin_total_history_raw(days: int) -> Optional[pd.DataFrame]:
        try:
            from mx_client import get_margin_total_history_mx

            return get_margin_total_history_mx(days)
        except Exception as e:
            logger.warning("获取两市融资融券余额历史失败: %s", e)
            return None

    @staticmethod
    def get_margin_stock_history(code: str, name: str = None, days: int = 5) -> Optional[pd.DataFrame]:
        """获取个股融资融券余额历史"""
        return mx_cached(
            "mx_feat",
            ("margin_stock", str(code), str(name or ""), int(days)),
            lambda: ExternalFeatureProvider._margin_stock_history_raw(code, name, days),
        )

    @staticmethod
    def _margin_stock_history_raw(code: str, name: str = None, days: int = 5) -> Optional[pd.DataFrame]:
        try:
            from mx_client import get_margin_stock_history_mx

            return get_margin_stock_history_mx(code, name, days)
        except Exception as e:
            logger.warning("获取个股融资融券余额历史失败 %s: %s", code, e)
            return None

    @staticmethod
    def get_market_sentiment() -> Optional[Dict]:
        """
        获取当日市场情绪（涨停/跌停家数、北向资金）

        Returns:
            {
                'limit_up_count': int,
                'limit_down_count': int,
                'northbound_net': float,
            }
        """
        return mx_cached(
            "mx_feat",
            ("sentiment",),
            ExternalFeatureProvider._market_sentiment_raw,
        )

    @staticmethod
    def _market_sentiment_raw() -> Optional[Dict]:
        try:
            from mx_client import get_market_sentiment_mx

            sentiment = get_market_sentiment_mx()
            if sentiment:
                return {
                    "limit_up_count": sentiment.get("limit_up_count", 0),
                    "limit_down_count": sentiment.get("limit_down_count", 0),
                    "northbound_net": sentiment.get("northbound_net", 0.0),
                }
            return None
        except Exception as e:
            logger.warning("获取市场情绪失败: %s", e)
            return None

    @staticmethod
    def get_money_flow(code: str, name: str = None) -> Optional[Dict]:
        """
        获取当日个股资金流向

        Returns:
            {
                'main_net': float,  # 主力净额（亿元）
                'large_net': float,  # 大单净额
                'ddx': float,
            }
        """
        return mx_cached(
            "mx_feat",
            ("money_flow", str(code), str(name or "")),
            lambda: ExternalFeatureProvider._money_flow_raw(code, name),
        )

    @staticmethod
    def _money_flow_raw(code: str, name: str = None) -> Optional[Dict]:
        try:
            from mx_client import get_money_flow_mx

            flow = get_money_flow_mx(code, name)
            if flow:
                return {
                    "main_net": flow.get("main_net", 0.0),
                    "large_net": flow.get("large_net", 0.0),
                    "ddx": flow.get("ddx", 0.0),
                }
            return None
        except Exception as e:
            logger.warning("获取个股资金流失败: %s", e)
            return None

    @staticmethod
    def get_sector_change_for_stock(code: str, name: str = None) -> Optional[float]:
        """
        获取个股所属行业当日涨跌幅

        自动尝试多个行业名称，直到获取成功。
        """
        return mx_cached(
            "mx_feat",
            ("sector_change", str(code), str(name or "")),
            lambda: ExternalFeatureProvider._sector_change_raw(code, name),
        )

    @staticmethod
    def _sector_change_raw(code: str, name: str = None) -> Optional[float]:
        try:
            from mx_client import get_sector_change_mx, get_stock_sector_mx

            sectors = get_stock_sector_mx(code, name)
            if not sectors:
                return None

            for sector in sectors:
                change = get_sector_change_mx(sector)
                if change is not None:
                    return change
            return None
        except Exception as e:
            logger.warning("获取个股行业涨幅失败 %s: %s", code, e)
            return None

    @staticmethod
    def get_realtime_external_features(code: str, name: str = None) -> Dict:
        """
        获取全部实时外部特征，返回已归一化的字典

        Returns:
            {
                'northbound_net': float,
                'limit_up_count': float,
                'limit_down_count': float,
                'main_net': float,
                'large_net': float,
                'sh_index_change': float,
                'csi300_change': float,
                'sector_change': float,
                'margin_total_change': float,
                'margin_stock': float,
            }
        """
        sentiment = ExternalFeatureProvider.get_market_sentiment() or {}
        flow = ExternalFeatureProvider.get_money_flow(code, name) or {}
        sector_change = ExternalFeatureProvider.get_sector_change_for_stock(code, name)

        # 获取指数实时涨跌（历史接口最新一天即为当日）
        sh_index = ExternalFeatureProvider.get_index_history('000001.SH', days=1)
        csi300 = ExternalFeatureProvider.get_index_history('000300.SH', days=1)

        # 获取融资融券最新变化
        margin_total = ExternalFeatureProvider.get_margin_total_history(days=2)
        margin_stock = ExternalFeatureProvider.get_margin_stock_history(code, name, days=2)

        sh_change = 0.0
        if sh_index is not None and not sh_index.empty:
            sh_change = float(sh_index['change_pct'].iloc[0])

        csi_change = 0.0
        if csi300 is not None and not csi300.empty:
            csi_change = float(csi300['change_pct'].iloc[0])

        margin_total_change = 0.0
        if margin_total is not None and len(margin_total) >= 2:
            margin_total_change = float(margin_total['margin_change_pct'].iloc[0])

        margin_stock_val = 0.0
        if margin_stock is not None and not margin_stock.empty:
            margin_stock_val = float(margin_stock['margin_stock'].iloc[0])

        return {
            'northbound_net': sentiment.get('northbound_net', 0.0) / 100.0,
            'limit_up_count': sentiment.get('limit_up_count', 0) / 100.0,
            'limit_down_count': sentiment.get('limit_down_count', 0) / 100.0,
            'main_net': flow.get('main_net', 0.0) * 10.0,
            'large_net': flow.get('large_net', 0.0) * 10.0,
            'sh_index_change': sh_change / 10.0,
            'csi300_change': csi_change / 10.0,
            'sector_change': (sector_change or 0.0) / 10.0,
            'margin_total_change': margin_total_change / 1.0,
            'margin_stock': margin_stock_val / 1000.0,
        }
