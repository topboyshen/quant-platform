"""Run the real Bash wrapper with fake docker/verifier executables only."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ["docker-compose.yml", "docker-compose.integration.override.yml", "docker-compose.quantdinger.yml", "docker-compose.runtime-cleanup.yml"]


@pytest.fixture
def gate(tmp_path):
    (tmp_path / ".env").write_text("COMPOSE_FILE=" + ":".join(LAYERS) + "\n")
    for layer in LAYERS:
        (tmp_path / layer).write_text("services: {}\n")
    binaries = tmp_path / "bin"; binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(f'''#!{sys.executable}
import os,sys
from pathlib import Path
with Path(os.environ["CALL_LOG"]).open("a") as out: out.write("docker " + " ".join(sys.argv[1:]) + "\\n")
if "config" in sys.argv: print('{{"services":{{}}}}')
''')
    verifier = tmp_path / "scripts/verify_production_pin.py"; verifier.parent.mkdir()
    verifier.write_text('''import os,sys
from pathlib import Path
phase = "pre" if "--phase" in sys.argv else "post"
if phase == "pre": sys.stdin.read()
with Path(os.environ["CALL_LOG"]).open("a") as out: out.write(phase + "\\n")
raise SystemExit(int(os.environ.get("FAIL_" + phase.upper(), "0")))
''')
    # The wrapper takes the shared release lock for mutating verbs; stub the
    # host-only lock helper (an absolute path) so the test stays hermetic.
    lock = tmp_path / "release-lock.py"
    source = (ROOT / "deploy/trading-radar-compose").read_text()
    wrapper = tmp_path / "wrapper"
    wrapper.write_text(source.replace("readonly root=/home/ubuntu/trading-radar", f"readonly root={tmp_path}")
                      .replace("/usr/local/libexec/trading-radar-release-lock.py", str(lock))
                      .replace("/usr/bin/python3", sys.executable))
    lock.write_text(f'''import os, sys
args = sys.argv[1:]
if args and args[0] == "assert":
    raise SystemExit(0)
if args and args[0] == "wrapper":
    args = args[1:]
    if args and args[0] == "--":
        args = args[1:]
else:
    raise SystemExit(2)
os.execvpe({str(wrapper)!r}, [{str(wrapper)!r}, *args],
           dict(os.environ, TRADING_RADAR_RELEASE_LOCK_FD="9"))
''')
    docker.chmod(0o755); wrapper.chmod(0o755)
    env = {**os.environ, "PATH": str(binaries) + ":" + os.environ["PATH"], "CALL_LOG": str(tmp_path / "calls")}
    def run(*args, **updates):
        log = tmp_path / "calls"
        if log.exists(): log.unlink()
        result = subprocess.run([str(wrapper), *args], env={**env, **updates}, capture_output=True, text=True, timeout=10)
        return result, log.read_text().splitlines() if log.exists() else []
    return run


def test_preflight_then_mutation_then_postflight(gate):
    result, log = gate("up", "-d", "quantdinger-backend")
    assert result.returncode == 0
    assert " config --format json" in log[0] and log[1] == "pre"
    assert " up -d quantdinger-backend" in log[2] and log[3] == "post"


def test_failed_preflight_prevents_mutation(gate):
    result, log = gate("up", "-d", FAIL_PRE="1")
    assert result.returncode != 0 and len(log) == 2 and log[-1] == "pre"


def test_postflight_failure_propagates_without_automatic_rollback(gate):
    result, log = gate("restart", "quantdinger-backend", FAIL_POST="1")
    assert result.returncode == 1 and log[-1] == "post" and len(log) == 4


@pytest.mark.parametrize("args", [("config", "--quiet"), ("ps",), ("logs", "--tail", "1")])
def test_read_only_compose_commands_do_not_need_running_services(gate, args):
    result, log = gate(*args)
    assert result.returncode == 0 and len(log) == 1


@pytest.mark.parametrize("args", [("--file", "evil.yml", "up"), ("up", "-f", "evil.yml"),
    ("up", "--project-name=evil"), ("--ansi", "never", "up"), ("build", "quantdinger-backend"),
    ("up", "--build", "quantdinger-frontend")])
def test_project_overrides_and_guarded_rebuilds_are_rejected(gate, args):
    result, log = gate(*args)
    assert result.returncode != 0 and log == []
