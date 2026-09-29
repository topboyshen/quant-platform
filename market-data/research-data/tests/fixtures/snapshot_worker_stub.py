"""No-network supervisor fixture; writes only paths supplied under test scratch."""
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
