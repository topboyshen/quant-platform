"""M6 report shadow comparator (S1-S8).

Read-only: legacy files are opened read-only and qp tables are only read; the
optional JSON/Markdown report under ``--out`` is the only write.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.reports import Report, ReportDelivery, ReportReceipt, ReportSource
from qp.models.signals import ExecutionDigest
from qp.reports.legacy import (digest, file_sha256, legacy_daily_path, load_legacy_daily,
                               validate_legacy_daily)
from qp.reports.hermes import find_envelope, read_body

UTC = dt.timezone.utc
STATUS_ORDER = {"PASS": 0, "WARN": 1, "BLOCKED": 2, "FAIL": 3}
HERMES_JOBS = {"profit_summary": "ec6c86fd856c", "profit_radar_health": "026a1083d9e0"}
SECTION_KEYS = ("index_closes", "index_close_cache", "macro", "strategies")


def _check(cid: str, status: str, summary: str, *, diffs: Iterable[str] = (),
           details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": cid, "status": status, "summary": summary,
            "diffs": [str(d) for d in diffs], "details": details or {}}


def _blocked(cid: str, reason: str) -> dict[str, Any]:
    return _check(cid, "BLOCKED", reason)


def _worst(checks: list[dict[str, Any]]) -> str:
    return max((c["status"] for c in checks),
               key=lambda s: STATUS_ORDER.get(s, 3), default="PASS")


def _latest_report(session: Session, kind: str, day: dt.date) -> Report | None:
    return session.scalar(select(Report).where(Report.kind == kind, Report.business_date == day)
                          .order_by(Report.version.desc()).limit(1))


def _s1_daily(session: Session, day: dt.date, daily_dir: str | Path | None) -> dict[str, Any]:
    report = _latest_report(session, "daily_research_summary", day)
    if report is None:
        return _blocked("S1", "no frozen daily report for the date")
    if daily_dir is None:
        return _blocked("S1", "legacy daily dir not configured")
    path = legacy_daily_path(daily_dir, day)
    if path is None:
        return _blocked("S1", f"legacy daily file missing for {day}")
    legacy = load_legacy_daily(path)
    errors = validate_legacy_daily(legacy)
    diffs = [f"legacy_integrity:{e}" for e in errors]
    if digest(report.payload) != digest(legacy):
        diffs.append("payload_digest_differs")
    return _check("S1", "FAIL" if diffs else "PASS",
                  "daily report matches the legacy frozen file" + ("" if not diffs else " differs"),
                  diffs=diffs,
                  details={"report_id": report.id, "legacy_file": path.name,
                           "content_sha256": report.content_sha256})


def _s2_research(session: Session, day: dt.date, digest_path: str | Path | None) -> dict[str, Any]:
    report = _latest_report(session, "research_digest", day)
    if report is None:
        return _blocked("S2", "no frozen research digest for the date")
    if digest_path is None or not Path(digest_path).is_file():
        return _blocked("S2", "legacy research digest file missing")
    current = json.loads(Path(digest_path).read_text(encoding="utf-8"))
    archived = report.payload if isinstance(report.payload, dict) else {}
    diffs, warnings = [], []
    for key in SECTION_KEYS:
        if archived.get(key) != current.get(key):
            diffs.append(f"section_differs:{key}")
    if digest(archived) != digest(current) and not diffs:
        warnings.append("generated_at_or_recent_runs_differs")
    status = "FAIL" if diffs else ("WARN" if warnings else "PASS")
    return _check("S2", status, "research digest sections" + ("" if not diffs else " differ"),
                  diffs=diffs + warnings, details={"report_id": report.id})


def _s3_execution(session: Session, day: dt.date) -> dict[str, Any]:
    report = _latest_report(session, "execution_digest", day)
    row = session.get(ExecutionDigest, day)
    if report is None or row is None:
        return _blocked("S3", f"execution digest missing (report={report is not None}, "
                              f"m4={row is not None})")
    payload = report.payload if isinstance(report.payload, dict) else {}
    diffs = []
    if payload.get("sha256") != row.sha256:
        diffs.append("digest_sha256_differs")
    if payload.get("body") != row.body:
        diffs.append("digest_body_differs")
    return _check("S3", "FAIL" if diffs else "PASS",
                  "execution digest references the M4 body" + ("" if not diffs else " differs"),
                  diffs=diffs, details={"report_id": report.id, "sha256": row.sha256})


def _s4_hermes(session: Session, day: dt.date, originals: str | Path | None) -> dict[str, Any]:
    if originals is None:
        return _blocked("S4", "hermes originals dir not configured")
    diffs, warnings, checked = [], [], {}
    for kind, job_id in HERMES_JOBS.items():
        report = _latest_report(session, kind, day)
        if report is None:
            diffs.append(f"missing_report:{kind}")
            continue
        payload = report.payload if isinstance(report.payload, dict) else {}
        envelope = find_envelope(originals, job_id, day)
        if envelope is None:
            diffs.append(f"missing_envelope:{kind}")
            continue
        checked[kind] = {"report_version": report.version,
                         "envelope_body_sha256": envelope.get("bodySha256")}
        if envelope.get("bodySha256") != payload.get("bodySha256"):
            warnings.append(f"newer_legacy_run_not_archived:{kind}")
            continue
        body = read_body(envelope)
        if body != payload.get("text"):
            diffs.append(f"body_differs:{kind}")
    status = "FAIL" if diffs else ("WARN" if warnings else "PASS")
    return _check("S4", status, "hermes summaries match their legacy envelopes"
                  + ("" if not diffs else " differ"), diffs=diffs + warnings, details=checked)


def _s5_frozen(session: Session) -> dict[str, Any]:
    reports = session.scalars(select(Report)).all()
    diffs = [f"content_sha256_mismatch:{r.id}" for r in reports
             if r.content_sha256 != digest(r.payload)]
    return _check("S5", "FAIL" if diffs else "PASS",
                  "frozen reports are internally consistent" + ("" if not diffs else " violated"),
                  diffs=diffs, details={"reports": len(reports)})


def _s6_shadow(session: Session) -> dict[str, Any]:
    modes = session.execute(select(ReportDelivery.mode, func.count())
                            .group_by(ReportDelivery.mode)).all()
    non_shadow = [f"delivery_mode:{mode}={n}" for mode, n in modes if mode != "shadow"]
    outbox = session.execute(select(Outbox.mode, func.count())
                             .where(Outbox.dedup_key.like("report:%"))
                             .group_by(Outbox.mode)).all()
    non_shadow += [f"outbox_mode:{mode}={n}" for mode, n in outbox if mode != "shadow"]
    attempts = session.scalar(select(func.count()).select_from(DeliveryAttempt)) or 0
    if attempts:
        non_shadow.append(f"delivery_attempt_rows={attempts}")
    return _check("S6", "FAIL" if non_shadow else "PASS",
                  "report delivery stays shadow-only" + ("" if not non_shadow else " violated"),
                  diffs=non_shadow,
                  details={"deliveries": {m: n for m, n in modes}, "delivery_attempt": attempts})


def _s7_receipts(session: Session) -> dict[str, Any]:
    receipts = session.execute(
        select(ReportReceipt.status, ReportDelivery.status, func.count())
        .join(ReportDelivery, ReportReceipt.delivery_id == ReportDelivery.id)
        .group_by(ReportReceipt.status, ReportDelivery.status)).all()
    diffs = [f"receipt_on_unattempted_delivery:{rs}/{ds}={n}"
             for rs, ds, n in receipts if rs == "received" and ds == "pending"]
    total = sum(n for _, _, n in receipts)
    return _check("S7", "FAIL" if diffs else "PASS",
                  "receipts are separate from send results" + ("" if not diffs else " violated"),
                  diffs=diffs,
                  details={"receipt_rows": total,
                           "by_status": [{"receipt": rs, "delivery": ds, "n": n}
                                         for rs, ds, n in receipts]})


def _s8_sources(session: Session) -> dict[str, Any]:
    diffs, checked = [], 0
    for source in session.scalars(select(ReportSource)).all():
        if source.source not in ("legacy_daily_summary", "hermes_cron") or not source.locator:
            continue
        checked += 1
        path = Path(source.locator)
        if not path.is_file():
            diffs.append(f"source_missing:{source.id}")
        elif source.sha256 and file_sha256(path) != source.sha256:
            diffs.append(f"source_changed:{source.id}")
    return _check("S8", "FAIL" if diffs else "PASS",
                  "legacy source files are unchanged" + ("" if not diffs else " violated"),
                  diffs=diffs, details={"checked": checked})


MORNING_KIND = "morning_chain"
FORBIDDEN_KEYS = {"chat_id", "message_id", "evidence", "attempts", "token", "secret"}


def _keys(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            out.add(str(key))
            out |= _keys(item)
    elif isinstance(value, list):
        for item in value:
            out |= _keys(item)
    return out


def _check_morning(session: Session, day: dt.date) -> list[dict[str, Any]]:
    report = _latest_report(session, MORNING_KIND, day)
    if report is None:
        return []
    payload = report.payload if isinstance(report.payload, dict) else {}
    generation = payload.get("generation") or {}
    seal = payload.get("seal") or {}
    relay = payload.get("relay") or {}
    alerts = payload.get("alerts")
    classification = payload.get("classification") or {}

    a1_diffs = []
    if generation.get("target_date") != day.isoformat():
        a1_diffs.append("generation_target_date")
    if "status" not in generation or "child_exit_code" not in generation:
        a1_diffs.append("generation_fields")
    a1 = _check("A1", "FAIL" if a1_diffs else "PASS",
                "generation state recorded" + ("" if not a1_diffs else " incomplete"),
                diffs=a1_diffs, details={"status": generation.get("status"),
                                         "exit": generation.get("child_exit_code")})

    a2_diffs = []
    if seal.get("state") != "SEALED":
        a2_diffs.append("seal_state")
    if seal.get("target_matches") is False or seal.get("report_sha_matches") is False:
        a2_diffs.append("seal_binding")
    if seal.get("digest_present") is not True:
        a2_diffs.append("seal_digest_missing")
    a2 = _check("A2", "FAIL" if a2_diffs else "PASS",
                "seal is valid" + ("" if not a2_diffs else " invalid"),
                diffs=a2_diffs, details={"report_sha256": seal.get("report_sha256")})

    a3_diffs, a3_warn = [], []
    if relay.get("target_matches") is False:
        a3_diffs.append("relay_target_date")
    if relay.get("produced_date") and relay["produced_date"] != day.isoformat():
        a3_diffs.append("relay_produced_date")
    if not relay.get("attempt") and not relay.get("delivery"):
        a3_warn.append("relay_attempt_missing")
    a3 = _check("A3", "FAIL" if a3_diffs else ("WARN" if a3_warn else "PASS"),
                "relay evidence recorded" + ("" if not a3_diffs else " inconsistent"),
                diffs=a3_diffs + a3_warn,
                details={"attempt": (relay.get("attempt") or {}).get("status"),
                         "delivery": (relay.get("delivery") or {}).get("status")})

    a4_diffs, a4_warn = [], []
    if alerts is None:
        a4_warn.append("alert_state_missing")
    elif classification.get("matches_legacy") is False:
        a4_diffs += [f"missing_in_legacy:{x}" for x in classification.get("missing_in_legacy", [])]
        a4_diffs += [f"extra_in_legacy:{x}" for x in classification.get("extra_in_legacy", [])]
    a4 = _check("A4", "FAIL" if a4_diffs else ("WARN" if a4_warn else "PASS"),
                "classification matches legacy alerts" + ("" if not a4_diffs else " differs"),
                diffs=a4_diffs + a4_warn,
                details={"state": classification.get("state")})

    deliveries = session.execute(select(ReportDelivery)
                                 .where(ReportDelivery.report_id == report.id)).scalars().all()
    a5_diffs = [f"delivery_mode:{d.mode}" for d in deliveries if d.mode != "shadow"]
    attempts = session.scalar(select(func.count()).select_from(DeliveryAttempt)) or 0
    if attempts:
        a5_diffs.append(f"delivery_attempt_rows={attempts}")
    a5 = _check("A5", "FAIL" if a5_diffs else "PASS",
                "morning chain stays read-only/shadow" + ("" if not a5_diffs else " violated"),
                diffs=a5_diffs, details={"deliveries": len(deliveries)})

    forbidden = sorted(_keys(payload) & FORBIDDEN_KEYS)
    a6 = _check("A6", "FAIL" if forbidden else "PASS",
                "no sensitive fields in the snapshot" + ("" if not forbidden else " violated"),
                diffs=[f"forbidden_key:{k}" for k in forbidden])
    return [a1, a2, a3, a4, a5, a6]


def compare_reports(session: Session, *, day: dt.date, daily_dir: str | Path | None = None,
                    digest_path: str | Path | None = None,
                    hermes_originals: str | Path | None = None,
                    now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(UTC)
    checks = [_s1_daily(session, day, daily_dir), _s2_research(session, day, digest_path),
              _s3_execution(session, day), _s4_hermes(session, day, hermes_originals),
              _s5_frozen(session), _s6_shadow(session), _s7_receipts(session),
              _s8_sources(session)]
    checks.extend(_check_morning(session, day))
    overall = _worst(checks)
    counts = Counter(c["status"] for c in checks)
    return {"schema_version": 1, "date": day.isoformat(),
            "generated_at": now.isoformat(timespec="seconds"),
            "overall": overall, "gate_ok": overall in ("PASS", "WARN"),
            "counts": {k: counts.get(k, 0) for k in ("PASS", "WARN", "BLOCKED", "FAIL")},
            "checks": checks}


def write_report(result: dict[str, Any], out_dir: str | Path) -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = result["date"]
    jpath = out / f"{stamp}.reports-compare.json"
    mpath = out / f"{stamp}.reports-compare.md"
    jpath.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# qp M6 报告影子对比（{stamp}）", "",
             f"- 生成时间：{result['generated_at']}",
             f"- **结论：{result['overall']}**（PASS {result['counts']['PASS']} / "
             f"WARN {result['counts']['WARN']} / BLOCKED {result['counts']['BLOCKED']} / "
             f"FAIL {result['counts']['FAIL']}）", "",
             "| 检查 | 状态 | 摘要 |", "|---|---|---|"]
    lines += [f"| {c['id']} | {c['status']} | {c['summary']} |" for c in result["checks"]]
    diffs = [(c["id"], d) for c in result["checks"] for d in c.get("diffs", [])]
    if diffs:
        lines += ["", "## 差异", ""] + [f"- {cid}: {d}" for cid, d in diffs]
    mpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(jpath), str(mpath)]
