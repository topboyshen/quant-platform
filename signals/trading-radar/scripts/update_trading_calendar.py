#!/usr/bin/env python3
"""Atomically refresh the local A-share trading calendar from AkShare."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "trading_calendar.json"
SOURCE = "akshare.tool_trade_date_hist_sina"
DEFAULT_MAX_AGE_DAYS = 14
MAX_FUTURE_DAYS = 550
RECENT_WINDOW_DAYS = 60
MIN_RECENT_TRADING_DAYS = 20
MAX_RECENT_GAP_DAYS = 14
LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _normalise_dates(values) -> list[str]:
    dates: set[str] = set()
    for value in values:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date):
            day = value
        else:
            day = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        dates.add(day.isoformat())
    return sorted(dates)


def _covers_through(last_trading_day: date, target: date) -> bool:
    """A last Friday also covers a weekend target, but never assumes a weekday closure."""
    if last_trading_day >= target:
        return True
    cursor = last_trading_day + timedelta(days=1)
    while cursor <= target:
        if cursor.weekday() < 5:
            return False
        cursor += timedelta(days=1)
    return True


def _validate_calendar_dates(dates: list[str], now: datetime) -> None:
    if len(dates) < 1000:
        raise ValueError(f"交易日历样本过少: {len(dates)}")

    parsed = [date.fromisoformat(value) for value in dates]
    first_day, last_day = parsed[0], parsed[-1]
    if not _covers_through(last_day, now.date()):
        raise ValueError(f"交易日历已过期: max_date={last_day}")
    if last_day > now.date() + timedelta(days=MAX_FUTURE_DAYS):
        raise ValueError(f"交易日历远期边界异常: max_date={last_day}")
    if first_day >= last_day:
        raise ValueError("交易日历日期范围异常")

    recent_floor = last_day - timedelta(days=RECENT_WINDOW_DAYS)
    recent = [day for day in parsed if day >= recent_floor]
    if len(recent) < MIN_RECENT_TRADING_DAYS:
        raise ValueError(f"交易日历近期样本过于稀疏: {len(recent)}")
    if any(day.weekday() >= 5 for day in recent):
        raise ValueError("交易日历近期包含周末日期")
    largest_gap = max((right - left).days for left, right in zip(recent, recent[1:]))
    if largest_gap > MAX_RECENT_GAP_DAYS:
        raise ValueError(f"交易日历近期断层异常: {largest_gap} 天")


def _load_existing(path: Path, *, now: datetime | None = None) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        raw_dates = payload.get("trading_dates")
        if not isinstance(raw_dates, list):
            return {}
        dates = _normalise_dates(raw_dates)
        _validate_calendar_dates(dates, now or _now())
        if payload.get("min_date") != dates[0] or payload.get("max_date") != dates[-1]:
            return {}
        if payload.get("count") not in (None, len(dates)):
            return {}
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _is_fresh(payload: dict, now: datetime, max_age_days: int) -> bool:
    try:
        generated_at = datetime.fromisoformat(str(payload["generated_at"]))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=now.tzinfo)
        else:
            generated_at = generated_at.astimezone(now.tzinfo)
        age = now - generated_at
        return -timedelta(minutes=5) <= age <= timedelta(days=max_age_days)
    except (KeyError, TypeError, ValueError):
        return False


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def refresh(
    output: Path,
    *,
    force: bool,
    min_ahead_days: int,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> int:
    now = _now()
    required_date = now.date() + timedelta(days=min_ahead_days)
    existing = _load_existing(output, now=now)
    existing_max = str(existing.get("max_date") or "")
    existing_max_day = date.fromisoformat(existing_max) if existing_max else None
    if (
        not force
        and existing_max_day is not None
        and _covers_through(existing_max_day, required_date)
        and _is_fresh(existing, now, max_age_days)
    ):
        LOGGER.info(
            "交易日历覆盖至 %s，且缓存未超过 %s 天，跳过联网更新",
            existing_max,
            max_age_days,
        )
        return 0

    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    if "trade_date" not in frame.columns:
        raise RuntimeError(f"AkShare 返回缺少 trade_date 列: {list(frame.columns)}")
    dates = _normalise_dates(frame["trade_date"].tolist())
    try:
        _validate_calendar_dates(dates, now)
    except ValueError as exc:
        raise RuntimeError(f"AkShare 交易日历校验失败: {exc}") from exc
    fetched_max_day = date.fromisoformat(dates[-1])
    if not _covers_through(fetched_max_day, required_date):
        raise RuntimeError(
            "AkShare 交易日历覆盖不足: "
            f"max_date={dates[-1]}, required_through={required_date}"
        )
    if existing_max_day is not None and fetched_max_day < existing_max_day:
        raise RuntimeError(
            "AkShare 交易日历覆盖退化，拒绝覆盖旧缓存: "
            f"fetched={fetched_max_day}, existing={existing_max_day}"
        )

    payload = {
        "schema_version": 1,
        "source": SOURCE,
        "generated_at": now.isoformat(timespec="seconds"),
        "count": len(dates),
        "min_date": dates[0],
        "max_date": dates[-1],
        "trading_dates": dates,
    }
    _write_atomic(output, payload)
    LOGGER.info(
        "交易日历更新完成: %s 日，%s..%s",
        len(dates),
        dates[0],
        dates[-1],
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="更新 Radar A 股交易日历缓存")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-ahead-days", type=int, default=30)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    args = parser.parse_args()
    if not 0 <= args.min_ahead_days <= 366:
        parser.error("--min-ahead-days 必须在 0..366")
    if not 1 <= args.max_age_days <= 366:
        parser.error("--max-age-days 必须在 1..366")

    os.umask(0o027)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name(f".{output.name}.update.lock")
    lock_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, lock_flags, 0o640)
    with os.fdopen(lock_fd, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.info("另一个交易日历更新正在运行，本次跳过")
            return 0
        return refresh(
            output,
            force=args.force,
            min_ahead_days=args.min_ahead_days,
            max_age_days=args.max_age_days,
        )


if __name__ == "__main__":
    raise SystemExit(main())
