#!/usr/bin/env python3
"""
Benchmark Runner - 自动化回测基准测试
运行所有策略在所有股票上的回测，并自动记录到 benchmark_tracker

数据源：统一走 MarketDataFetcher.get_historical_data()（东财 push2his，已标准化 + 血缘）
"""

import os
import sys
import logging

from backtest_pro import BacktestEngine, PriceTriggerStrategy
from rl_strategy import RLStrategy
from radar.data.market_data import MarketDataFetcher
from radar.data.unified_history import log_history_lineage
from benchmark_tracker import (
    record_from_backtest, save_record, append_to_csv,
    generate_comparison_chart, generate_report, compare_experiments
)

logger = logging.getLogger(__name__)


def run_all_benchmarks() -> None:
    client = MarketDataFetcher(logger=logger, use_akshare=True)
    
    targets = [
        {'code': '300059', 'name': '东方财富', 'trigger_price': 23.7, 'direction': 'below'},
        {'code': '300033', 'name': '同花顺', 'trigger_price': 244.7, 'direction': 'below'},
        {'code': '300058', 'name': '蓝色光标', 'trigger_price': 13.0, 'direction': 'below'},

        {'code': '000001', 'name': '平安银行', 'trigger_price': 11.5, 'direction': 'above'},
    ]
    
    strategies = [
        ("price_trigger", lambda t: PriceTriggerStrategy(
            trigger_price=t['trigger_price'],
            direction=t['direction'],
            stop_loss_pct=0.05,
            take_profit_pct=0.10,
            position_pct=0.2
        )),
        ("rl_single", lambda t: RLStrategy(
            code=t['code'],
            direction=t['direction'],
            trigger_price=t['trigger_price'],
            model_dir='rl_models',
            universal=False
        )),
        ("rl_universal", lambda t: RLStrategy(
            code=t['code'],
            direction=t['direction'],
            trigger_price=t['trigger_price'],
            model_dir='rl_models',
            universal=True
        )),
    ]
    
    print("=" * 90)
    print("🚀 启动自动化回测基准测试")
    print("=" * 90)
    print()
    
    total_records = 0
    
    for target in targets:
        print(f"📊 测试股票: {target['name']} ({target['code']})")
        df = client.get_historical_data(target['code'], days=120)
        log_history_lineage(logger, f"benchmark回测 {target['code']}", df)
        if df is None or len(df) < 30:
            print(f"  ⚠️ 数据不足 ({len(df) if df is not None else 0} 条)，跳过\n")
            continue
        
        start_price = df['close'].iloc[0]
        end_price = df['close'].iloc[-1]
        buy_hold = (end_price - start_price) / start_price
        
        for exp_name, strategy_factory in strategies:
            try:
                strategy = strategy_factory(target)
                engine = BacktestEngine(initial_capital=100000, commission_rate=0.0003)
                result = engine.run(df, strategy)
                
                record = record_from_backtest(
                    experiment=exp_name,
                    strategy_name=strategy.name,
                    stock_code=target['code'],
                    stock_name=target['name'],
                    trigger_price=target['trigger_price'],
                    direction=target['direction'],
                    result=result,
                    buy_hold_return=buy_hold,
                    data_days=len(df),
                    notes="Auto benchmark run"
                )
                
                save_record(record)
                append_to_csv(record)
                total_records += 1
                
                print(f"  ✅ {exp_name:15s} | 收益:{result.total_return:>7.2%} | "
                      f"回撤:{result.max_drawdown:>7.2%} | Sharpe:{result.sharpe_ratio:>5.2f} | "
                      f"交易:{result.total_trades:>2d}")
            except Exception as e:
                print(f"  ❌ {exp_name:15s} | 失败: {e}")
        
        print()
    
    print("=" * 90)
    print(f"📁 共记录 {total_records} 条回测结果")
    
    # 生成对比报告
    print("\n📈 生成对比报告...")
    chart_path = generate_comparison_chart(output_path="benchmarks/comparison_all.png")
    
    for target in targets:
        try:
            generate_comparison_chart(
                stock_code=target['code'],
                output_path=f"benchmarks/comparison_{target['code']}.png"
            )
        except Exception:
            # 图表生成失败不影响报告生成
            pass
    
    report_path = generate_report(output_path="benchmarks/latest_report.md")
    print(f"📝 报告已保存: {report_path}")
    
    # 打印汇总表
    print("\n" + "=" * 90)
    print("📊 核心指标汇总")
    print("=" * 90)
    summary = compare_experiments()
    print(summary.to_string(index=False))
    print()


if __name__ == "__main__":
    run_all_benchmarks()
