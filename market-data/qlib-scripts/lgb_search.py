"""LightGBM hyperparameter search on csi800+Alpha158 (tuned baseline).
Grid: num_leaves x max_depth, keep other tuned params. Pick best by valid IC."""
import qlib
import pandas as pd
import numpy as np
import os, json
from qlib.workflow import R

qlib.init(
    provider_uri="~/qlib/data/cn_data",
    region="cn",
    exp_manager={
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {"uri": "sqlite:////home/ubuntu/qlib/mlflow.db", "default_exp_name": "lgb_search"},
    },
)
from qlib.contrib.data.handler import Alpha158
from qlib.data.dataset import DatasetH
from qlib.contrib.model.gbdt import LGBModel

MARKET = "csi800"
# prepare dataset once (reuse across trials)
handler = Alpha158(
    instruments=MARKET,
    start_time="2018-01-01", end_time="2026-07-30",
    fit_start_time="2018-01-01", fit_end_time="2022-12-31",
)
dataset = DatasetH(handler=handler, segments={
    "train": ("2018-01-01", "2022-12-31"),
    "valid": ("2023-01-01", "2024-12-31"),
    "test":  ("2025-01-01", "2026-07-30"),
})

def daily_ic(pred, label):
    df = pred.to_frame("score").join(label.to_frame("label"), how="inner")
    return df.groupby(level=0).apply(lambda g: g.corr("spearman").iloc[0, 1] if len(g) > 5 else np.nan).dropna()

# get valid & test labels
valid_label = dataset.prepare("valid", col_set="label").droplevel(1, axis=1)
test_label = dataset.prepare("test", col_set="label").droplevel(1, axis=1)

grid = [
    {"num_leaves": 31, "max_depth": 6},
    {"num_leaves": 64, "max_depth": 8},   # baseline
    {"num_leaves": 128, "max_depth": 10},
    {"num_leaves": 64, "max_depth": 6},
    {"num_leaves": 31, "max_depth": -1},  # no depth limit
]

results = []
for i, g in enumerate(grid):
    print(f"\n=== trial {i+1}/{len(grid)}: {g} ===", flush=True)
    model = LGBModel(
        loss="mse", colsample_bytree=0.8879, learning_rate=0.05,
        subsample=0.8789, lambda_l1=5.0, lambda_l2=10.0,
        num_threads=20, **g,
    )
    try:
        model.fit(dataset)
        valid_pred = model.predict(dataset, segments="valid")
        test_pred = model.predict(dataset, segments="test")
        v_ic = daily_ic(valid_pred, valid_label)
        t_ic = daily_ic(test_pred, test_label)
        stats = {
            "trial": i + 1, **g,
            "valid_IC": float(v_ic.mean()), "valid_ICIR": float(v_ic.mean() / v_ic.std()) if v_ic.std() > 0 else np.nan,
            "test_IC": float(t_ic.mean()), "test_ICIR": float(t_ic.mean() / t_ic.std()) if t_ic.std() > 0 else np.nan,
        }
        results.append(stats)
        print(f"  valid IC={stats['valid_IC']:.4f} ICIR={stats['valid_ICIR']:.4f} | test IC={stats['test_IC']:.4f} ICIR={stats['test_ICIR']:.4f}", flush=True)
    except Exception as e:
        print(f"  FAILED: {e}", flush=True)

OUT = os.path.expanduser("~/qlib/results")
os.makedirs(OUT, exist_ok=True)
res_df = pd.DataFrame(results)
res_df.to_csv(f"{OUT}/lgb_hyperparam_search.csv", index=False)
print(f"\n=== Hyperparameter search results ===\n{res_df.to_string(index=False)}")
best = res_df.loc[res_df["valid_IC"].idxmax()]
print(f"\nBest by valid IC: trial {best['trial']}, num_leaves={best['num_leaves']}, max_depth={best['max_depth']}")
print(f"  valid IC={best['valid_IC']:.4f}, test IC={best['test_IC']:.4f}")
