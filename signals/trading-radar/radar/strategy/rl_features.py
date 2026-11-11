#!/usr/bin/env python3
"""
RL 特征工程 - 独立于 StockTradingEnv 的 Observation 构建器
用于 inference（rl_strategy.py / radar_pro.py）时生成与训练一致的 state，
但使用真实账户状态而非 Env 内部状态。

支持磁盘缓存（parquet），避免训练/推理时重复计算技术指标。
"""

from typing import Dict, Optional

import numpy as np
import pandas as pd

from radar.rl.external_feature_provider import ExternalFeatureProvider
from radar.rl.feature_cache import FeatureCache
from radar.strategy.rl_schema import RL_OBS_DIM, build_live_observation


class RLObservationBuilder:
    """
    RL Observation 构建器

    一次性预处理所有技术指标，step 时 O(1) 索引查找，
    并允许传入真实持仓状态拼接动态特征。
    """

    def __init__(self, df: pd.DataFrame, trigger_price: float, cache_dir: str = None):
        self.df = df.reset_index(drop=True)
        self.trigger_price = trigger_price

        if cache_dir:
            cache = FeatureCache(cache_dir)
            cached = cache.load(self.df, trigger_price)
            if cached is not None:
                self.features = cached
            else:
                self.features = self._precompute_features()
                cache.save(self.df, trigger_price, self.features)
        else:
            self.features = self._precompute_features()

        self._compute_norm_stats()

    def _precompute_features(self) -> pd.DataFrame:
        """预计算所有技术指标"""
        df = self.df.copy()

        # 基础价格特征
        df['price'] = df['close']
        df['price_pct'] = df['close'].pct_change()
        df['price_ma5'] = df['close'].rolling(5).mean()
        df['price_ma20'] = df['close'].rolling(20).mean()

        # 波动率
        df['volatility'] = df['close'].rolling(20).std()

        # 成交量特征
        df['volume_ma5'] = df['volume'].rolling(5).mean()
        df['volume_ratio'] = df['volume'] / df['volume_ma5']

        # 价格位置
        df['price_position'] = (df['close'] - df['low'].rolling(20).min()) / \
                               (df['high'].rolling(20).max() - df['low'].rolling(20).min() + 1e-8)

        # 触发价距离
        df['trigger_distance'] = (df['close'] - self.trigger_price) / self.trigger_price

        # RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df['rsi'] = 100 - (100 / (1 + gain / (loss + 1e-8)))

        # MACD
        ema12 = df['close'].ewm(span=12).mean()
        ema26 = df['close'].ewm(span=26).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9).mean()

        # 填充 NaN
        df = df.fillna(0)

        return df

    def _compute_norm_stats(self) -> None:
        """计算归一化统计量"""
        self.price_mean = self.df['close'].mean()
        self.price_std = self.df['close'].std() + 1e-8
        self.volume_mean = self.df['volume'].mean()
        self.volume_std = self.df['volume'].std() + 1e-8

    def get_observation(self, index: int, position_shares: int, position_value: float) -> np.ndarray:
        """
        构建观察向量

        Args:
            index: 当前时间步
            position_shares: 持仓股数
            position_value: 持仓成本
        """
        row = self.features.iloc[index]
        prices = self.features['close'].iloc[:index+1].tolist()
        volumes = self.features['volume'].iloc[:index+1].tolist() if 'volume' in self.features.columns else []
        position_cost = position_value / position_shares if position_shares > 0 else 0.0

        obs = build_live_observation(
            prices=prices,
            volumes=volumes,
            trigger_price=self.trigger_price,
            position_shares=position_shares,
            position_cost=position_cost,
        )
        # 保留 row 读取以确保 index 越界时沿用 pandas 的明确异常语义。
        _ = row
        return obs

    @property
    def obs_dim(self) -> int:
        """观察维度"""
        return RL_OBS_DIM

    def slice(self, start: int, end: int) -> "RLObservationBuilder":
        """切片出一个子区间的新 builder"""
        new = object.__new__(RLObservationBuilder)
        new.df = self.df.iloc[start:end].reset_index(drop=True)
        new.trigger_price = self.trigger_price
        new.features = self.features.iloc[start:end].reset_index(drop=True)
        new._compute_norm_stats()
        return new


