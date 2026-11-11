#!/usr/bin/env python3
"""Export host textfile metrics for Prometheus node_exporter.

Writes:
  - disk usage gauges from `df`
  - datasource / Swing EOD outcome gauges from recent journal + index cache

No secrets. Read-only journal + JSON + df.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

OUT_DIR = Path(os.environ.get("RADAR_TEXTFILE_DIR", "/home/ubuntu/trading-radar/data/prometheus-textfile"))
INDEX_CACHE = Path(os.environ.get("RADAR_INDEX_CACHE", "/home/ubuntu/trading-radar/data/index_closes_latest.json"))
JOURNAL_SINCE = os.environ.get("RADAR_EOD_JOURNAL_SINCE", "36 hours ago")


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def disk_metrics() -> str:
    lines = [
        "# HELP node_textfile_disk_used_percent Disk used percent from df (host textfile).",
        "# TYPE node_textfile_disk_used_percent gauge",
        "# HELP node_textfile_disk_avail_bytes Disk available bytes from df.",
        "# TYPE node_textfile_disk_avail_bytes gauge",
    ]
    try:
        out = subprocess.check_output(
            ["df", "-PB1", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs"],
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "\n".join(lines) + "\n"
    for row in out.splitlines()[1:]:
        parts = row.split()
        if len(parts) < 6:
            continue
        filesystem, _blocks, used, avail, pct, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        if mount not in {"/", "/home", "/var", "/var/lib/docker"}:
            # Keep root always; also emit docker data if distinct
            if mount != "/":
                continue
        try:
            used_pct = float(pct.rstrip("%"))
            avail_i = int(avail)
            used_i = int(used)
        except ValueError:
            continue
        mlabel = mount.replace("\\", "\\\\").replace('"', '\\"')
        flabel = filesystem.replace("\\", "\\\\").replace('"', '\\"')
        labels = f'mountpoint="{mlabel}",filesystem="{flabel}"'
        lines.append(f"node_textfile_disk_used_percent{{{labels}}} {used_pct}")
        lines.append(f"node_textfile_disk_avail_bytes{{{labels}}} {avail_i}")
        lines.append(f"node_textfile_disk_used_bytes{{{labels}}} {used_i}")
    return "\n".join(lines) + "\n"


def index_cache_metrics() -> str:
    lines = [
        "# HELP radar_datasource_index_cache_is_primary 1 if index close cache source is pure tencent.",
        "# TYPE radar_datasource_index_cache_is_primary gauge",
        "# HELP radar_datasource_index_cache_uses_fallback 1 if sina appears in index cache source.",
        "# TYPE radar_datasource_index_cache_uses_fallback gauge",
        "# HELP radar_datasource_index_cache_source Source indicator for index close cache.",
        "# TYPE radar_datasource_index_cache_source gauge",
    ]
    primary = 0
    fallback = 0
    source = "unknown"
    try:
        data = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
        source = str(data.get("source") or "unknown")
        primary = int(source == "tencent")
        fallback = int("sina" in source.split("+") or source == "sina")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        source = "unreadable"
    safe = re.sub(r"[^a-zA-Z0-9_+.-]", "_", source)[:64]
    lines.append(f"radar_datasource_index_cache_is_primary {primary}")
    lines.append(f"radar_datasource_index_cache_uses_fallback {fallback}")
    lines.append(f'radar_datasource_index_cache_source{{source="{safe}"}} 1')
    return "\n".join(lines) + "\n"


def parse_eod_journal() -> str:
    """Parse recent Swing EOD journal for tencent/sina/akshare outcomes."""
    lines = [
        "# HELP radar_datasource_requests_total Approximate request outcomes from last Swing EOD journal window.",
        "# TYPE radar_datasource_requests_total gauge",
        "# HELP radar_datasource_fallback_total Fallback successes after primary failure in last EOD window.",
        "# TYPE radar_datasource_fallback_total gauge",
        "# HELP radar_datasource_primary_failure_ratio Primary(tencent) failure ratio in last EOD window.",
        "# TYPE radar_datasource_primary_failure_ratio gauge",
        "# HELP radar_datasource_eod_journal_parsed 1 if journal parse succeeded.",
        "# TYPE radar_datasource_eod_journal_parsed gauge",
    ]
    counts = {
        ("tencent", "fail"): 0,
        ("tencent", "ok"): 0,
        ("sina", "ok"): 0,
        ("sina", "fail"): 0,
        ("akshare", "ok"): 0,
        ("akshare", "fail"): 0,
    }
    fallback = 0
    parsed = 0
    try:
        out = subprocess.check_output(
            [
                "journalctl",
                "-u",
                "stock-radar-swing-eod.service",
                "--since",
                JOURNAL_SINCE,
                "--no-pager",
                "-o",
                "cat",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        parsed = 1
    except (OSError, subprocess.CalledProcessError):
        out = ""
        parsed = 0

    for line in out.splitlines():
        low = line.lower()
        if "fetch_tencent_kline" in low and ("失败" in line or "fail" in low or "error" in low or "501" in low):
            counts[("tencent", "fail")] += 1
        elif "_tencent_total_amount" in low and ("失败" in line or "501" in line or "error" in low):
            counts[("tencent", "fail")] += 1
        elif "fetch_tencent_kline" in low and ("成功" in line or "success" in low):
            counts[("tencent", "ok")] += 1
        if "akshare_fund_etf_hist_sina" in low and ("成功" in line or "success" in low):
            counts[("sina", "ok")] += 1
            counts[("akshare", "ok")] += 1
            fallback += 1
        elif "akshare" in low and ("失败" in line or "fail" in low or "error" in low):
            counts[("akshare", "fail")] += 1
        if "来源:sina" in line or "source:sina" in low or "sina+" in low:
            fallback += 1

    for (source, result), value in sorted(counts.items()):
        lines.append(f'radar_datasource_requests_total{{source="{source}",result="{result}"}} {value}')
    lines.append(f"radar_datasource_fallback_total {fallback}")
    t_fail = counts[("tencent", "fail")]
    t_ok = counts[("tencent", "ok")]
    denom = t_fail + t_ok
    ratio = (float(t_fail) / float(denom)) if denom else (1.0 if t_fail else 0.0)
    # If we only saw failures (common when ok lines are absent), ratio=1.
    if t_fail > 0 and t_ok == 0:
        ratio = 1.0
    lines.append(f"radar_datasource_primary_failure_ratio {ratio}")
    lines.append(f"radar_datasource_eod_journal_parsed {parsed}")
    lines.append(f"radar_datasource_metrics_unixtime {int(time.time())}")
    return "\n".join(lines) + "\n"


def main() -> int:
    disk_body = disk_metrics()
    ds_body = index_cache_metrics() + parse_eod_journal()
    atomic_write(OUT_DIR / "disk.prom", disk_body)
    atomic_write(OUT_DIR / "datasource.prom", ds_body)
    print(f"wrote {OUT_DIR / 'disk.prom'} and {OUT_DIR / 'datasource.prom'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
