"""One-off read-only import of legacy swing decisions for historical ETF-pool dates.

The legacy SQLite database is opened with ``mode=ro``; qp only inserts into its own
``signals`` schema.  Dates that already have a qp run (for example today's live EOD)
are skipped, so the import can never overwrite a natural run.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qp.models.signals import EodRun, TargetDecision, TrendSignal

CST = dt.timezone(dt.timedelta(hours=8))


def _connect_ro(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)


def _date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def import_legacy_history(session: Session, db_path: str | Path, *,
                          before: dt.date | None = None) -> dict[str, list[str]]:
    """Import every legacy decision date older than ``before`` that qp does not have yet."""
    con = _connect_ro(db_path)
    try:
        dates = [row[0] for row in con.execute(
            "SELECT DISTINCT decision_date FROM swing_eod_run ORDER BY decision_date")]
        existing = set(session.scalars(select(EodRun.decision_date).distinct()))
        imported: list[str] = []
        skipped: list[str] = []
        for date_text in dates:
            day = _date(date_text)
            if day is None:
                continue
            if (before is not None and day >= before) or day in existing:
                skipped.append(date_text)
                continue
            runs = con.execute("SELECT * FROM swing_eod_run WHERE decision_date=? ORDER BY id",
                               (date_text,)).fetchall()
            if not runs:
                continue
            run_row = runs[-1]
            decisions = con.execute("SELECT * FROM eod_target_decision WHERE run_id=? ORDER BY code",
                                    (run_row["run_id"],)).fetchall()
            run = EodRun(decision_date=day, attempt_no=1,
                         data_date=_date(run_row["data_date"]),
                         status=run_row["status"] or "success",
                         decision_outcome=run_row["decision_outcome"],
                         target_count=int(run_row["target_count"] or 0),
                         evaluated_count=int(run_row["evaluated_count"] or 0),
                         failed_count=int(run_row["failed_count"] or 0),
                         signal_count=int(run_row["signal_count"] or 0),
                         allowed_signal_count=int(run_row["allowed_signal_count"] or 0),
                         blocked_signal_count=int(run_row["blocked_signal_count"] or 0),
                         reason_codes=_json(run_row["reason_codes"], []),
                         details={"imported_from": "legacy_swing_macro", "legacy_run_id": run_row["run_id"]},
                         legacy_run_id=run_row["run_id"],
                         started_at=_dt(run_row["started_at"]) or dt.datetime.now(CST),
                         finished_at=_dt(run_row["completed_at"]))
            session.add(run)
            session.flush()
            for row in decisions:
                session.add(TargetDecision(
                    run_id=run.run_id, decision_date=day, code=row["code"],
                    name=row["name"] or "", sector=row["sector"] or "",
                    data_status=row["data_status"], decision_state=row["decision_state"],
                    risk_action=row["risk_action"], risk_level=row["risk_level"],
                    reason_code=row["reason_code"] or "",
                    reference_close=row["reference_close"],
                    raw_trigger_count=int(row["raw_trigger_count"] or 0)))
            for row in con.execute("SELECT * FROM trend_signal WHERE date=? ORDER BY id",
                                   (date_text,)).fetchall():
                session.add(TrendSignal(
                    decision_date=day, code=row["code"], name=row["name"] or "",
                    signal_type=row["signal_type"], period=row["period"],
                    data_date=_date(row["data_date"]), title=row["title"] or "",
                    detail=_json(row["detail"], {}), confidence=row["confidence"] or 0,
                    notified_at=_dt(row["notified_at"])))
            session.flush()
            imported.append(date_text)
        return {"imported": imported, "skipped": skipped}
    finally:
        con.close()
