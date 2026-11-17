"""Built-in M1 jobs: ops.healthcheck, ops.backup, notify.send (shadow or live)."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from qp.config import get_settings
from qp.jobs.registry import JobContext, job
from qp.models.base import utcnow
from qp.models.notify import DeliveryAttempt, Outbox
from qp.models.ops import HealthCheck
from qp.notify.render import render_outbox
from qp.notify.telegram import NotifyConfigError, TelegramError, TelegramSender


def _mem_available_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def enqueue(
    s: Session,
    *,
    channel: str,
    recipient: str,
    dedup_key: str,
    body: str,
    subject: str | None = None,
    source_job_run_id: int | None = None,
    mode: str | None = None,
) -> bool:
    """Insert into notify.outbox unless dedup_key exists.

    ``mode`` defaults to ``QP_NOTIFY_MODE`` (shadow).  A module that has completed its
    own live cutover passes ``mode="live"``; shadow rows are never sent retroactively.
    """
    effective = (mode if mode is not None else get_settings().notify_mode).strip().lower()
    if effective not in {"shadow", "live"}:
        effective = "shadow"
    exists = s.execute(select(Outbox.id).where(Outbox.dedup_key == dedup_key)).first()
    if exists:
        return False
    s.add(
        Outbox(
            channel=channel,
            recipient=recipient,
            dedup_key=dedup_key,
            body=body,
            subject=subject,
            mode=effective,
            status="pending",
            source_job_run_id=source_job_run_id,
        )
    )
    return True


# A deploy restarts qp-api in place; a single 5s probe can land exactly in that
# window. Retry briefly so a planned restart is not recorded as a health failure.
API_PROBE_ATTEMPTS = int(os.getenv("QP_HEALTHCHECK_API_ATTEMPTS", "3"))
API_PROBE_DELAY_SEC = float(os.getenv("QP_HEALTHCHECK_API_DELAY_SEC", "1.5"))
BACKUP_MAX_AGE_HOURS = float(os.getenv("QP_BACKUP_MAX_AGE_HOURS", "36"))


def _backup_age_hours(directory: str) -> float | None:
    """Age of the newest qp-*.dump, or None when there is no dump at all."""
    d = Path(directory)
    if not d.is_dir():
        return None
    dumps = list(d.glob("qp-*.dump"))
    if not dumps:
        return None
    newest = max(dumps, key=lambda p: p.stat().st_mtime)
    return (time.time() - newest.stat().st_mtime) / 3600


def _probe_api(url: str) -> tuple[bool, dict]:
    attempts = max(1, API_PROBE_ATTEMPTS)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status == 200, {"status": response.status, "attempts": attempt}
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(API_PROBE_DELAY_SEC)
    return False, {"error": type(last).__name__, "attempts": attempts}


@job(
    "ops.healthcheck",
    "DB, disk, memory and API health; failures go to notify outbox (shadow)",
    schedule_hint="every 5 min (qp-healthcheck.timer)",
)
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
    api_ok, api_detail = _probe_api(st.api_health_url)
    checks.append(("api", api_ok, None, api_detail))
    backup_age = _backup_age_hours(st.backup_dir)
    checks.append(
        (
            "backup_age_hours",
            backup_age is not None and backup_age <= BACKUP_MAX_AGE_HOURS,
            round(backup_age, 1) if backup_age is not None else None,
            {"max": BACKUP_MAX_AGE_HOURS},
        )
    )
    ok = all(c[1] for c in checks)
    with Session(ctx.engine) as s:
        for name, cok, val, det in checks:
            s.add(HealthCheck(check=name, ok=cok, value=val, detail=det, job_run_id=ctx.run_id))
        if not ok:
            failed = ",".join(c[0] for c in checks if not c[1])
            hour = utcnow().strftime("%Y%m%dT%H")
            enqueue(
                s,
                channel="telegram",
                recipient="ops",
                dedup_key=f"ops.healthcheck:{failed}:{hour}",
                subject="qp healthcheck failed",
                body=f"qp healthcheck failed: {failed}",
                source_job_run_id=ctx.run_id,
            )
        try:
            from qp.jobs.alert_rules import evaluate_alerts

            alerts = evaluate_alerts(s, ctx.run_id)
        except Exception as exc:  # a rule bug must be visible, not break the health probe
            alerts = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        s.commit()
    return {"ok": ok, "checks": {c[0]: {"ok": c[1], "value": c[2]} for c in checks}, "alerts": alerts}


@job(
    "ops.backup",
    "pg_dump of database qp (custom format), keep N days",
    schedule_hint="daily 03:50 CST (qp-backup.timer)",
    dedup=lambda: "day:" + dt.datetime.now().strftime("%Y-%m-%d"),
)
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
    return {
        "ok": True,
        "file": out.name,
        "bytes": out.stat().st_size,
        "sha256": h,
        "removed": removed,
        "keep_days": st.backup_keep_days,
    }


def _notify_disabled(st) -> bool:
    if st.notify_kill_switch:
        return True
    return Path(st.notify_disabled_file).exists()


@job(
    "notify.send",
    "Deliver notify.outbox: shadow rows are marked, live rows go to Telegram",
    schedule_hint="every 2 min (qp-notify.timer)",
)
def notify_send(ctx: JobContext) -> dict:
    """Row mode is authoritative: ``shadow`` is marked, ``live`` is sent.

    Live sends are fail-closed on missing credentials and stop immediately on the kill
    switch (``QP_NOTIFY_KILL_SWITCH=1`` or the disabled file).  A row is retried on
    network/429/5xx up to ``QP_NOTIFY_MAX_ATTEMPTS``; other 4xx responses are permanent.
    """
    st = get_settings()
    counts = {"sent": 0, "shadowed": 0, "failed": 0, "retry": 0}
    if _notify_disabled(st):
        return {"ok": True, "skipped": "kill_switch", **counts}
    max_attempts = max(1, st.notify_max_attempts)

    with Session(ctx.engine) as s:
        rows = (
            s.execute(
                select(Outbox)
                .where(Outbox.status == "pending")
                .order_by(Outbox.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        sender = None
        if any(row.mode == "live" for row in rows):
            try:
                sender = TelegramSender.from_settings(st)
            except NotifyConfigError as exc:
                # Fail closed: nothing is marked or sent, rows stay pending for the next tick.
                return {"ok": False, "error": str(exc), **counts}
        for row in rows:
            if row.mode != "live":
                s.add(
                    DeliveryAttempt(
                        outbox_id=row.id,
                        mode="shadow",
                        ok=None,
                        detail={"note": "shadow mode: not sent", "configured_mode": st.notify_mode},
                    )
                )
                row.status, row.send_result, row.processed_at = "shadowed", "not_sent_shadow", utcnow()
                counts["shadowed"] += 1
                continue

            prior = int(
                s.scalar(select(func.count()).select_from(DeliveryAttempt).where(DeliveryAttempt.outbox_id == row.id))
                or 0
            )
            attempt = prior + 1
            try:
                result = sender.send(render_outbox(row))
            except TelegramError as exc:
                s.add(
                    DeliveryAttempt(
                        outbox_id=row.id,
                        mode="live",
                        ok=False,
                        detail={
                            "attempt": attempt,
                            "error": exc.code,
                            "status_code": exc.status_code,
                            "retryable": exc.retryable,
                            "retry_after": exc.retry_after,
                            "detail": exc.detail,
                        },
                    )
                )
                if exc.retryable and attempt < max_attempts:
                    counts["retry"] += 1
                else:
                    row.status = "failed"
                    row.send_result = f"failed:{exc.code}"[:32]
                    row.processed_at = utcnow()
                    counts["failed"] += 1
                continue
            s.add(
                DeliveryAttempt(
                    outbox_id=row.id,
                    mode="live",
                    ok=True,
                    detail={"attempt": attempt, "message_id": result.message_id, "latency_ms": result.latency_ms},
                )
            )
            row.status, row.send_result, row.processed_at = "sent", "sent", utcnow()
            counts["sent"] += 1
        s.commit()
    return {"ok": counts["failed"] == 0, **counts}
