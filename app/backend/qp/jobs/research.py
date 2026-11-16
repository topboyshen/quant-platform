"""M5 research shadow jobs: read-only imports from the legacy research stores.

No job in this module writes a legacy database, triggers a legacy task, sends a
notification or calls an LLM.  Sources are configured by path/DSN only; when a
source is not configured the job is a no-op so the timer can stay enabled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.jobs.registry import JobContext, job
from qp.models.signals import RotationShadowDay, RotationShadowRun
from qp.research import etf_score
from qp.signals.eod import now_cst
from qp.signals.universe import TARGETS
from qp.signals.watchdog import build_daily_report, run_watchdog
from qp.research.compare import compare_research, write_report
from qp.research.importer import (import_ai, import_backtests, import_ledger, import_phase2,
                                  import_profit, import_projects)
from qp.research.sources import LedgerReader, Phase2Reader, QDReader

DEFAULT_PHASE2_DB = "/var/lib/research-system-phase2/phase2.db"
DEFAULT_REPORT_DIR = "/var/lib/qp/research-reports"


@job("research.import_ledger", "Mirror research_ledger into research.* (read-only source)",
     schedule_hint="every 10 min (qp-research-ledger.timer)")
def import_ledger_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_LEDGER_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_LEDGER_DSN is not configured"}
    reader = LedgerReader(dsn)
    try:
        counts = import_ledger(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_phase2", "Mirror phase2 lineage into research.* (SQLite read-only)",
     schedule_hint="every 10 min (qp-research-phase2.timer)")
def import_phase2_job(ctx: JobContext) -> dict:
    path = os.getenv("QP_PHASE2_DB", DEFAULT_PHASE2_DB).strip()
    if not Path(path).is_file():
        return {"ok": True, "skipped": f"phase2 database not found: {path}"}
    counts = import_phase2(ctx.engine, Phase2Reader(path))
    return {"ok": True, "counts": counts}


DEFAULT_ETF_SCORE_INBOX = "/var/lib/qp/etf-score/latest.json"


@job("research.etf_score_import",
     "Import an offline ETF research score snapshot (read-only; never an order/notification)",
     schedule_hint="Mon-Fri 16:55 CST (after 16:45 offline score)")
def import_etf_score_job(ctx: JobContext) -> dict:
    """Import the 20d snapshot and, when present, the independent 5d shadow.

    Each horizon has its own savepoint.  A malformed 5d file must not roll back
    a valid 20d snapshot (and vice versa); the failing feed is recorded, the
    job is marked failed so the timer retries, and a retry re-imports the good
    feed as an idempotent duplicate.
    """
    path = Path(os.getenv("QP_ETF_SCORE_INBOX", DEFAULT_ETF_SCORE_INBOX)).expanduser()
    feeds = [("20d", path), ("5d", path.with_name("latest-5d.json"))]
    existing = [(label, p) for label, p in feeds if p.is_file()]
    if not existing:
        return {"ok": True, "skipped": "no_score_file", "path": str(path),
                "paths": [str(p) for _, p in feeds]}
    imports: list[dict] = []
    with Session(ctx.engine) as session:
        for label, feed in existing:
            try:
                payload = json.loads(feed.read_text())
                with session.begin_nested():
                    result = etf_score.import_payload(session, payload)
                imports.append({"feed": label, "path": str(feed), "ok": True, **result})
            except Exception as exc:  # isolate one feed; report it, keep the other
                imports.append({"feed": label, "path": str(feed), "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"[:300]})
        session.commit()
    failed = [row["feed"] for row in imports if not row["ok"]]
    return {"ok": not failed, "imports": imports, "failed": failed}


TARGET_NAMES = {target.code: target.name for target in TARGETS}


def _score_block(session: Session, horizon: int) -> dict | None:
    out = etf_score.latest(session, horizon=horizon)
    run = out.get("run")
    if not run:
        return None
    rows = [row for row in (out.get("scores") or []) if row.get("eligible")]
    rows.sort(key=lambda row: row.get("rank") or 999)
    return {
        "run_id": run["id"], "model_version": run["model_version"],
        "data_cutoff": run["data_cutoff"], "stale": bool(run["stale"]),
        "expected_cutoff": run["expected_cutoff"],
        "eligible": len(rows),
        "top8": [{"code": row["code"], "name": row["name"],
                  "rank": row["rank"], "score": row["score"]} for row in rows[:8]],
        "metrics": run.get("metrics") or {},
    }


def _rotation_block(session: Session, today) -> dict | None:
    day = session.scalar(select(RotationShadowDay)
                         .where(RotationShadowDay.trade_date <= today)
                         .order_by(RotationShadowDay.trade_date.desc()).limit(1))
    if day is None:
        return None
    first = session.scalar(select(RotationShadowDay)
                           .order_by(RotationShadowDay.trade_date.asc()).limit(1))
    run = session.scalar(select(RotationShadowRun)
                         .where(RotationShadowRun.execute_date <= today)
                         .order_by(RotationShadowRun.execute_date.desc(),
                                   RotationShadowRun.id.desc()).limit(1))
    positions = []
    for code in (day.positions or {}):
        positions.append({"code": code, "name": TARGET_NAMES.get(code, code)})
    nav = float(day.nav)
    first_nav = float(first.nav) if first and first.nav else None
    benchmark = float(day.benchmark_nav) if day.benchmark_nav is not None else None
    first_bench = (float(first.benchmark_nav)
                   if first and first.benchmark_nav is not None else None)
    ew = float(day.ew_nav) if day.ew_nav is not None else None
    first_ew = float(first.ew_nav) if first and first.ew_nav is not None else None
    return {
        "rule_version": day.rule_version, "trade_date": day.trade_date.isoformat(),
        "signal_date": run.signal_date.isoformat() if run and run.signal_date else None,
        "execute_date": run.execute_date.isoformat() if run and run.execute_date else None,
        "nav": nav,
        "nav_return": (nav / first_nav - 1) if first_nav else None,
        "benchmark_nav": benchmark,
        "benchmark_return": (benchmark / first_bench - 1) if benchmark and first_bench else None,
        "ew_nav": ew,
        "ew_return": (ew / first_ew - 1) if ew and first_ew else None,
        "positions": positions,
    }


def build_etf_shadow_report(session: Session, *, today) -> dict:
    """Read-only 17:40 research shadow; no order/formal-chain write."""
    score20 = _score_block(session, 20)
    score5 = _score_block(session, 5)
    missing = [f"{h}d 快照缺失" for h, block in ((20, score20), (5, score5)) if block is None]
    stale = [f"{h}d stale（cutoff {block['data_cutoff']}）"
             for h, block in ((20, score20), (5, score5))
             if block and block["stale"]]
    rotation = _rotation_block(session, today)
    rolling = ((score5 or {}).get("metrics") or {}).get("rolling_14f") or {}
    conclusion = "研究影子，不构成推荐；5d 仍不晋级"
    if rolling.get("median_alpha") is not None and rolling.get("alpha_nw_t") is not None:
        conclusion += (f"（14 折滚动 alpha 中位 {rolling['median_alpha'] * 100:+.2f}%，"
                       f"NW t {rolling['alpha_nw_t']:+.2f}）。")
    else:
        conclusion += "。"
    return {
        "schema_version": 1,
        "report_type": "etf_shadow_report",
        "trade_date": today.isoformat(),
        "generated_at": now_cst().isoformat(timespec="seconds"),
        "read_only": True,
        "is_trade_signal": False,
        "scores": {"20": score20, "5": score5},
        "rotation": rotation,
        "data_quality": {
            "missing": missing,
            "stale": stale,
            "note": "；".join(missing + stale) if (missing or stale) else "5d/20d 均为当日快照，40/40 可评分",
        },
        "conclusion": conclusion,
    }


def build_combined_report(session: Session, *, today) -> dict:
    """17:40 single message: EOD market/risk/signal + ETF research shadow."""
    now = now_cst()
    audit = run_watchdog(session, now=now, send_alert=False)
    daily = build_daily_report(session, result=audit, now=now)
    shadow = build_etf_shadow_report(session, today=today)
    daily["report_type"] = "daily_combined_report"
    daily["etf_shadow"] = shadow
    daily["read_only"] = True
    daily["is_trade_signal"] = False
    daily["combined_from"] = ["eod_daily_report", "etf_shadow_report"]
    return daily


@job("research.etf_shadow_report",
     "17:40 combined daily: EOD market/risk/signal + 5d/20d + C13 (read-only)",
     schedule_hint="Mon-Fri 17:40 CST (after 17:25 score import)")
def etf_shadow_report_job(ctx: JobContext) -> dict:
    today = now_cst().date()
    with Session(ctx.engine) as session:
        payload = build_combined_report(session, today=today)
        from qp.jobs.builtin import enqueue
        mode = ("live" if get_settings().signals_notify_mode.strip().lower() == "live"
                else "shadow")
        inserted = enqueue(
            session, channel="telegram",
            recipient="signals" if mode == "live" else "shadow:signals-etf",
            dedup_key=f"daily-combined:{today.isoformat()}",
            subject=f"每日报告 {today.isoformat()}",
            body=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            source_job_run_id=ctx.run_id, mode=mode)
        session.commit()
    return {"ok": True, "enqueued": inserted, "mode": mode,
            "trade_date": today.isoformat(),
            "missing": payload["etf_shadow"]["data_quality"]["missing"],
            "combined": True}


@job("research.import_backtests", "Mirror QuantDinger backtest history into research.* "
                                  "(read-only; never re-runs a backtest)",
     schedule_hint="daily 17:30 CST (qp-research-backtests.timer)")
def import_backtests_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_backtests(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_ai", "Mirror QuantDinger AI analysis/chat records into research.* "
                           "(read-only; no LLM calls)",
     schedule_hint="daily 17:40 CST (qp-research-ai.timer)")
def import_ai_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_ai(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_profit", "Mirror QuantDinger profit-research history into research.* "
                               "(read-only; never triggers a run)",
     schedule_hint="daily 17:45 CST (qp-research-profit.timer)")
def import_profit_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_profit(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.import_projects", "Mirror QuantDinger research projects/branches into research.* "
                                 "(read-only; writes stay with QD during shadow)",
     schedule_hint="daily 17:50 CST (qp-research-projects.timer)")
def import_projects_job(ctx: JobContext) -> dict:
    dsn = os.getenv("QP_QD_DSN", "").strip()
    if not dsn:
        return {"ok": True, "skipped": "QP_QD_DSN is not configured"}
    reader = QDReader(dsn)
    try:
        counts = import_projects(ctx.engine, reader)
    finally:
        reader.close()
    return {"ok": True, "counts": counts}


@job("research.compare", "R1-R10 research shadow comparison (read-only; report only)",
     schedule_hint="daily 18:10 CST (qp-research-compare.timer)")
def compare_job(ctx: JobContext) -> dict:
    ledger = LedgerReader(os.environ["QP_LEDGER_DSN"]) if os.getenv("QP_LEDGER_DSN") else None
    qd = QDReader(os.environ["QP_QD_DSN"]) if os.getenv("QP_QD_DSN") else None
    path = os.getenv("QP_PHASE2_DB", DEFAULT_PHASE2_DB).strip()
    phase2 = Phase2Reader(path) if path and Path(path).is_file() else None
    try:
        with Session(ctx.engine) as session:
            result = compare_research(session, ledger=ledger, phase2=phase2, qd=qd)
    finally:
        for reader in (ledger, qd):
            if reader is not None:
                reader.close()
    files = write_report(result, os.getenv("QP_RESEARCH_REPORT_DIR", DEFAULT_REPORT_DIR))
    return {"overall": result["overall"], "counts": result["counts"], "files": files}