def _merge_feature_by_date(df_dates, external_df, src_df, src_col, dst_col, norm_factor=1.0):
    """按日期合并并归一化特征"""
    if src_df is None or src_df.empty or src_col not in src_df.columns:
        return
    src = src_df[['date', src_col]].copy()
    src['date'] = pd.to_datetime(src['date']).dt.strftime('%Y-%m-%d')
    merged = df_dates.merge(src, on='date', how='left')
    if src_col in merged.columns:
        external_df[dst_col] = (merged[src_col].fillna(0.0) / norm_factor).values


def prepare_external_features(df: pd.DataFrame, code: str = None, name: str = None) -> Optional[pd.DataFrame]:
    """
    为训练准备外部特征 DataFrame

    支持的历史特征：
    - 北向资金净流入（按日期）
    - 上证指数/沪深300 涨跌幅（按日期，3-5天）
    - 两市融资融券余额变化（按日期，3-5天）
    - 个股融资融券余额（按日期，3-5天）

    当日特征（涨跌停、个股资金流、行业涨幅）由于 API 限制只能获取当日数据，
    训练时暂用 0 填充，推理时由 rl_strategy.py 实时注入。

    所有存入 DataFrame 的特征均已按 live_mode 的归一化尺度预处理，
    保证训练与推理分布一致。

    Args:
        df: 股票价格数据，需要包含 'date' 列
        code: 股票代码
        name: 股票名称

    Returns:
        与 df 等长的外部特征 DataFrame，或 None
    """
    if 'date' not in df.columns:
        return None

    provider = ExternalFeatureProvider()

    # 获取各类历史数据
    northbound = provider.get_northbound_history()
    sh_index = provider.get_index_history('000001.SH')
    csi300 = provider.get_index_history('000300.SH')
    margin_total = provider.get_margin_total_history()
    margin_stock = provider.get_margin_stock_history(code, name) if code else None

    # 统一日期格式
    df_dates = df[['date']].copy()
    df_dates['date'] = pd.to_datetime(df_dates['date']).dt.strftime('%Y-%m-%d')

    # 构建外部特征 DataFrame（按与 live_mode 一致的归一化尺度存储）
    external_df = pd.DataFrame(index=df.index)
    external_df['northbound_net'] = 0.0
    external_df['limit_up_count'] = 0.0
    external_df['limit_down_count'] = 0.0
    external_df['main_net'] = 0.0
    external_df['large_net'] = 0.0
    external_df['sh_index_change'] = 0.0
    external_df['csi300_change'] = 0.0
    external_df['sector_change'] = 0.0
    external_df['margin_total_change'] = 0.0
    external_df['margin_stock'] = 0.0

    _merge_feature_by_date(df_dates, external_df, northbound, 'northbound_net', 'northbound_net', 100.0)
    _merge_feature_by_date(df_dates, external_df, sh_index, 'change_pct', 'sh_index_change', 10.0)
    _merge_feature_by_date(df_dates, external_df, csi300, 'change_pct', 'csi300_change', 10.0)
    _merge_feature_by_date(df_dates, external_df, margin_total, 'margin_change_pct', 'margin_total_change', 1.0)
    _merge_feature_by_date(df_dates, external_df, margin_stock, 'margin_stock', 'margin_stock', 1000.0)

    return external_df


