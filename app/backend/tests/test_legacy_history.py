"""Legacy swing history import: read-only, idempotent, never touches today's run."""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from qp.models.signals import EodRun, TargetDecision, TrendSignal
from qp.signals.legacy_history import import_legacy_history

D1, D2, TODAY = "2026-11-11", "2026-11-12", "2026-11-13"


def make_db(tmp_path: Path) -> Path:
    path = tmp_path / "swing_macro.db"
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE swing_eod_run (id INTEGER PRIMARY KEY, run_id TEXT, decision_date TEXT,
      data_date TEXT, status TEXT, started_at TEXT, completed_at TEXT, target_count INT,
      evaluated_count INT, failed_count INT, signal_count INT, allowed_signal_count INT,
      blocked_signal_count INT, decision_outcome TEXT, reason_codes TEXT, details TEXT,
      created_at TEXT);
    CREATE TABLE eod_target_decision (decision_id TEXT PRIMARY KEY, run_id TEXT, snapshot_id TEXT,
      decision_date TEXT, decision_at TEXT, data_date TEXT, code TEXT, name TEXT, sector TEXT,
      data_status TEXT, decision_state TEXT, raw_trigger_count INT, raw_signals_json TEXT,
      risk_action TEXT, risk_level TEXT, reason_code TEXT, reference_close REAL,
      validation_split TEXT, created_at TEXT);
    CREATE TABLE trend_signal (id INTEGER PRIMARY KEY, date TEXT, code TEXT, name TEXT,
      signal_type TEXT, period TEXT, data_date TEXT, title TEXT, detail TEXT, confidence REAL,
      notified_at TEXT, created_at TEXT);
    """)
    for i, day in enumerate((D1, D2, TODAY), start=1):
        con.execute("INSERT INTO swing_eod_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, f"run-{day}", day, day, "success", f"{day}T15:30:00+08:00",
                     f"{day}T15:31:00+08:00", 40, 40, 0, 1, 1, 0, "emitted", "[]", "{}",
                     f"{day}T15:31:00+08:00"))
        con.execute("INSERT INTO eod_target_decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"dec-{day}", f"run-{day}", "snap", day, f"{day}T15:30:30+08:00", day,
                     "510300", "沪深300ETF", "宽基", "ok", "allowed", 1, "[]", "allowed",
                     "growth_ok", "trigger", 4.12, "", f"{day}T15:30:30+08:00"))
        con.execute("INSERT INTO trend_signal VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, day, "510300", "沪深300ETF", "volume_breakout_ma60", "D", day,
                     "放量突破", '{"close": 4.12}', 0.55, f"{day}T15:30:30+08:00",
                     f"{day}T15:30:30+08:00"))
    con.commit()
    con.close()
    return path


def cleanup(engine) -> None:
    with Session(engine) as session:
        session.execute(delete(TrendSignal))
        session.execute(delete(TargetDecision))
        session.execute(delete(EodRun))
        session.commit()


def test_import_backfills_past_dates_and_skips_today(engine, tmp_path: Path):
    cleanup(engine)
    try:
        db = make_db(tmp_path)
        before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
        with Session(engine) as session:
            result = import_legacy_history(session, db, before=dt.date(2026, 11, 13))
            session.commit()
            assert result["imported"] == [D1, D2]
            assert result["skipped"] == [TODAY]
            assert session.scalar(select(func.count()).select_from(EodRun)) == 2
            assert session.scalar(select(func.count()).select_from(TargetDecision)) == 2
            assert session.scalar(select(func.count()).select_from(TrendSignal)) == 2
            run = session.scalar(select(EodRun).where(EodRun.decision_date == dt.date(2026, 11, 11)))
            assert run.legacy_run_id == f"run-{D1}" and run.signal_count == 1
            signal = session.scalar(select(TrendSignal).where(TrendSignal.decision_date == dt.date(2026, 11, 11)))
            assert signal.detail == {"close": 4.12}

            again = import_legacy_history(session, db, before=dt.date(2026, 11, 13))
            session.commit()
            assert again["imported"] == []
            assert session.scalar(select(func.count()).select_from(EodRun)) == 2
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    finally:
        cleanup(engine)
