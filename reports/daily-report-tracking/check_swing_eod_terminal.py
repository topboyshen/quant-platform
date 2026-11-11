#!/usr/bin/env python3
"""15:40/15:50 watchdog for the daily Swing EOD terminal audit."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import tempfile
import traceback
from datetime import date, datetime, time as daytime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from radar.core.scan_scheduler import HolidayCalendar
from radar.notify.health import write_notification_health
from radar.notify.outbox import DurableOutboxNotifier
from radar.notify.result import DeliveryResult
from radar.notify.telegram import TelegramNotifier
from radar.secrets import merge_env_into_config_data
from radar.swing.daily_report import build_daily_report, notification_plan, persist_daily_report
from radar.swing.store import SwingStore
from runtime_contracts.eod import classify_day_status, retryable_data_failure

BEIJING = ZoneInfo("Asia/Shanghai")
# Same-day data_unavailable retry runs at 15:48. The 15:40 watchdog
# must not page that expected gap; 15:50 still fails closed if retry did not.
RETRY_ALERT_CUTOFF = daytime(15, 48)


def routine_summary_enabled(now: datetime) -> bool:
    """Delegate routine delivery only when the existing summary control is valid."""
    path = Path("/home/ubuntu/releases/daily-research-summary-20261012.1/control.json")
    try:
        if not isinstance(now, datetime) or now.tzinfo is None:
            return False
        if path.is_symlink() or path.stat().st_size > 16384:
            return False
        control = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(control, dict) or control.get("enabled") is not True
                or control.get("contract") != "daily-research-summary.v1"
                or control.get("accountId") != "1"):
            return False
        effective = control.get("effectiveDate")
        fingerprint = control.get("targetFingerprint")
        if (not isinstance(effective, str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", effective)
                or not isinstance(fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
            return False
        parsed = date.fromisoformat(effective)
        return parsed.isoformat() == effective and parsed <= now.astimezone(BEIJING).date()
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return False


def summary_consumer_availability(now: datetime) -> Dict[str, Any]:
    """Load the accepted fixed release helper without bytecode or state writes."""
    import types
    try:
        path = Path("/home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/summary_availability.py")
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("linked_availability_helper")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("availability_helper_not_regular")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("availability_helper_limit")
        helper = types.ModuleType("_fixed_summary_availability")
        helper.__file__ = str(path)
        exec(compile(raw, str(path), "exec"), helper.__dict__)
        return helper.checked_probe(helper.probe, business_date=now.astimezone(BEIJING).date().isoformat(), now=now)
    except Exception:
        return {"available": False, "reason": "availability_probe_failed", "status": "unknown",
                "checkedAt": now.isoformat()}


def summary_batch_coverage(run: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Inspect only existing, exact-bound delivery evidence; never send or write."""
    import types
    try:
        path = Path("/home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/etf_summary_coverage.py")
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("linked_coverage_helper")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o644)):
                raise ValueError("coverage_helper_identity")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("coverage_helper_limit")
        helper = types.ModuleType("_fixed_etf_summary_coverage")
        helper.__file__ = str(path)
        exec(compile(raw, str(path), "exec"), helper.__dict__)
        return helper.inspect_coverage(run, business_date=now.astimezone(BEIJING).date().isoformat(), now=now)
    except Exception:
        return {"schema": "quant.etf-delivery-coverage.v1", "status": "unknown",
                "reason": "coverage_unavailable", "businessDate": now.astimezone(BEIJING).date().isoformat()}


def _config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return merge_env_into_config_data(json.load(handle))


def _attempt_view(run: Dict[str, Any] | None) -> Optional[Dict[str, Any]]:
    if not run:
        return None
    return {
        "run_id": run.get("run_id"),
        "decision_date": run.get("decision_date"),
        "status": run.get("status"),
        "decision_outcome": run.get("decision_outcome"),
        "completed_at": run.get("completed_at"),
        "evaluated_count": run.get("evaluated_count"),
        "failed_count": run.get("failed_count"),
        "signal_count": run.get("signal_count"),
        "allowed_signal_count": run.get("allowed_signal_count"),
        "blocked_signal_count": run.get("blocked_signal_count"),
    }


