#!/usr/bin/env python3
"""
运行三向回测对比：PriceTrigger vs RL Base vs RL External (new 10-dim features)
"""

import logging
from radar.data.market_data import MarketDataFetcher
from radar.data.unified_history import log_history_lineage
from backtest_pro import BacktestEngine, PriceTriggerStrategy
from rl_strategy import RLStrategy
from benchmark_tracker import (
    record_from_backtest, save_record, append_to_csv,
    generate_report, generate_comparison_chart
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


TARGETS = [
    {'code': '300059', 'name': '东方财富', 'direction': 'below', 'trigger_price': 23.7},

    {'code': '000001', 'name': '平安银行', 'direction': 'above', 'trigger_price': 11.5},
    {'code': '300033', 'name': '同花顺', 'direction': 'below', 'trigger_price': 244.7},
    {'code': '300058', 'name': '蓝色光标', 'direction': 'below', 'trigger_price': 13.0},
]


def run() -> None:
    fetcher = MarketDataFetcher(logger)
    initial_capital = 100000
    experiment = "v2_10ext"

    for t in TARGETS:
        code = t['code']
        name = t['name']
        direction = t['direction']
        trigger_price = t['trigger_price']

        print(f"\n{'='*60}")
        print(f"回测 {name} ({code})")
        print(f"{'='*60}")

        df = fetcher.get_historical_data(code, days=150)
        log_history_lineage(logger, f"回测历史K {code}", df)
        if len(df) < 60:
            print(f"数据不足，跳过")
            continue

        buy_hold_return = (df['close'].iloc[-1] - df['close'].iloc[0]) / df['close'].iloc[0]
        data_days = len(df)

        # 1. PriceTrigger
        pt_strategy = PriceTriggerStrategy(trigger_price, direction)
        engine_pt = BacktestEngine(initial_capital=initial_capital)
        result_pt = engine_pt.run(df, pt_strategy)

        # 2. RL Base (15-dim, no external)
        rl_base = RLStrategy(code, direction, trigger_price, universal=False, use_external_features=False)
        engine_base = BacktestEngine(initial_capital=initial_capital)
        result_base = engine_base.run(df, rl_base)

        # 3. RL External (25-dim, new features)
        rl_ext = RLStrategy(code, direction, trigger_price, universal=False, use_external_features=True)
        engine_ext = BacktestEngine(initial_capital=initial_capital)
        result_ext = engine_ext.run(df, rl_ext)

        print(f"Buy&Hold: {buy_hold_return:+.2%}")
        print(f"PriceTrigger: {result_pt.total_return:+.2%} | trades={result_pt.total_trades}")
        print(f"RL Base: {result_base.total_return:+.2%} | trades={result_base.total_trades}")
        print(f"RL External (NEW): {result_ext.total_return:+.2%} | trades={result_ext.total_trades}")

        # Record benchmarks
        for strategy_name, result, notes_suffix in [
            ("PriceTrigger", result_pt, ""),
            ("RL_Base", result_base, "no_ext"),
            ("RL_External", result_ext, "10ext"),
        ]:
            record = record_from_backtest(
                experiment=experiment,
                strategy_name=strategy_name,
                stock_code=code,
                stock_name=name,
                trigger_price=trigger_price,
                direction=direction,
                result=result,
                buy_hold_return=buy_hold_return,
                data_days=data_days,
                notes=notes_suffix
            )
            save_record(record)
            append_to_csv(record)

    generate_report()
    generate_comparison_chart(output_path="benchmarks/comparison_all_v2.png")
    for t in TARGETS:
        code = t['code']
        generate_comparison_chart(stock_code=code, output_path=f"benchmarks/comparison_{code}_v2.png")
    print("\n✅ 回测完成，报告保存至 benchmarks/latest_report.md")


if __name__ == "__main__":
    run()
