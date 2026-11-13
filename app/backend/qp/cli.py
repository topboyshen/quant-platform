"""`qp` command line: qp job run <name> | qp job list | qp db upgrade | qp user import-legacy --stdin"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _alembic_cfg():
    from alembic.config import Config
    # alembic.ini + migrations/ live in the app dir (QP_APP_DIR, else CWD, else the source checkout)
    cands = [os.getenv("QP_APP_DIR"), os.getcwd(), str(Path(__file__).resolve().parent.parent)]
    here = next(Path(c) for c in cands if c and (Path(c) / "alembic.ini").is_file())
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here / "migrations"))
    return cfg


def cmd_db_upgrade(_a) -> int:
    from alembic import command
    command.upgrade(_alembic_cfg(), "head")
    print("db upgraded to head")
    return 0


def cmd_job_list(_a) -> int:
    from qp.jobs import JOBS
    for n, s in sorted(JOBS.items()):
        print(f"{n}\t{s.schedule_hint or '-'}\t{s.description}")
    return 0


def cmd_job_run(a) -> int:
    from qp.db import get_engine
    from qp.jobs import run_job
    run = run_job(get_engine(), a.name, trigger=a.trigger, dedup_key=a.dedup_key)
    print(json.dumps({"id": run.id, "job": run.job_name, "status": run.status, "duration_s": run.duration_s,
                      "result": run.result}, default=str, ensure_ascii=False))
    return 0 if run.status in ("succeeded", "skipped_dup", "skipped_locked") else 1


def cmd_shadow_compare(a) -> int:
    import datetime as dt

    from sqlalchemy.orm import Session

    from qp.db import get_engine
    from qp.shadow.compare import compare_day, write_report
    from qp.signals.approved import load_hmac_secret

    try:
        day = dt.date.fromisoformat(a.date)
    except ValueError:
        print(json.dumps({"ok": False, "error": "bad_date"}))
        return 2
    secret = load_hmac_secret(a.hmac_secret_file) if a.hmac_secret_file else None
    with Session(get_engine()) as session:
        result = compare_day(session, day, a.legacy, hmac_secret=secret)
    files = write_report(result, a.out) if a.out else []
    summary = {"date": result["date"], "overall": result["overall"],
               "gate_ok": result["gate_ok"], "counts": result["counts"],
               "checks": [{"id": c["id"], "status": c["status"], "summary": c["summary"]}
                          for c in result["checks"]],
               "files": files}
    print(json.dumps(summary, ensure_ascii=False))
    if result["overall"] == "FAIL":
        return 1
    if result["overall"] == "BLOCKED":
        return 2
    return 0


def cmd_research_compare(a) -> int:
    from sqlalchemy.orm import Session

    from qp.db import get_engine
    from qp.research.compare import compare_research, write_report
    from qp.research.sources import LedgerReader, Phase2Reader, QDReader

    ledger = LedgerReader(a.ledger_dsn) if a.ledger_dsn else None
    qd = QDReader(a.qd_dsn) if a.qd_dsn else None
    phase2 = Phase2Reader(a.phase2_db) if a.phase2_db and Path(a.phase2_db).is_file() else None
    try:
        with Session(get_engine()) as session:
            result = compare_research(session, ledger=ledger, phase2=phase2, qd=qd)
    finally:
        for reader in (ledger, qd):
            if reader is not None:
                reader.close()
    files = write_report(result, a.out) if a.out else []
    print(json.dumps({"overall": result["overall"], "gate_ok": result["gate_ok"],
                      "counts": result["counts"],
                      "checks": [{"id": c["id"], "status": c["status"], "summary": c["summary"]}
                                 for c in result["checks"]],
                      "files": files}, ensure_ascii=False))
    if result["overall"] == "FAIL":
        return 1
    if result["overall"] == "BLOCKED":
        return 2
    return 0


def cmd_user_import_legacy(a) -> int:
    """Read one line '<username>\\t<password_hash>' from stdin (e.g. piped from a read-only QD query).
    Never prints the hash. Creates/updates the single core.user."""
    from sqlalchemy import select
    from qp.auth.passwords import scheme_of
    from qp.db import session_scope
    from qp.models.core import AuditEvent, User
    line = sys.stdin.readline().rstrip("\n")
    if "\t" not in line:
        print("expected '<username>\\t<hash>' on stdin", file=sys.stderr)
        return 2
    username, ph = line.split("\t", 1)
    scheme = scheme_of(ph)
    if scheme == "unknown":
        print("unsupported hash format", file=sys.stderr)
        return 2
    with session_scope() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        others = s.execute(select(User).where(User.username != username)).scalars().all()
        if others:
            print("refusing: a different user already exists (single-account mode)", file=sys.stderr)
            return 3
        if u is None:
            s.add(User(username=username, password_hash=ph, hash_scheme=scheme, role="admin", source=a.source))
            action = "created"
        elif u.hash_scheme == "argon2" and not a.force:
            action = "kept (already argon2; use --force to overwrite)"
        else:
            u.password_hash, u.hash_scheme, u.source = ph, scheme, a.source
            action = "updated"
        s.add(AuditEvent(actor="cli", action="user.import_legacy", ok=True, detail={"scheme": scheme, "result": action}))
    print(f"user {action}; scheme={scheme}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qp")
    sub = p.add_subparsers(dest="cmd", required=True)
    db = sub.add_parser("db").add_subparsers(dest="sub", required=True)
    db.add_parser("upgrade").set_defaults(fn=cmd_db_upgrade)
    jb = sub.add_parser("job").add_subparsers(dest="sub", required=True)
    jb.add_parser("list").set_defaults(fn=cmd_job_list)
    r = jb.add_parser("run")
    r.add_argument("name")
    r.add_argument("--trigger", default=os.getenv("QP_TRIGGER", "manual"))
    r.add_argument("--dedup-key", default=None)
    r.set_defaults(fn=cmd_job_run)
    sh = sub.add_parser("shadow").add_subparsers(dest="sub", required=True)
    sc = sh.add_parser("compare")
    sc.add_argument("--date", required=True)
    sc.add_argument("--legacy", required=True)
    sc.add_argument("--out", default=None)
    sc.add_argument("--hmac-secret-file", default=None)
    sc.set_defaults(fn=cmd_shadow_compare)
    rs = sub.add_parser("research").add_subparsers(dest="sub", required=True)
    rc = rs.add_parser("compare")
    rc.add_argument("--out", default=None)
    rc.add_argument("--ledger-dsn", default=os.getenv("QP_LEDGER_DSN"))
    rc.add_argument("--phase2-db", default=os.getenv("QP_PHASE2_DB", "/var/lib/research-system-phase2/phase2.db"))
    rc.add_argument("--qd-dsn", default=os.getenv("QP_QD_DSN"))
    rc.set_defaults(fn=cmd_research_compare)
    us = sub.add_parser("user").add_subparsers(dest="sub", required=True)
    il = us.add_parser("import-legacy")
    il.add_argument("--stdin", action="store_true", required=True)
    il.add_argument("--source", default="quantdinger:qd_users")
    il.add_argument("--force", action="store_true")
    il.set_defaults(fn=cmd_user_import_legacy)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