def summarize_attempts(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Describe first 15:30 attempt vs latest terminal run. rows are newest-first."""
    if not rows:
        return {
            "attempts": 0,
            "first": None,
            "latest": None,
            "recovered_after_data_gap": False,
        }
    latest = rows[0]
    first = rows[-1]
    return {
        "attempts": len(rows),
        "first": _attempt_view(first),
        "latest": _attempt_view(latest),
        "recovered_after_data_gap": (
            (str(first.get("status") or "") == "failed"
             or (retryable_data_failure(first, decision_date=latest.get("decision_date"))
                 and isinstance(latest.get("run_id"), str)
                 and bool(latest.get("run_id"))
                 and latest.get("run_id") != first.get("run_id")))
            and str(first.get("decision_outcome") or "") == "data_unavailable"
            and str(latest.get("status") or "") == "success"
        ),
    }


def format_attempt_summary(summary: Dict[str, Any]) -> str:
    first = summary.get("first") or {}
    latest = summary.get("latest") or {}
    return (
        f"attempts={summary.get('attempts', 0)} "
        f"first={first.get('status')}/{first.get('decision_outcome')}"
        f"@{first.get('completed_at')} "
        f"latest={latest.get('status')}/{latest.get('decision_outcome')}"
        f"@{latest.get('completed_at')}"
    )


def persist_watchdog_status(store_file: str, payload: Dict[str, Any]) -> None:
    path = Path(store_file).expanduser().resolve().parent / "swing_eod_watchdog.json"
    fd, temporary = tempfile.mkstemp(prefix=".swing_eod_watchdog.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def persist_result(store_file: str, result: Dict[str, Any], now: datetime) -> None:
    payload = {**classify_day_status(result), "ok": result["ok"],
               "reason": result["reason"], "checked_at": now.isoformat(),
               "trade_date": now.date().isoformat(), "deferred": bool(result.get("deferred")),
               "attempts": result["attempts"], "message": result["message"]}
    for key in ("delivery_state", "summary_consumer", "handoff_status", "alert_sent", "alert_confirmed"):
        if key in result:
            payload[key] = result[key]
    persist_watchdog_status(store_file, payload)


WATCHDOG_RUN_FILE = "swing_eod_watchdog_run.json"


def _run_failure_location(exc: BaseException) -> str:
    """'file.py:line in func' of the innermost frame. Never includes the message,
    which may embed URLs/tokens from transport errors."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"


def persist_watchdog_run_status(
    store_file: str, now: datetime, status: str, exc: BaseException | None = None
) -> None:
    """Independent of the business artifact: records whether THIS program ran to completion.

    status is "ok" (run() returned) or "failed" (run() raised). The Prometheus textfile
    exporter turns it into radar_swing_eod_watchdog_run_* metrics, so a watchdog /
    daily-report crash is visible even though the process that would alert is dead.
    """
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "status": status,
        "trade_date": now.date().isoformat(),
        "checked_at": now.isoformat(),
    }
    if exc is not None:
        payload["error_type"] = type(exc).__name__
        payload["error_location"] = _run_failure_location(exc)
    path = Path(store_file).expanduser().resolve().parent / WATCHDOG_RUN_FILE
    fd, temporary = tempfile.mkstemp(prefix=".swing_eod_watchdog_run.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inspect_terminal_audit(
    store: SwingStore, *, decision_date: str, now: datetime, max_running_minutes: int = 8
) -> Dict[str, Any]:
    rows = store.get_eod_runs_for_date(decision_date)
    attempts = summarize_attempts(rows)
    if not rows:
        return {
            "ok": False,
            "reason": "missing",
            "message": "当天没有任何 EOD 审计记录",
            "attempts": attempts,
        }
    latest = rows[0]
    status = str(latest.get("status") or "")
    if status == "running":
        try:
            started = datetime.fromisoformat(str(latest["started_at"]).replace("Z", "+00:00"))
            age_minutes = max(0, (now.astimezone(started.tzinfo) - started).total_seconds() / 60)
        except (TypeError, ValueError):
            age_minutes = float(max_running_minutes)
        if age_minutes >= max_running_minutes:
            return {
                "ok": False,
                "reason": "running_timeout",
                "message": f"EOD 仍为 running，已持续 {age_minutes:.1f} 分钟",
                "run": latest,
                "attempts": attempts,
            }
        return {
            "ok": False,
            "reason": "running",
            "message": f"EOD 尚未形成终态，已运行 {age_minutes:.1f} 分钟",
            "run": latest,
            "attempts": attempts,
        }
    if status != "success":
        return {
            "ok": False,
            "reason": status or "invalid_status",
            "message": (
                f"EOD 终态为 {status or 'unknown'}，"
                f"outcome={latest.get('decision_outcome')}, "
                f"reasons={latest.get('reason_codes')}"
            ),
            "run": latest,
            "attempts": attempts,
        }
    evidence = store.get_run_evidence_counts(str(latest["run_id"]))
    if (
        evidence["target_decisions"] != int(latest.get("target_count") or 0)
        or evidence["risk_snapshots"] != 1
    ):
        return {
            "ok": False,
            "reason": "evidence_incomplete",
            "message": (
                f"运行虽为 success，但不可变证据不完整：逐标的 "
                f"{evidence['target_decisions']}/{latest.get('target_count')}，"
                f"风控快照 {evidence['risk_snapshots']}/1"
            ),
            "run": latest,
            "attempts": attempts,
        }
    return {
        "ok": True,
        "reason": "success",
        "message": (
            f"EOD success，逐标的证据 {evidence['target_decisions']}/"
            f"{latest.get('target_count')}，outcome={latest.get('decision_outcome')}"
        ),
        "run": latest,
        "attempts": attempts,
    }


