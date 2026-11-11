"""Contract tests for the anonymous Mihomo egress watchdog."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_mihomo_egress.sh"
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def _fake_curl(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "curl.calls"
    curl = tmp_path / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \\
  "${HTTP_PROXY-unset}" "${HTTPS_PROXY-unset}" \\
  "${ALL_PROXY-unset}" "${NO_PROXY-unset}" \\
  "${http_proxy-unset}" "${https_proxy-unset}" \\
  "${all_proxy-unset}" "${no_proxy-unset}" "$*" >> "$CALLS_FILE"
case "$*" in
  *api.telegram.org*) code=${FAKE_TELEGRAM_CODE:-302} ;;
  *www.gstatic.com*) code=${FAKE_GSTATIC_CODE:-204} ;;
  *) code=000 ;;
esac
printf '%s' "$code"
if [[ "$code" == 000 ]]; then
  exit 28
fi
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return curl, calls


def _run(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    _curl, calls = _fake_curl(tmp_path)
    env = os.environ.copy()
    env.update({key: "http://ambient.invalid:1" for key in _PROXY_ENV_KEYS})
    env.update(overrides)
    env["CALLS_FILE"] = str(calls)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        # The host script retries each probe once after a 3 s pause.
        timeout=30,
    )
    return result, calls.read_text(encoding="utf-8").splitlines()


def test_watchdog_checks_all_four_anonymous_routes_and_clears_ambient_proxy(tmp_path):
    result, calls = _run(tmp_path)

    assert result.returncode == 0
    assert "mihomo_egress_status=ok" in result.stdout
    assert len(calls) == 4
    assert all(line.startswith("unset|" * 8) for line in calls)
    assert all("--disable -4" in line for line in calls)
    assert all("--connect-timeout 5 --max-time 12" in line for line in calls)
    assert sum("-x http://127.0.0.1:7890" in line for line in calls) == 2
    assert sum("--socks5-hostname 127.0.0.1:7891" in line for line in calls) == 2
    assert sum("https://api.telegram.org/" in line for line in calls) == 2
    assert sum("https://www.gstatic.com/generate_204" in line for line in calls) == 2

    source = SCRIPT.read_text(encoding="utf-8")
    assert "sendMessage" not in source
    assert "TELEGRAM_BOT_TOKEN" not in source
    assert "docker restart" not in source
    assert "docker compose" not in source


def test_watchdog_reports_failure_but_finishes_every_probe(tmp_path):
    result, calls = _run(tmp_path, FAKE_TELEGRAM_CODE="000")

    assert result.returncode == 1
    assert "telegram_http code=000 expected=302 curl_status=28" in result.stdout
    assert "telegram_socks code=000 expected=302 curl_status=28" in result.stdout
    assert "gstatic_http code=204 expected=204 curl_status=0" in result.stdout
    assert "gstatic_socks code=204 expected=204 curl_status=0" in result.stdout
    assert "mihomo_egress_status=failed" in result.stdout
    # The two Telegram routes fail on the first attempt and are retried once
    # after the 3 s pause; the gstatic routes succeed immediately.
    assert len(calls) == 6
