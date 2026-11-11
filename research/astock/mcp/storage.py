"""SQLite persistence layer for the AStock morning workflow.

Stores each collected morning context and published report so the workflow
keeps a queryable, append-only history alongside the JSON last-good cache.
Uses only the Python standard library (sqlite3) so it adds no dependencies.

The database lives at ``data/astock.db`` under the project root.  Override
with the ``ASTOCK_DB`` environment variable (absolute path) if needed.

This module is intentionally side-effect-free for callers: every write is
wrapped so a storage failure never breaks the data bridge or MCP server.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "astock.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  recorded_at   TEXT NOT NULL,
  target_date   TEXT,
  status        TEXT,
  provider      TEXT,
  source_type   TEXT,
  as_of         TEXT,
  timezone      TEXT,
  is_live       INTEGER,
  completeness_market    REAL,
  completeness_capital   REAL,
  completeness_news      REAL,
  completeness_financial REAL,
  completeness_overall   REAL,
  context_json  TEXT NOT NULL,
  error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_target_date ON runs(target_date);
CREATE INDEX IF NOT EXISTS idx_runs_recorded_at ON runs(recorded_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS reports (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  target_date TEXT NOT NULL,
  level       TEXT NOT NULL,
  file_path   TEXT,
  status      TEXT,
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_target_date ON reports(target_date);
"""


def db_path() -> Path:
  override = os.environ.get("ASTOCK_DB")
  if override:
    return Path(override).expanduser()
  return DEFAULT_DB_PATH


def _connect(path: Path | None = None) -> sqlite3.Connection:
  conn = sqlite3.connect(str(path or db_path()))
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("PRAGMA synchronous=NORMAL")
  return conn


def init_db(path: Path | None = None) -> Path:
  target = path or db_path()
  target.parent.mkdir(parents=True, exist_ok=True)
  with _connect(target) as conn:
    conn.executescript(SCHEMA)
    conn.commit()
  return target


def _completeness(context: dict[str, Any]) -> dict[str, float | None]:
  dc = (context.get("meta") or {}).get("dataCompleteness") or {}
  return {
    "market": dc.get("market"),
    "capital": dc.get("capital"),
    "news": dc.get("news"),
    "financial": dc.get("financial"),
    "overall": dc.get("overall"),
  }


def record_context(context: dict[str, Any], *, error: str | None = None, path: Path | None = None) -> int:
  """Persist one morning context. Returns the new run row id, or -1 on failure."""
  try:
    meta = context.get("meta") or {}
    comp = _completeness(context)
    run_id = meta.get("runId") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    init_db(path)
    with _connect(path) as conn:
      cur = conn.execute(
        """INSERT INTO runs
           (run_id, recorded_at, target_date, status, provider, source_type,
            as_of, timezone, is_live,
            completeness_market, completeness_capital, completeness_news,
            completeness_financial, completeness_overall,
            context_json, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
          run_id,
          datetime.now(timezone.utc).isoformat(),
          meta.get("targetDate"),
          meta.get("status"),
          meta.get("provider"),
          meta.get("sourceType"),
          meta.get("asOf"),
          meta.get("timezone"),
          int(bool(meta.get("isLive"))),
          comp["market"],
          comp["capital"],
          comp["news"],
          comp["financial"],
          comp["overall"],
          json.dumps(context, ensure_ascii=False),
          error,
        ),
      )
      conn.commit()
      return int(cur.lastrowid)
  except Exception as exc:  # never propagate to the bridge
    sys.stderr.write(f"storage.record_context failed: {type(exc).__name__}: {exc}\n")
    return -1


def record_report(target_date: str, level: str, file_path: str | None, status: str, *, path: Path | None = None) -> int:
  try:
    init_db(path)
    with _connect(path) as conn:
      cur = conn.execute(
        """INSERT INTO reports (target_date, level, file_path, status, created_at)
           VALUES (?,?,?,?,?)""",
        (target_date, level, file_path, status, datetime.now(timezone.utc).isoformat()),
      )
      conn.commit()
      return int(cur.lastrowid)
  except Exception as exc:
    sys.stderr.write(f"storage.record_report failed: {type(exc).__name__}: {exc}\n")
    return -1


def recent_runs(limit: int = 10, *, path: Path | None = None) -> list[dict[str, Any]]:
  with _connect(path) as conn:
    rows = conn.execute(
      """SELECT id, run_id, recorded_at, target_date, status, provider, is_live,
                completeness_overall, error
         FROM runs ORDER BY id DESC LIMIT ?""",
      (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_context_by_date(target_date: str, *, path: Path | None = None) -> dict[str, Any] | None:
  with _connect(path) as conn:
    row = conn.execute(
      """SELECT context_json, status, recorded_at FROM runs
         WHERE target_date = ? ORDER BY id DESC LIMIT 1""",
      (target_date,),
    ).fetchone()
    if not row:
      return None
    return {
      "targetDate": target_date,
      "status": row["status"],
      "recordedAt": row["recorded_at"],
      "context": json.loads(row["context_json"]),
    }


def db_info(*, path: Path | None = None) -> dict[str, Any]:
  target = path or db_path()
  if not target.exists():
    return {"path": str(target), "exists": False}
  with _connect(target) as conn:
    runs = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    reports = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]
    latest = conn.execute(
      "SELECT target_date, status, recorded_at FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
  return {
    "path": str(target),
    "exists": True,
    "runs": int(runs),
    "reports": int(reports),
    "latest": dict(latest) if latest else None,
  }


def main() -> int:
  cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
  if cmd == "init":
    p = init_db()
    print(f"SQLite ready: {p}")
    return 0
  if cmd == "info":
    print(json.dumps(db_info(), ensure_ascii=False, indent=2))
    return 0
  if cmd == "recent":
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    print(json.dumps(recent_runs(limit), ensure_ascii=False, indent=2))
    return 0
  if cmd == "get":
    if len(sys.argv) < 3:
      print("usage: storage.py get YYYY-MM-DD", file=sys.stderr)
      return 2
    result = get_context_by_date(sys.argv[2])
    if result is None:
      print(f"no run for {sys.argv[2]}", file=sys.stderr)
      return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
  print(f"usage: storage.py {{init|info|recent [N]|get YYYY-MM-DD}}", file=sys.stderr)
  return 2


if __name__ == "__main__":
  raise SystemExit(main())