def defer_until_retry(result: Dict[str, Any], now: datetime) -> bool:
    """True when 15:40 is looking at the expected 15:30 data_unavailable gap."""
    if result.get("ok") or result.get("reason") not in {"failed", "partial"}:
        return False
    run = result.get("run") or {}
    local = now if now.tzinfo else now.replace(tzinfo=BEIJING)
    local = local.astimezone(BEIJING)
    if (result.get("reason") != run.get("status")
            or not retryable_data_failure(run, decision_date=local.date().isoformat())):
        return False
    reasons = run.get("reason_codes") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    expected_reason = "trend_data_partial" if run.get("status") == "partial" else "trend_data_unavailable"
    if expected_reason not in [str(item) for item in reasons]:
        return False
    cutoff = RETRY_ALERT_CUTOFF.hour * 60 + RETRY_ALERT_CUTOFF.minute
    return local.hour * 60 + local.minute < cutoff


def _alert_fields(delivery: Any) -> Dict[str, Any]:
    """``alert_sent`` is now ``{outcome, attempts, confirmed}``; ``alert_confirmed`` keeps a
    plain boolean (True only for a confirmed receipt).  An uncertain / paused / blocked result
    is therefore never representable as a truthy "sent"."""
    summary = delivery.summary()
    return {
        "alert_sent": {key: summary[key] for key in ("outcome", "attempts", "confirmed")},
        "alert_confirmed": bool(delivery),
    }


