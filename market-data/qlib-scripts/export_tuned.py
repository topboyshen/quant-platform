"""Export tuned-model (csi800+Alpha158) backtest details to CSV + plot."""
import qlib
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import os

qlib.init(
    provider_uri="~/qlib/data/cn_data",
    region="cn",
    exp_manager={
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {"uri": "sqlite:////home/ubuntu/qlib/mlflow.db", "default_exp_name": "workflow"},
    },
)
from qlib.workflow import R

OUT = os.path.expanduser("~/qlib/results/tuned_csi800")
os.makedirs(OUT, exist_ok=True)

# the tuned run is b119655b (in experiment 1)
exp = R.get_exp(experiment_name="workflow")
rec = exp.get_recorder("b119655b7b0f46a6b0463e6e89e4e6d7")

# 1. report_normal: daily returns (return, cost, bench, turnover)
report = rec.load_object("portfolio_analysis/report_normal_1day.pkl")
report.to_csv(f"{OUT}/report_normal.csv")
print(f"report_normal: {report.shape}, cols={list(report.columns)}")

# 2. positions — extract daily holdings count from Position objects
pos = rec.load_object("portfolio_analysis/positions_normal_1day.pkl")
rows = []
for date, position in pos.items():
    # Position object: position.position dict has cash + instrument keys
    pdict = position.position if hasattr(position, "position") else position
    held = {k: v for k, v in pdict.items() if k not in ("cash", "now_account_value")}
    rows.append({"date": date, "num_holdings": len(held), "cash": pdict.get("cash", np.nan), "holdings": ";".join(sorted(held.keys()))})
pos_df = pd.DataFrame(rows)
pos_df.to_csv(f"{OUT}/positions_normal.csv", index=False)
print(f"positions: {len(pos_df)} days, avg holdings={pos_df["num_holdings"].mean():.1f}")

# 3. indicators
ind = rec.load_object("portfolio_analysis/indicator_analysis_1day.pkl")
ind.to_csv(f"{OUT}/indicators.csv")
print(f"indicators: {ind.shape}")

# 4. port_analysis summary
pa = rec.load_object("portfolio_analysis/port_analysis_1day.pkl")
pa.to_csv(f"{OUT}/port_analysis_summary.csv")
print(f"port_analysis summary:\n{pa}")

# 5. IC
ic = rec.load_object("sig_analysis/ic.pkl")
ric = rec.load_object("sig_analysis/ric.pkl")
ic.to_csv(f"{OUT}/ic_daily.csv")
ric.to_csv(f"{OUT}/rank_ic_daily.csv")
print(f"IC mean={ic.mean():.4f}, RankIC mean={ric.mean():.4f}")

# 6. plot: cumulative return vs benchmark
fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
# cumulative return
cumret = (1 + report["return"]).cumprod()
cumbench = (1 + report["bench"]).cumprod()
excess = (1 + report["return"] - report["bench"]).cumprod()
axes[0].plot(cumret.index, cumret, label="Strategy", linewidth=1.5)
axes[0].plot(cumbench.index, cumbench, label="CSI300", linewidth=1.2, alpha=0.7)
axes[0].set_ylabel("Cumulative Return")
axes[0].set_title("Tuned LightGBM + Alpha158 (csi800) — 2025-01 to 2026-07")
axes[0].legend()
axes[0].grid(True, alpha=0.3)
# excess return
axes[1].plot(excess.index, excess, color="green", linewidth=1.5)
axes[1].axhline(1, color="gray", linestyle="--", alpha=0.5)
axes[1].set_ylabel("Excess Cumulative")
axes[1].grid(True, alpha=0.3)
# daily IC
axes[2].bar(ic.index, ic.values, color="steelblue", alpha=0.6, width=1)
roll_ic = ic.rolling(20).mean()
axes[2].plot(roll_ic.index, roll_ic.values, color="red", linewidth=1.2, label="20d MA")
axes[2].axhline(0, color="gray", linestyle="-", alpha=0.5)
axes[2].set_ylabel("Daily IC")
axes[2].legend()
axes[2].grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(f"{OUT}/backtest_detail.png", dpi=120)
print(f"\nplot saved: {OUT}/backtest_detail.png")

# summary metrics
print("\n=== Final Summary ===")
ann_ret = report["return"].mean() * 252
ann_vol = report["return"].std() * np.sqrt(252)
sharpe = ann_ret / ann_vol
max_dd = (cumret / cumret.cummax() - 1).min()
print(f"Annualized return: {ann_ret*100:.2f}%")
print(f"Annualized vol:    {ann_vol*100:.2f}%")
print(f"Sharpe:            {sharpe:.3f}")
print(f"Max drawdown:      {max_dd*100:.2f}%")
print(f"Total return:      {(cumret.iloc[-1]-1)*100:.2f}%")
