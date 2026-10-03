"""No-network supervisor fixture; writes only paths supplied under test scratch."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

if len(sys.argv) > 1 and sys.argv[1] == "--helper":
    while True:
        time.sleep(1)

gate_fd = int(os.environ["RESEARCH_DATA_SUPERVISOR_GATE_FD"])
expected_parent = int(os.environ["RESEARCH_DATA_SUPERVISOR_PID"])
released = os.read(gate_fd, 1)
os.close(gate_fd)
if released != b"G" or os.getppid() != expected_parent:
    raise SystemExit(75)

run_id = os.environ["RESEARCH_DATA_SUPERVISED_RUN_ID"]
state = Path(os.environ["RESEARCH_DATA_STATE_DIR"])
record_path = state / "runs" / f"{run_id}.json"
mode = os.environ["SNAPSHOT_WORKER_TEST_MODE"]
target = "2026-09-24"


def record(status, **values):
    payload = {"runId": run_id, "status": status, "target": target, **values}
    temporary = record_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, record_path)


if mode == "success":
    record("inserted", payloadSha256="a" * 64)
elif mode in {"core_stale_then_success", "core_stale_then_hang"}:
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    target = now.date().isoformat()
    attempt_path = state / "fixture-core-attempts.json"
    attempts = json.loads(attempt_path.read_text()) if attempt_path.exists() else []
    attempts.append({"runId": run_id, "soft": os.environ["RESEARCH_DATA_SOFT_DEADLINE_EPOCH"],
                     "day": os.environ.get("RESEARCH_DATA_RETRY_CORE_DAY"),
                     "calendarSha": os.environ.get("RESEARCH_DATA_RETRY_CORE_CALENDAR_SHA256"),
                     "stageRetry": any(k.startswith("RESEARCH_DATA_RETRY_STAGE_") for k in os.environ)})
    attempt_path.write_text(json.dumps(attempts))
    if len(attempts) == 1:
        observed = (now - dt.timedelta(seconds=2)).isoformat()
        evidence = {"document": {"schemaVersion": 1, "targetDate": target, "status": "open",
            "previousTradingDate": (now.date() - dt.timedelta(days=1)).isoformat(),
            "nextTradingDate": (now.date() + dt.timedelta(days=1)).isoformat(),
            "sourceIds": ["akshare:sina:trade-calendar"], "observedAt": observed,
            "validUntil": now.replace(hour=23, minute=59, second=59).isoformat()},
            "binding": {"archiveDate": target, "inputSha256": "a" * 64,
                        "cutoffInstant": observed, "publishedStatus": "sealed"}}
        evidence["sha256"] = hashlib.sha256(json.dumps(evidence, sort_keys=True,
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        record("deferred", schema="research.core-market.v1", fullSnapshot=False,
            reason="core_qlib_not_current_for_expected_trading_day", calendarEvidence=evidence)
        raise SystemExit(69)
    if mode == "core_stale_then_hang":
        while True:
            time.sleep(1)
    record("inserted", schema="research.core-market.v1", fullSnapshot=False, payloadSha256="a" * 64)
elif mode in {"qlib_stale_then_success", "qlib_stale_then_hang"}:
    target = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    stage = json.loads((Path(os.environ["RESEARCH_DATA_AKSHARE_STAGE_DIR"]) / (target + ".json")).read_text())
    attempt_path = state / "fixture-attempts.json"
    attempts = json.loads(attempt_path.read_text()) if attempt_path.exists() else []
    attempts.append({"pid": os.getpid(), "runId": run_id,
                     "softDeadline": os.environ["RESEARCH_DATA_SOFT_DEADLINE_EPOCH"]})
    attempt_path.write_text(json.dumps(attempts))
    if len(attempts) == 1:
        record("deferred", reason="qlib_not_current_for_expected_trading_day",
               akshareStage={key: stage[key] for key in
                             ("tradeDate", "payloadSha256", "recordSha256")})
        raise SystemExit(69)
    if mode == "qlib_stale_then_hang":
        record("running", activePhase="fixed-retry-hang")
        while True:
            time.sleep(1)
    record("inserted", payloadSha256="a" * 64)
elif mode == "hang_with_helper":
    helper = subprocess.Popen(
        [sys.executable, "-B", __file__, "--helper"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
    )
    (state / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
    (state / "helper.pid").write_text(str(helper.pid), encoding="ascii")
    record("running", activeSource="fault_injection")
    blocked_read, blocked_write = os.pipe()
    os.read(blocked_read, 1)  # interruptible kernel I/O, not a Python sleep
elif mode == "sqlite_transaction_hang":
    db_path = os.environ["RESEARCH_DATA_SNAPSHOT_DB"]
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE snapshot(date TEXT PRIMARY KEY, payload TEXT)")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO snapshot VALUES(?, ?)", (target, "uncommitted"))
    (state / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
    record("running", activePhase="sqlite_transaction")
    while True:
        time.sleep(1)
elif mode == "success_with_live_helper":
    helper = subprocess.Popen(
        [sys.executable, "-B", __file__, "--helper"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
    )
    (state / "helper.pid").write_text(str(helper.pid), encoding="ascii")
    record("inserted", payloadSha256="a" * 64)
elif mode == "deferred":
    record("deferred", reason="source unavailable")
    raise SystemExit(69)
elif mode == "hang":
    record("running", qlibTag="test-source")
    while True:
        time.sleep(1)
elif mode == "hang_ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    record("running", qlibTag="test-source")
    while True:
        time.sleep(1)
elif mode == "commit_then_hang":
    db = os.environ["RESEARCH_DATA_SNAPSHOT_DB"]
    document = {
        "date": target,
        "qlib": {"date": target},
        "akshare": {"activity": {"统计日期": target}},
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE snapshot(date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO snapshot VALUES(?,?,?)", (target, target + "T22:30:00+08:00", payload)
        )
    record("running", qlibTag="test-source", preexistingRowSha256=None,
           snapshotCandidateSha256=digest)
    while True:
        time.sleep(1)
else:
    raise SystemExit(64)
