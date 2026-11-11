#!/usr/bin/env python3
"""
恢复训练 20-dim 外部特征模型（原始 5 维外部特征）
临时覆盖 10-dim 配置，训练完成后自行恢复即可
"""

import logging
import shutil
import pandas as pd
from radar.data.market_data import MarketDataFetcher
from rl_agent import RLAgent
from rl_features import RLObservationBuilderWithExternal, ExternalFeatureProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# 临时 monkey-patch：仅使用原始 5 维外部特征
ORIGINAL_EXTERNAL_NAMES = [
    'northbound_net',
    'limit_up_count',
    'limit_down_count',
    'main_net',
    'large_net',
]
RLObservationBuilderWithExternal.EXTERNAL_FEATURE_NAMES = ORIGINAL_EXTERNAL_NAMES

TARGETS = [
    {'code': '300059', 'name': '东方财富', 'direction': 'below', 'trigger_price': 23.7},

    {'code': '000001', 'name': '平安银行', 'direction': 'above', 'trigger_price': 11.5},
    {'code': '300033', 'name': '同花顺', 'direction': 'below', 'trigger_price': 244.7},
    {'code': '300058', 'name': '蓝色光标', 'direction': 'below', 'trigger_price': 13.0},
]


def prepare_external_features_20dim(df: pd.DataFrame, code: str = None, name: str = None):
    """仅准备原始 5 维外部特征（与旧版一致）"""
    if 'date' not in df.columns:
        return None
    
    provider = ExternalFeatureProvider()
    northbound = provider.get_northbound_history()
    
    external_df = pd.DataFrame(index=df.index)
    external_df['northbound_net'] = 0.0
    external_df['limit_up_count'] = 0.0
    external_df['limit_down_count'] = 0.0
    external_df['main_net'] = 0.0
    external_df['large_net'] = 0.0
    
    if northbound is not None and not northbound.empty:
        df_dates = df[['date']].copy()
        df_dates['date'] = pd.to_datetime(df_dates['date']).dt.strftime('%Y-%m-%d')
        northbound['date'] = pd.to_datetime(northbound['date']).dt.strftime('%Y-%m-%d')
        merged = df_dates.merge(northbound, on='date', how='left')
        if 'northbound_net' in merged.columns:
            external_df['northbound_net'] = merged['northbound_net'].fillna(0.0).values / 100.0
    
    return external_df


def train() -> None:
    fetcher = MarketDataFetcher(logger)
    agent = RLAgent(model_dir="rl_models")

    for target in TARGETS:
        code = target['code']
        name = target['name']
        direction = target['direction']
        trigger_price = target['trigger_price']

        logger.info(f"🚀 开始恢复训练 {name} ({code}) 20-dim 外部特征模型...")

        try:
            df = fetcher.get_historical_data(code, days=120)
            if len(df) < 60:
                logger.warning(f"{name} 历史数据不足 ({len(df)} < 60)，跳过")
                continue

            external_df = prepare_external_features_20dim(df, code, name)
            if external_df is not None:
                filled = (external_df['northbound_net'] != 0).sum()
                logger.info(f"   外部特征: northbound filled {filled}/{len(external_df)} days")

            result = agent.train(
                code=code,
                direction=direction,
                df=df,
                trigger_price=trigger_price,
                total_timesteps=15000,
                eval_freq=5000,
                patience=2,
                external_features=external_df,
                live_mode=False,
                trading_penalty_weight=0.05  # 加大交易惩罚，抑制过度交易
            )

            # 重命名为 _ext 版本
            original_path = result['model_path']
            ext_path = original_path.replace('.zip', '_ext.zip')
            shutil.copy(original_path, ext_path)
            logger.info(f"✅ {name} 20-dim 模型保存: {ext_path}")
            logger.info(f"   最终收益: {result['avg_return']:.2%}, Val Sharpe: {result['best_val_sharpe']:.2f}")

        except Exception as e:
            logger.error(f"❌ {name} 训练失败: {e}", exc_info=True)


if __name__ == "__main__":
    train()