class RLObservationBuilderWithExternal(RLObservationBuilder):
    """
    带外部特征的 Observation Builder

    在基础 15 维特征上，拼接外部特征（北向资金、市场情绪、资金流、
    指数涨跌、行业涨幅、融资融券）
    """

    # 定义固定的外部特征顺序和数量（必须与 prepare_external_features 列顺序一致）
    EXTERNAL_FEATURE_NAMES = [
        'northbound_net',
        'limit_up_count',
        'limit_down_count',
        'main_net',
        'large_net',
        'sh_index_change',
        'csi300_change',
        'sector_change',
        'margin_total_change',
        'margin_stock',
    ]

    def __init__(self, df: pd.DataFrame, trigger_price: float, cache_dir: str = None,
                 external_features: pd.DataFrame = None, live_mode: bool = False):
        """
        Args:
            df: 价格数据
            trigger_price: 触发价
            cache_dir: 特征缓存目录
            external_features: 外部特征 DataFrame，需与 df 长度相同
                列名必须与 EXTERNAL_FEATURE_NAMES 一致，且值为已归一化
            live_mode: 是否为实时模式（实时模式下每步获取最新外部特征）
        """
        super().__init__(df, trigger_price, cache_dir)
        self.external_features = external_features
        self.live_mode = live_mode
        self.external_dim = len(self.EXTERNAL_FEATURE_NAMES) if (external_features is not None or live_mode) else 0

    def slice(self, start: int, end: int) -> "RLObservationBuilderWithExternal":
        """切片出一个子区间的新 builder，保留外部特征"""
        new = object.__new__(RLObservationBuilderWithExternal)
        new.df = self.df.iloc[start:end].reset_index(drop=True)
        new.trigger_price = self.trigger_price
        new.features = self.features.iloc[start:end].reset_index(drop=True)
        new._compute_norm_stats()
        new.live_mode = self.live_mode
        new.external_dim = self.external_dim
        if self.external_features is not None:
            new.external_features = self.external_features.iloc[start:end].reset_index(drop=True)
        else:
            new.external_features = None
        return new

    @property
    def obs_dim(self) -> int:
        """总观察维度 = 基础维度 + 外部特征维度"""
        return 15 + self.external_dim

    def get_observation(self, index: int, position_shares: int, position_value: float,
                        external_feats: Dict = None) -> np.ndarray:
        """
        构建观察向量，包含外部特征

        Args:
            index: 当前时间步
            position_shares: 持仓股数
            position_value: 持仓成本
            external_feats: 实时外部特征（live_mode 时使用），
                要求传入的值已经是归一化后的（与 training DataFrame 中存储的一致）
        """
        # 获取基础特征（15维）
        base_obs = super().get_observation(index, position_shares, position_value)

        if self.external_dim == 0:
            return base_obs

        # 获取外部特征
        if self.live_mode and external_feats:
            # 实时模式：external_feats 应该已经是归一化后的值
            # 但为了兼容旧调用方式，对前5个老特征做兜底处理
            ext_values = []
            for i, name in enumerate(self.EXTERNAL_FEATURE_NAMES):
                val = external_feats.get(name, 0.0)
                # 兼容旧调用：如果 northbound_net 还是原始值（>1 或 <-1），做兜底归一化
                if i == 0 and abs(val) > 10:  # northbound_net 原始值通常 >10 亿元
                    val = val / 100.0
                elif i in (1, 2) and abs(val) > 5:  # limit_up/down 原始值通常 >10
                    val = val / 100.0
                elif i in (3, 4) and abs(val) > 50:  # main/large_net *10 后可能过大
                    val = val / 10.0
                ext_values.append(val)
            ext_obs = np.array(ext_values, dtype=np.float32)
        elif self.external_features is not None and index < len(self.external_features):
            # 历史模式：从预计算的 DataFrame 获取（已归一化）
            ext_obs = self.external_features.iloc[index].values.astype(np.float32)
        else:
            # 无外部特征时补零
            ext_obs = np.zeros(self.external_dim, dtype=np.float32)

        # 拼接
        obs = np.concatenate([base_obs, ext_obs])
        obs = np.clip(obs, -10, 10)
        return obs
