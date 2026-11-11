"""
akshare 数据源适配器
职责：将 akshare 集成为 Trading Radar 的第三级数据源

数据源优先级：
1. 东方财富 push2 批量接口 (主源)
2. 腾讯财经单股接口 (备用)
3. akshare 买卖盘接口 (第三备用)

akshare 优势：
- 卖盘深度数据（买一买五、卖一卖五、外盘内盘）
- 龙虎榜、板块行情等特色数据
- 不依赖于特定 HTTP 接口，通过库封装调用

注意：
- 全市场实时行情(stock_zh_a_spot_em) 在某些网络环境下较慢，本适配器主要使用单股接口
- 特色数据采集通过 tools/akshare_daily.py 定时执行
"""

import logging
import time
from typing import Dict, Optional

# 统一数据 Schema


class AKShareDataFetcher:
    """
    akshare 数据获取器
    作为 MarketDataFetcher 的备选数据源，提供卖盘级别的实时数据
    """

    def __init__(self, logger: logging.Logger, timeout: float = 10.0):
        self.logger = logger
        self.timeout = timeout
        self._ak = None
        self._available = False
        self._init_akshare()

    def _init_akshare(self) -> None:
        """尝试导入 akshare"""
        try:
            import akshare as ak
            self._ak = ak
            self._available = True
            self.logger.info("akshare 数据源已加载 (version: %s)", ak.__version__)
        except ImportError:
            self.logger.warning("akshare 未安装，跳过此数据源")
        except Exception as e:
            self.logger.warning("akshare 加载失败: %s", e)

    @property
    def is_available(self) -> bool:
        return self._available

    def get_stock_data(self, code: str, name: str = None) -> Optional[Dict]:
        """
        通过 akshare 获取单股实时数据
        使用 stock_bid_ask_em 接口，返回包含卖盘的完整数据

        :param code: 股票代码（6位数字）
        :param name: 股票名称
        :return: 统一格式的股票数据字典
        """
        if not self._available:
            return None

        try:
            start = time.time()
            df = self._ak.stock_bid_ask_em(symbol=code)
            elapsed = time.time() - start

            if df is None or df.empty:
                return None

            # 解析买卖盘数据
            data_map = dict(zip(df['item'].values, df['value'].values))

            # 提取关键字段（字段名在 akshare 中是中文）
            price = self._safe_float(data_map.get('最新'))
            if price == 0:
                self.logger.warning(f"akshare 获取 {code} 价格为0，可能是非交易时间")
                return None

            result = {
                'code': code,
                'name': name or code,
                'price': price,
                'close': price,
                'open': self._safe_float(data_map.get('今开')),
                'high': self._safe_float(data_map.get('最高')),
                'low': self._safe_float(data_map.get('最低')),
                'prev_close': self._safe_float(data_map.get('昨收')),
                'volume': int(self._safe_float(data_map.get('总手', 0))),
                'change_pct': self._safe_float(data_map.get('涨幅')),
                'change': self._safe_float(data_map.get('涨跌')),
                'turnover': self._safe_float(data_map.get('换手', 0)),
                'amount': self._safe_float(data_map.get('金额', 0)),
                # 卖盘深度数据（akshare 特有）
                'bid_ask': {
                    'sell_5': self._safe_float(data_map.get('sell_5')),
                    'sell_5_vol': int(self._safe_float(data_map.get('sell_5_vol', 0))),
                    'sell_4': self._safe_float(data_map.get('sell_4')),
                    'sell_4_vol': int(self._safe_float(data_map.get('sell_4_vol', 0))),
                    'sell_3': self._safe_float(data_map.get('sell_3')),
                    'sell_3_vol': int(self._safe_float(data_map.get('sell_3_vol', 0))),
                    'sell_2': self._safe_float(data_map.get('sell_2')),
                    'sell_2_vol': int(self._safe_float(data_map.get('sell_2_vol', 0))),
                    'sell_1': self._safe_float(data_map.get('sell_1')),
                    'sell_1_vol': int(self._safe_float(data_map.get('sell_1_vol', 0))),
                    'buy_1': self._safe_float(data_map.get('buy_1')),
                    'buy_1_vol': int(self._safe_float(data_map.get('buy_1_vol', 0))),
                    'buy_2': self._safe_float(data_map.get('buy_2')),
                    'buy_2_vol': int(self._safe_float(data_map.get('buy_2_vol', 0))),
                    'buy_3': self._safe_float(data_map.get('buy_3')),
                    'buy_3_vol': int(self._safe_float(data_map.get('buy_3_vol', 0))),
                    'buy_4': self._safe_float(data_map.get('buy_4')),
                    'buy_4_vol': int(self._safe_float(data_map.get('buy_4_vol', 0))),
                    'buy_5': self._safe_float(data_map.get('buy_5')),
                    'buy_5_vol': int(self._safe_float(data_map.get('buy_5_vol', 0))),
                },
                'inner_vol': int(self._safe_float(data_map.get('内盘', 0))),
                'outer_vol': int(self._safe_float(data_map.get('外盘', 0))),
                'source': 'akshare',
            }

            self.logger.debug(f"akshare 获取 {code} 成功，耗时 {elapsed:.2f}s，价格 {price}")
            return result

        except Exception as e:
            self.logger.warning(f"akshare 获取 {code} 失败: {e}")
            return None

    @staticmethod
    def _safe_float(value, default=0.0) -> None:
        """安全转换为 float"""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # =========================================================================
    # 以下为特色数据采集接口（主要用于定时报告，而非实时监控）
    # =========================================================================

    def get_lhb_data(self, date_str: str) -> Optional[Dict]:
        """
        获取龙虎榜数据
        :param date_str: 日期格式 YYYYMMDD
        :return: {
            'date': date_str,
            'count': 上榜股票数量,
            'stocks': [
                {
                    'code': '代码',
                    'name': '名称',
                    'close': 收盘价,
                    'change_pct': 涨跌幅,
                    'net_buy': 龙虎榜净买额,
                    'buy_amount': 买入额,
                    'sell_amount': 卖出额,
                    'reason': '上榜原因',
                },
                ...
            ]
        }
        """
        if not self._available:
            return None

        try:
            start = time.time()
            df = self._ak.stock_lhb_detail_em(
                start_date=date_str,
                end_date=date_str
            )
            elapsed = time.time() - start

            if df is None or df.empty:
                return None

            stocks = []
            for _, row in df.iterrows():
                stocks.append({
                    'code': str(row.get('代码', '')).zfill(6),
                    'name': row.get('名称', ''),
                    'close': self._safe_float(row.get('收盘价')),
                    'change_pct': self._safe_float(row.get('涨跌幅')),
                    'net_buy': self._safe_float(row.get('龙虎榜净买额')),
                    'buy_amount': self._safe_float(row.get('龙虎榜买入额')),
                    'sell_amount': self._safe_float(row.get('龙虎榜卖出额')),
                    'reason': row.get('上榜原因', ''),
                    'turnover': self._safe_float(row.get('换手率', 0)),
                })

            self.logger.info(
                f"akshare 龙虎榜 {date_str} 获取成功，"
                f"共 {len(stocks)} 只，耗时 {elapsed:.2f}s"
            )

            return {
                'date': date_str,
                'count': len(stocks),
                'stocks': stocks,
            }

        except Exception as e:
            self.logger.warning(f"akshare 龙虎榜 {date_str} 获取失败: {e}")
            return None

    def get_sector_board_data(self) -> Optional[Dict]:
        """
        获取行业板块实时行情

        ⚠️ 东方财富板块接口 (stock_board_industry_name_em) 已被服务器限制，
        频繁返回 RemoteDisconnected/ConnectionReset。自 2026-05-06 起永久降级到同花顺接口。

        :return: {
            'count': 板块数量,
            'sectors': [
                {
                    'rank': 排名,
                    'name': '板块名称',
                    'code': '板块代码',
                    'price': 最新价,
                    'change_pct': 涨跌幅,
                    'turnover': 换手率,
                    'up_count': 上涨家数,
                    'down_count': 下跌家数,
                    'lead_stock': '领涨股票',
                    'lead_change_pct': 领涨股票涨跌幅,
                },
                ...
            ]
        }
        """
        if not self._available:
            return None

        # 东方财富接口已被服务器限制，直接使用同花顺
        return self._get_sector_from_ths()

    def _get_sector_from_em(self) -> Optional[Dict]:
        """
        [已废弃] 东方财富板块接口
        服务器频繁返回 RemoteDisconnected，不再使用。
        保留代码仅供参考，实际调用已被移除。
        """
        self.logger.debug("东方财富板块接口已废弃，跳过")
        return None

    def _get_sector_from_ths(self) -> Optional[Dict]:
        """
        通过同花顺行业资金流向接口获取板块行情

        替代已失效的接口：
        - stock_board_industry_name_em (东方财富) — 服务器限制，RemoteDisconnected
        - stock_board_industry_summary_ths (同花顺) — 网页结构变更，No tables found

        当前使用: stock_fund_flow_industry — 稳定可用，字段完整
        """
        try:
            start = time.time()
            df = self._ak.stock_fund_flow_industry()
            elapsed = time.time() - start

            if df is None or df.empty:
                return None

            sectors = []
            for _, row in df.iterrows():
                sectors.append({
                    'rank': int(self._safe_float(row.get('序号', 0))),
                    'name': row.get('行业', ''),
                    'code': '',  # 此接口无板块代码
                    'price': self._safe_float(row.get('行业指数')),
                    'change_pct': self._safe_float(row.get('行业-涨跌幅')),
                    'change': 0.0,  # 此接口无涨跌额
                    'turnover': 0.0,  # 此接口无换手率
                    'up_count': 0,  # 此接口无涨跌家数
                    'down_count': 0,
                    'lead_stock': row.get('领涨股', ''),
                    'lead_change_pct': self._safe_float(row.get('领涨股-涨跌幅')),
                })

            sectors.sort(key=lambda x: x['change_pct'], reverse=True)

            self.logger.info(
                f"akshare 行业板块(同花顺资金流向) 获取成功，"
                f"共 {len(sectors)} 个，耗时 {elapsed:.2f}s"
            )

            return {
                'count': len(sectors),
                'sectors': sectors,
                'source': 'ths_fund_flow',
            }

        except Exception as e:
            self.logger.warning(f"akshare 板块行情(同花顺资金流向) 获取失败: {e}")
            return None

    def get_stock_sector_belong(self, code: str) -> Optional[list]:
        """
        查询个股所属行业板块
        :param code: 股票代码
        :return: [板块名1, 板块名2, ...]
        """
        if not self._available:
            return None

        try:
            df = self._ak.stock_board_industry_cons_em(symbol=code)
            if df is None or df.empty:
                return []
            # 返回该股票所属的板块列表
            sectors = df['板块名称'].unique().tolist()
            return sectors
        except Exception as e:
            self.logger.debug(f"akshare 查询 {code} 板块 失败: {e}")
            return []
