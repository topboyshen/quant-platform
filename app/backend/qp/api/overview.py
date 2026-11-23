"""Read-only business overview for the home page.

The overview is a business read model, not a dump of job tables.  It answers:
what is due today, what completed, what is still open, why, and where is the
evidence.  Modules are filtered by the caller's domain scopes so a service token
cannot use the overview to read a domain it could not read directly.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.api.main import Principal, get_db, require_auth
from qp.config import get_settings
from qp.models.market import HealthResult, QlibStatus, TradingDay
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.ops import Alert, JobRun
from qp.models.reports import Report
from qp.models.signals import EodRun
from qp.release import release_info
from qp.research import etf_score

router = APIRouter(prefix="/api/overview")

CST = dt.timezone(dt.timedelta(hours=8))
MODULE_SCOPES = {
    "market": "market:read",
    "signals": "signals:read",
    "research": "research:read",
    "reports": "reports:read",
    "ops": "ops:read",
}
DEADLINES = {"market": "07:05", "signals": "16:50", "research": "18:10", "reports": "17:40"}
DAILY_PREFIXES = ("watchdog:", "daily-combined:")


def _allowed(principal: Principal, module: str) -> bool:
    return principal.kind == "user" or MODULE_SCOPES[module] in (principal.scopes or [])


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else value


def _now() -> dt.datetime:
    return dt.datetime.now(CST)


def _open_days(session: Session, end: dt.date, limit: int = 10) -> list[dt.date]:
    return list(
        session.scalars(
            select(TradingDay.date)
            .where(
                TradingDay.origin == "qp",
                TradingDay.is_open.is_(True),
                TradingDay.date <= end,
            )
            .order_by(TradingDay.date.desc())
            .limit(limit)
        )
    )[::-1]


def _business_context(session: Session, now: dt.datetime) -> tuple[dt.date, str, str]:
    today = now.date()
    open_today = session.scalar(
        select(func.count())
        .select_from(TradingDay)
        .where(TradingDay.origin == "qp", TradingDay.date == today, TradingDay.is_open.is_(True))
    )
    days = _open_days(session, today, limit=1)
    business_date = today if open_today else (days[-1] if days else today)
    if not open_today:
        session_name = "non_trading"
    elif now.time() < dt.time(9, 30):
        session_name = "pre_open"
    elif now.time() < dt.time(15, 0):
        session_name = "open"
    else:
        session_name = "closed"
    labels = {
        "pre_open": "盘前",
        "open": "交易中",
        "closed": "已收盘",
        "non_trading": "休市",
    }
    return business_date, session_name, labels[session_name]


def _due(business_date: dt.date, deadline: str, now: dt.datetime) -> bool:
    if business_date < now.date():
        return True
    hour, minute = (int(part) for part in deadline.split(":"))
    return business_date == now.date() and now.time() >= dt.time(hour, minute)


def _latest_job(session: Session, name: str, day: dt.date | None = None) -> JobRun | None:
    stmt = select(JobRun).where(JobRun.job_name == name)
    if day is not None:
        stmt = stmt.where(JobRun.result["date"].astext == day.isoformat())
    return session.scalar(stmt.order_by(JobRun.started_at.desc(), JobRun.id.desc()).limit(1))


def _job_day(job: JobRun | None) -> dt.date | None:
    if job is None or job.started_at is None:
        return None
    return job.started_at.astimezone(CST).date()


def _outbox_summary(session: Session, outbox: Outbox | None) -> dict[str, Any] | None:
    if outbox is None:
        return None
    attempts = list(
        session.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.outbox_id == outbox.id)
            .order_by(DeliveryAttempt.at, DeliveryAttempt.id)
        )
    )
    successes = [attempt for attempt in attempts if attempt.ok is True]
    failures = [attempt for attempt in attempts if attempt.ok is False]
    first_ok = bool(attempts and attempts[0].ok is True)
    last = successes[-1] if successes else None
    last_no = None
    if last is not None:
        try:
            last_no = int((last.detail or {}).get("attempt"))
        except (TypeError, ValueError):
            last_no = len(attempts)
    gap = None
    if last is not None and outbox.created_at is not None:
        gap = round((last.at - outbox.created_at).total_seconds() / 3600, 2)
    max_attempts = get_settings().notify_max_attempts
    manual = bool(
        successes
        and not first_ok
        and ((last_no is not None and last_no > max_attempts) or (gap is not None and gap > 1))
    )
    recovery = (
        "natural" if first_ok else "manual" if manual else "retry" if successes else "failed" if attempts else "unknown"
    )
    return {
        "id": outbox.id,
        "dedup_key": outbox.dedup_key,
        "mode": outbox.mode,
        "status": outbox.status,
        "receipt_status": outbox.receipt_status or "unknown",
        "attempts": len(attempts),
        "failed_attempts": len(failures),
        "successful_attempts": len(successes),
        "first_attempt_ok": first_ok,
        "last_ok_attempt": last_no,
        "first_attempt_at": attempts[0].at.isoformat() if attempts else None,
        "first_failed_at": failures[0].at.isoformat() if failures else None,
        "last_ok_at": last.at.isoformat() if last is not None else None,
        "gap_hours": gap,
        "recovery": recovery,
        "message_id": (last.detail or {}).get("message_id") if last is not None else None,
        "processed_at": _iso(outbox.processed_at),
    }


def _daily_outbox(session: Session, day: dt.date, prefix: str) -> Outbox | None:
    if prefix == "etf":
        keys = [f"etf-shadow:{day.isoformat()}"]
    else:
        keys = [f"watchdog:{day.isoformat()}:daily", f"daily-combined:{day.isoformat()}"]
    rows = list(session.scalars(select(Outbox).where(Outbox.dedup_key.in_(keys)).order_by(Outbox.id.desc())))
    return next((row for row in rows if row.mode == "live" and row.status == "sent"), rows[0] if rows else None)


def _module(available: bool, status: str, **kwargs: Any) -> dict[str, Any]:
    if not available:
        return {"available": False, "required_scope": kwargs.pop("required_scope", None)}
    labels = {
        "ok": "正常",
        "warning": "需关注",
        "failed": "失败",
        "pending": "待运行",
        "unknown": "未知",
    }
    return {
        "available": True,
        "status": status,
        "status_label": labels.get(status, status),
        **kwargs,
    }


def _market_module(session: Session, day: dt.date, now: dt.datetime, due: bool) -> dict[str, Any]:
    job = _latest_job(session, "market.health")
    result = (job.result or {}) if job is not None else {}
    checks = result.get("checks") or {}
    run_day = _job_day(job)
    health_at = job.started_at if job is not None else None
    if not checks:
        legacy_at = session.scalar(select(func.max(HealthResult.run_at)))
        if legacy_at is not None:
            rows = session.execute(
                select(HealthResult.check, HealthResult.status).where(HealthResult.run_at == legacy_at)
            ).all()
            checks = {check: status for check, status in rows}
            health_at = legacy_at
            run_day = legacy_at.astimezone(CST).date()
    qlib = _latest_job(session, "market.qlib_check")
    qlib_result = (qlib.result or {}) if qlib is not None else {}
    latest_catalog = session.scalar(
        select(QlibStatus)
        .where(QlibStatus.kind == "catalog")
        .order_by(QlibStatus.at.desc(), QlibStatus.id.desc())
        .limit(1)
    )
    gaps = [name for name, status in checks.items() if status != "ok"]
    if qlib is not None and not qlib_result.get("ok", True):
        gaps.append("qlib_check")
    stale = bool(due and run_day is not None and run_day < day)
    if stale:
        gaps.append("health_run_stale")
    if not due:
        status = "pending"
    elif not checks:
        status = "warning"
    elif gaps:
        status = "warning"
    else:
        status = "ok"
    return _module(
        True,
        status,
        deadline=DEADLINES["market"],
        actual_at=_iso(health_at),
        summary=(
            f"市场检查 {sum(1 for value in checks.values() if value == 'ok')}/{len(checks)} 正常"
            if checks
            else "没有市场检查记录"
        ),
        metrics={
            "checks": checks,
            "qlib_latest_eod": qlib_result.get("latest_eod"),
            "qlib_source": qlib_result.get("latest_eod_source"),
            "qlib_checked_at": _iso(qlib.started_at) if qlib is not None else None,
            "qlib_catalog": latest_catalog.data if latest_catalog is not None else None,
        },
        gaps=gaps,
        evidence={"api": "/api/market/health", "job_run_id": job.id if job is not None else None},
    )


def _signals_module(session: Session, day: dt.date, due: bool) -> dict[str, Any]:
    run = session.scalar(
        select(EodRun)
        .where(EodRun.decision_date == day)
        .order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc())
        .limit(1)
    )
    acceptance = _latest_job(session, "signals.acceptance", day)
    acceptance_result = (acceptance.result or {}) if acceptance is not None else {}
    window = _latest_job(session, "signals.chain_window")
    window_result = (window.result or {}) if window is not None else {}
    window_row = next(
        (row for row in window_result.get("rows", []) if row.get("date") == day.isoformat()),
        None,
    )
    gaps: list[str] = []
    if not due:
        status = "pending"
    elif run is None:
        status = "warning"
        gaps.append("eod_missing")
    elif run.status not in ("success", "partial") or run.finished_at is None:
        status = "failed"
        gaps.append(f"eod_{run.status}")
    else:
        status = "ok"
    acceptance_verdict = acceptance_result.get("verdict")
    run_quality = acceptance_result.get("run_quality")
    if due:
        if acceptance is None:
            gaps.append("acceptance_missing")
            status = "warning" if status == "ok" else status
        elif acceptance_verdict == "FAIL":
            gaps.append("acceptance_fail")
            status = "failed"
        elif acceptance_verdict == "WARN":
            gaps.append("acceptance_warn")
            status = "warning" if status == "ok" else status
        elif run_quality == "manual_recovered":
            gaps.append("notification_manual_recovery")
            status = "warning" if status == "ok" else status
        if window_row is None:
            gaps.append("chain_window_missing")
            status = "warning" if status == "ok" else status
        elif window_row.get("status") == "FAIL":
            gaps.append("chain_window_fail")
            status = "failed"
    summary = "今日 EOD 尚未到点" if not due else "无 EOD 运行"
    if run is not None:
        summary = (
            f"{run.decision_date} {run.status}/{run.decision_outcome or '—'}；"
            f"{run.evaluated_count}/{run.target_count} 评估，信号 {run.signal_count}"
        )
    return _module(
        True,
        status,
        deadline=DEADLINES["signals"],
        actual_at=_iso(
            acceptance.finished_at if acceptance is not None else run.finished_at if run is not None else None
        ),
        summary=summary,
        metrics={
            "run_id": run.run_id if run is not None else None,
            "run_status": run.status if run is not None else None,
            "decision_outcome": run.decision_outcome if run is not None else None,
            "targets": run.target_count if run is not None else None,
            "evaluated": run.evaluated_count if run is not None else None,
            "signals": run.signal_count if run is not None else None,
            "allowed": run.allowed_signal_count if run is not None else None,
            "blocked": run.blocked_signal_count if run is not None else None,
            "acceptance_verdict": acceptance_verdict,
            "run_quality": run_quality,
            "natural_pass": acceptance_result.get("natural_pass"),
            "chain_window": window_row.get("status") if window_row is not None else None,
        },
        gaps=gaps,
        evidence={
            "api": "/api/signals/runs",
            "acceptance_job_run_id": acceptance.id if acceptance is not None else None,
            "chain_window_job_run_id": window.id if window is not None else None,
        },
    )


def _research_module(session: Session, day: dt.date, due: bool) -> dict[str, Any]:
    feeds: dict[str, Any] = {}
    gaps: list[str] = []
    for horizon in (20, 5):
        data = etf_score.latest(session, horizon=horizon)
        run = data.get("run")
        scores = data.get("scores") or []
        if run is None:
            gaps.append(f"score_{horizon}d_missing")
            feeds[f"{horizon}d"] = None
            continue
        if run.get("stale"):
            gaps.append(f"score_{horizon}d_stale")
        feeds[f"{horizon}d"] = {
            "run_id": run.get("id"),
            "model_version": run.get("model_version"),
            "data_cutoff": run.get("data_cutoff"),
            "eligible": sum(1 for row in scores if row.get("eligible")),
            "scores": len(scores),
            "stale": bool(run.get("stale")),
        }
    compare = _latest_job(session, "research.compare")
    compare_result = (compare.result or {}) if compare is not None else {}
    compare_day = _job_day(compare)
    if due and compare_day is not None and compare_day < day:
        gaps.append("research_compare_stale")
    if compare_result.get("overall") == "FAIL":
        gaps.append("research_compare_fail")
    if not due:
        status = "pending"
        gaps = []
    elif gaps:
        status = "warning"
    else:
        status = "ok"
    return _module(
        True,
        status,
        deadline=DEADLINES["research"],
        actual_at=_iso(compare.finished_at if compare is not None else None),
        summary=("5d/20d 评分快照已导入；研究影子，不等于模型晋级" if not gaps else "评分或研究对比仍有缺口"),
        metrics={
            "feeds": feeds,
            "compare": compare_result.get("overall"),
            "compare_at": _iso(compare.started_at) if compare is not None else None,
            "read_only": True,
            "promotion": "not_promoted",
        },
        gaps=gaps,
        evidence={
            "api": "/api/research/etf-score",
            "compare_job_run_id": compare.id if compare is not None else None,
        },
    )


def _reports_module(session: Session, day: dt.date, due: bool) -> dict[str, Any]:
    daily = _outbox_summary(session, _daily_outbox(session, day, "daily"))
    etf = _outbox_summary(session, _daily_outbox(session, day, "etf"))
    gaps: list[str] = []
    if not due:
        status = "pending"
    elif daily is None:
        status = "warning"
        gaps.append("daily_notification_missing")
    elif daily["status"] == "failed":
        status = "failed"
        gaps.append("daily_notification_failed")
    elif daily["mode"] != "live":
        status = "warning"
        gaps.append("daily_notification_shadow")
    elif daily["recovery"] == "manual":
        status = "warning"
        gaps.append("daily_notification_manual_recovery")
    else:
        status = "ok"
    if due and etf is not None and etf["status"] == "failed":
        status = "warning" if status == "ok" else status
        gaps.append("etf_notification_failed")
    latest_report = session.scalar(select(func.max(Report.business_date)))
    report_count = session.scalar(select(func.count()).select_from(Report)) or 0
    return _module(
        True,
        status,
        deadline=DEADLINES["reports"],
        actual_at=daily.get("processed_at") if daily else None,
        summary=(
            "日报已发送" if daily and daily["status"] == "sent" else "日报尚未完成" if due else "今日日报尚未到点"
        ),
        metrics={
            "business_date": day.isoformat(),
            "daily": daily,
            "etf": etf,
            "archive_reports": int(report_count),
            "latest_archive_date": _iso(latest_report),
        },
        gaps=gaps,
        evidence={"api": "/api/reports/deliveries"},
    )


def _ops_module(session: Session, day: dt.date, now: dt.datetime) -> dict[str, Any]:
    alerts = list(session.scalars(select(Alert).where(Alert.state == "firing").order_by(Alert.id.desc()).limit(50)))
    day_start = dt.datetime.combine(day, dt.time.min, tzinfo=CST)
    day_end = day_start + dt.timedelta(days=1)
    runs = list(
        session.scalars(
            select(JobRun)
            .where(JobRun.started_at >= day_start, JobRun.started_at < day_end)
            .order_by(JobRun.started_at.desc())
        )
    )
    failed = [run for run in runs if run.status == "failed"]
    blocked = [run for run in runs if run.status == "blocked"]
    if failed:
        status = "failed"
    elif blocked or alerts:
        status = "warning"
    else:
        status = "ok"
    return _module(
        True,
        status,
        summary=f"firing 告警 {len(alerts)}；今日失败 {len(failed)}；阻断 {len(blocked)}",
        metrics={
            "firing_alerts": [
                {
                    "id": alert.id,
                    "key": alert.key,
                    "severity": alert.severity,
                    "summary": alert.summary,
                    "created_at": _iso(alert.created_at),
                    "last_seen_at": _iso(alert.last_seen_at),
                }
                for alert in alerts
            ],
            "failed_jobs": [
                {
                    "id": run.id,
                    "job_name": run.job_name,
                    "started_at": _iso(run.started_at),
                    "error": (run.error or "")[:200] or None,
                }
                for run in failed[:10]
            ],
            "blocked_jobs": [
                {"id": run.id, "job_name": run.job_name, "started_at": _iso(run.started_at)} for run in blocked[:10]
            ],
            "recent_jobs": len(runs),
        },
        gaps=[f"alert:{alert.key}" for alert in alerts] + [f"failed:{run.job_name}" for run in failed[:5]],
        evidence={"api": "/api/ops/job-runs"},
    )


def _recent_quality(session: Session, day: dt.date, now: dt.datetime, limit: int = 10) -> list[dict[str, Any]]:
    days = _open_days(session, day, limit=limit)
    out: list[dict[str, Any]] = []
    for current in days:
        run = session.scalar(
            select(EodRun)
            .where(EodRun.decision_date == current)
            .order_by(EodRun.attempt_no.desc(), EodRun.run_id.desc())
            .limit(1)
        )
        acceptance = _latest_job(session, "signals.acceptance", current)
        acceptance_result = (acceptance.result or {}) if acceptance is not None else {}
        window = _latest_job(session, "signals.chain_window")
        window_result = (window.result or {}) if window is not None else {}
        window_row = next(
            (row for row in window_result.get("rows", []) if row.get("date") == current.isoformat()), None
        )
        daily = _outbox_summary(session, _daily_outbox(session, current, "daily"))
        if current == day and not _due(day, DEADLINES["signals"], now):
            quality = "pending"
        elif acceptance_result.get("run_quality"):
            quality = acceptance_result["run_quality"]
        elif acceptance_result.get("verdict") == "FAIL":
            quality = "failed"
        elif acceptance_result.get("verdict") == "WARN":
            quality = "warn"
        elif daily and daily["recovery"] in ("natural", "retry", "manual"):
            quality = f"{daily['recovery']}_recovered" if daily["recovery"] != "natural" else "natural"
        else:
            quality = "incomplete"
        natural = acceptance_result.get("natural_pass")
        if natural is None and acceptance_result.get("verdict") == "PASS" and quality == "natural":
            natural = True
        out.append(
            {
                "business_date": current.isoformat(),
                "eod": f"{run.status}/{run.decision_outcome}" if run is not None else None,
                "acceptance": acceptance_result.get("verdict"),
                "run_quality": quality,
                "natural_pass": bool(natural),
                "chain_window": window_row.get("status") if window_row is not None else None,
                "notification": daily["recovery"] if daily else "missing",
            }
        )
    return out


def _issues(
    modules: dict[str, Any], ops: dict[str, Any] | None, signals: dict[str, Any] | None
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for name, module in modules.items():
        if name == "ops" or not module.get("available") or module.get("status") not in ("warning", "failed"):
            continue
        for gap in module.get("gaps", [])[:8]:
            if name == "signals" and gap == "notification_manual_recovery":
                continue
            issues.append(
                {
                    "id": f"{name}:{gap}",
                    "scope": name,
                    "severity": "critical" if module["status"] == "failed" else "warning",
                    "summary": f"{name}：{gap}",
                    "impact": "对应业务尚未完整完成",
                    "reason": gap,
                    "started_at": module.get("actual_at"),
                    "evidence": module.get("evidence"),
                }
            )
    if ops is not None:
        for alert in ops.get("metrics", {}).get("firing_alerts", []):
            issues.append(
                {
                    "id": f"ops:alert:{alert['key']}",
                    "scope": "ops",
                    "severity": "critical" if alert.get("severity") == "critical" else "warning",
                    "summary": alert.get("summary") or alert.get("key"),
                    "impact": "运维告警未恢复",
                    "reason": alert.get("key"),
                    "started_at": alert.get("created_at"),
                    "evidence": "/api/ops/alerts",
                }
            )
    if signals is not None and signals.get("metrics", {}).get("run_quality") == "manual_recovered":
        issues.append(
            {
                "id": "signals:manual_recovery",
                "scope": "signals",
                "severity": "warning",
                "summary": "最近完整交易日日报为手工补发",
                "impact": "业务已完成，但自然通过率不计入",
                "reason": "manual_recovered",
                "started_at": signals.get("actual_at"),
                "evidence": signals.get("evidence"),
            }
        )
    seen: set[str] = set()
    unique = []
    for issue in issues:
        if issue["id"] in seen:
            continue
        seen.add(issue["id"])
        unique.append(issue)
    return unique[:20]


@router.get("")
def overview(p: Principal = Depends(require_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    now = _now()
    day, session_name, session_label = _business_context(db, now)
    info = release_info()
    modules: dict[str, Any] = {}
    if _allowed(p, "market"):
        modules["market"] = _market_module(db, day, now, _due(day, DEADLINES["market"], now))
    else:
        modules["market"] = _module(False, "unknown", required_scope=MODULE_SCOPES["market"])
    if _allowed(p, "signals"):
        modules["signals"] = _signals_module(db, day, _due(day, DEADLINES["signals"], now))
    else:
        modules["signals"] = _module(False, "unknown", required_scope=MODULE_SCOPES["signals"])
    if _allowed(p, "research"):
        modules["research"] = _research_module(db, day, _due(day, DEADLINES["research"], now))
    else:
        modules["research"] = _module(False, "unknown", required_scope=MODULE_SCOPES["research"])
    if _allowed(p, "reports"):
        modules["reports"] = _reports_module(db, day, _due(day, DEADLINES["reports"], now))
    else:
        modules["reports"] = _module(False, "unknown", required_scope=MODULE_SCOPES["reports"])
    ops = _ops_module(db, day, now) if _allowed(p, "ops") else None
    if ops is not None:
        modules["ops"] = ops
    else:
        modules["ops"] = _module(False, "unknown", required_scope=MODULE_SCOPES["ops"])
    signals = modules.get("signals") if modules.get("signals", {}).get("available") else None
    return {
        "generated_at": now.isoformat(),
        "version": info["version"],
        "release_sha": info["sha"],
        "release_base": info["base"],
        "today": now.date().isoformat(),
        "business_date": day.isoformat(),
        "session": session_name,
        "session_label": session_label,
        "timezone": "Asia/Shanghai",
        "access": {"kind": p.kind, "name": p.name, "scopes": list(p.scopes or [])},
        "modules": modules,
        "pending": _issues(modules, ops, signals),
        "recent_quality": _recent_quality(db, day, now) if _allowed(p, "signals") or _allowed(p, "ops") else [],
    }
