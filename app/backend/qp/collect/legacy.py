"""Read-only collector for legacy nnG8 state. Runs as the legacy owner (ubuntu) from qp-collect.service,
writes one JSON snapshot for the qp ingest job (ops.inventory). It never starts/stops/edits anything:
only `systemctl list-*/show`, `docker ps`, `ss`, `journalctl` counts, TLS handshakes and HTTP GETs.
Log content is never copied - only error counts per unit."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

PROXY = os.getenv("QP_EGRESS_PROXY", "http://127.0.0.1:7890")
CERT_TARGETS = [("443", "127.0.0.1", 443, "150.158.46.28"), ("8443", "127.0.0.1", 8443, "150.158.46.28")]
EGRESS_TARGETS = [  # GET only; no bot token, no message - pure reachability
    ("telegram_api", "https://api.telegram.org/", True),
    ("gstatic_204", "https://www.gstatic.com/generate_204", True),
    ("dolthub", "https://www.dolthub.com/", True),
    ("telegram_direct", "https://api.telegram.org/", False),
]
USER_ENV = {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"}


def run(cmd: list[str], timeout: int = 20, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, f"__error__ {type(e).__name__}"


def us_to_iso(us) -> str | None:
    try:
        us = int(us)
    except (TypeError, ValueError):
        return None
    if us <= 0:
        return None
    return datetime.fromtimestamp(us / 1e6, tz=UTC).isoformat()


def systemd_scope(user: bool) -> dict:
    base = ["systemctl"] + (["--user"] if user else [])
    env = USER_ENV if user else None
    out: dict = {"ok": True, "units": [], "timers": []}
    rc, txt = run(base + ["list-units", "--all", "--type=service,timer", "-o", "json", "--no-pager"], env=env)
    if rc != 0:
        out["ok"] = False
        return out
    units = {u["unit"]: {"name": u["unit"], "load": u.get("load"), "active": u.get("active"), "sub": u.get("sub"),
                         "description": u.get("description")} for u in json.loads(txt or "[]")}
    rc, txt = run(base + ["list-timers", "--all", "-o", "json", "--no-pager"], env=env)
    timers = json.loads(txt or "[]") if rc == 0 else []
    for t in timers:
        out["timers"].append({"timer": t.get("unit"), "activates": t.get("activates"),
                              "next": us_to_iso(t.get("next")), "last": us_to_iso(t.get("last"))})
    services = [n for n in units if n.endswith(".service")]
    props = "Id,ActiveState,SubState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp,FragmentPath,User"
    for i in range(0, len(services), 80):
        rc, txt = run(base + ["show", "-p", props, "--timestamp=unix"] + services[i:i + 80], env=env, timeout=30)
        if rc != 0:
            continue
        for block in txt.strip().split("\n\n"):
            kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            u = units.get(kv.get("Id", ""))
            if u is None:
                continue
            u.update({"result": kv.get("Result"), "exit_status": kv.get("ExecMainStatus"),
                      "fragment": kv.get("FragmentPath"), "run_user": kv.get("User") or None,
                      "last_start": _unix_ts(kv.get("ExecMainStartTimestamp")),
                      "last_exit": _unix_ts(kv.get("ExecMainExitTimestamp"))})
    out["units"] = list(units.values())
    return out


def _unix_ts(v: str | None) -> str | None:
    if not v or not v.startswith("@"):
        return None
    try:
        return datetime.fromtimestamp(int(v[1:]), tz=UTC).isoformat()
    except ValueError:
        return None


def containers() -> list[dict]:
    rc, txt = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"])
    if rc != 0:
        return [{"error": "docker ps failed"}]
    res = []
    for line in txt.splitlines():
        d = json.loads(line)
        status = d.get("Status", "")
        health = "healthy" if "(healthy)" in status else "unhealthy" if "(unhealthy)" in status else \
            "starting" if "health: starting" in status else None
        res.append({"name": d.get("Names"), "image": d.get("Image"), "state": d.get("State"),
                    "status": status, "health": health})
    return res


def ports() -> list[dict]:
    rc, txt = run(["ss", "-ltnH"])
    res = []
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            addr = parts[3]
            host, _, port = addr.rpartition(":")
            res.append({"addr": host, "port": int(port) if port.isdigit() else port})
    return sorted({(r["addr"], r["port"]): r for r in res}.values(), key=lambda r: (str(r["port"]), r["addr"]))


def certs() -> list[dict]:
    res = []
    for label, host, port, sni in CERT_TARGETS:
        item = {"target": label, "ok": False}
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=5) as s, ctx.wrap_socket(s, server_hostname=sni) as t:
                der = t.getpeercert(binary_form=True)
            with tempfile.NamedTemporaryFile("wb", suffix=".der") as f:
                f.write(der)
                f.flush()
                rc, txt = run(["openssl", "x509", "-inform", "DER", "-in", f.name, "-noout", "-enddate", "-issuer"])
            m = re.search(r"notAfter=(.+)", txt)
            if m:
                exp = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
                item.update(ok=True, not_after=exp.isoformat(),
                            hours_left=round((exp - datetime.now(UTC)).total_seconds() / 3600, 1),
                            issuer=(re.search(r"issuer=(.+)", txt) or [None, ""])[1].strip())
        except Exception as e:
            item["error"] = type(e).__name__
        res.append(item)
    return res


def egress() -> list[dict]:
    res = []
    for name, url, via_proxy in EGRESS_TARGETS:
        handlers = [urllib.request.ProxyHandler({"http": PROXY, "https": PROXY} if via_proxy else {})]
        opener = urllib.request.build_opener(*handlers)
        t0 = time.monotonic()
        item = {"target": name, "via_proxy": via_proxy}
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "qp-egress-probe/1"})
            with opener.open(req, timeout=8 if via_proxy else 4) as r:
                item.update(ok=True, status=r.status)
        except urllib.error.HTTPError as e:  # an HTTP answer still proves reachability
            item.update(ok=e.code < 500, status=e.code)
        except Exception as e:
            item.update(ok=False, error=type(e).__name__)
        item["ms"] = round((time.monotonic() - t0) * 1000)
        res.append(item)
    return res


# Pre-auth connection noise from internet scanners: sshd logs it at err level but it says
# nothing about the service's health.  Only applied to ssh units, so real sshd errors and
# other units' messages still count.
BENIGN_SSH_LOG = re.compile(r"kex_exchange_identification|Connection reset by peer|Connection closed by .*\[preauth\]")


def count_log_lines(lines) -> Counter:
    c: Counter = Counter()
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        unit = d.get("_SYSTEMD_USER_UNIT") or d.get("_SYSTEMD_UNIT") or d.get("SYSLOG_IDENTIFIER") or "?"
        if unit.startswith(("ssh.service", "sshd.service")) and BENIGN_SSH_LOG.search(d.get("MESSAGE") or ""):
            continue
        c[unit] += 1
    return c


def log_errors(minutes: int = 15) -> dict:
    out = {}
    for scope, args, env in (("system", [], None), ("user", ["--user"], USER_ENV)):
        rc, txt = run(["journalctl", *args, "-p", "3", "--since", f"-{minutes}min", "-o", "json",
                       "--output-fields=_SYSTEMD_UNIT,_SYSTEMD_USER_UNIT,SYSLOG_IDENTIFIER,MESSAGE", "--no-pager"],
                      env=env, timeout=30)
        c = count_log_lines(txt.splitlines())
        out[scope] = {"ok": rc == 0, "window_min": minutes, "counts": dict(c.most_common(30)), "total": sum(c.values())}
    return out


def host() -> dict:
    du = shutil.disk_usage("/")
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            mem[k] = int(v.split()[0]) // 1024
    return {"disk_root": {"total_gb": round(du.total / 1024**3, 1), "free_gb": round(du.free / 1024**3, 2)},
            "mem_mb": mem, "load": os.getloadavg()}


def collect() -> dict:
    t0 = time.monotonic()
    snap = {"schema": "qp.legacy.v1", "collected_at": datetime.now(UTC).isoformat(), "host": socket.gethostname()}
    for key, fn in (("systemd_system", lambda: systemd_scope(False)), ("systemd_user", lambda: systemd_scope(True)),
                    ("containers", containers), ("ports", ports), ("certs", certs), ("egress", egress),
                    ("logs", log_errors), ("host", host)):
        try:
            snap[key] = fn()
        except Exception as e:
            snap[key] = {"__error__": type(e).__name__}
    snap["duration_s"] = round(time.monotonic() - t0, 2)
    return snap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    data = collect()
    out = Path(a.out)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str))
    os.chmod(tmp, 0o640)
    tmp.replace(out)
    print(json.dumps({"ok": True, "duration_s": data["duration_s"], "bytes": out.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
