"""ops.inventory: ingest the read-only legacy snapshot written by qp-collect, update the task center
(ops.legacy_unit), latest inventory, health metrics, and evaluate alert rules (ops.alert + shadow outbox)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from qp.jobs.builtin import enqueue
from qp.jobs.registry import JobContext, job
from qp.models.base import utcnow
from qp.models.ops import Alert, HealthCheck, Inventory, LegacyUnit
from qp.retired import RETIRED_CONTAINERS

INBOX = Path(os.getenv("QP_INBOX_FILE", "/var/lib/qp-inbox/legacy.json"))
STALE_MIN = 30
CERT_WARN_HOURS = 48
DISK_FREE_MIN_GB = 5.0
HOST_MEM_MIN_MB = 600
LOG_ERROR_MIN = 50
OS_UNIT = re.compile(
    r"^(apt|apport|dpkg|e2scrub|fstrim|fwupd|logrotate|man-db|motd|snap|systemd-|ua-|update-notifier|"
    r"esm-|ubuntu-|sysstat|launchpadlib|plymouth|cloud-|getty|user@|dbus|polkit|ssh|cron|rsyslog|"
    r"unattended|multipathd|lvm2|grub|kmod|keyboard|console|networkd|netplan|tat_agent|YunJing|"
    r"stargate|inetutils)",
    re.I,
)
CATEGORIES = [  # display only; order matters
    ("morning_chain", r"^(astock-|nng8-research-digest|research-chain-alert)"),
    ("research", r"^(research-|quant-research-)"),
    ("radar", r"^(stock-radar|radar-|quant-approved|quant-execution|qd-approved)"),
    ("quantdinger", r"^(qd-|quantdinger)"),
    ("data", r"^(signal-bus|research-data|trading-radar-sqlite)"),
    ("hermes", r"^hermes"),
    ("qp", r"^qp-"),
    ("ops", r"^(nng8-|quant-|mihomo|nginx|docker|postgresql)"),
]


def _ts(v: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(v) if v else None


def categorize(name: str) -> str:
    if OS_UNIT.match(name):
        return "os"
    for cat, rx in CATEGORIES:
        if re.match(rx, name):
            return cat
    return "other"


def upsert_units(s: Session, scope: str, data: dict) -> list[dict]:
    if not data or not data.get("ok"):
        return []
    triggers = {t["activates"]: t for t in data.get("timers", []) if t.get("activates")}
    timers = {t["timer"]: t for t in data.get("timers", []) if t.get("timer")}
    rows = []
    for u in data.get("units", []):
        name = u["name"]
        kind = "timer" if name.endswith(".timer") else "service"
        t = timers.get(name) if kind == "timer" else None
        trig = triggers.get(name) if kind == "service" else None
        rows.append(
            dict(
                scope=scope,
                name=name,
                kind=kind,
                description=u.get("description"),
                load_state=u.get("load"),
                active_state=u.get("active"),
                sub_state=u.get("sub"),
                result=u.get("result"),
                exit_status=u.get("exit_status"),
                run_user=u.get("run_user"),
                fragment=u.get("fragment"),
                activates=t.get("activates") if t else None,
                triggered_by=trig.get("timer") if trig else None,
                last_trigger=_ts((t or trig or {}).get("last")),
                next_elapse=_ts((t or trig or {}).get("next")),
                last_start=_ts(u.get("last_start")),
                last_exit=_ts(u.get("last_exit")),
                category=categorize(name),
                tags=[],
                present=True,
                updated_at=utcnow(),
            )
        )
    s.execute(update(LegacyUnit).where(LegacyUnit.scope == scope).values(present=False))
    for r in rows:
        stmt = insert(LegacyUnit).values(**r)
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=["scope", "name"], set_={k: stmt.excluded[k] for k in r if k not in ("scope", "name")}
            )
        )
    return rows


def set_inventory(s: Session, kind: str, at: dt.datetime, data: Any) -> None:
    stmt = insert(Inventory).values(kind=kind, at=at, data=data)
    s.execute(stmt.on_conflict_do_update(index_elements=["kind"], set_={"at": at, "data": data}))


def pg_stats(s: Session) -> dict:
    rows = s.execute(
        text(
            "SELECT datname, CASE WHEN has_database_privilege(datname, 'CONNECT') THEN pg_database_size(datname) END "
            "FROM pg_database WHERE NOT datistemplate"
        )
    ).all()
    conns = s.execute(text("SELECT count(*) FROM pg_stat_activity")).scalar_one()
    maxc = s.execute(text("SHOW max_connections")).scalar_one()
    return {"databases": {r[0]: r[1] for r in rows}, "connections": conns, "max_connections": int(maxc)}


def evaluate(snap: dict, unit_rows: list[dict], age_min: float) -> list[tuple[str, str, str, dict]]:
    """Return list of (key, severity, summary, detail) currently firing."""
    f: list[tuple[str, str, str, dict]] = []
    if age_min > STALE_MIN:
        f.append(("collector:stale", "warning", f"legacy collector snapshot is {age_min:.0f} min old", {}))
    for r in unit_rows:
        if r["kind"] != "service" or r["category"] == "os" or r["load_state"] == "not-found":
            continue
        if r["active_state"] == "failed" or (
            r["result"] not in (None, "success") and r["active_state"] != "activating"
        ):
            f.append(
                (
                    f"unit:{r['scope']}:{r['name']}",
                    "warning",
                    f"{r['scope']} unit {r['name']} result={r['result']} state={r['active_state']}",
                    {"category": r["category"], "last_exit": str(r["last_exit"])},
                )
            )
    for c in snap.get("containers") or []:
        if c.get("name") in RETIRED_CONTAINERS:
            continue
        if c.get("state") != "running" or c.get("health") not in (None, "healthy"):
            f.append(
                (
                    f"container:{c.get('name')}",
                    "critical" if c.get("state") != "running" else "warning",
                    f"container {c.get('name')} state={c.get('state')} health={c.get('health')}",
                    {},
                )
            )
    for ct in snap.get("certs") or []:
        h = ct.get("hours_left")
        if not ct.get("ok"):
            f.append((f"cert:{ct['target']}", "warning", f"cert check failed on :{ct['target']}", {}))
        elif h is not None and h < CERT_WARN_HOURS:
            f.append(
                (
                    f"cert:{ct['target']}",
                    "critical" if h < 24 else "warning",
                    f"TLS cert on :{ct['target']} expires in {h:.0f} h",
                    {"not_after": ct.get("not_after")},
                )
            )
    for e in snap.get("egress") or []:
        if e.get("via_proxy") and not e.get("ok"):
            f.append((f"egress:{e['target']}", "warning", f"egress {e['target']} unreachable via proxy", {}))
    hostd = snap.get("host") or {}
    free = (hostd.get("disk_root") or {}).get("free_gb")
    if free is not None and free < DISK_FREE_MIN_GB:
        f.append(("host:disk", "critical" if free < 2 else "warning", f"root disk free {free} GB", {}))
    avail = (hostd.get("mem_mb") or {}).get("MemAvailable")
    if avail is not None and avail < HOST_MEM_MIN_MB:
        f.append(("host:mem", "warning", f"MemAvailable {avail} MB", {}))
    for scope, lg in (snap.get("logs") or {}).items():
        for unit, n in (lg.get("counts") or {}).items():
            if n >= LOG_ERROR_MIN:
                f.append(
                    (
                        f"logs:{scope}:{unit}",
                        "warning",
                        f"{n} error-level log lines in {lg.get('window_min')} min from {unit}",
                        {},
                    )
                )
    return f


def recovery_evidence(snap: dict, unit_rows: list[dict], age_min: float, open_keys: set[str]) -> set[str]:
    """Return open-alert keys this snapshot positively observed as healthy.

    Only an explicit healthy observation counts.  A failed collector section, a missing
    unit/container/cert/egress record, or any key this snapshot does not cover stays out
    of the result, so apply_alerts keeps the old alert firing instead of treating
    absence as recovery.
    """
    units = {f"unit:{r['scope']}:{r['name']}": r for r in unit_rows}
    containers = {f"container:{c.get('name')}": c for c in snap.get("containers") or [] if c.get("name")}
    certs = {f"cert:{c.get('target')}": c for c in snap.get("certs") or [] if c.get("target") is not None}
    egress = {f"egress:{e.get('target')}": e for e in snap.get("egress") or [] if e.get("target")}
    logs = snap.get("logs") or {}
    host = snap.get("host") or {}
    recovered: set[str] = set()
    for key in open_keys:
        if key == "collector:stale":
            if age_min <= STALE_MIN:
                recovered.add(key)
        elif key.startswith("unit:"):
            r = units.get(key)
            if (
                r
                and r["kind"] == "service"
                and r["category"] != "os"
                and r["load_state"] != "not-found"
                and r["active_state"] != "failed"
                and (r["result"] in (None, "success") or r["active_state"] == "activating")
            ):
                recovered.add(key)
        elif key.startswith("container:"):
            c = containers.get(key)
            if c and c.get("name") in RETIRED_CONTAINERS:
                recovered.add(key)  # retired: an old alert is obsolete, not a recovery claim
            elif c and c.get("state") == "running" and c.get("health") in (None, "healthy"):
                recovered.add(key)
        elif key.startswith("cert:"):
            c = certs.get(key)
            if c and c.get("ok"):
                left = c.get("hours_left")
                if left is None or left >= CERT_WARN_HOURS:
                    recovered.add(key)
        elif key.startswith("egress:"):
            e = egress.get(key)
            if e and not (e.get("via_proxy") and not e.get("ok")):
                recovered.add(key)
        elif key == "host:disk":
            free = (host.get("disk_root") or {}).get("free_gb")
            if free is not None and free >= DISK_FREE_MIN_GB:
                recovered.add(key)
        elif key == "host:mem":
            avail = (host.get("mem_mb") or {}).get("MemAvailable")
            if avail is not None and avail >= HOST_MEM_MIN_MB:
                recovered.add(key)
        elif key.startswith("logs:"):
            parts = key.split(":", 2)
            lg = (logs.get(parts[1]) or {}) if len(parts) == 3 and isinstance(logs, dict) else {}
            counts = lg.get("counts") if isinstance(lg, dict) else None
            if (
                isinstance(lg, dict)
                and lg.get("ok") is not False
                and isinstance(counts, dict)
                and counts.get(parts[2], 0) < LOG_ERROR_MIN
            ):
                recovered.add(key)
    return recovered


def apply_alerts(
    s: Session,
    firing: list[tuple[str, str, str, dict]],
    run_id: int,
    owns=lambda key: not key.startswith("market:"),
    keep_open: set[str] | None = None,
    *,
    only_resolve: set[str],
) -> dict:
    """Upsert firing alerts. Close an owned open alert only with valid recovery evidence.

    - firing: keys known to be in fault this run.
    - keep_open: keys whose evidence is unknown/insufficient; never auto-resolved.
    - only_resolve: required set of keys with explicit recovery evidence for this run.
      An owned open alert is resolved only when its key is in this set; keys absent from
      this run, unknown, insufficient, or not collected stay firing.
    """
    now = utcnow()
    keep = set(keep_open or ())
    recover = set(only_resolve)
    open_alerts = {a.key: a for a in s.execute(select(Alert).where(Alert.state == "firing")).scalars() if owns(a.key)}
    new = 0
    for key, sev, summary, detail in firing:
        a = open_alerts.pop(key, None)
        if a is None:
            s.add(Alert(key=key, severity=sev, state="firing", summary=summary, last_seen_at=now, detail=detail))
            enqueue(
                s,
                channel="telegram",
                recipient="ops",
                dedup_key=f"alert:{key}:{now:%Y%m%dT%H%M}",
                subject=f"[{sev}] {key}",
                body=summary,
                source_job_run_id=run_id,
            )
            new += 1
        else:
            a.severity, a.summary, a.last_seen_at, a.detail = sev, summary, now, detail
    resolved = 0
    kept = 0
    for a in open_alerts.values():
        if a.key in keep or a.key not in recover:
            kept += 1
            continue
        a.state, a.resolved_at = "resolved", now
        resolved += 1
    return {"firing": len(firing), "new": new, "resolved": resolved, "kept_open": kept}


@job(
    "ops.inventory",
    "Ingest legacy read-only snapshot: task center, inventory, monitors, alerts (shadow)",
    schedule_hint="after qp-collect (every 10 min outside change-freeze windows)",
)
def inventory(ctx: JobContext) -> dict:
    if not INBOX.is_file():
        return {"ok": False, "error": "no snapshot"}
    snap = json.loads(INBOX.read_text())
    at = dt.datetime.fromisoformat(snap["collected_at"])
    age_min = (utcnow() - at).total_seconds() / 60
    with Session(ctx.engine) as s:
        units = upsert_units(s, "system", snap.get("systemd_system") or {}) + upsert_units(
            s, "user", snap.get("systemd_user") or {}
        )
        for kind in ("containers", "ports", "certs", "egress", "logs", "host"):
            set_inventory(s, kind, at, snap.get(kind))
        pg = pg_stats(s)
        set_inventory(s, "pg", utcnow(), pg)
        set_inventory(
            s,
            "collector",
            at,
            {"duration_s": snap.get("duration_s"), "host": snap.get("host"), "age_min": round(age_min, 1)},
        )
        hostd = snap.get("host") or {}
        metrics = [
            (
                "legacy.units_failed",
                sum(
                    1
                    for u in units
                    if u["kind"] == "service"
                    and u["category"] != "os"
                    and (u["active_state"] == "failed" or u["result"] not in (None, "success"))
                ),
            ),
            (
                "legacy.containers_unhealthy",
                sum(
                    1
                    for c in snap.get("containers") or []
                    if c.get("name") not in RETIRED_CONTAINERS
                    and (c.get("state") != "running" or c.get("health") not in (None, "healthy"))
                ),
            ),
            ("host.disk_free_gb", (hostd.get("disk_root") or {}).get("free_gb")),
            ("host.mem_available_mb", (hostd.get("mem_mb") or {}).get("MemAvailable")),
            ("pg.connections", pg["connections"]),
        ]
        for ct in snap.get("certs") or []:
            metrics.append((f"cert.{ct['target']}.hours_left", ct.get("hours_left")))
        for e in snap.get("egress") or []:
            metrics.append((f"egress.{e['target']}.ms", e.get("ms") if e.get("ok") else None))
        for name, val in metrics:
            s.add(HealthCheck(check=f"inv:{name}", ok=val is not None, value=val, detail={}, job_run_id=ctx.run_id))
        firing = evaluate(snap, units, age_min)
        open_keys = {
            a.key
            for a in s.execute(select(Alert).where(Alert.state == "firing")).scalars()
            if not a.key.startswith("market:")
        }
        recover = recovery_evidence(snap, units, age_min, open_keys)
        res = apply_alerts(s, firing, ctx.run_id, only_resolve=recover)
        # retention: keep 30 days of health rows
        s.execute(delete(HealthCheck).where(HealthCheck.at < utcnow() - dt.timedelta(days=30)))
        s.commit()
    return {"ok": True, "units": len(units), "snapshot_age_min": round(age_min, 1), "alerts": res}