def _run_checked(
    config_path: str,
    *,
    now: Optional[datetime] = None,
    max_running_minutes: int = 8,
    send_alert: bool = True,
) -> Dict[str, Any]:
    config = _config(config_path)
    now = now or datetime.now(BEIJING)
    now = (now if now.tzinfo else now.replace(tzinfo=BEIJING)).astimezone(BEIJING)
    logger = logging.getLogger("swing_eod_watchdog")
    if now.weekday() >= 5 or HolidayCalendar.is_holiday(now):
        return {"ok": True, "reason": "non_trading_day", "message": "非交易日无需审计"}
    swing = config["swing"]
    store_file = swing.get("store_file", "data/swing_macro.db")
    store = SwingStore(store_file)
    result = inspect_terminal_audit(
        store,
        decision_date=now.date().isoformat(),
        now=now,
        max_running_minutes=max_running_minutes,
    )
    if defer_until_retry(result, now):
        result = {
            "ok": True,
            "reason": "awaiting_same_day_retry",
            "message": (
                "15:30 为 data_unavailable，15:48 重试前不告警；"
                f"{result['message']}"
            ),
            "run": result.get("run"),
            "attempts": result.get("attempts"),
            "deferred": True,
        }
    # Publish even if the notification transport subsequently fails. No DB edits.
    try:
        persist_result(store_file, result, now)
    except OSError:
        logger.exception("failed to persist swing EOD watchdog status")
    try:  # independent observability of uncertain/stuck notifications; never fatal
        write_notification_health(store)
    except Exception:  # noqa: BLE001
        logger.exception("failed to persist notification health snapshot")
    plan = notification_plan(result, now) if send_alert else "none"
    result["notification_plan"] = plan
    if plan != "none":
        if plan == "daily":
            payload = build_daily_report(store_file, result, now)
            report_archived = False
            try:
                persist_daily_report(store_file, payload)
                report_archived = True
            except OSError:
                logger.exception("failed to persist EOD daily report")
            result["daily_report"] = {
                "conclusion": payload.get("conclusion"),
                "terminal_ok": payload.get("terminal_ok"),
            }
            if result.get("ok") is True and report_archived and routine_summary_enabled(now):
                consumer = summary_consumer_availability(now)
                ready = consumer.get("available") is True
                # Readiness for a future slot and prior delivery coverage are
                # different facts. Never mark a new late recovery delivered just
                # because some earlier same-day summary was accepted.
                coverage = {"status": "not_evaluated", "reason": "future_slot_ready" if ready else "readiness_gate_blocked"}
                handoff = "scheduled" if ready else "unavailable"
                reason = "routine_deferred_to_16_20_summary" if ready else "summary_consumer_unavailable"
                if (not ready and consumer.get("reason") == "schedule_window_elapsed"
                        and consumer.get("service_health", {}).get("status") in ("healthy", "previous_failure")
                        and consumer.get("schedule_readiness", {}).get("status") == "window_elapsed"
                        and consumer.get("maintenancePresent") is False
                        and consumer.get("identity", {}).get("verified") is True):
                    coverage = summary_batch_coverage(result.get("run") or {}, now)
                    if coverage.get("status") == "covered":
                        reason, handoff = "routine_summary_already_delivered", "already_delivered"
                    elif coverage.get("status") == "not_covered":
                        reason, handoff = "summary_window_elapsed_batch_not_covered", "not_covered"
                    else:
                        reason, handoff = "summary_window_elapsed_coverage_unproven", "coverage_unproven"
                consumer["batch_coverage"] = coverage
                result.update(_alert_fields(DeliveryResult(
                    "deferred", attempts=0, state="not_sent", reason=reason,
                    receipt_saved=False,
                )))
                result["delivery_state"] = reason
                result["summary_consumer"] = consumer
                result["handoff_status"] = handoff
                try:
                    persist_result(store_file, result, now)
                except OSError:
                    logger.error("failed to persist watchdog summary handoff status")
                return result
        # Keep current v2 dispatch/UNKNOWN semantics for genuine faults and for
        # invalid or disabled summary control. Never instantiate a sender for a
        # routine report already delegated to the unified summary.
        base_notifier = TelegramNotifier(
            config["telegram"]["bot_token"], str(config["telegram"]["chat_id"]), logger
        )
        notifier = DurableOutboxNotifier(
            store, base_notifier, logger, scope=f"watchdog:{now.date().isoformat()}:{plan}"
        )
        if plan == "daily":
            delivery = notifier.send_system_alert_ex(
                f"EOD 日报 {now.date().isoformat()}", payload["text"]
            )
            result.update(_alert_fields(delivery))
        else:
            clock = now.astimezone(BEIJING).strftime("%H:%M")
            title = f"Swing EOD 审计异常 {now.date().isoformat()}"
            message = (
                f"{clock} 终态检查失败：{result['message']}\n"
                f"{format_attempt_summary(result.get('attempts') or {})}\n"
                "15:30 失败记录保留；15:50 仍未恢复则并入日报。"
                "请检查 stock-radar-swing-eod-retry.service、行情源和 swing_eod_run。"
            )
            result.update(_alert_fields(notifier.send_system_alert_ex(title, message)))
    return result


def run(
    config_path: str,
    *,
    now: Optional[datetime] = None,
    max_running_minutes: int = 8,
    send_alert: bool = True,
    record_run_status: bool = True,
) -> Dict[str, Any]:
    """Run the watchdog; any crash is first recorded to the independent run-status file."""
    started = now or datetime.now(BEIJING)
    started = (started if started.tzinfo else started.replace(tzinfo=BEIJING)).astimezone(BEIJING)
    logger = logging.getLogger("swing_eod_watchdog")
    store_file = "data/swing_macro.db"
    try:
        store_file = (_config(config_path).get("swing") or {}).get("store_file", store_file)
    except Exception:  # noqa: BLE001 - the real run below reports the config error itself
        pass
    try:
        result = _run_checked(
            config_path,
            now=now,
            max_running_minutes=max_running_minutes,
            send_alert=send_alert,
        )
    except BaseException as exc:
        if record_run_status:
            try:
                persist_watchdog_run_status(store_file, started, "failed", exc)
            except Exception:  # noqa: BLE001 - never mask the original crash
                logger.exception("failed to persist watchdog run-failure status")
        raise
    if record_run_status:
        try:
            persist_watchdog_run_status(store_file, started, "ok")
        except OSError:
            logger.exception("failed to persist watchdog run status")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing EOD terminal audit watchdog")
    parser.add_argument("--config", default="config.swing.json")
    parser.add_argument("--max-running-minutes", type=int, default=8)
    parser.add_argument("--no-alert", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run(
        args.config,
        max_running_minutes=max(1, args.max_running_minutes),
        send_alert=not args.no_alert,
    )
    logging.getLogger("swing_eod_watchdog").info(
        "终态检查: %s", json.dumps(result, ensure_ascii=False, default=str)
    )
    if not result["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
