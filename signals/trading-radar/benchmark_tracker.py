#!/usr/bin/env python3
"""
Benchmark Tracker - 回测绩效追踪器
自动记录、对比和可视化不同策略/模型的回测指标
"""

import os
import json
import csv
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from collections import defaultdict

BENCHMARK_DIR = "benchmarks"
os.makedirs(BENCHMARK_DIR, exist_ok=True)


@dataclass
class BenchmarkRecord:
    """单次回测记录的完整指标"""
    timestamp: str
    experiment: str          # 实验名称，如 "rl_single_v1"
    strategy: str            # 策略类型
    stock_code: str
    stock_name: str
    trigger_price: float
    direction: str
    
    # 核心收益指标
    total_return: float
    annualized_return: float
    max_drawdown: float
    max_drawdown_duration: int
    
    # 风险调整收益
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    
    # 交易统计
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    
    # 收益分布
    avg_trade_return: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    
    # 持仓统计
    avg_holding_days: float
    max_holding_days: int
    min_holding_days: int
    
    # 连续统计
    max_consecutive_wins: int
    max_consecutive_losses: int
    
    # 市场环境
    buy_hold_return: float
    data_days: int
    
    # 备注
    notes: str = ""


def record_from_backtest(
    experiment: str,
    strategy_name: str,
    stock_code: str,
    stock_name: str,
    trigger_price: float,
    direction: str,
    result,
    buy_hold_return: float,
    data_days: int,
    notes: str = ""
) -> BenchmarkRecord:
    """从 BacktestResult 构建记录"""
    return BenchmarkRecord(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        experiment=experiment,
        strategy=strategy_name,
        stock_code=stock_code,
        stock_name=stock_name,
        trigger_price=trigger_price,
        direction=direction,
        total_return=result.total_return,
        annualized_return=result.annualized_return,
        max_drawdown=result.max_drawdown,
        max_drawdown_duration=result.max_drawdown_duration,
        sharpe_ratio=result.sharpe_ratio,
        sortino_ratio=result.sortino_ratio,
        calmar_ratio=result.calmar_ratio,
        total_trades=result.total_trades,
        winning_trades=result.winning_trades,
        losing_trades=result.losing_trades,
        win_rate=result.win_rate,
        profit_factor=result.profit_factor,
        expectancy=result.expectancy,
        avg_trade_return=result.avg_trade_return,
        avg_win=result.avg_win,
        avg_loss=result.avg_loss,
        largest_win=result.largest_win,
        largest_loss=result.largest_loss,
        avg_holding_days=result.avg_holding_days,
        max_holding_days=result.max_holding_days,
        min_holding_days=result.min_holding_days,
        max_consecutive_wins=result.max_consecutive_wins,
        max_consecutive_losses=result.max_consecutive_losses,
        buy_hold_return=buy_hold_return,
        data_days=data_days,
        notes=notes
    )


