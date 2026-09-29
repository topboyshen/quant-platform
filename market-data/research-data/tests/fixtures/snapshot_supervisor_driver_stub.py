"""No-network supervisor fault-injection entrypoint, scratch paths only."""
import os
import datetime as dt
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from snapshot_supervisor import ZONE, run_supervised

scratch = Path(os.environ["SNAPSHOT_SUPERVISOR_TEST_ROOT"]).resolve()
state = Path(os.environ["RESEARCH_DATA_STATE_DIR"]).resolve()
if state != scratch / "state" or not str(scratch).startswith("/tmp/"):
    raise SystemExit(64)

run_supervised(
    worker_path=str(ROOT / "tests" / "fixtures" / "snapshot_worker_stub.py"),
    watchdog_path=str(ROOT / "tests" / "fixtures" / "snapshot_watchdog_stub.py"),
    state_dir=str(state), db_path=str(scratch / "snapshot.db"),
    stage_dir=str(scratch / "stage"), log_path=str(scratch / "snapshot.log"),
    deadline_seconds_override=30,
    now=dt.datetime.now(ZONE).replace(hour=22, minute=30, second=0,
                                      microsecond=0),
)
