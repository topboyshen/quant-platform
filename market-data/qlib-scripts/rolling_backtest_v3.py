"""Per-segment backtest v3 using qlib.contrib.evaluate.backtest_daily."""
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

OUT = os.path.expanduser("~/qlib/results/rolling_stitched")
os.makedirs(OUT, exist_ok=True)

exp = R.get_exp(experiment_name="rolling_v3")
rids = exp.list_recorders()

all_reports = []
segment_stats = []

for rid in rids:
    rec = exp.get_recorder(rid)
    try:
        pred = rec.load_object("pred.pkl")
        if pred is None or len(pred) == 0:
            continue
    except Exception:
        continue

    pred_df = pred.copy()
    if isinstance(pred_df, pd.DataFrame):
        pred_df.columns = ["score"]
    else:
        pred_df = pred_df.to_frame("score")

    seg_start = str(pred_df.index.get_level_values(0).min().date())
    seg_end = str(pred_df.index.get_level_values(0).max().date())
    print(f"\n=== {rid[:8]} segment {seg_start} ~ {seg_end} ({len(pred_df)} preds) ===", flush=True)

    strategy_config = {
        "class": "TopkDropoutStrategy",
        "module_path": "qlib.contrib.strategy.signal_strategy",
        "kwargs": {"signal": pred_df, "topk": 50, "n_drop": 5},
    }

    try:
        portfolio_metric = backtest_daily(
            start_time=seg_start,
            end_time=seg_end,
            strategy=strategy_config,
            benchmark="SH000300",
            account=1e8,
            exchange_kwargs={
                "limit_threshold": 0.095,
                "deal_price": "close",
                "open_cost": 0.0005,
                "close_cost": 0.0015,
                "min_cost": 5,
            },
        )
        # portfolio_metric returns (report_dict, indicator_dict); extract the day-level report
        # portfolio_metric = (report_df, indicator_dict)
        report = portfolio_metric[0] if isinstance(portfolio_metric, tuple) else portfolio_metric
        if isinstance(report, dict):
            report = pd.DataFrame(report)
        if not isinstance(report, pd.DataFrame) or report.empty:
            print(f"  empty report, raw type={type(report_dict)}", flush=True)
            continue
        report.to_csv(f"{OUT}/segment_{rid[:8]}.csv")
        all_reports.append(report)
        seg_excess = report["return"] - report["bench"]
        seg_ret = report["return"]
        cum = (1 + seg_ret).cumprod()
        stats = {
            "segment": f"{seg_start}~{seg_end}",
            "days": len(report),
            "ann_return": seg_ret.mean() * 252,
            "ann_excess": seg_excess.mean() * 252,
            "excess_sharpe": seg_excess.mean() / seg_excess.std() * np.sqrt(252) if seg_excess.std() > 0 else np.nan,
            "max_dd": (cum / cum.cummax() - 1).min(),
        }
        segment_stats.append(stats)
        print(f"  ann_excess={stats['ann_excess']*100:.2f}%, sharpe={stats['excess_sharpe']:.3f}, dd={stats['max_dd']*100:.2f}%", flush=True)
    except Exception as e:
        import traceback
        print(f"  FAILED: {e}", flush=True)
        traceback.print_exc()

if all_reports:
    stitched = pd.concat(all_reports).sort_index()
    stitched = stitched[~stitched.index.duplicated(keep="first")]
    stitched.to_csv(f"{OUT}/stitched_report.csv")
    print(f"\n=== Stitched rolling backtest ({len(stitched)} days) ===", flush=True)
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
    print(f"\n=== Per-segment breakdown ===", flush=True)
    seg_df = pd.DataFrame(segment_stats)
    print(seg_df.to_string(index=False))
    seg_df.to_csv(f"{OUT}/segment_summary.csv", index=False)
