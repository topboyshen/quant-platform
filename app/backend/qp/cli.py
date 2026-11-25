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


def cmd_db_check(_a) -> int:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from qp.db import get_engine

    cfg = _alembic_cfg()
    heads = sorted(ScriptDirectory.from_config(cfg).get_heads())
    with get_engine().connect() as conn:
        current = MigrationContext.configure(
            conn,
            opts={"version_table": "alembic_version", "version_table_schema": "ops"},
        ).get_current_revision()
    ok = current in heads
    print(json.dumps({"ok": ok, "current": current, "heads": heads}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_job_list(_a) -> int:
    from qp.jobs import JOBS

    for n, s in sorted(JOBS.items()):
        print(f"{n}\t{s.schedule_hint or '-'}\t{s.description}")
    return 0


def cmd_job_run(a) -> int:
    from qp.db import get_engine
    from qp.jobs import run_job

    run = run_job(get_engine(), a.name, trigger=a.trigger, dedup_key=a.dedup_key)
    print(
        json.dumps(
            {
                "id": run.id,
                "job": run.job_name,
                "status": run.status,
                "duration_s": run.duration_s,
                "result": run.result,
            },
            default=str,
            ensure_ascii=False,
        )
    )
    return 0 if run.status in ("succeeded", "skipped_dup", "skipped_locked") else 1


def cmd_job_review(a) -> int:
    from qp.db import session_scope
    from qp.jobs.reviews import append_review

    fields: dict[str, str] = {}
    for item in a.field or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"invalid --field {item!r}; expected key=value")
        fields[key.strip()] = value
    with session_scope() as s:
        row = append_review(
            s,
            original_run_id=a.run_id,
            reviewer=a.reviewer,
            reason=a.reason,
            fields=fields,
        )
        out = {"id": row.id, "review_of_run_id": a.run_id, "result": row.result}
    print(json.dumps(out, default=str, ensure_ascii=False))
    return 0


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
    summary = {
        "date": result["date"],
        "overall": result["overall"],
        "gate_ok": result["gate_ok"],
        "counts": result["counts"],
        "checks": [{"id": c["id"], "status": c["status"], "summary": c["summary"]} for c in result["checks"]],
        "files": files,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if result["overall"] == "FAIL":
        return 1
    if result["overall"] == "BLOCKED":
        return 2
    return 0


def cmd_reports_compare(a) -> int:
    import datetime as dt

    from sqlalchemy.orm import Session

    from qp.db import get_engine
    from qp.reports.compare import compare_reports, write_report

    try:
        day = dt.date.fromisoformat(a.date)
    except ValueError:
        print(json.dumps({"ok": False, "error": "bad_date"}))
        return 2
    with Session(get_engine()) as session:
        result = compare_reports(
            session, day=day, daily_dir=a.daily_dir, digest_path=a.digest_json, hermes_originals=a.hermes_originals
        )
    files = write_report(result, a.out) if a.out else []
    print(
        json.dumps(
            {
                "date": result["date"],
                "overall": result["overall"],
                "gate_ok": result["gate_ok"],
                "counts": result["counts"],
                "checks": [{"id": c["id"], "status": c["status"], "summary": c["summary"]} for c in result["checks"]],
                "files": files,
            },
            ensure_ascii=False,
        )
    )
    if result["overall"] == "FAIL":
        return 1
    if result["overall"] == "BLOCKED":
        return 2
    return 0


def cmd_research_etf_score(a) -> int:
    """Import an offline ETF research score snapshot (read-only shadow)."""
    from qp.db import session_scope
    from qp.research import etf_score

    payload = json.loads(Path(a.import_path).read_text())
    with session_scope() as s:
        out = etf_score.import_payload(s, payload)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


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
    print(
        json.dumps(
            {
                "overall": result["overall"],
                "gate_ok": result["gate_ok"],
                "counts": result["counts"],
                "checks": [{"id": c["id"], "status": c["status"], "summary": c["summary"]} for c in result["checks"]],
                "files": files,
            },
            ensure_ascii=False,
        )
    )
    if result["overall"] == "FAIL":
        return 1
    if result["overall"] == "BLOCKED":
        return 2
    return 0


def cmd_decommission_inventory(a) -> int:
    import json

    from qp.decommission.inventory import collect, manifest_sha256

    manifest = collect(paths=a.path)
    manifest["sha256"] = manifest_sha256(manifest)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if a.out:
        from pathlib import Path

        Path(a.out).write_text(text, encoding="utf-8")
        print(
            json.dumps(
                {"ok": True, "out": a.out, "sha256": manifest["sha256"], "totals": manifest["totals"]},
                ensure_ascii=False,
            )
        )
    else:
        print(text, end="")
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
        s.add(
            AuditEvent(actor="cli", action="user.import_legacy", ok=True, detail={"scheme": scheme, "result": action})
        )
    print(f"user {action}; scheme={scheme}")
    return 0


def cmd_chain_import_history(a) -> int:
    """Dry-run by default; --apply requires QP_RECORD_CHAIN=1."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import chain, chain_import

    if a.apply and not chain.enabled():
        print("refusing: QP_RECORD_CHAIN is not enabled", file=sys.stderr)
        return 3
    since = dt.date.fromisoformat(a.since) if a.since else None
    with session_scope() as s:
        out = chain_import.import_history(s, apply=a.apply, since=since)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_chain_window(a) -> int:
    """Record-chain shadow window table (first N native sessions)."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import chain_window

    end = dt.date.fromisoformat(a.end) if a.end else None
    start = dt.date.fromisoformat(a.start) if a.start else None
    with session_scope() as s:
        out = chain_window.report(s, end=end, sessions=a.sessions, start=start)
    text = chain_window.markdown(out) if a.markdown else json.dumps(out, ensure_ascii=False, indent=2, default=str)
    if a.out:
        Path(a.out).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    print(text)
    return 0 if out.get("verdict") != "FAIL" else 1


def cmd_chain_attribution(a) -> int:
    """Read-only performance attribution by eligibility, sector or signal type."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import attribution

    start = dt.date.fromisoformat(a.start) if a.start else None
    end = dt.date.fromisoformat(a.end) if a.end else None
    with session_scope() as s:
        out = attribution.report(s, horizon=a.horizon, start=start, end=end, group_by=a.group_by)
    text = attribution.markdown(out) if a.markdown else json.dumps(out, ensure_ascii=False, indent=2, default=str)
    if a.out:
        Path(a.out).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    print(text)
    return 0


def cmd_chain_compare(a) -> int:
    """Shadow comparison of the chain snapshot against the legacy signals tables."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import chain_compare

    day = dt.date.fromisoformat(a.date)
    with session_scope() as s:
        out = chain_compare.compare(s, day)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out.get("ok") else 1


def cmd_signals_rule_shadow(a) -> int:
    """Experimental rule shadow: record v2 signals or collect their outcomes."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import rule_shadow
    from qp.signals.eod import now_cst

    with session_scope() as s:
        if a.outcomes:
            as_of = dt.date.fromisoformat(a.until) if a.until else None
            out = rule_shadow.collect(s, as_of=as_of)
            s.commit()
        elif a.since:
            out = rule_shadow.backfill(
                s, since=dt.date.fromisoformat(a.since), until=dt.date.fromisoformat(a.until) if a.until else None
            )
            s.commit()
        else:
            day = dt.date.fromisoformat(a.date) if a.date else now_cst().date()
            out = rule_shadow.generate(s, day=day)
            s.commit()
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


def cmd_signals_rotation_shadow(a) -> int:
    """C13 paper account: seed raw history, register next-open start, or advance."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import rotation_shadow

    with session_scope() as s:
        out: dict = {}
        if a.seed_bars:
            payload = json.loads(Path(a.seed_bars).read_text())
            out["seed"] = rotation_shadow.seed_bars(s, payload)
        if a.initialize:
            out["initialize"] = rotation_shadow.initialize(
                s, start_date=dt.date.fromisoformat(a.initialize), phase=a.phase
            )
        if a.step:
            if a.phase != "forward":
                raise ValueError("--step is forward-only")
            out["step"] = rotation_shadow.step(s, as_of=dt.date.fromisoformat(a.date) if a.date else None)
        elif a.until:
            out["advance"] = rotation_shadow.replay(s, until=dt.date.fromisoformat(a.until), phase=a.phase)
        if a.summary or not out:
            out["summary"] = rotation_shadow.summary(s, phase=a.phase, series=False)
        s.commit()
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if all(v.get("ok", True) for v in out.values() if isinstance(v, dict)) else 1


def cmd_signals_shadow_record(a) -> int:
    """Backfill the shadow sample channel from blocked-but-triggered decisions."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import shadow

    since = dt.date.fromisoformat(a.since) if a.since else None
    day = dt.date.fromisoformat(a.date) if a.date else None
    with session_scope() as s:
        out = shadow.record(s, since=since, decision_date=day)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_signals_shadow_outcomes(a) -> int:
    """Collect matured forward returns for shadow samples."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import shadow

    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else None
    with session_scope() as s:
        out = shadow.collect(s, as_of=as_of)
        s.commit()
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


def cmd_signals_outcomes(a) -> int:
    """Collect pending outcomes, or reconcile recorded rows with the trading calendar."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals.outcomes import evaluate_pending, reconcile_recorded

    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else None
    day = dt.date.fromisoformat(a.date) if a.date else None
    with session_scope() as s:
        if a.reconcile:
            out = reconcile_recorded(s, as_of=as_of, limit=a.limit, decision_date=day, dry_run=a.dry_run)
        else:
            out = evaluate_pending(s, as_of=as_of, limit=a.limit, decision_date=day)
        s.commit()
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("ok", True) else 1


def cmd_signals_acceptance(a) -> int:
    """M4 live acceptance invariants for one decision date (default: latest run)."""
    import datetime as dt

    from qp.db import session_scope
    from qp.signals import acceptance

    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    with session_scope() as s:
        if a.date:
            day = dt.date.fromisoformat(a.date)
        else:
            day = acceptance.latest_decision_date(s, today)
            if day is None:
                print(json.dumps({"ok": False, "error": "no_runs"}, ensure_ascii=False))
                return 2
        out = acceptance.evaluate(s, day, today=today)
    text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if out["verdict"] != "FAIL" else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qp")
    sub = p.add_subparsers(dest="cmd", required=True)
    db = sub.add_parser("db").add_subparsers(dest="sub", required=True)
    db.add_parser("upgrade").set_defaults(fn=cmd_db_upgrade)
    db.add_parser("check").set_defaults(fn=cmd_db_check)
    jb = sub.add_parser("job").add_subparsers(dest="sub", required=True)
    jb.add_parser("list").set_defaults(fn=cmd_job_list)
    r = jb.add_parser("run")
    r.add_argument("name")
    r.add_argument("--trigger", default=os.getenv("QP_TRIGGER", "manual"))
    r.add_argument("--dedup-key", default=None)
    r.set_defaults(fn=cmd_job_run)
    rv = jb.add_parser("review")
    rv.add_argument("--run-id", type=int, required=True)
    rv.add_argument("--reviewer", required=True)
    rv.add_argument("--reason", required=True)
    rv.add_argument("--field", action="append", default=[])
    rv.set_defaults(fn=cmd_job_review)
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
    re = rs.add_parser("etf-score")
    re.add_argument("--import", dest="import_path", required=True)
    re.set_defaults(fn=cmd_research_etf_score)
    rp = sub.add_parser("reports").add_subparsers(dest="sub", required=True)
    rpc = rp.add_parser("compare")
    rpc.add_argument("--date", required=True)
    rpc.add_argument("--out", default=None)
    rpc.add_argument("--daily-dir", default=os.getenv("QP_LEGACY_DAILY_DIR"))
    rpc.add_argument("--digest-json", default=os.getenv("QP_RESEARCH_DIGEST_JSON"))
    rpc.add_argument("--hermes-originals", default=os.getenv("QP_HERMES_ORIGINALS_DIR"))
    rpc.set_defaults(fn=cmd_reports_compare)
    ch = sub.add_parser("chain").add_subparsers(dest="sub", required=True)
    ci = ch.add_parser("import-history")
    ci.add_argument("--apply", action="store_true")
    ci.add_argument("--since", default=None)
    ci.set_defaults(fn=cmd_chain_import_history)
    cc = ch.add_parser("compare")
    cc.add_argument("--date", required=True)
    cc.set_defaults(fn=cmd_chain_compare)
    cw = ch.add_parser("window")
    cw.add_argument("--end", default=None)
    cw.add_argument("--start", default=None)
    cw.add_argument("--sessions", type=int, default=5)
    cw.add_argument("--markdown", action="store_true")
    cw.add_argument("--out", default=None)
    cw.set_defaults(fn=cmd_chain_window)
    ca = ch.add_parser("attribution")
    ca.add_argument("--horizon", type=int, default=20)
    ca.add_argument("--start", default=None)
    ca.add_argument("--end", default=None)
    ca.add_argument("--group-by", default="eligibility,sector,signal_type")
    ca.add_argument("--markdown", action="store_true")
    ca.add_argument("--out", default=None)
    ca.set_defaults(fn=cmd_chain_attribution)
    sg = sub.add_parser("signals").add_subparsers(dest="sub", required=True)
    sa = sg.add_parser("acceptance")
    sa.add_argument("--date", default=None, help="decision date (default: latest run on/before --today)")
    sa.add_argument("--today", default=None, help="reference date for maturity math")
    sa.add_argument("--out", default=None)
    sa.set_defaults(fn=cmd_signals_acceptance)
    sr = sg.add_parser("shadow-record")
    sr.add_argument("--since", default=None)
    sr.add_argument("--date", default=None)
    sr.set_defaults(fn=cmd_signals_shadow_record)
    so = sg.add_parser("shadow-outcomes")
    so.add_argument("--as-of", default=None)
    so.set_defaults(fn=cmd_signals_shadow_outcomes)
    ou = sg.add_parser("outcomes")
    ou.add_argument("--as-of", default=None)
    ou.add_argument("--date", default=None)
    ou.add_argument("--limit", type=int, default=500)
    ou.add_argument("--reconcile", action="store_true", help="append chain revisions for recorded rows")
    ou.add_argument("--dry-run", action="store_true", help="list affected rows without appending revisions")
    ou.set_defaults(fn=cmd_signals_outcomes)
    rs = sg.add_parser("rule-shadow")
    rs.add_argument("--date", default=None)
    rs.add_argument("--since", default=None)
    rs.add_argument("--until", default=None)
    rs.add_argument("--outcomes", action="store_true")
    rs.set_defaults(fn=cmd_signals_rule_shadow)
    rr = sg.add_parser("rotation-shadow")
    rr.add_argument("--seed-bars", default=None)
    rr.add_argument("--initialize", default=None, metavar="FIRST_EXECUTION_DATE")
    rr.add_argument("--phase", choices=("forward", "replay"), default="forward")
    rr.add_argument("--until", default=None)
    rr.add_argument("--step", action="store_true")
    rr.add_argument("--date", default=None)
    rr.add_argument("--summary", action="store_true")
    rr.set_defaults(fn=cmd_signals_rotation_shadow)
    dc = sub.add_parser("decommission").add_subparsers(dest="sub", required=True)
    dci = dc.add_parser("inventory")
    dci.add_argument("--out", default=None)
    dci.add_argument("--path", action="append", default=None)
    dci.set_defaults(fn=cmd_decommission_inventory)
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
