"""Fixed no-network entrypoint for the production watchdog logic under tests."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from snapshot_watchdog import run_watchdog

if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(64)
    state = Path(os.environ["RESEARCH_DATA_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "watchdog.pid").write_text(str(os.getpid()), encoding="ascii")
    result = run_watchdog(int(sys.argv[1]), int(sys.argv[2]))
    if result == 0:
        (state / "watchdog.disarmed").write_text("clean\n", encoding="ascii")
    raise SystemExit(result)
