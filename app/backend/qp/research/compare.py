"""M5 research shadow comparator (R1-R10).

Read-only on both sides: source readers only issue SELECTs inside a read-only
transaction, qp side reads the ``research``/``notify`` schemas.  The only writes
are the optional JSON/Markdown report files.

Exit-code convention (CLI): 0 PASS/WARN, 1 FAIL, 2 BLOCKED.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.ops import JobRun
from qp.models.research import (
    AiMessage,
    BacktestEquity,
    BacktestTrade,
    Forecast,
    ForecastOutcome,
    ImportRun,
    LlmCall,
    ProfitObservation,
    ProfitPick,
    Project,
)
from qp.research.importer import (
    AI_SPECS,
    LEDGER_SPECS,
    PHASE2_SPECS,
    PROFIT_SPECS,
    PROJECT_SPECS,
    QD_SPECS,
    TableSpec,
)

UTC = dt.UTC
STATUS_ORDER = {"PASS": 0, "WARN": 1, "BLOCKED": 2, "FAIL": 3}


def _check(
    cid: str, status: str, summary: str, *, diffs: Iterable[str] = (), details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": cid,
        "status": status,
        "summary": summary,
        "diffs": [str(item) for item in diffs],
        "details": details or {},
    }


def _blocked(cid: str, reason: str) -> dict[str, Any]:
    return _check(cid, "BLOCKED", reason)


def _worst(checks: list[dict[str, Any]]) -> str:
    return max((c["status"] for c in checks), key=lambda status: STATUS_ORDER.get(status, 3), default="PASS")


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model.__table__)) or 0


def _count_specs(
    session: Session, reader: Any, specs: tuple[TableSpec, ...]
) -> tuple[dict[str, Any], list[str], list[str]]:
    details: dict[str, Any] = {}
    diffs: list[str] = []
    errors: list[str] = []
    for spec in specs:
        name = spec.model.__tablename__
        try:
            source = reader.count(spec.source_table)
        except Exception as exc:  # one broken source table blocks the check, not the run
            errors.append(f"{name}: source_error {type(exc).__name__}")
            continue
        mirror = _count(session, spec.model)
        details[name] = {"source": source, "qp": mirror}
        if source != mirror:
            diffs.append(f"{name}: source={source} qp={mirror}")
    return details, diffs, errors


def _distribution(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    return {str(k): v for k, v in Counter(row.get(key) for row in rows).items()}


def _close(left: float, right: float, *, tolerance: float = 1e-6) -> bool:
    return abs(float(left) - float(right)) <= max(tolerance, abs(float(left)) * 1e-9)


# --------------------------------------------------------------------------- R1/R2 ledger
def _check_ledger(session: Session, ledger: Any) -> list[dict[str, Any]]:
    if ledger is None:
        return [_blocked("R1", "ledger source not configured"), _blocked("R2", "ledger source not configured")]
    details, diffs, errors = _count_specs(session, ledger, LEDGER_SPECS)
    r1 = (
        _blocked("R1", "ledger source error", diffs=errors)
        if errors
        else _check(
            "R1",
            "FAIL" if diffs else "PASS",
            "ledger mirror row counts" + ("" if not diffs else " differ"),
            diffs=diffs,
            details=details,
        )
    )
    try:
        src_f = _distribution(ledger.fetch("forecast"), "status")
        src_o = _distribution(ledger.fetch("outcome_observation"), "outcome_status")
    except Exception as exc:
        r2 = _blocked("R2", f"ledger status distribution error: {type(exc).__name__}")
    else:
        qp_f = {
            str(k): v for k, v in session.execute(select(Forecast.status, func.count()).group_by(Forecast.status)).all()
        }
        qp_o = {
            str(k): v
            for k, v in session.execute(
                select(ForecastOutcome.outcome_status, func.count()).group_by(ForecastOutcome.outcome_status)
            ).all()
        }
        diffs = []
        if src_f != qp_f:
            diffs.append(f"forecast.status: source={src_f} qp={qp_f}")
        if src_o != qp_o:
            diffs.append(f"outcome.outcome_status: source={src_o} qp={qp_o}")
        r2 = _check(
            "R2",
            "FAIL" if diffs else "PASS",
            "forecast/outcome status distribution" + ("" if not diffs else " differs"),
            diffs=diffs,
            details={"forecast": src_f, "outcome": src_o},
        )
    return [r1, r2]


# --------------------------------------------------------------------------- R3-R6 QD mirrors
def _check_qd(session: Session, qd: Any) -> list[dict[str, Any]]:
    if qd is None:
        return [
            _blocked("R3", "QD source not configured"),
            _blocked("R4", "QD source not configured"),
            _blocked("R5", "QD source not configured"),
            _blocked("R6", "QD source not configured"),
        ]

    # R3 backtests: counts + two aggregate sums
    details, diffs, errors = _count_specs(session, qd, QD_SPECS)
    if not errors:
        try:
            src_eq = float(qd.scalar("SELECT coalesce(sum(point_value), 0) FROM public.qd_backtest_equity_points"))
            src_pf = float(qd.scalar("SELECT coalesce(sum(profit), 0) FROM public.qd_backtest_trades"))
        except Exception as exc:
            errors.append(f"aggregate: {type(exc).__name__}")
        else:
            qp_eq = float(session.scalar(select(func.coalesce(func.sum(BacktestEquity.point_value), 0))))
            qp_pf = float(session.scalar(select(func.coalesce(func.sum(BacktestTrade.profit), 0))))
            details["equity_sum"] = {"source": src_eq, "qp": qp_eq}
            details["trade_profit_sum"] = {"source": src_pf, "qp": qp_pf}
            if not _close(src_eq, qp_eq):
                diffs.append(f"equity_sum: source={src_eq} qp={qp_eq}")
            if not _close(src_pf, qp_pf):
                diffs.append(f"trade_profit_sum: source={src_pf} qp={qp_pf}")
    r3 = (
        _blocked("R3", "QD source error", diffs=errors)
        if errors
        else _check(
            "R3",
            "FAIL" if diffs else "PASS",
            "backtest history" + ("" if not diffs else " differs"),
            diffs=diffs,
            details=details,
        )
    )

    # R4 profit research
    details, diffs, errors = _count_specs(session, qd, PROFIT_SPECS)
    if not errors:
        try:
            src_keys = int(qd.scalar("SELECT count(DISTINCT pick_key) FROM public.qd_profit_picks"))
            src_net = float(qd.scalar("SELECT coalesce(sum(net_return), 0) FROM public.qd_profit_pick_observations"))
        except Exception as exc:
            errors.append(f"aggregate: {type(exc).__name__}")
        else:
            qp_keys = session.scalar(select(func.count(func.distinct(ProfitPick.pick_key)))) or 0
            qp_net = float(session.scalar(select(func.coalesce(func.sum(ProfitObservation.net_return), 0))))
            details["distinct_pick_key"] = {"source": src_keys, "qp": qp_keys}
            details["net_return_sum"] = {"source": src_net, "qp": qp_net}
            if src_keys != qp_keys:
                diffs.append(f"distinct_pick_key: source={src_keys} qp={qp_keys}")
            if not _close(src_net, qp_net):
                diffs.append(f"net_return_sum: source={src_net} qp={qp_net}")
    r4 = (
        _blocked("R4", "QD source error", diffs=errors)
        if errors
        else _check(
            "R4",
            "FAIL" if diffs else "PASS",
            "profit research" + ("" if not diffs else " differs"),
            diffs=diffs,
            details=details,
        )
    )

    # R5 AI records
    details, diffs, errors = _count_specs(session, qd, AI_SPECS)
    if not errors:
        try:
            src_sessions = int(qd.scalar("SELECT count(DISTINCT session_id) FROM public.ai_chat_messages"))
        except Exception as exc:
            errors.append(f"aggregate: {type(exc).__name__}")
        else:
            qp_sessions = session.scalar(select(func.count(func.distinct(AiMessage.session_id)))) or 0
            details["message_sessions"] = {"source": src_sessions, "qp": qp_sessions}
            if src_sessions != qp_sessions:
                diffs.append(f"message_sessions: source={src_sessions} qp={qp_sessions}")
    r5 = (
        _blocked("R5", "QD source error", diffs=errors)
        if errors
        else _check(
            "R5",
            "FAIL" if diffs else "PASS",
            "AI tasks/sessions/messages" + ("" if not diffs else " differ"),
            diffs=diffs,
            details=details,
        )
    )

    # R6 projects/branches
    details, diffs, errors = _count_specs(session, qd, PROJECT_SPECS)
    if not errors:
        try:
            src_rev = int(qd.scalar("SELECT coalesce(sum(revision), 0) FROM public.qd_research_projects"))
        except Exception as exc:
            errors.append(f"aggregate: {type(exc).__name__}")
        else:
            qp_rev = session.scalar(select(func.coalesce(func.sum(Project.revision), 0))) or 0
            details["project_revision_sum"] = {"source": src_rev, "qp": int(qp_rev)}
            if src_rev != int(qp_rev):
                diffs.append(f"project_revision_sum: source={src_rev} qp={qp_rev}")
    r6 = (
        _blocked("R6", "QD source error", diffs=errors)
        if errors
        else _check(
            "R6",
            "FAIL" if diffs else "PASS",
            "research projects/branches" + ("" if not diffs else " differ"),
            diffs=diffs,
            details=details,
        )
    )
    return [r3, r4, r5, r6]


# --------------------------------------------------------------------------- R7-R10 qp side
def _check_llm(session: Session) -> dict[str, Any]:
    total = _count(session, LlmCall)
    live_bad = (
        session.scalar(
            select(func.count()).select_from(LlmCall).where(LlmCall.mode == "live", LlmCall.status != "refused")
        )
        or 0
    )
    shadow_bad = (
        session.scalar(
            select(func.count()).select_from(LlmCall).where(LlmCall.mode == "replay", LlmCall.shadow.is_(False))
        )
        or 0
    )
    diffs = []
    if live_bad:
        diffs.append(f"live_calls_not_refused={live_bad}")
    if shadow_bad:
        diffs.append(f"replay_calls_not_shadow={shadow_bad}")
    return _check(
        "R7",
        "FAIL" if diffs else "PASS",
        "LLM entry point is replay-only" + ("" if not diffs else " violated"),
        diffs=diffs,
        details={"llm_calls": total, "live_not_refused": live_bad, "replay_not_shadow": shadow_bad},
    )


def _check_read_only(session: Session, readers: dict[str, Any]) -> dict[str, Any]:
    diffs: list[str] = []
    modes: dict[str, Any] = {}
    for name, reader in readers.items():
        if reader is None:
            modes[name] = "not_configured"
            continue
        try:
            modes[name] = "read_only" if reader.read_only() else "writable"
        except Exception as exc:
            modes[name] = f"error:{type(exc).__name__}"
        if modes[name] not in ("read_only", "not_configured"):
            diffs.append(f"{name}: {modes[name]}")
    latest: dict[str, Any] = {}
    for source in (
        "research_ledger",
        "phase2",
        "quantdinger",
        "quantdinger_ai",
        "quantdinger_profit",
        "quantdinger_projects",
    ):
        run = session.scalar(select(ImportRun).where(ImportRun.source == source).order_by(ImportRun.id.desc()).limit(1))
        latest[source] = run.status if run is not None else "missing"
        if run is not None and run.status == "failed":
            diffs.append(f"import_run_failed:{source}")
    return _check(
        "R8",
        "FAIL" if diffs else "PASS",
        "read-only sources and successful imports" + ("" if not diffs else " violated"),
        diffs=diffs,
        details={"source_modes": modes, "latest_imports": latest},
    )


def _check_idempotent(session: Session) -> dict[str, Any]:
    duplicates: list[str] = []
    for spec in LEDGER_SPECS + PHASE2_SPECS + QD_SPECS + AI_SPECS + PROFIT_SPECS + PROJECT_SPECS:
        table = spec.model.__table__
        pks = list(spec.pk_columns)
        sub = (
            select(*[table.c[name] for name in pks])
            .group_by(*[table.c[name] for name in pks])
            .having(func.count() > 1)
            .subquery()
        )
        n = session.scalar(select(func.count()).select_from(sub)) or 0
        if n:
            duplicates.append(f"{table.name}: {n} duplicate key(s)")
    return _check(
        "R9",
        "FAIL" if duplicates else "PASS",
        "natural-key uniqueness" + ("" if not duplicates else " violated"),
        diffs=duplicates,
        details={
            "tables_checked": len(LEDGER_SPECS + PHASE2_SPECS + QD_SPECS + AI_SPECS + PROFIT_SPECS + PROJECT_SPECS)
        },
    )


ALLOWED_LIVE_PREFIXES = ("watchdog:", "daily-combined:", "etf-shadow:", "smoke:")
ALLOWED_LIVE_JOBS = frozenset({"signals.watchdog", "signals.eod", "signals.eod_retry", "research.etf_shadow_report"})


def _check_notify(session: Session) -> dict[str, Any]:
    """R10: live delivery is allowed only for approved signal/report producers.

    Research imports must stay shadow-only.  The previous rule failed every live
    row and every delivery attempt, which made the check permanently red after
    M4/M6 intentionally switched signal and ETF reports to live delivery.
    """
    rows = session.execute(
        select(Outbox, JobRun.job_name)
        .outerjoin(JobRun, JobRun.id == Outbox.source_job_run_id)
        .where(Outbox.mode != "shadow")
    ).all()
    unexpected: list[str] = []
    allowed: list[str] = []
    for outbox, job_name in rows:
        if job_name is not None:
            ok = job_name in ALLOWED_LIVE_JOBS
        else:
            ok = any(outbox.dedup_key.startswith(prefix) for prefix in ALLOWED_LIVE_PREFIXES)
        target = f"{outbox.dedup_key} ({outbox.mode}/{outbox.status}, source={job_name or 'none'})"
        (allowed if ok else unexpected).append(target)
    attempts = session.scalar(select(func.count()).select_from(DeliveryAttempt)) or 0
    return _check(
        "R10",
        "FAIL" if unexpected else "PASS",
        "live notifications stay within approved producers" + ("" if not unexpected else " violated"),
        diffs=unexpected,
        details={
            "live_rows": len(rows),
            "allowed_live": allowed,
            "unexpected_live": unexpected,
            "delivery_attempt": attempts,
        },
    )


# --------------------------------------------------------------------------- orchestration
def compare_research(
    session: Session, *, ledger: Any = None, phase2: Any = None, qd: Any = None, now: dt.datetime | None = None
) -> dict[str, Any]:
    now = now or dt.datetime.now(UTC)
    checks: list[dict[str, Any]] = []
    checks.extend(_check_ledger(session, ledger))
    if phase2 is not None:
        details, diffs, errors = _count_specs(session, phase2, PHASE2_SPECS)
        checks.append(
            _blocked("R2P", "phase2 source error", diffs=errors)
            if errors
            else _check(
                "R2P",
                "FAIL" if diffs else "PASS",
                "phase2 lineage row counts" + ("" if not diffs else " differ"),
                diffs=diffs,
                details=details,
            )
        )
    checks.extend(_check_qd(session, qd))
    checks.append(_check_llm(session))
    checks.append(_check_read_only(session, {"ledger": ledger, "phase2": phase2, "qd": qd}))
    checks.append(_check_idempotent(session))
    checks.append(_check_notify(session))
    overall = _worst(checks)
    counts = Counter(c["status"] for c in checks)
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "overall": overall,
        "gate_ok": overall in ("PASS", "WARN"),
        "counts": {k: counts.get(k, 0) for k in ("PASS", "WARN", "BLOCKED", "FAIL")},
        "checks": checks,
    }


def write_report(result: dict[str, Any], out_dir: str | Path) -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = str(result["generated_at"])[:10]
    jpath = out / f"{stamp}.research-compare.json"
    mpath = out / f"{stamp}.research-compare.md"
    jpath.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# qp M5 研究影子对比（{stamp}）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- **结论：{result['overall']}**（PASS {result['counts']['PASS']} / "
        f"WARN {result['counts']['WARN']} / BLOCKED {result['counts']['BLOCKED']} / "
        f"FAIL {result['counts']['FAIL']}）",
        "",
        "| 检查 | 状态 | 摘要 |",
        "|---|---|---|",
    ]
    for c in result["checks"]:
        lines.append(f"| {c['id']} | {c['status']} | {c['summary']} |")
    diffs = [(c["id"], d) for c in result["checks"] for d in c.get("diffs", [])]
    if diffs:
        lines += ["", "## 差异", ""]
        lines += [f"- {cid}: {d}" for cid, d in diffs]
    mpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(jpath), str(mpath)]
