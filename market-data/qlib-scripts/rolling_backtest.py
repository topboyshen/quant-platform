"""Per-segment backtest for rolling model: each segment backtested independently,
then stitch daily returns. This avoids the cross-segment scale mismatch that
corrupted the naive concat-IC in rolling_v3.

Uses the 5 trained rolling runs in experiment 'rolling_v3', backtests each on
its own test segment with TopkDropoutStrategy, concatenates daily returns.
"""
import qlib
import pandas as pd
import numpy as np
import os

qlib.init(
    provider_uri="~/qlib/data/cn_data",
    region="cn",
    exp_manager={
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {"uri": "sqlite:////home/ubuntu/qlib/mlflow.db", "default_exp_name": "rolling_v3"},
    },
)
from qlib.workflow import R
from qlib.contrib.evaluate import backtest_daily, risk_analysis
from qlib.data import D

OUT = os.path.expanduser("~/qlib/results/rolling_stitched")
os.makedirs(OUT, exist_ok=True)

exp = R.get_exp(experiment_name="rolling_v3")
rids = exp.list_recorders()

all_daily = []
segment_stats = []
for rid in rids:
    rec = exp.get_recorder(rid)
    try:
        pred = rec.load_object("pred.pkl")
        if pred is None or len(pred) == 0:
            continue
    except Exception as e:
        print(f"{rid[:8]}: no pred, skip")
        continue

    # pred index is (datetime, instrument); get segment range
    seg_start = pred.index.get_level_values(0).min()
    seg_end = pred.index.get_level_values(0).max()
    print(f"\n=== {rid[:8]} segment {seg_start.date()} ~ {seg_end.date()} ({len(pred)} preds) ===")

    # backtest this segment using the standard daily backtest with TopkDropoutStrategy
    # pred must have a 'score' column
    pred_df = pred.copy()
    if isinstance(pred_df, pd.DataFrame):
        pred_df.columns = ["score"]
    else:
        pred_df = pred_df.to_frame("score")

    try:
        portfolio_metric = backtest_daily(
            pred_df,
            start_time=str(seg_start.date()),
            end_time=str(seg_end.date()),
            account=1e8,
            benchmark="SH000300",
            exchange_kwargs={
                "limit_threshold": 0.095,
                "deal_price": "close",
                "open_cost": 0.0005,
                "close_cost": 0.0015,
                "min_cost": 5,
            },
            strategy_kwargs={"topk": 50, "n_drop": 5},
        )
        # portfolio_metric[0] is the report DataFrame
        report = portfolio_metric[0]
        report.to_csv(f"{OUT}/segment_{rid[:8]}.csv")
        all_daily.append(report)
        seg_ret = report["return"]
        seg_excess = report["return"] - report["bench"]
        segment_stats.append({
            "segment": f"{seg_start.date()}~{seg_end.date()}",
            "days": len(report),
            "ann_return": seg_ret.mean() * 252,
            "ann_excess": seg_excess.mean() * 252,
            "excess_sharpe": seg_excess.mean() / seg_excess.std() * np.sqrt(252) if seg_excess.std() > 0 else np.nan,
            "max_dd": (1 + seg_ret).cumprod().div((1 + seg_ret).cumprod().cummax()).min() - 1,
        })
        print(f"  ann_excess={seg_excess.mean()*252*100:.2f}%, excess_sharpe={segment_stats[-1]['excess_sharpe']:.3f}")
    except Exception as e:
        print(f"  backtest FAILED: {e}")

if all_daily:
    stitched = pd.concat(all_daily).sort_index()
    stitched = stitched[~stitched.index.duplicated(keep="first")]
    stitched.to_csv(f"{OUT}/stitched_report.csv")
    print(f"\n=== Stitched rolling backtest ({len(stitched)} days) ===")
    excess = stitched["return"] - stitched["bench"]
    cumret = (1 + stitched["return"]).cumprod()
    cumbench = (1 + stitched["bench"]).cumprod()
    print(f"period: {stitched.index.min().date()} ~ {stitched.index.max().date()}")
    print(f"strategy total return: {(cumret.iloc[-1]-1)*100:.2f}%")
    print(f"benchmark total return: {(cumbench.iloc[-1]-1)*100:.2f}%")
    print(f"excess total return:    {((cumret/cumbench).iloc[-1]-1)*100:.2f}%")
    print(f"ann excess return:      {excess.mean()*252*100:.2f}%")
    print(f"ann excess vol:         {excess.std()*np.sqrt(252)*100:.2f}%")
    print(f"excess sharpe:          {excess.mean()/excess.std()*np.sqrt(252):.3f}")
    print(f"max drawdown:           {((1+stitched['return']).cumprod()/(1+stitched['return']).cumprod().cummax()-1).min()*100:.2f}%")
    print(f"\n=== Per-segment breakdown ===")
    seg_df = pd.DataFrame(segment_stats)
    print(seg_df.to_string(index=False))
    seg_df.to_csv(f"{OUT}/segment_summary.csv", index=False)
