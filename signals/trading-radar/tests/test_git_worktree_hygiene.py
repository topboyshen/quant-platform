import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "git_hygiene_under_test", ROOT / "scripts/report_git_worktree_hygiene.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_backup_and_runtime_dirt_is_expected():
    buckets = module.classify_lines(
        [
            " D backups/20260523/config.json",
            " M data/research_digest.json",
            "?? data/eod_daily_report.json",
            "?? data/swing_eod_watchdog.json",
            " M scripts/check_swing_eod_terminal.py",
            " M config.swing.json",
        ]
    )
    assert buckets["expected"] == [
        " D backups/20260523/config.json",
        " M data/research_digest.json",
        "?? data/eod_daily_report.json",
        "?? data/swing_eod_watchdog.json",
    ]
    assert buckets["unexpected"] == [
        " M scripts/check_swing_eod_terminal.py",
        " M config.swing.json",
    ]


def test_prefix_collision_and_rename_do_not_hide_source_changes():
    lines = [" M data/research_digest.json.py", "R  scripts/critical.py -> data/research_digest.json",
             "?? deploy/approved-signals/tls/key.pem"]
    assert module.classify_lines(lines)["unexpected"] == lines
    parsed = module.nul_status_lines("R  data/research_digest.json\0scripts/critical.py\0")
    assert module.classify_lines(parsed)["unexpected"]


def test_cli_reads_supplied_fixture_and_propagates_status(tmp_path):
    fixture = tmp_path / "status.txt"
    command = [sys.executable, "-B", str(ROOT / "scripts/report_git_worktree_hygiene.py"), "--porcelain", str(fixture)]
    fixture.write_text(" M scripts/example.py\n")
    failed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert failed.returncode == 1 and "unexpected=1" in failed.stdout
    fixture.write_text(" M data/research_digest.json\0")
    passed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert passed.returncode == 0 and "unexpected=0" in passed.stdout
    fixture.unlink()
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 2


def test_live_tls_is_ignored_without_hiding_integration_source(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    for path in ("deploy/approved-signals/tls/key.pem", "deploy/approved-signals/tls/cert.pem"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=tmp_path, timeout=10).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", "deploy/approved-signals/app.py"], cwd=tmp_path, timeout=10).returncode == 1
