from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_watchdog_wrapper(tmp_path: Path, *, proxy: str | None) -> dict[str, str]:
    source = (ROOT / "scripts/run_swing_watchdog.sh").read_text(encoding="utf-8")
    source = source.replace(
        "cd /home/ubuntu/trading-radar || exit 1",
        f"cd {shlex.quote(str(tmp_path))} || exit 1",
    ).replace(
        "/etc/systemd/system/stock-radar.env",
        str(tmp_path / "missing-stock-radar.env"),
    )
    wrapper = tmp_path / "run_swing_watchdog.sh"
    wrapper.write_text(source, encoding="utf-8")

    runtime = tmp_path / "fake-python"
    runtime.write_text(
        "#!/bin/sh\n"
        "printf 'RADAR_ALL_PROXY=%s\\n' \"${RADAR_ALL_PROXY-<unset>}\"\n"
        "printf 'HTTP_PROXY=%s\\n' \"${HTTP_PROXY-<unset>}\"\n"
        "printf 'HTTPS_PROXY=%s\\n' \"${HTTPS_PROXY-<unset>}\"\n"
        "printf 'ALL_PROXY=%s\\n' \"${ALL_PROXY-<unset>}\"\n"
        "printf 'http_proxy=%s\\n' \"${http_proxy-<unset>}\"\n"
        "printf 'https_proxy=%s\\n' \"${https_proxy-<unset>}\"\n"
        "printf 'all_proxy=%s\\n' \"${all_proxy-<unset>}\"\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "RADAR_RUNTIME_PYTHON": str(runtime),
            "HTTP_PROXY": "http://ambient.invalid:1",
            "HTTPS_PROXY": "http://ambient.invalid:2",
            "ALL_PROXY": "socks5://ambient.invalid:3",
            "http_proxy": "http://ambient.invalid:4",
            "https_proxy": "http://ambient.invalid:5",
            "all_proxy": "socks5://ambient.invalid:6",
        }
    )
    if proxy is None:
        env.pop("RADAR_ALL_PROXY", None)
    else:
        env["RADAR_ALL_PROXY"] = proxy

    completed = subprocess.run(
        ["bash", str(wrapper)],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return dict(line.split("=", 1) for line in completed.stdout.splitlines())


def test_watchdog_wrapper_defaults_to_local_proxy_and_clears_ambient(tmp_path):
    observed = _run_watchdog_wrapper(tmp_path, proxy=None)

    assert observed["RADAR_ALL_PROXY"] == "http://127.0.0.1:7890"
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert observed[name] == "<unset>"


def test_watchdog_wrapper_preserves_explicit_radar_proxy(tmp_path):
    observed = _run_watchdog_wrapper(tmp_path, proxy="http://custom-proxy:17890")

    assert observed["RADAR_ALL_PROXY"] == "http://custom-proxy:17890"
