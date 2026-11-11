"""Built-in M1 jobs: ops.healthcheck, ops.backup, notify.send (shadow only)."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.jobs.registry import JobContext, job
from qp.models.base import utcnow
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.ops import HealthCheck


def _mem_available_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def enqueue(s: Session, *, channel: str, recipient: str, dedup_key: str, body: str,
            subject: str | None = None, source_job_run_id: int | None = None) -> bool:
    """Insert into notify.outbox unless dedup_key exists. Mode is forced to shadow in M1."""
    exists = s.execute(select(Outbox.id).where(Outbox.dedup_key == dedup_key)).first()
    if exists:
        return False
    s.add(Outbox(channel=channel, recipient=recipient, dedup_key=dedup_key, body=body, subject=subject,
                 mode="shadow", status="pending", source_job_run_id=source_job_run_id))
    return True


@job("ops.healthcheck", "DB, disk, memory and API health; failures go to notify outbox (shadow)",
     schedule_hint="every 5 min (qp-healthcheck.timer)")
def healthcheck(ctx: JobContext) -> dict:
    st = get_settings()
    checks: list[tuple[str, bool, float | None, dict]] = []
    try:
        with ctx.engine.connect() as c:
            c.execute(text("SELECT 1"))
        checks.append(("db", True, None, {}))
    except Exception as e:
        checks.append(("db", False, None, {"error": type(e).__name__}))
    du = shutil.disk_usage("/")
    free_gb = du.free / 1024**3
    checks.append(("disk_root_free_gb", free_gb >= 5, round(free_gb, 2), {}))
    mem = _mem_available_mb()
    checks.append(("mem_available_mb", mem is None or mem >= 600, round(mem, 1) if mem else None, {}))
    try:
        with urllib.request.urlopen(st.api_health_url, timeout=5) as r:
            checks.append(("api", r.status == 200, None, {"status": r.status}))
    except Exception as e:
        checks.append(("api", False, None, {"error": type(e).__name__}))
    ok = all(c[1] for c in checks)
    with Session(ctx.engine) as s:
        for name, cok, val, det in checks:
            s.add(HealthCheck(check=name, ok=cok, value=val, detail=det, job_run_id=ctx.run_id))
        if not ok:
            failed = ",".join(c[0] for c in checks if not c[1])
            hour = utcnow().strftime("%Y%m%dT%H")
            enqueue(s, channel="telegram", recipient="ops", dedup_key=f"ops.healthcheck:{failed}:{hour}",
                    subject="qp healthcheck failed", body=f"qp healthcheck failed: {failed}",
                    source_job_run_id=ctx.run_id)
        s.commit()
    return {"ok": ok, "checks": {c[0]: {"ok": c[1], "value": c[2]} for c in checks}}


@job("ops.backup", "pg_dump of database qp (custom format), keep N days",
     schedule_hint="daily 03:50 CST (qp-backup.timer)",
     dedup=lambda: "day:" + dt.datetime.now().strftime("%Y-%m-%d"))
def backup(ctx: JobContext) -> dict:
    st = get_settings()
    d = Path(st.backup_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out = d / f"qp-{stamp}.dump"
    url = ctx.engine.url
    cmd = ["pg_dump", "-Fc", "--no-owner", "-f", str(out)]
    host = url.host or url.query.get("host")
    if host:
        cmd += ["-h", host if isinstance(host, str) else host[0]]
    if url.port:
        cmd += ["-p", str(url.port)]
    if url.username:
        cmd += ["-U", url.username]
    cmd += [url.database or "qp"]
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    p = subprocess.run(cmd, env=env, timeout=1800, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"pg_dump exit {p.returncode}: {p.stderr.strip()[-300:]}")
    os.chmod(out, 0o600)
    import hashlib
    hsh = hashlib.sha256()
    with out.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hsh.update(chunk)
    h = hsh.hexdigest()
    cutoff = dt.datetime.now() - dt.timedelta(days=st.backup_keep_days)
    removed = []
    for f in sorted(d.glob("qp-*.dump")):
        if f != out and dt.datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            removed.append(f.name)
    return {"ok": True, "file": out.name, "bytes": out.stat().st_size, "sha256": h, "removed": removed,
            "keep_days": st.backup_keep_days}


@job("notify.send", "Process notify.outbox. M1: shadow sender only - records attempts, never sends",
     schedule_hint="manual / future timer")
def notify_send(ctx: JobContext) -> dict:
    st = get_settings()
    n = 0
    with Session(ctx.engine) as s:
        rows = s.execute(select(Outbox).where(Outbox.status == "pending").order_by(Outbox.id).limit(100)
                         .with_for_update(skip_locked=True)).scalars().all()
        for o in rows:
            # M1 hard rule: never send. Even if QP_NOTIFY_MODE says otherwise, only shadow is implemented.
            s.add(DeliveryAttempt(outbox_id=o.id, mode="shadow", ok=None,
                                  detail={"note": "shadow mode: not sent", "configured_mode": st.notify_mode}))
            o.status, o.send_result, o.processed_at = "shadowed", "not_sent_shadow", utcnow()
            n += 1
        s.commit()
    return {"ok": True, "shadowed": n, "sent": 0}
