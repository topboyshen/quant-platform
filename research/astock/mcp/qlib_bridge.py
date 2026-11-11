#!/usr/bin/env python3
"""Read fixed, verified Qlib summaries and historical prediction rankings."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat as stat_module
import sys
from typing import Any

import pandas as pd


RESULTS_ROOT = Path("/opt/ai/research/qlib/results")
MLRUNS_ROOT = Path("/opt/ai/data/mlruns/qlib")
TUNED_SUMMARY = Path("tuned_csi800/port_analysis_summary.csv")
TUNED_REPORT = Path("tuned_csi800/report_normal.csv")
ROLLING_SUMMARY = Path("rolling_stitched/segment_summary.csv")
ROLLING_REPORT = Path("rolling_stitched/stitched_report.csv")
PREDICTION_ARTIFACT = Path("artifacts/pred.pkl")
PREDICTION_COMPANION = Path("artifacts/portfolio_analysis/report_normal_1day.pkl")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PREDICTION_FILE_BYTES = 32 * 1024 * 1024
MAX_SYMBOLS = 10
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactPins:
    experiment_id: str
    run_id: str
    prediction_sha256: str
    companion_sha256: str
    tuned_report_sha256: str
    tuned_summary_sha256: str


DEFAULT_ARTIFACT_PINS = ArtifactPins(
    experiment_id="1",
    run_id="4e23d21a8118423787052ff83d00ae56",
    prediction_sha256="bae8ef96b18e60aa0881c75820c42936708c2dfa135b04ff26d284269c50d469",
    companion_sha256="901ad772b2c6e87d2045fe159591864aa216a45be999b865f4f5190db9a2de07",
    tuned_report_sha256="28e868d2627a77d1c78106ea622c739b1c7fbb7965bc74231676320d13a7fa21",
    tuned_summary_sha256="b697d2b769db16241fa91c010c9c03f1076c75ca7b91bd606c39a5eb558d444f",
)


def checked_file(root: Path, relative: Path, *, max_bytes: int = MAX_FILE_BYTES) -> Path:
    """Return a bounded ordinary file below a fixed root, never a symlink."""

    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Qlib artifact path must be relative to its fixed root")
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"Qlib artifact root is not a directory: {root}")
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Qlib artifact path must not contain symlinks: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except FileNotFoundError:
        raise FileNotFoundError(f"required Qlib artifact is missing: {relative}") from None
    except ValueError:
        raise ValueError(f"Qlib artifact escapes its fixed root: {relative}") from None
    file_stat = resolved.stat(follow_symlinks=False)
    if not stat_module.S_ISREG(file_stat.st_mode):
        raise ValueError(f"Qlib artifact is not an ordinary file: {relative}")
    if file_stat.st_size > max_bytes:
        raise ValueError(f"Qlib artifact exceeds {max_bytes} bytes: {relative}")
    return resolved


def parse_float(value: str) -> float | str | None:
    try:
        parsed = float(value)
    except ValueError:
        return value
    return parsed if math.isfinite(parsed) else None


def read_tuned_summary(path: Path) -> dict[str, dict[str, float | str | None]]:
    output: dict[str, dict[str, float | str | None]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 3 or row[0] == "":
                continue
            output.setdefault(row[0], {})[row[1]] = parse_float(row[2])
    return output


def read_rolling_summary(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append(
                {
                    key: int(value) if key == "days" else parse_float(value)
                    for key, value in row.items()
                    if key is not None and value is not None
                }
            )
        return rows[:12]


def last_market_date(path: Path) -> str | None:
    latest: str | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if row and DATE_PATTERN.fullmatch(row[0]) and (latest is None or row[0] > latest):
                latest = row[0]
    return latest


def file_metadata(root: Path, relative: Path) -> dict[str, Any]:
    path = checked_file(root, relative)
    file_stat = path.stat()
    return {
        "artifact": relative.as_posix(),
        "bytes": file_stat.st_size,
        "modifiedAt": datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat(),
    }


def validate_control(control: Any) -> tuple[str | None, list[str]]:
    """Validate the entire caller-controlled object before touching artifacts."""

    if not isinstance(control, dict):
        raise ValueError("control input must be a JSON object")
    unexpected = set(control) - {"targetDate", "symbols"}
    if unexpected:
        raise ValueError(f"only targetDate and symbols are accepted; rejected: {', '.join(sorted(unexpected))}")

    target_date = control.get("targetDate")
    if target_date is not None:
        if not isinstance(target_date, str) or not DATE_PATTERN.fullmatch(target_date):
            raise ValueError("targetDate must be a real YYYY-MM-DD date")
        try:
            date.fromisoformat(target_date)
        except ValueError:
            raise ValueError("targetDate must be a real YYYY-MM-DD date") from None

    raw_symbols = control.get("symbols", [])
    if not isinstance(raw_symbols, list) or len(raw_symbols) > MAX_SYMBOLS:
        raise ValueError("symbols must be an array containing at most 10 stock symbols")
    symbols: list[str] = []
    for symbol in raw_symbols:
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"invalid controlled stock symbol: {symbol!r}")
        symbols.append(symbol)
    if len(set(symbols)) != len(symbols):
        raise ValueError("symbols must not contain duplicates")
    return target_date, symbols


def _validate_artifact_pins(pins: ArtifactPins) -> None:
    if not pins.experiment_id.isdigit():
        raise ValueError("pinned Qlib experiment id is invalid")
    if not RUN_ID_PATTERN.fullmatch(pins.run_id):
        raise ValueError("pinned Qlib run id is invalid")
    for digest in (
        pins.prediction_sha256,
        pins.companion_sha256,
        pins.tuned_report_sha256,
        pins.tuned_summary_sha256,
    ):
        if not SHA256_PATTERN.fullmatch(digest):
            raise ValueError("pinned Qlib SHA-256 is invalid")


def _open_bounded_file(path: Path, max_bytes: int) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        if not stat_module.S_ISREG(opened_stat.st_mode):
            raise ValueError("Qlib artifact changed to a non-ordinary file before reading")
        if opened_stat.st_size > max_bytes:
            raise ValueError("Qlib artifact exceeded its size limit before reading")
        return descriptor, opened_stat
    except Exception:
        os.close(descriptor)
        raise


def _sha256_file(path: Path, *, max_bytes: int) -> str:
    descriptor, _ = _open_bounded_file(path, max_bytes)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str, *, max_bytes: int, label: str) -> None:
    if _sha256_file(path, max_bytes=max_bytes) != expected:
        raise ValueError(f"{label} SHA-256 does not match the fixed pin")


def _prediction_dates(frame: pd.DataFrame) -> tuple[pd.DatetimeIndex, int, int]:
    if not isinstance(frame.index, pd.MultiIndex) or frame.index.nlevels != 2:
        raise ValueError("pred.pkl must use a two-level (datetime, instrument) index")
    names = list(frame.index.names)
    if "datetime" not in names or "instrument" not in names:
        raise ValueError("pred.pkl index levels must be named datetime and instrument")
    date_level = names.index("datetime")
    instrument_level = names.index("instrument")
    dates = pd.to_datetime(frame.index.get_level_values(date_level), errors="coerce")
    if not isinstance(dates, pd.DatetimeIndex) or dates.isna().any():
        raise ValueError("pred.pkl contains invalid datetime index values")
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    return dates.normalize(), date_level, instrument_level


def _load_prediction_slice(
    path: Path, target_date: str | None, expected_sha256: str
) -> dict[str, Any]:
    # Pickle can execute code while loading. This is intentionally the sole
    # deserialization site: callers cannot supply a path or pin, the file must
    # be an ordinary bounded file at the fixed run path, and its pinned SHA-256
    # is checked on the same open descriptor before pandas sees any bytes.
    # These checks do not make arbitrary pickle safe; they establish the trusted
    # immutable local-artifact boundary required for this legacy Qlib artifact.
    descriptor, _ = _open_bounded_file(path, MAX_PREDICTION_FILE_BYTES)
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError("pred.pkl SHA-256 does not match the fixed pin")
            handle.seek(0)
            value = pd.read_pickle(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if isinstance(value, pd.Series):
        frame = value.rename("score").to_frame()
    elif isinstance(value, pd.DataFrame):
        frame = value
    else:
        raise ValueError("pred.pkl must contain a pandas DataFrame or Series")
    if "score" not in frame.columns:
        raise ValueError("pred.pkl does not contain the required score column")

    normalized_dates, _, instrument_level = _prediction_dates(frame)
    if len(normalized_dates) == 0:
        raise ValueError("pred.pkl is empty")
    prediction_start = normalized_dates.min().date().isoformat()
    prediction_end = normalized_dates.max().date().isoformat()
    cutoff = pd.Timestamp(target_date) if target_date is not None else normalized_dates.max()
    eligible = normalized_dates <= cutoff
    if not eligible.any():
        return {
            "predictionStart": prediction_start,
            "predictionEnd": prediction_end,
            "dataAsOf": None,
            "scores": None,
            "invalidScoreCount": 0,
        }

    selected_date = normalized_dates[eligible].max()
    selected_mask = normalized_dates == selected_date
    selected_scores = pd.to_numeric(frame.loc[selected_mask, "score"], errors="coerce")
    instruments = frame.index.get_level_values(instrument_level)[selected_mask].astype(str)
    section = pd.DataFrame(
        {"instrument": list(instruments), "score": selected_scores.to_numpy(copy=False)}
    )
    finite = section["score"].map(lambda value: pd.notna(value) and math.isfinite(float(value)))
    invalid_score_count = int((~finite).sum())
    section = section.loc[finite].copy()
    if section.empty:
        raise ValueError("selected pred.pkl cross-section has no finite scores")
    section["instrument"] = section["instrument"].map(_canonical_qlib_symbol)
    if section["instrument"].duplicated().any():
        raise ValueError("selected pred.pkl cross-section contains duplicate instruments")
    section["rank"] = section["score"].rank(method="min", ascending=False).astype(int)
    # Empirical percentile: the lowest score is 1/N and the highest score is 1.
    section["percentile"] = section["score"].rank(method="average", pct=True, ascending=True)
    section.set_index("instrument", inplace=True)
    return {
        "predictionStart": prediction_start,
        "predictionEnd": prediction_end,
        "dataAsOf": selected_date.date().isoformat(),
        "scores": section,
        "invalidScoreCount": invalid_score_count,
    }


def _canonical_qlib_symbol(value: Any) -> str:
    text = str(value).strip().upper()
    qlib_match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", text)
    if qlib_match:
        return f"{qlib_match.group(2)}.{qlib_match.group(1)}"
    if SYMBOL_PATTERN.fullmatch(text):
        return text
    return text


def read_prediction_ranking(
    *,
    tuned_report_path: Path,
    tuned_summary_path: Path,
    mlruns_root: Path,
    target_date: str | None,
    symbols: list[str],
    pins: ArtifactPins,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not symbols:
        return None, []
    _validate_artifact_pins(pins)
    warnings: list[str] = []
    try:
        _verify_sha256(
            tuned_report_path,
            pins.tuned_report_sha256,
            max_bytes=MAX_FILE_BYTES,
            label="tuned report_normal.csv",
        )
        _verify_sha256(
            tuned_summary_path,
            pins.tuned_summary_sha256,
            max_bytes=MAX_FILE_BYTES,
            label="tuned port_analysis_summary.csv",
        )
    except Exception as exc:
        return None, [f"Qlib 预测排名不可用：tuned CSV 产物未通过固定 SHA-256 校验（{exc}）。"]
    tuned_as_of = last_market_date(tuned_report_path)
    if tuned_as_of is None:
        return None, ["Qlib 预测排名不可用：tuned report 无法解析出受控截至日。"]

    relative_pred = Path(pins.experiment_id) / pins.run_id / PREDICTION_ARTIFACT
    relative_companion = Path(pins.experiment_id) / pins.run_id / PREDICTION_COMPANION
    try:
        pred_path = checked_file(
            mlruns_root, relative_pred, max_bytes=MAX_PREDICTION_FILE_BYTES
        )
        companion_path = checked_file(
            mlruns_root, relative_companion, max_bytes=MAX_PREDICTION_FILE_BYTES
        )
        _verify_sha256(
            companion_path,
            pins.companion_sha256,
            max_bytes=MAX_PREDICTION_FILE_BYTES,
            label="report_normal_1day.pkl",
        )
        loaded = _load_prediction_slice(
            pred_path, target_date, pins.prediction_sha256
        )
    except Exception as exc:
        warnings.append(
            "Qlib 预测排名不可用：固定 pinned run 产物缺失、不安全、超限或哈希不匹配"
            f"（{exc.__class__.__name__}: {exc}）；历史 CSV 回测摘要仍可用。"
        )
        return None, warnings

    if loaded["predictionEnd"] != tuned_as_of:
        warnings.append(
            "Qlib 预测排名不可用：固定 pred.pkl 截至日 "
            f"{loaded['predictionEnd']} 与 tuned report 截至日 {tuned_as_of} 不一致；"
            "历史 CSV 回测摘要仍可用。"
        )
        return None, warnings
    if loaded["scores"] is None:
        warnings.append(
            "Qlib 预测排名不可用：targetDate 早于所选预测产物的首个截面 "
            f"{loaded['predictionStart']}；历史 CSV 回测摘要仍可用。"
        )
        return None, warnings
    if loaded["invalidScoreCount"]:
        warnings.append(
            f"所选 Qlib 截面有 {loaded['invalidScoreCount']} 个非有限 score，排名时已排除。"
        )

    scores: pd.DataFrame = loaded["scores"]
    universe_count = int(len(scores))
    symbol_rows: list[dict[str, Any]] = []
    missing_symbols: list[str] = []
    for symbol in symbols:
        if symbol not in scores.index:
            missing_symbols.append(symbol)
            symbol_rows.append(
                {
                    "symbol": symbol,
                    "available": False,
                    "rawScore": None,
                    "rank": None,
                    "percentile": None,
                    "universeCount": universe_count,
                    "dataAsOf": loaded["dataAsOf"],
                }
            )
            continue
        row = scores.loc[symbol]
        symbol_rows.append(
            {
                "symbol": symbol,
                "available": True,
                "rawScore": float(row["score"]),
                "rank": int(row["rank"]),
                "percentile": float(row["percentile"]),
                "universeCount": universe_count,
                "dataAsOf": loaded["dataAsOf"],
            }
        )
    if missing_symbols:
        warnings.append("请求股票不在所选 Qlib 截面中：" + ", ".join(missing_symbols))

    ranking = {
        "dataAsOf": loaded["dataAsOf"],
        "universeCount": universe_count,
        "symbols": symbol_rows,
        "artifact": {
            "artifact": relative_pred.as_posix(),
            "experimentId": pins.experiment_id,
            "runId": pins.run_id,
            "bytes": pred_path.stat().st_size,
            "modifiedAt": datetime.fromtimestamp(
                pred_path.stat().st_mtime, timezone.utc
            ).isoformat(),
            "matchedBy": "fixed experiment/run pins, four SHA-256 pins, and prediction end date",
            "predictionSha256": pins.prediction_sha256,
            "companionSha256": pins.companion_sha256,
            "tunedReportSha256": pins.tuned_report_sha256,
            "tunedSummarySha256": pins.tuned_summary_sha256,
            "predictionEnd": loaded["predictionEnd"],
            "tunedReportDataAsOf": tuned_as_of,
        },
    }
    return ranking, warnings


def build_context(
    root: Path = RESULTS_ROOT,
    *,
    mlruns_root: Path = MLRUNS_ROOT,
    target_date: str | None = None,
    symbols: list[str] | None = None,
    artifact_pins: ArtifactPins = DEFAULT_ARTIFACT_PINS,
) -> dict[str, Any]:
    _validate_artifact_pins(artifact_pins)
    tuned_summary_path = checked_file(root, TUNED_SUMMARY)
    rolling_summary_path = checked_file(root, ROLLING_SUMMARY)
    tuned_report_path = checked_file(root, TUNED_REPORT)
    rolling_report_path = checked_file(root, ROLLING_REPORT)
    warnings: list[str] = []
    tuned_summary_verified = True
    tuned_report_verified = True
    try:
        _verify_sha256(
            tuned_summary_path,
            artifact_pins.tuned_summary_sha256,
            max_bytes=MAX_FILE_BYTES,
            label="tuned port_analysis_summary.csv",
        )
    except Exception as exc:
        tuned_summary_verified = False
        warnings.append(f"tuned CSI800 回测摘要未通过固定 SHA-256 校验，摘要已停用（{exc}）。")
    try:
        _verify_sha256(
            tuned_report_path,
            artifact_pins.tuned_report_sha256,
            max_bytes=MAX_FILE_BYTES,
            label="tuned report_normal.csv",
        )
    except Exception as exc:
        tuned_report_verified = False
        warnings.append(f"tuned CSI800 回测报告未通过固定 SHA-256 校验，报告已停用（{exc}）。")

    tuned_date = last_market_date(tuned_report_path) if tuned_report_verified else None
    rolling_date = last_market_date(rolling_report_path)
    dates = [
        value
        for value in (tuned_date, rolling_date)
        if value is not None
    ]
    summary_as_of = max(dates) if dates else None
    if tuned_summary_verified and tuned_report_verified:
        prediction_ranking, ranking_warnings = read_prediction_ranking(
            tuned_report_path=tuned_report_path,
            tuned_summary_path=tuned_summary_path,
            mlruns_root=mlruns_root,
            target_date=target_date,
            symbols=list(symbols or []),
            pins=artifact_pins,
        )
        warnings.extend(ranking_warnings)
    else:
        prediction_ranking = None
        if symbols:
            warnings.append(
                "Qlib 预测排名不可用：tuned CSV 产物未全部通过固定 SHA-256 校验。"
            )
    context = {
        "schemaVersion": "1.0",
        "provider": "Qlib local artifacts",
        "mode": "read_only_verified_backtest_and_ranking_artifacts",
        "requestedOperation": "run_backtest",
        "operationSemantics": (
            "run_backtest 仅读取最近验证过的 Qlib 历史回测摘要和预测排名产物；"
            "本请求不会启动训练、动态回测或任何文件写入。"
        ),
        "dataAsOf": summary_as_of,
        "tunedCsi800": (
            read_tuned_summary(tuned_summary_path) if tuned_summary_verified else {}
        ),
        "rollingSegments": read_rolling_summary(rolling_summary_path),
        "predictionRanking": prediction_ranking,
        "integrity": {
            "tunedSummarySha256Verified": tuned_summary_verified,
            "tunedReportSha256Verified": tuned_report_verified,
            "pinnedExperimentId": artifact_pins.experiment_id,
            "pinnedRunId": artifact_pins.run_id,
        },
        "artifacts": [
            file_metadata(root, relative)
            for relative in (TUNED_SUMMARY, TUNED_REPORT, ROLLING_SUMMARY, ROLLING_REPORT)
        ],
        "warnings": warnings,
        "limitations": [
            "这些是历史回测和历史预测产物，不是实时行情或今日预测。",
            "score 只用于同一历史截面的横截面相对排名，不构成个股买卖信号。",
            "不得把回测收益外推为未来收益；分析时必须同时报告回撤和样本截止日。",
        ],
    }
    if target_date and summary_as_of and summary_as_of > target_date:
        context["usableForTargetDate"] = False
        context["asOfWarning"] = "Qlib 回测摘要晚于目标日期，禁止用于该日期的时点分析。"
    else:
        context["usableForTargetDate"] = True
    return context


def run_bridge(
    control: Any,
    *,
    results_root: Path = RESULTS_ROOT,
    mlruns_root: Path = MLRUNS_ROOT,
    artifact_pins: ArtifactPins = DEFAULT_ARTIFACT_PINS,
) -> dict[str, Any]:
    # Root and pin overrides exist only for dependency injection in tests.
    # main() never derives a root, run id, digest, or path from controlled JSON.
    target_date, symbols = validate_control(control)
    return build_context(
        results_root,
        mlruns_root=mlruns_root,
        target_date=target_date,
        symbols=symbols,
        artifact_pins=artifact_pins,
    )


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise ValueError("control input exceeds 16 KiB")
        control = json.loads(raw.decode("utf-8") or "{}")
        context = run_bridge(control)
        json.dump(context, sys.stdout, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"Qlib bridge failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
