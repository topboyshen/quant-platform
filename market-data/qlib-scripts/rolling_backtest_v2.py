"""Per-segment backtest v2: use qlib.backtest.backtest with proper strategy config."""
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
from qlib.backtest import backtest, executor as executor_mod
from qlib.contrib.evaluate import risk_analysis
from qlib.data import D

OUT = os.path.expanduser("~/qlib/results/rolling_stitched")
os.makedirs(OUT, exist_ok=True)

exp = R.get_exp(experiment_name="rolling_v3")
rids = exp.list_recorders()

all_daily = []
segment_stats = []

# common exchange & executor config
EXECUTOR_CONFIG = {
    "class": "SimulatorExecutor",
    "module_path": "qlib.backtest.executor",
    "kwargs": {
        "time_per_step": "day",
        "generate_portfolio_metrics": True,
    },
}

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

    # build a ModelSignal-like object: a simple signal that returns pred scores
    from qlib.model.ens.ensemble import AverageEnsemble
    from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy

    # TopkDropoutStrategy accepts a `signal` that has a `get_signal` method or is a DataFrame
    # Use the simplest path:qlib.contrib.strategy.signal_strategy.TopkDropoutStrategy with model+dataset
    # But we already have predictions, so use a wrapper
    class PredSignal:
        """Wrap a prediction DataFrame as a signal object."""
        def __init__(self, pred):
            self.pred = pred
        def get_signal(self, start_time, end_time):
            return self.pred

    strategy_config = {
        "class": "TopkDropoutStrategy",
        "module_path": "qlib.contrib.strategy.signal_strategy",
        "kwargs": {
            "signal": pred_df,
            "topk": 50,
            "n_drop": 5,
        },
    }

    try:
        portfolio_metric, indicator_metric = backtest(
            start_time=seg_start,
            end_time=seg_end,
            strategy=strategy_config,
            executor=EXECUTOR_CONFIG,
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
        report = portfolio_metric
        report.to_csv(f"{OUT}/segment_{rid[:8]}.csv")
        all_daily.append(report)
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
            "total_turnover": report["total_turnover"].iloc[-1] if "total_turnover" in report else np.nan,
        }
        segment_stats.append(stats)
        print(f"  ann_excess={stats['ann_excess']*100:.2f}%, sharpe={stats['excess_sharpe']:.3f}, dd={stats['max_dd']*100:.2f}%", flush=True)
    except Exception as e:
        import traceback
        print(f"  FAILED: {e}", flush=True)
        traceback.print_exc()

if all_daily:
    stitched = pd.concat(all_daily).sort_index()
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
