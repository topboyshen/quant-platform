"""Job registry + runner. systemd timers only trigger `qp job run <name>`; dedup, locking and
run records live in ops.job_run. Locking uses a PostgreSQL session advisory lock per job name."""
from __future__ import annotations

import os
import socket
import time
import traceback
import zlib
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from qp.models.base import utcnow
from qp.models.ops import JobDef, JobRun


@dataclass
class JobSpec:
    name: str
    func: Callable[["JobContext"], dict[str, Any]]
    description: str
    schedule_hint: str | None
    dedup: Callable[[], str | None] | None


@dataclass
class JobContext:
    engine: Engine
    run_id: int
    name: str


JOBS: dict[str, JobSpec] = {}


def job(name: str, description: str = "", schedule_hint: str | None = None,
        dedup: Callable[[], str | None] | None = None):
    def deco(f):
        JOBS[name] = JobSpec(name, f, description, schedule_hint, dedup)
        return f
    return deco


def _lock_key(name: str) -> int:
    # stable signed 32-bit key in a qp-specific namespace (classid 0x5150 'QP')
    return zlib.crc32(name.encode()) & 0x7FFFFFFF


def sync_job_defs(engine: Engine) -> None:
    with Session(engine, expire_on_commit=False) as s:
        for spec in JOBS.values():
            d = s.get(JobDef, spec.name)
            if d is None:
                s.add(JobDef(name=spec.name, description=spec.description, schedule_hint=spec.schedule_hint))
            else:
                d.description, d.schedule_hint, d.updated_at = spec.description, spec.schedule_hint, utcnow()
        s.commit()


def run_job(engine: Engine, name: str, trigger: str = "manual", dedup_key: str | None = None) -> JobRun:
    spec = JOBS.get(name)
    if spec is None:
        raise KeyError(f"unknown job: {name}")
    sync_job_defs(engine)
    if dedup_key is None and spec.dedup is not None:
        dedup_key = spec.dedup()
    with engine.connect() as lock_conn:
        got = lock_conn.execute(text("SELECT pg_try_advisory_lock(20816, :k)"), {"k": _lock_key(name)}).scalar()
        try:
            with Session(engine, expire_on_commit=False) as s:
                run = JobRun(job_name=name, dedup_key=dedup_key, status="running", trigger=trigger,
                             host=socket.gethostname(), pid=os.getpid(), result={})
                if not got:
                    run.status, run.finished_at, run.duration_s = "skipped_locked", utcnow(), 0.0
                    s.add(run); s.commit()
                    return run
                if dedup_key is not None:
                    dup = s.execute(select(JobRun.id).where(JobRun.job_name == name, JobRun.dedup_key == dedup_key,
                                                            JobRun.status == "succeeded")).first()
                    if dup:
                        run.status, run.finished_at, run.duration_s = "skipped_dup", utcnow(), 0.0
                        run.result = {"duplicate_of": dup[0]}
                        s.add(run); s.commit()
                        return run
                s.add(run); s.commit()
                run_id = run.id
            t0 = time.monotonic()
            status, result, error = "succeeded", {}, None
            try:
                result = spec.func(JobContext(engine, run_id, name)) or {}
                if result.get("ok") is False:
                    status = "failed"
            except Exception as e:  # record, don't crash the timer silently
                status, error = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}"
            with Session(engine, expire_on_commit=False) as s:
                run = s.get(JobRun, run_id)
                run.status, run.result, run.error = status, result, error
                run.finished_at, run.duration_s = utcnow(), round(time.monotonic() - t0, 3)
                s.commit()
                return run
        finally:
            if got:
                lock_conn.execute(text("SELECT pg_advisory_unlock(20816, :k)"), {"k": _lock_key(name)})
                lock_conn.commit()
