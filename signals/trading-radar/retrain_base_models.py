#!/usr/bin/env python3
"""
重新训练基础 RL 模型（15维）- 恢复被覆盖的模型
"""

import logging
from radar.data.market_data import MarketDataFetcher
from rl_agent import RLAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


TARGETS = [
    {'code': '300059', 'name': '东方财富', 'direction': 'below', 'trigger_price': 23.7},

    {'code': '000001', 'name': '平安银行', 'direction': 'above', 'trigger_price': 11.5},
]


def train() -> None:
    fetcher = MarketDataFetcher(logger)
    agent = RLAgent(model_dir="rl_models")

    for target in TARGETS:
        code = target['code']
        name = target['name']
        direction = target['direction']
        trigger_price = target['trigger_price']

        logger.info(f"🚀 开始恢复训练 {name} ({code}) 基础模型...")

        try:
            df = fetcher.get_historical_data(code, days=120)
            if len(df) < 60:
                logger.warning(f"{name} 历史数据不足 ({len(df)} < 60)，跳过")
                continue

            result = agent.train(
                code=code,
                direction=direction,
                df=df,
                trigger_price=trigger_price,
                total_timesteps=15000,
                eval_freq=5000,
                patience=2,
                external_features=None,
                live_mode=False
            )

            logger.info(f"✅ {name} 基础模型恢复: {result['model_path']}")
            logger.info(f"   最终收益: {result['avg_return']:.2%}, Val Sharpe: {result['best_val_sharpe']:.2f}")

        except Exception as e:
            logger.error(f"❌ {name} 训练失败: {e}", exc_info=True)


if __name__ == "__main__":
    train()
