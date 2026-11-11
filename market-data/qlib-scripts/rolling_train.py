"""Walk-forward rolling training with LightGBM + Alpha158 on csi300.

Uses RollingGen (expanding window, step=120 trading days) + TrainerR (no MongoDB).
Each rolling task trains on data up to test-segment-start, predicts that segment,
then the collector merges all segment predictions for a unified backtest.
"""
import qlib
from qlib.config import C
from qlib.workflow import R
from qlib.workflow.task.gen import RollingGen, task_generator
from qlib.model.trainer import TrainerR, task_train

# --- 1. qlib init (sqlite mlflow backend) ---
qlib.init(
    provider_uri="~/qlib/data/cn_data",
    region="cn",
    exp_manager={
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {
            "uri": "sqlite:////home/ubuntu/qlib/mlflow.db",
            "default_exp_name": "rolling_workflow",
        },
    },
)

# --- 2. base task config (tuned LightGBM + Alpha158 + csi300) ---
MARKET = "csi300"
BENCH = "SH000300"

base_task = {
    "model": {
        "class": "LGBModel",
        "module_path": "qlib.contrib.model.gbdt",
        "kwargs": {
            "loss": "mse",
            "colsample_bytree": 0.8879,
            "learning_rate": 0.05,
            "subsample": 0.8789,
            "lambda_l1": 5.0,
            "lambda_l2": 10.0,
            "max_depth": 8,
            "num_leaves": 64,
            "num_threads": 20,
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
                    "start_time": "2015-01-01",
                    "end_time": "2026-07-30",
                    "fit_start_time": "2015-01-01",
                    "fit_end_time": "2024-12-31",
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

# --- 3. generate rolling tasks (expanding window, 120-day step) ---
rolling_gen = RollingGen(step=120, rtype=RollingGen.ROLL_EX)
tasks = task_generator(base_task, rolling_gen)
print(f"=== generated {len(tasks)} rolling tasks ===")
for i, t in enumerate(tasks):
    seg = t["dataset"]["kwargs"]["segments"]
    print(f"  task {i}: train={seg['train']} test={seg['test']}")

# --- 4. train each rolling task ---
trainer = TrainerR(experiment_name="rolling_workflow")
for i, task in enumerate(tasks):
    seg = task["dataset"]["kwargs"]["segments"]
    print(f"\n=== training rolling task {i+1}/{len(tasks)}: test={seg['test']} ===")
    try:
        task_train(task, experiment_name="rolling_workflow")
    except Exception as e:
        print(f"  FAILED: {e}")

print("\n=== all rolling tasks done ===")
print("Runs in experiment:")
exp = R.get_exp(experiment_name="rolling_workflow")
for rid in exp.list_recorders():
    rec = exp.get_recorder(rid)
    print(f"  {rid}: {rec.info.get('start_time','')} segments={rec.info.get('dataset',{}).get('kwargs',{}).get('segments',{})}")
