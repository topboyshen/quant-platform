#!/usr/bin/env python3
"""Validate VIX risk buckets against future CSI 300 drawdowns without lookahead."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/000300.SS"
TENCENT_BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
BUCKETS = ("low", "yellow", "orange", "red")
BUCKET_LABELS = {
    "low": "低(<17)",
    "yellow": "黄[17,20)",
    "orange": "橙[20,25)",
    "red": "红(>=25)",
}
BOOTSTRAP_REPS = 10_000
PERMUTATION_REPS = 5_000
RANDOM_SEED = 20260815


def fetch_bytes(url: str, *, proxy: str | None, timeout: int = 25) -> bytes:
    handlers = []
    if proxy:
        handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(ProxyHandler({}))
    opener = build_opener(*handlers)
    last_error = "unknown"
    for delay in (0, 2, 6, 18):
        if delay:
            time.sleep(delay)
        request = Request(
            url,
            headers={
                "Accept": "application/json,text/csv,*/*",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36 "
                    "QuantDinger-Risk-Validation/1.0"
                ),
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                try:
                    time.sleep(min(30.0, float(retry_after)))
                except (TypeError, ValueError):
                    pass
        except (OSError, TimeoutError, URLError) as exc:
            last_error = type(exc).__name__
    raise RuntimeError(f"download failed: {last_error}")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_vix(payload: bytes) -> pd.DataFrame:
    frame = pd.read_csv(io.BytesIO(payload))
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    frame["date"] = pd.to_datetime(frame["date"], format="%m/%d/%Y", errors="coerce")
    frame["vix_close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "vix_close"]).copy()
    frame = frame[frame["vix_close"] > 0].sort_values("date").drop_duplicates("date")
    frame["vix_ts_utc"] = frame["date"].map(
        lambda value: datetime.combine(value.date(), clock_time(16, 0), tzinfo=NEW_YORK)
        .astimezone(UTC)
    )
    return frame[["date", "vix_close", "vix_ts_utc"]].reset_index(drop=True)


def load_csi300_yahoo(payload: bytes) -> pd.DataFrame:
    document = json.loads(payload.decode("utf-8"))
    result = document["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    lengths = [len(timestamps)] + [len(quote.get(name) or []) for name in ("open", "high", "low", "close")]
    size = min(lengths)
    if size <= 0:
        raise RuntimeError("Yahoo returned no CSI 300 rows")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps[:size],
            "open": (quote.get("open") or [])[:size],
            "high": (quote.get("high") or [])[:size],
            "low": (quote.get("low") or [])[:size],
            "close": (quote.get("close") or [])[:size],
        }
    )
    frame["observed_at"] = pd.to_datetime(frame["timestamp"], unit="s", utc=True).dt.tz_convert(SHANGHAI)
    frame["cn_date"] = frame["observed_at"].dt.date
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["cn_date", "close", "low"]).copy()
    frame = frame[(frame["close"] > 0) & (frame["low"] > 0)]
    frame = frame.sort_values("cn_date").drop_duplicates("cn_date")
    return frame[["cn_date", "open", "high", "low", "close"]].reset_index(drop=True)


def load_csi300_tencent(payload: bytes) -> pd.DataFrame:
    document = json.loads(payload.decode("utf-8"))
    rows = document["data"]["sh000300"].get("day") or []
    frame = pd.DataFrame(
        rows,
        columns=["cn_date", "open", "close", "high", "low", "volume"],
    )
    frame["cn_date"] = pd.to_datetime(frame["cn_date"], errors="coerce").dt.date
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["cn_date", "close", "low"]).copy()
    frame = frame[(frame["close"] > 0) & (frame["low"] > 0)]
    frame = frame.sort_values("cn_date").drop_duplicates("cn_date")
    return frame[["cn_date", "open", "high", "low", "close"]].reset_index(drop=True)


def bucket_for(value: float) -> str:
    if value >= 25:
        return "red"
    if value >= 20:
        return "orange"
    if value >= 17:
        return "yellow"
    return "low"


def align_and_score(csi: pd.DataFrame, vix: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    csi = csi.copy()
    latest_date = max(csi["cn_date"])
    start_date = (pd.Timestamp(latest_date) - pd.DateOffset(years=3)).date()
    csi = csi[csi["cn_date"] >= start_date].copy().reset_index(drop=True)
    csi["cn_open_ts_utc"] = csi["cn_date"].map(
        lambda value: datetime.combine(value, clock_time(8, 30), tzinfo=SHANGHAI).astimezone(UTC)
    )
    aligned = pd.merge_asof(
        csi.sort_values("cn_open_ts_utc"),
        vix.sort_values("vix_ts_utc"),
        left_on="cn_open_ts_utc",
        right_on="vix_ts_utc",
        direction="backward",
    )
    aligned["age_hours"] = (
        aligned["cn_open_ts_utc"] - aligned["vix_ts_utc"]
    ).dt.total_seconds() / 3600
    aligned["bucket"] = aligned["vix_close"].map(bucket_for)
    aligned["high_gate"] = aligned["vix_close"] >= 20

    close = aligned["close"]
    low = aligned["low"]
    aligned["d1"] = np.maximum(0.0, 1.0 - close.shift(-1) / close)
    aligned["d1_low"] = np.maximum(0.0, 1.0 - low.shift(-1) / close)
    future_close_5 = pd.concat([close.shift(-offset) for offset in range(1, 6)], axis=1)
    future_low_5 = pd.concat([low.shift(-offset) for offset in range(1, 6)], axis=1)
    aligned["d5"] = np.maximum(0.0, 1.0 - future_close_5.min(axis=1, skipna=False) / close)
    aligned["d5_low"] = np.maximum(0.0, 1.0 - future_low_5.min(axis=1, skipna=False) / close)

    counts = {
        "csi_rows_3y": int(len(aligned)),
        "missing_vix": int(aligned["vix_close"].isna().sum()),
        "stale_over_120h": int((aligned["age_hours"] > 120).fillna(False).sum()),
        "insufficient_future_5d": int(aligned["d5"].isna().sum()),
    }
    eligible = aligned[
        aligned["vix_close"].notna()
        & aligned["age_hours"].between(0, 120, inclusive="both")
        & aligned["d1"].notna()
        & aligned["d5"].notna()
    ].copy()
    eligible["bucket"] = pd.Categorical(eligible["bucket"], categories=BUCKETS, ordered=True)
    return eligible.reset_index(drop=True), counts


def episode_count(mask: pd.Series) -> int:
    boolean = mask.astype(bool)
    return int((boolean & ~boolean.shift(1, fill_value=False)).sum())


def ratio(high: np.ndarray, all_values: np.ndarray) -> float:
    denominator = float(np.mean(all_values))
    return float(np.mean(high) / denominator) if denominator > 0 and len(high) else math.nan


def moving_block_ci(values: np.ndarray, high_mask: np.ndarray, *, block: int = 5) -> tuple[float, float]:
    rng = np.random.default_rng(RANDOM_SEED)
    n = len(values)
    estimates: list[float] = []
    blocks_needed = math.ceil(n / block)
    offsets = np.arange(block)
    for _ in range(BOOTSTRAP_REPS):
        starts = rng.integers(0, n, size=blocks_needed)
        indexes = ((starts[:, None] + offsets[None, :]) % n).ravel()[:n]
        sampled = values[indexes]
        sampled_high = high_mask[indexes]
        if not sampled_high.any():
            continue
        estimate = ratio(sampled[sampled_high], sampled)
        if math.isfinite(estimate):
            estimates.append(estimate)
    if not estimates:
        return math.nan, math.nan
    return tuple(float(item) for item in np.percentile(estimates, [2.5, 97.5]))


def spearman_circular_p(bucket_codes: np.ndarray, values: np.ndarray) -> tuple[float, float]:
    x_rank = pd.Series(bucket_codes).rank(method="average").to_numpy()
    y_rank = pd.Series(values).rank(method="average").to_numpy()
    observed = float(np.corrcoef(x_rank, y_rank)[0, 1])
    if not math.isfinite(observed) or len(values) < 3:
        return observed, math.nan
    rng = np.random.default_rng(RANDOM_SEED + 1)
    exceed = 0
    for _ in range(PERMUTATION_REPS):
        shift = int(rng.integers(1, len(values)))
        permuted = float(np.corrcoef(x_rank, np.roll(y_rank, shift))[0, 1])
        if abs(permuted) >= abs(observed):
            exceed += 1
    return observed, (exceed + 1) / (PERMUTATION_REPS + 1)


def holm_adjust(pvalues: dict[str, float]) -> dict[str, float]:
    finite = [(key, value) for key, value in pvalues.items() if math.isfinite(value)]
    ordered = sorted(finite, key=lambda item: item[1])
    adjusted: dict[str, float] = {key: math.nan for key in pvalues}
    running = 0.0
    total = len(ordered)
    for index, (key, value) in enumerate(ordered):
        candidate = min(1.0, value * (total - index))
        running = max(running, candidate)
        adjusted[key] = running
    return adjusted


def summarize_buckets(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket in BUCKETS:
        group = frame[frame["bucket"] == bucket]
        rows.append(
            {
                "bucket": bucket,
                "n": int(len(group)),
                "episodes": episode_count(frame["bucket"] == bucket),
                "d1_mean": float(group["d1"].mean()) if len(group) else math.nan,
                "d1_median": float(group["d1"].median()) if len(group) else math.nan,
                "d5_mean": float(group["d5"].mean()) if len(group) else math.nan,
                "d5_median": float(group["d5"].median()) if len(group) else math.nan,
                "d5_q90": float(group["d5"].quantile(0.9)) if len(group) else math.nan,
                "p_d1_2pct": float((group["d1"] >= 0.02).mean()) if len(group) else math.nan,
                "p_d5_5pct": float((group["d5"] >= 0.05).mean()) if len(group) else math.nan,
            }
        )
    return rows


def split_metrics(frame: pd.DataFrame, label: str) -> dict[str, Any]:
    high = frame["high_gate"].to_numpy(dtype=bool)
    result: dict[str, Any] = {
        "label": label,
        "n": int(len(frame)),
        "high_n": int(high.sum()),
        "high_episodes": episode_count(frame["high_gate"]),
        "effective_5d_blocks": int(len(frame) // 5),
    }
    for horizon in ("d1", "d5"):
        values = frame[horizon].to_numpy(dtype=float)
        estimate = ratio(values[high], values)
        ci_low, ci_high = moving_block_ci(values, high)
        result[f"{horizon}_ratio"] = estimate
        result[f"{horizon}_ci_low"] = ci_low
        result[f"{horizon}_ci_high"] = ci_high
        result[f"{horizon}_all_mean"] = float(values.mean())
        result[f"{horizon}_high_mean"] = float(values[high].mean()) if high.any() else math.nan
    return result


def format_pct(value: float) -> str:
    return "暂无" if not math.isfinite(value) else f"{value * 100:.3f}%"


def format_num(value: float, digits: int = 2) -> str:
    return "暂无" if not math.isfinite(value) else f"{value:.{digits}f}"


def render_report(
    frame: pd.DataFrame,
    counts: dict[str, int],
    bucket_rows: list[dict[str, Any]],
    full: dict[str, Any],
    discovery: dict[str, Any],
    holdout: dict[str, Any],
    trend: dict[str, Any],
    etf_sensitivity: dict[str, Any],
    decision: str,
    reasons: list[str],
    hashes: dict[str, str],
    generated_at: datetime,
) -> str:
    bucket_lines = [
        "| VIX档 | n | episode | D1均值 | D5均值 | D5 q90 | P(D1>=2%) | P(D5>=5%) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in bucket_rows:
        bucket_lines.append(
            "| {label} | {n} | {episodes} | {d1} | {d5} | {q90} | {p1} | {p5} |".format(
                label=BUCKET_LABELS[row["bucket"]],
                n=row["n"],
                episodes=row["episodes"],
                d1=format_pct(row["d1_mean"]),
                d5=format_pct(row["d5_mean"]),
                q90=format_pct(row["d5_q90"]),
                p1=format_pct(row["p_d1_2pct"]),
                p5=format_pct(row["p_d5_5pct"]),
            )
        )

    split_lines = [
        "| 样本 | n | VIX>=20 n/episode | D1风险比 (95% CI) | D5风险比 (95% CI) |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in (full, discovery, holdout):
        split_lines.append(
            "| {label} | {n} | {hn}/{episodes} | {r1} ({l1}, {u1}) | {r5} ({l5}, {u5}) |".format(
                label=item["label"],
                n=item["n"],
                hn=item["high_n"],
                episodes=item["high_episodes"],
                r1=format_num(item["d1_ratio"]),
                l1=format_num(item["d1_ci_low"]),
                u1=format_num(item["d1_ci_high"]),
                r5=format_num(item["d5_ratio"]),
                l5=format_num(item["d5_ci_low"]),
                u5=format_num(item["d5_ci_high"]),
            )
        )

    return "\n".join(
        [
            "# VIX 分档对沪深300未来回撤区分度验证",
            "",
            f"生成时间：{generated_at.astimezone(SHANGHAI).isoformat(timespec='seconds')}",
            f"结论：**{decision}**",
            "",
            "## 决策依据",
            "",
            *[f"- {reason}" for reason in reasons],
            "",
            "## 数据与防未来函数口径",
            "",
            f"- 合格A股样本：{len(frame)}，日期 {frame['cn_date'].min()} 至 {frame['cn_date'].max()}。",
            "- VIX：Cboe官方日收盘；每个A股交易日08:30仅使用时间戳不晚于该时刻的最新美东16:00收盘值。",
            "- 沪深300：腾讯行情 sh000300 日OHLC；主结果使用未来1/5交易日最低收盘相对条件日收盘的损失。",
            "- VIX值超过120小时的样本剔除；最后不足5个未来交易日的样本剔除。",
            f"- 剔除计数：{json.dumps(counts, ensure_ascii=False, sort_keys=True)}。",
            "- 固定阈值：<17、17–20、20–25、>=25；未根据结果调参。",
            "",
            "## 分档结果",
            "",
            *bucket_lines,
            "",
            "## 主效应与样本外",
            "",
            *split_lines,
            "",
            "## 趋势检验",
            "",
            (
                f"- D1 Spearman={format_num(trend['d1_rho'], 3)}，"
                f"循环置换p={format_num(trend['d1_p'], 4)}，Holm={format_num(trend['d1_holm'], 4)}。"
            ),
            (
                f"- D5 Spearman={format_num(trend['d5_rho'], 3)}，"
                f"循环置换p={format_num(trend['d5_p'], 4)}，Holm={format_num(trend['d5_holm'], 4)}。"
            ),
            "",
            "## 510300 ETF 稳健性复核",
            "",
            (
                f"- 现有腾讯前复权ETF样本仅覆盖 {etf_sensitivity['date_min']} 至 "
                f"{etf_sensitivity['date_max']}，合格n={etf_sensitivity['n']}，只作方向复核。"
            ),
            (
                f"- VIX>=20：n={etf_sensitivity['high_n']}，"
                f"D1风险比={format_num(etf_sensitivity['d1_ratio'])}，"
                f"D5风险比={format_num(etf_sensitivity['d5_ratio'])}。"
            ),
            "",
            "## 验收规则",
            "",
            "只有全样本D1/D5风险比均>=1.5、95%CI下限>1、样本量与episode达标、趋势显著且最近一年样本外同向>=1.5，才升级为研究门控；否则仅作背景信息。",
            "",
            "## 可复核哈希",
            "",
            f"- Cboe VIX CSV：`{hashes['vix']}`",
            f"- 腾讯 CSI300 JSON：`{hashes['csi300']}`",
            f"- Yahoo交叉核对 JSON：`{hashes['csi300_yahoo']}`",
            f"- 合格对齐样本 CSV：`{hashes['aligned']}`",
            f"- 510300 ETF Parquet：`{hashes['etf_510300']}`",
            "",
            "本验证仅评价风险标签的历史区分度，不构成投资建议，也不触发交易。",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--proxy", default="http://127.0.0.1:7890")
    parser.add_argument(
        "--etf-parquet",
        type=Path,
        default=Path("/home/ubuntu/trading-radar/data/kline_parquet/510300.parquet"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    vix_raw = fetch_bytes(CBOE_URL, proxy=None)
    yahoo_url = YAHOO_BASE + "?" + urlencode(
        {
            "interval": "1d",
            "range": "3y",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    end_date = datetime.now(SHANGHAI).date()
    start_date = end_date - timedelta(days=1110)
    tencent_url = TENCENT_BASE + "?" + urlencode(
        {
            "param": (
                f"sh000300,day,{start_date.isoformat()},"
                f"{end_date.isoformat()},1000,qfq"
            )
        }
    )
    csi_raw = fetch_bytes(tencent_url, proxy=None)
    yahoo_raw = fetch_bytes(yahoo_url, proxy=args.proxy)
    (args.output_dir / "VIX_History.csv").write_bytes(vix_raw)
    (args.output_dir / "csi300_tencent.json").write_bytes(csi_raw)
    (args.output_dir / "csi300_yahoo_chart.json").write_bytes(yahoo_raw)

    vix = load_vix(vix_raw)
    csi = load_csi300_tencent(csi_raw)
    yahoo_crosscheck = load_csi300_yahoo(yahoo_raw)
    aligned, counts = align_and_score(csi, vix)
    if len(aligned) < 500:
        raise RuntimeError(f"insufficient aligned sample: {len(aligned)}")

    aligned_path = args.output_dir / "aligned_sample.csv"
    aligned.to_csv(aligned_path, index=False)
    aligned_hash = hashlib.sha256(aligned_path.read_bytes()).hexdigest()

    latest = max(aligned["cn_date"])
    holdout_start = (pd.Timestamp(latest) - pd.DateOffset(years=1)).date()
    discovery_frame = aligned[aligned["cn_date"] < holdout_start].copy()
    holdout_frame = aligned[aligned["cn_date"] >= holdout_start].copy()

    full = split_metrics(aligned, "全3年")
    discovery = split_metrics(discovery_frame, "前2年发现样本")
    holdout = split_metrics(holdout_frame, "最近1年样本外")
    bucket_rows = summarize_buckets(aligned)

    codes = aligned["bucket"].cat.codes.to_numpy(dtype=float)
    d1_rho, d1_p = spearman_circular_p(codes, aligned["d1"].to_numpy(dtype=float))
    d5_rho, d5_p = spearman_circular_p(codes, aligned["d5"].to_numpy(dtype=float))
    adjusted = holm_adjust({"d1": d1_p, "d5": d5_p})
    trend = {
        "d1_rho": d1_rho,
        "d1_p": d1_p,
        "d1_holm": adjusted["d1"],
        "d5_rho": d5_rho,
        "d5_p": d5_p,
        "d5_holm": adjusted["d5"],
    }

    etf = pd.read_parquet(args.etf_parquet)
    etf["cn_date"] = pd.to_datetime(etf["date"], errors="coerce").dt.date
    for column in ("open", "high", "low", "close"):
        etf[column] = pd.to_numeric(etf[column], errors="coerce")
    etf = etf.dropna(subset=["cn_date", "close", "low"])
    etf_aligned, _ = align_and_score(
        etf[["cn_date", "open", "high", "low", "close"]],
        vix,
    )
    etf_full = split_metrics(etf_aligned, "510300 ETF")
    etf_sensitivity = {
        "date_min": str(etf_aligned["cn_date"].min()),
        "date_max": str(etf_aligned["cn_date"].max()),
        "n": int(len(etf_aligned)),
        "high_n": etf_full["high_n"],
        "d1_ratio": etf_full["d1_ratio"],
        "d5_ratio": etf_full["d5_ratio"],
    }

    sample_ok = full["high_n"] >= 50 and full["high_episodes"] >= 10 and full["effective_5d_blocks"] >= 30
    ratio_ok = full["d1_ratio"] >= 1.5 and full["d5_ratio"] >= 1.5
    ci_ok = full["d1_ci_low"] > 1.0 and full["d5_ci_low"] > 1.0
    holdout_ok = holdout["d1_ratio"] >= 1.5 and holdout["d5_ratio"] >= 1.5
    trend_ok = (
        trend["d1_rho"] > 0
        and trend["d5_rho"] > 0
        and trend["d1_holm"] < 0.05
        and trend["d5_holm"] < 0.05
    )
    if sample_ok and ratio_ok and ci_ok and holdout_ok and trend_ok:
        decision = "通过：可保留为研究门控（仍不得自动交易）"
    elif not ratio_ok or not holdout_ok:
        decision = "未通过：降级为宏观背景信息"
    else:
        decision = "证据不足：仅保留为宏观背景信息"

    reasons = [
        f"全样本VIX>=20：n={full['high_n']}，episode={full['high_episodes']}，样本门槛={'通过' if sample_ok else '未通过'}。",
        (
            f"全样本风险比：D1={format_num(full['d1_ratio'])} "
            f"(95%CI {format_num(full['d1_ci_low'])}–{format_num(full['d1_ci_high'])})；"
            f"D5={format_num(full['d5_ratio'])} "
            f"(95%CI {format_num(full['d5_ci_low'])}–{format_num(full['d5_ci_high'])})。"
        ),
        (
            f"最近一年样本外风险比：D1={format_num(holdout['d1_ratio'])}，"
            f"D5={format_num(holdout['d5_ratio'])}，样本外门槛={'通过' if holdout_ok else '未通过'}。"
        ),
        (
            f"四档趋势Holm校正：D1 p={format_num(trend['d1_holm'], 4)}，"
            f"D5 p={format_num(trend['d5_holm'], 4)}，趋势门槛={'通过' if trend_ok else '未通过'}。"
        ),
    ]

    hashes = {
        "vix": sha256_bytes(vix_raw),
        "csi300": sha256_bytes(csi_raw),
        "csi300_yahoo": sha256_bytes(yahoo_raw),
        "aligned": aligned_hash,
        "etf_510300": hashlib.sha256(args.etf_parquet.read_bytes()).hexdigest(),
    }
    report = render_report(
        aligned,
        counts,
        bucket_rows,
        full,
        discovery,
        holdout,
        trend,
        etf_sensitivity,
        decision,
        reasons,
        hashes,
        datetime.now(UTC),
    )
    report_path = args.output_dir / "VIX_CSI300_VALIDATION_20260815.md"
    report_path.write_text(report + "\n", encoding="utf-8")

    summary = {
        "decision": decision,
        "date_min": str(aligned["cn_date"].min()),
        "date_max": str(aligned["cn_date"].max()),
        "eligible_n": int(len(aligned)),
        "tencent_rows": int(len(csi)),
        "yahoo_crosscheck_rows": int(len(yahoo_crosscheck)),
        "yahoo_crosscheck_date_max": str(yahoo_crosscheck["cn_date"].max()),
        "high_n": full["high_n"],
        "high_episodes": full["high_episodes"],
        "d1_ratio": full["d1_ratio"],
        "d1_ci": [full["d1_ci_low"], full["d1_ci_high"]],
        "d5_ratio": full["d5_ratio"],
        "d5_ci": [full["d5_ci_low"], full["d5_ci_high"]],
        "holdout_d1_ratio": holdout["d1_ratio"],
        "holdout_d5_ratio": holdout["d5_ratio"],
        "trend": trend,
        "etf_sensitivity": etf_sensitivity,
        "hashes": hashes,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
