#!/usr/bin/env python3
"""
训练带外部特征的 RL 模型
"""

import logging
from radar.data.market_data import MarketDataFetcher
from rl_agent import RLAgent
from rl_features import prepare_external_features

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


TARGETS = [
    {'code': '300059', 'name': '东方财富', 'direction': 'below', 'trigger_price': 23.7},

    {'code': '000001', 'name': '平安银行', 'direction': 'above', 'trigger_price': 11.5},
    {'code': '300033', 'name': '同花顺', 'direction': 'below', 'trigger_price': 244.7},
    {'code': '300058', 'name': '蓝色光标', 'direction': 'below', 'trigger_price': 13.0},
]


def train() -> None:
    fetcher = MarketDataFetcher(logger)
    agent = RLAgent(model_dir="rl_models")

    for target in TARGETS:
        code = target['code']
        name = target['name']
        direction = target['direction']
        trigger_price = target['trigger_price']

        logger.info(f"🚀 开始训练 {name} ({code}) 外部特征模型...")

        try:
            df = fetcher.get_historical_data(code, days=120)
            if len(df) < 60:
                logger.warning(f"{name} 历史数据不足 ({len(df)} < 60)，跳过")
                continue

            external_df = prepare_external_features(df, code, name)
            if external_df is not None:
                filled_n = (external_df['northbound_net'] != 0).sum()
                filled_sh = (external_df['sh_index_change'] != 0).sum()
                filled_csi = (external_df['csi300_change'] != 0).sum()
                filled_mt = (external_df['margin_total_change'] != 0).sum()
                filled_ms = (external_df['margin_stock'] != 0).sum()
                logger.info(
                    f"   外部特征: northbound={filled_n}, sh_index={filled_sh}, "
                    f"csi300={filled_csi}, margin_total={filled_mt}, margin_stock={filled_ms} / {len(external_df)}"
                )

            result = agent.train(
                code=code,
                direction=direction,
                df=df,
                trigger_price=trigger_price,
                total_timesteps=15000,
                eval_freq=5000,
                patience=2,
                external_features=external_df,
                live_mode=False
            )

            # 重命名为 _ext 版本
            import shutil
            original_path = result['model_path']
            ext_path = original_path.replace('.zip', '_ext.zip')
            shutil.copy(original_path, ext_path)
            logger.info(f"✅ {name} 模型保存: {ext_path}")
            logger.info(f"   最终收益: {result['avg_return']:.2%}, Val Sharpe: {result['best_val_sharpe']:.2f}")

        except Exception as e:
            logger.error(f"❌ {name} 训练失败: {e}", exc_info=True)


if __name__ == "__main__":
    train()