def save_record(record: BenchmarkRecord) -> str:
    """保存单条记录到 JSON"""
    filename = f"{record.timestamp.replace(' ', '_').replace(':', '-')}_{record.experiment}_{record.stock_code}.json"
    filepath = os.path.join(BENCHMARK_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(asdict(record), f, ensure_ascii=False, indent=2)
    return filepath


def append_to_csv(record: BenchmarkRecord, csv_name: str = "benchmark_history.csv") -> str:
    """追加记录到 CSV 汇总表"""
    csv_path = os.path.join(BENCHMARK_DIR, csv_name)
    record_dict = asdict(record)
    file_exists = os.path.exists(csv_path)
    
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=record_dict.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(record_dict)
    return csv_path


def load_all_records() -> List[BenchmarkRecord]:
    """加载所有历史记录"""
    records = []
    for filename in sorted(os.listdir(BENCHMARK_DIR)):
        if filename.endswith(".json"):
            with open(os.path.join(BENCHMARK_DIR, filename), "r", encoding="utf-8") as f:
                data = json.load(f)
                records.append(BenchmarkRecord(**data))
    return records


def load_csv(csv_name: str = "benchmark_history.csv") -> Optional[pd.DataFrame]:
    """加载 CSV 汇总表"""
    csv_path = os.path.join(BENCHMARK_DIR, csv_name)
    if not os.path.exists(csv_path):
        return None
    return pd.read_csv(csv_path)


def compare_experiments(stock_code: Optional[str] = None) -> pd.DataFrame:
    """对比不同实验的绩效指标"""
    df = load_csv()
    if df is None:
        return pd.DataFrame()
    
    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    
    if stock_code:
        df = df[df["stock_code"] == stock_code.zfill(6)]
    
    # 选取核心对比指标
    cols = [
        "experiment", "strategy", "stock_code", "stock_name",
        "total_return", "max_drawdown", "sharpe_ratio",
        "win_rate", "total_trades", "avg_holding_days",
        "buy_hold_return", "timestamp"
    ]
    return df[cols].sort_values(["stock_code", "experiment"])


def generate_comparison_chart(stock_code: Optional[str] = None, output_path: str = "benchmarks/comparison_chart.png"):
    """生成对比图表"""
    df = load_csv()
    if df is None or df.empty:
        print("No benchmark data available")
        return
    
    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    
    if stock_code:
        df = df[df["stock_code"] == stock_code.zfill(6)]
        title_suffix = f" - {stock_code}"
    else:
        title_suffix = " - All Stocks"
    
    if df.empty:
        print(f"No data for {stock_code}")
        return
    
    experiments = df["experiment"].unique()
    metrics = ["total_return", "max_drawdown", "sharpe_ratio", "win_rate"]
    metric_labels = ["Total Return", "Max Drawdown", "Sharpe Ratio", "Win Rate"]
    
    fig, axes = plt.subplots(2, 2, figsize=(12, 10))
    axes = axes.flatten()
    
    for idx, (metric, label) in enumerate(zip(metrics, metric_labels)):
        ax = axes[idx]
        
        if stock_code:
            # 单股票：按实验分组柱状图
            grouped = df.groupby("experiment")[metric].mean().reindex(experiments)
            colors = plt.cm.Set3(range(len(grouped)))
            bars = ax.bar(grouped.index, grouped.values, color=colors)
            ax.set_ylabel(label)
            for bar in bars:
                height = bar.get_height()
                ax.annotate(f"{height:.2f}" if metric in ["sharpe_ratio", "win_rate"] else f"{height:.1%}",
                            xy=(bar.get_x() + bar.get_width() / 2, height),
                            xytext=(0, 3), textcoords="offset points",
                            ha="center", va="bottom", fontsize=9)
        else:
            # 多股票：按股票+实验分组
            pivot = df.pivot_table(values=metric, index="stock_code", columns="experiment", aggfunc="mean")
            pivot.plot(kind="bar", ax=ax, rot=45)
            ax.set_ylabel(label)
            ax.legend(title="Experiment", loc="best")
        
        ax.set_title(label)
        ax.axhline(y=0, color="black", linestyle="-", linewidth=0.5)
        ax.grid(axis="y", alpha=0.3)
    
    plt.suptitle(f"Strategy Benchmark Comparison{title_suffix}", fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Chart saved: {output_path}")


def generate_report(output_path: str = "benchmarks/latest_report.md") -> str:
    """生成 Markdown 对比报告"""
    df = load_csv()
    if df is None or df.empty:
        return "No benchmark data available"
    
    lines = ["# 📊 回测绩效追踪报告\n", f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"]
    
    # 最新实验对比
    lines.append("## 🔥 最新实验对比\n")
    lines.append(compare_experiments().to_markdown(index=False))
    lines.append("\n")
    
    # 按股票分组最佳实验
    lines.append("## 🏆 各股票最佳策略\n")
    for code in sorted(df["stock_code"].unique()):
        sub = df[df["stock_code"] == code]
        best = sub.loc[sub["total_return"].idxmax()]
        lines.append(f"- **{best['stock_name']} ({code})**: `{best['experiment']}` | "
                     f"收益 {best['total_return']:.2%} | Sharpe {best['sharpe_ratio']:.2f} | "
                     f"胜率 {best['win_rate']:.1%}\n")
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    
    return output_path


if __name__ == "__main__":
    print("Benchmark Tracker loaded")
    print(f"Records dir: {BENCHMARK_DIR}")
    records = load_all_records()
    print(f"Total records: {len(records)}")
