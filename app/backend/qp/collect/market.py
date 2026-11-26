"""Read-only legacy market-data reader (runs as ubuntu; stdlib only; no network; no writes except --out).

Reads the legacy outputs and the legacy Qlib directory and writes one JSON snapshot for the qp
ingest job. Opens SQLite databases with mode=ro. Never modifies, locks or touches legacy files.
Also computes a shadow Qlib market summary (breadth/coverage/top lists; same rules as
research-data qlib_summary.qlib_market) by seeking into *.day.bin files (no copy of Qlib data).
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

HOME = Path("/home/ubuntu")
DEFAULTS = {
    "calendar": HOME / "trading-radar/data/trading_calendar.json",
    "index_close": HOME / "trading-radar/data/index_closes_latest.json",
    "qlib": HOME / "qlib/data/cn_data",
    "qlib_status": HOME / "qlib/logs/update-status.json",
    "core_db": HOME / "research-data/core-market.db",
    "snapshot_db": HOME / "research-data/snapshot.db",
    "health": Path("/var/lib/research-data-health/last.json"),
    "core_last_run": HOME / ".local/state/research-data/core-market/last-run.json",
    "cninfo_staging": HOME / "AStock_AI_System/work",
}
INDEX_FILES = ("csi300", "csi500", "csi800", "csi1000")
REF_SYMS = ("sh600000", "sz000001", "sh600519", "sz300750")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canon(lines) -> str:
    return sha256_bytes("\n".join(lines).encode())


def _read(path: Path, limit: int = 50_000_000) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        return None


def legacy_calendar(path: Path) -> dict:
    raw = _read(path)
    if raw is None:
        return {"ok": False, "path": str(path)}
    d = json.loads(raw)
    dates = sorted({str(x)[:10] for x in d.get("trading_dates") or []})
    return {
        "ok": True,
        "path": str(path),
        "file_sha256": sha256_bytes(raw),
        "generated_at": d.get("generated_at"),
        "source": d.get("source"),
        "dates": dates,
        "rows": len(dates),
        "checksum": canon(dates),
    }


def legacy_index_close(path: Path) -> dict:
    raw = _read(path)
    if raw is None:
        return {"ok": False, "path": str(path)}
    d = json.loads(raw)
    rows = [
        {
            "symbol": c["symbol"],
            "trade_date": c["trade_date"],
            "close": round(float(c["close"]), 4),
            "source": c.get("source") or "",
        }
        for c in d.get("closes") or []
    ]
    rows.sort(key=lambda r: (r["symbol"], r["trade_date"]))
    return {
        "ok": True,
        "path": str(path),
        "file_sha256": sha256_bytes(raw),
        "fetched_at": d.get("fetched_at"),
        "trade_date": d.get("trade_date"),
        "stale": d.get("stale"),
        "rows": rows,
        "checksum": canon(f"{r['symbol']}|{r['trade_date']}|{r['close']:.4f}|{r['source']}" for r in rows),
    }


def _lines(path: Path) -> tuple[list[str], str | None]:
    raw = _read(path)
    if raw is None:
        return [], None
    return [ln.strip() for ln in raw.decode().splitlines() if ln.strip()], sha256_bytes(raw)


def qlib_catalog(root: Path, status_path: Path) -> dict:
    cal, cal_sha = _lines(root / "calendars/day.txt")
    inst_lines, inst_sha = _lines(root / "instruments/all.txt")
    insts = []
    for ln in inst_lines:
        p = ln.split()
        if len(p) >= 3:
            insts.append([p[0], p[1], p[2]])
    members = {}
    for idx in INDEX_FILES:
        lines, sha = _lines(root / f"instruments/{idx}.txt")
        raw_rows = [tuple(ln.split()[:3]) for ln in lines if len(ln.split()) >= 3]
        rows = [list(r) for r in sorted(set(raw_rows))]
        members[idx] = {
            "file_sha256": sha,
            "rows": rows,
            "raw_rows": len(raw_rows),
            "checksum": canon(sorted(f"{idx}|{a}|{b}|{c}" for a, b, c in rows)),
        }
    try:
        feature_dirs = sum(1 for _ in os.scandir(root / "features"))
    except OSError:
        feature_dirs = None
    status = {}
    raw = _read(status_path, 2_000_000)
    if raw:
        try:
            s = json.loads(raw)
            status = {
                k: s.get(k)
                for k in ("status", "mode", "plannedDate", "targetDate", "time", "failureStage", "error")
                if k in s
            }
            if isinstance(status.get("error"), str):
                status["error"] = status["error"][:200]
        except ValueError:
            status = {"unreadable": True}
    tag = None
    try:
        tag = (root.parent / ".tag").read_text().strip()[:64]
    except OSError:
        pass
    return {
        "ok": bool(cal),
        "root": str(root),
        "calendar": cal,
        "calendar_sha256": cal_sha,
        "calendar_checksum": canon(cal),
        "instruments": insts,
        "instruments_sha256": inst_sha,
        "instruments_checksum": canon(sorted(f"{a}|{b}|{c}" for a, b, c in insts)),
        "members": members,
        "feature_dirs": feature_dirs,
        "update_status": status,
        "tag": tag,
    }


# ---- shadow Qlib market summary (mirrors qlib_summary.qlib_market breadth/coverage/top lists) ----
def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


class _Bin:
    def __init__(self, root: Path, sym: str, field: str):
        self.path = root / "features" / sym / f"{field}.day.bin"
        with open(self.path, "rb") as fh:
            hdr = fh.read(4)
        start = struct.unpack("<f", hdr)[0]
        if len(hdr) != 4 or not math.isfinite(start) or not start.is_integer() or start < 0:
            raise ValueError("bad header")
        self.start = int(start)
        self.length = (self.path.stat().st_size - 4) // 4

    def at(self, i: int) -> float:
        with open(self.path, "rb") as fh:
            fh.seek(4 + 4 * i)
            return struct.unpack("<f", fh.read(4))[0]


def _reference_date(root: Path, cal: list[str], sym: str) -> str | None:
    try:
        bins = {f: _Bin(root, sym, f) for f in ("close", "high", "low", "amount")}
    except (OSError, ValueError):
        return None
    start = bins["close"].start
    usable = min(b.length for b in bins.values())
    for i in range(usable - 1, max(0, usable - 40), -1):
        vals = (
            bins["close"].at(i - 1),
            bins["close"].at(i),
            bins["high"].at(i),
            bins["low"].at(i),
            bins["amount"].at(i),
        )
        if all(math.isfinite(v) for v in vals) and 0 <= start + i < len(cal):
            return cal[start + i]
    return None


def qlib_summary(root: Path, cal: list[str], insts: list[list[str]], date: str, tag: str | None = None) -> dict:
    if date not in cal:
        return {"ok": False, "date": date, "error": "date_not_in_calendar"}
    di = cal.index(date)
    up = down = flat = invalid = eligible = 0
    rows = []
    errors = 0
    for sym, sd, ed in insts:
        if not (sd < date <= ed):
            continue
        eligible += 1
        s = sym.lower()
        try:
            c = _Bin(root, s, "close")
            others = [_Bin(root, s, f) for f in ("high", "low", "amount")]
        except (OSError, ValueError):
            errors += 1
            invalid += 1
            continue
        li = di - c.start
        if li < 1 or any(li >= b.length for b in [c, *others]):
            errors += 1
            invalid += 1
            continue
        c0, c1 = c.at(li - 1), c.at(li)
        h, lo, am = (b.at(li) for b in others)
        if not all(math.isfinite(v) for v in (c0, c1, h, lo, am)):
            invalid += 1
            continue
        chg = _f32(_f32(c1 / c0) - 1.0)
        if chg > 1e-4:
            up += 1
        elif chg < -1e-4:
            down += 1
        else:
            flat += 1
        rows.append((s, chg, am))
    coverage = len(rows) / eligible if eligible else 0.0
    rows.sort(key=lambda r: -r[1])
    out = {
        "ok": True,
        "date": date,
        "coverage": {"valid": len(rows), "eligible": eligible, "invalid": invalid, "ratio": round(coverage, 6)},
        "breadth": {"up": up, "down": down, "flat": flat, "total": up + down + flat},
        "structure_errors": errors,
        "topGainers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "amount": round(r[2], 0)} for r in sorted(rows, key=lambda r: -r[2])[:10]],
    }
    # The Qlib release tag lets consumers tell two vintages of the same tree apart.  A
    # deliberate tree replacement revises the instrument universe of old dates, so a
    # recomputed historical summary is only fully comparable with a same-tag record.
    if tag:
        out["generation"] = {"tag": tag}
    return out


def legacy_core(path: Path) -> dict:
    if not path.is_file():
        return {"ok": False, "path": str(path)}
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = []
        for date, schema, payload, psha in c.execute(
            "SELECT date, schema, payload, payload_sha256 FROM core_market ORDER BY date"
        ):
            p = json.loads(payload)
            q = p.get("qlib") or {}
            rows.append(
                {
                    "date": date,
                    "schema": schema,
                    "payload_sha256": psha,
                    "computed_sha256": sha256_bytes(payload.encode() if isinstance(payload, str) else payload),
                    "generated_at": p.get("generatedAt"),
                    "qlib": {
                        k: q.get(k)
                        for k in (
                            "date",
                            "coverage",
                            "breadth",
                            "limitUpCount",
                            "limitDownCount",
                            "topGainers",
                            "topLosers",
                            "topAmount",
                            "generation",
                        )
                    },
                }
            )
    finally:
        c.close()
    return {
        "ok": True,
        "path": str(path),
        "rows": rows,
        "checksum": canon(f"{r['date']}|{r['payload_sha256']}" for r in rows),
    }


def legacy_snapshot_index(path: Path) -> dict:
    if not path.is_file():
        return {"ok": False, "path": str(path)}
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = []
        for date, gen, payload in c.execute("SELECT date, generated_at, payload FROM snapshot ORDER BY date"):
            b = payload.encode() if isinstance(payload, str) else payload
            try:
                keys = sorted(json.loads(b))[:60]
            except ValueError:
                keys = []
            rows.append(
                {"date": date, "generated_at": gen, "payload_sha256": sha256_bytes(b), "bytes": len(b), "keys": keys}
            )
    finally:
        c.close()
    return {
        "ok": True,
        "path": str(path),
        "rows": rows,
        "checksum": canon(f"{r['date']}|{r['payload_sha256']}" for r in rows),
    }


def legacy_health(path: Path, last_run: Path) -> dict:
    out: dict = {"ok": False, "path": str(path)}
    raw = _read(path, 5_000_000)
    if raw:
        d = json.loads(raw)
        issues = d.get("issues") or []
        out = {
            "ok": True,
            "path": str(path),
            "file_sha256": sha256_bytes(raw),
            **{
                k: d.get(k)
                for k in (
                    "status",
                    "generatedAt",
                    "effectiveTradeDate",
                    "businessMode",
                    "independentExpectedMarketDate",
                    "independentCalendarStatus",
                    "activeContract",
                )
            },
            "issues": [
                {
                    "code": (i.get("code") or i.get("kind") or i.get("check")) if isinstance(i, dict) else str(i)[:80],
                    "severity": i.get("severity") if isinstance(i, dict) else None,
                }
                for i in issues
            ][:30],
            "qlib": {
                k: (d.get("qlib") or {}).get(k)
                for k in ("status", "latestDate", "tag", "calendarLast", "marketDate")
                if k in (d.get("qlib") or {})
            },
        }
    raw = _read(last_run, 2_000_000)
    if raw:
        d = json.loads(raw)
        out["core_last_run"] = {k: d.get(k) for k in ("status", "target", "reason", "timestamp", "errorType")}
    return out


def cninfo_staging(work: Path) -> dict:
    files = []
    for p in sorted(glob.glob(str(work / "*cninfo*" / "*.json")))[-15:]:
        try:
            st = os.stat(p)
            files.append(
                {
                    "name": os.path.basename(p),
                    "bytes": st.st_size,
                    "mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.UTC).isoformat(),
                }
            )
        except OSError:
            pass
    return {"ok": True, "files": files}


def collect(paths: dict, summary_dates: list[str] | None = None) -> dict:
    t0 = time.monotonic()
    snap: dict = {"schema": "qp.market-legacy.v1", "collected_at": dt.datetime.now(dt.UTC).isoformat()}
    for name, fn in (
        ("calendar", lambda: legacy_calendar(paths["calendar"])),
        ("index_close", lambda: legacy_index_close(paths["index_close"])),
        ("qlib", lambda: qlib_catalog(paths["qlib"], paths["qlib_status"])),
        ("core_market", lambda: legacy_core(paths["core_db"])),
        ("snapshot_db", lambda: legacy_snapshot_index(paths["snapshot_db"])),
        ("health", lambda: legacy_health(paths["health"], paths["core_last_run"])),
        ("cninfo", lambda: cninfo_staging(paths["cninfo_staging"])),
    ):
        try:
            snap[name] = fn()
        except Exception as e:  # noqa: BLE001 - record type only
            snap[name] = {"ok": False, "error": type(e).__name__}
    q = snap.get("qlib") or {}
    sums = []
    if q.get("ok"):
        cal, insts = q["calendar"], q["instruments"]
        dates = set(summary_dates or [])
        refs = {s: _reference_date(paths["qlib"], cal, s) for s in REF_SYMS}
        q["reference_dates"] = refs
        if len(set(refs.values())) == 1 and None not in refs.values():
            dates.add(next(iter(refs.values())))
        core_rows = (snap.get("core_market") or {}).get("rows") or []
        if core_rows:
            dates.add(core_rows[-1]["date"])
        for d in sorted(dates)[-3:]:
            try:
                sums.append(qlib_summary(paths["qlib"], cal, insts, d, tag=q.get("tag")))
            except Exception as e:  # noqa: BLE001
                sums.append({"ok": False, "date": d, "error": type(e).__name__})
    snap["qlib_summary"] = sums
    snap["duration_s"] = round(time.monotonic() - t0, 2)
    return snap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary-date", action="append", default=[])
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", default=str(v))
    a = ap.parse_args(argv)
    try:
        with open("/proc/meminfo") as fh:
            avail = next(int(ln.split()[1]) // 1024 for ln in fh if ln.startswith("MemAvailable:"))
        if avail < int(os.getenv("QP_MIN_MEM_MB", "600")):
            print(json.dumps({"ok": False, "skipped": "low_memory", "mem_available_mb": avail}))
            return 75
    except (OSError, StopIteration):
        pass
    paths = {k: Path(getattr(a, k)) for k in DEFAULTS}
    snap = collect(paths, a.summary_date)
    out = Path(a.out)
    tmp = out.with_name(f".{out.name}.tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":")))
    os.chmod(tmp, 0o640)
    tmp.replace(out)
    print(json.dumps({"ok": True, "duration_s": snap["duration_s"], "bytes": out.stat().st_size}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
