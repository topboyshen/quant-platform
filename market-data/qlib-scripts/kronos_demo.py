"""Kronos-mini A股预测 demo：用 qlib 茅台日K，预测未来20天 OHLCV。
验证链路：qlib数据 → Kronos tokenizer → mini模型推理 → 反量化预测结果。
"""
import sys
sys.path.insert(0, "/home/ubuntu/Kronos")

import os
# force CPU, avoid any socks proxy env
os.environ["CUDA_VISIBLE_DEVICES"] = ""
for k in ["ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]:
    os.environ.pop(k, None)

import pandas as pd
import numpy as np
import qlib
from qlib.data import D
from model import Kronos, KronosTokenizer, KronosPredictor

# 1. init qlib (read-only, light)
qlib.init(provider_uri="~/qlib/data/cn_data", region="cn")

# 2. pull 茅台 SH600519 recent daily K-line (400 days for context)
print("=== loading 茅台 SH600519 daily K-line from qlib ===")
df = D.features(
    ["SH600519"],
    ["$open", "$high", "$low", "$close", "$volume"],
    start_time="2024-12-01", end_time="2026-07-30",
)
df = df.reset_index()
df.columns = ["instrument", "datetime", "open", "high", "low", "close", "volume"]
df = df.sort_values("datetime").reset_index(drop=True)
print(f"loaded {len(df)} bars, range {df['datetime'].min().date()} ~ {df['datetime'].max().date()}")
print(df.tail(3).to_string(index=False))

lookback = 400  # mini supports 2048, use 400
pred_len = 20   # predict 20 trading days ahead

x_df = df.loc[:lookback-1, ["open", "high", "low", "close", "volume"]].copy()
# Kronos expects columns: open, high, low, close, volume (amount optional)
x_timestamp = df.loc[:lookback-1, "datetime"]

# build future timestamps: next 20 trading days from qlib calendar
cal = D.calendar(start_time=df["datetime"].iloc[lookback], end_time="2026-12-31")
y_timestamp = pd.Series(cal[:pred_len])
print(f"\ncontext: {x_timestamp.iloc[0].date()} ~ {x_timestamp.iloc[-1].date()} ({len(x_df)} bars)")
print(f"predict: {y_timestamp.iloc[0].date()} ~ {y_timestamp.iloc[-1].date()} ({pred_len} bars)")

# 3. load Kronos-mini + tokenizer-2k
print("\n=== loading Kronos-mini (CPU) ===")
import time
t0 = time.time()
tokenizer = KronosTokenizer.from_pretrained("/home/ubuntu/kronos-models/tokenizer-2k")
model = Kronos.from_pretrained("/home/ubuntu/kronos-models/mini")
predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=2048)
print(f"model loaded in {time.time()-t0:.1f}s")

# 4. predict
print("\n=== predicting (CPU, may take ~30-60s) ===")
t0 = time.time()
pred_df = predictor.predict(
    df=x_df,
    x_timestamp=x_timestamp,
    y_timestamp=y_timestamp,
    pred_len=pred_len,
    T=1.0,
    top_p=0.9,
    sample_count=1,
    verbose=True,
)
print(f"\nprediction done in {time.time()-t0:.1f}s")

# 5. show results
print("\n=== predicted next 20 days (茅台 SH600519) ===")
pred_df.index = y_timestamp
print(pred_df.round(2).to_string())

# 6. compare with actual if available
actual_end = df["datetime"].iloc[-1]
if y_timestamp.iloc[-1] <= actual_end:
    print("\n=== compare with actual ===")
    actual = df.set_index("datetime").loc[y_timestamp, ["open", "high", "low", "close", "volume"]]
    cmp = pd.DataFrame({
        "pred_close": pred_df["close"].values,
        "actual_close": actual["close"].values,
    }, index=y_timestamp)
    cmp["error_pct"] = (cmp["pred_close"] - cmp["actual_close"]) / cmp["actual_close"] * 100
    print(cmp.round(2).to_string())
    mape = (cmp["error_pct"].abs().mean())
    print(f"\nMAPE: {mape:.2f}%")

# 7. save
out = "/home/ubuntu/qlib/results/kronos_mini_demo.csv"
os.makedirs(os.path.dirname(out), exist_ok=True)
pred_df.to_csv(out)
print(f"\nsaved: {out}")
