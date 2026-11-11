"""Walk-forward rolling training v2: train + predict + save IC per segment."""
import qlib
import pandas as pd
import numpy as np
from qlib.workflow import R
from qlib.workflow.task.gen import RollingGen, task_generator
from qlib.model.trainer import task_train

qlib.init(
    provider_uri="~/qlib/data/cn_data",
    region="cn",
    exp_manager={
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {"uri": "sqlite:////home/ubuntu/qlib/mlflow.db", "default_exp_name": "rolling_v2"},
    },
)

MARKET = "csi300"
base_task = {
    "model": {
        "class": "LGBModel",
        "module_path": "qlib.contrib.model.gbdt",
        "kwargs": {
            "loss": "mse", "colsample_bytree": 0.8879, "learning_rate": 0.05,
            "subsample": 0.8789, "lambda_l1": 5.0, "lambda_l2": 10.0,
            "max_depth": 8, "num_leaves": 64, "num_threads": 20,
        },
    },
    "dataset": {
        "class": "DatasetH",
        "module_path": "qlib.data.dataset",
        "kwargs": {
            "handler": {
                "class": "Alpha158",
                "module_path": "qlib.contrib.data.handler",
                "kwargs": {
                    "start_time": "2015-01-01", "end_time": "2026-07-30",
                    "fit_start_time": "2015-01-01", "fit_end_time": "2024-12-31",
                    "instruments": MARKET,
                },
            },
            "segments": {
                "train": ("2015-01-01", "2022-12-31"),
                "valid": ("2023-01-01", "2024-06-30"),
                "test": ("2024-07-01", "2026-07-30"),
            },
        },
    },
}

rolling_gen = RollingGen(step=120, rtype=RollingGen.ROLL_EX)
tasks = task_generator(base_task, rolling_gen)
print(f"=== {len(tasks)} rolling tasks ===", flush=True)

all_preds = []
for i, task in enumerate(tasks):
    seg = task["dataset"]["kwargs"]["segments"]
    print(f"\n=== task {i+1}/{len(tasks)} test={seg['test']} ===", flush=True)
    try:
        rec = task_train(task, experiment_name="rolling_v2")
        model = rec.load_object("params.pkl")
        dataset = rec.load_object("dataset")
        pred = model.predict(dataset)
        if pred is not None and len(pred) > 0:
            label = dataset.prepare(
                ["label"], col_set="label", segments=slice(None)
            ).droplevel(1, axis=1) if hasattr(dataset, "prepare") else None
            rec.save_object(pred, "pred.pkl")
            all_preds.append(pred)
            print(f"  saved {len(pred)} preds", flush=True)
    except Exception as e:
        print(f"  FAILED: {e}", flush=True)

print("\n=== merging all segment predictions ===", flush=True)
if all_preds:
    merged = pd.concat(all_preds).sort_index()
    merged = merged[~merged.index.duplicated(keep="first")]
    print(f"merged: {len(merged)} preds, {merged.index.get_level_values(1).min().date()} ~ {merged.index.get_level_values(1).max().date()}", flush=True)
    # compute daily IC against the handler's label
    from qlib.data import D
    label_df = D.features(D.instruments(market=MARKET), ["$close/Ref($close,1)-1"], start_time="2024-07-01", end_time="2026-07-30")
    label_df.columns = ["label"]
    joined = merged.join(label_df, how="inner")
    daily_ic = joined.groupby(level=1).apply(lambda g: g.corr("spearman").iloc[0, 1] if len(g) > 5 else np.nan).dropna()
    print(f"\n=== Rolling IC (csi300, Alpha158, tuned LightGBM, walk-forward) ===", flush=True)
    print(f"days: {len(daily_ic)}", flush=True)
    print(f"mean IC: {daily_ic.mean():.4f}", flush=True)
    print(f"ICIR: {daily_ic.mean()/daily_ic.std():.4f}", flush=True)
    print(f"positive IC days: {(daily_ic>0).sum()}/{len(daily_ic)} ({(daily_ic>0).mean()*100:.1f}%)", flush=True)
