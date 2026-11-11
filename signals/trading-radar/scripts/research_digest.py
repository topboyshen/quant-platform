#!/usr/bin/env python3
"""Local research digest: A-share official closes + Quant strategy catalog.

Does not call OpenBB. Does not mutate production DBs or tracked Grafana JSON.
Writes markdown/JSON under data/ for curl; Grafana must read those files.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.update_index_close_cache import expected_market_as_of
from scripts.index_close_cache_contract import read_state

TZ = ZoneInfo("Asia/Shanghai")
SWING_DB = Path("/home/ubuntu/trading-radar/data/swing_macro.db")
OUT_MD = Path("/home/ubuntu/trading-radar/data/research_digest.md")
OUT_JSON = Path("/home/ubuntu/trading-radar/data/research_digest.json")
INDEX_CLOSE_CACHE = Path("/home/ubuntu/trading-radar/data/index_closes_latest.json")
REQUIRED = (
    ("sh000001", "上证指数"),
    ("sh000300", "沪深300"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
)


def _pg_strategies() -> list[dict]:
    try:
        import subprocess

        raw = subprocess.check_output(
            [
                "docker",
                "exec",
                "radar-quantdinger-postgres",
                "psql",
                "-U",
                "quantdinger",
                "-d",
                "quantdinger",
                "-t",
                "-A",
                "-F",
                "|",
                "-c",
                "SELECT id, strategy_name FROM qd_strategies_trading ORDER BY id",
            ],
            timeout=10,
        ).decode()
    except Exception as exc:
        return [{"error": str(exc)[:160]}]
    rows = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        sid, name = line.split("|", 1)
        rows.append({"id": int(sid), "name": name.strip()})
    return rows


def _pg_last_runs() -> list[dict]:
    try:
        import subprocess

        raw = subprocess.check_output(
            [
                "docker",
                "exec",
                "radar-quantdinger-postgres",
                "psql",
                "-U",
                "quantdinger",
                "-d",
                "quantdinger",
                "-t",
                "-A",
                "-F",
                "|",
                "-c",
                "SELECT id, strategy_id, symbol, left(result_json,1) FROM qd_backtest_runs ORDER BY id DESC LIMIT 8",
            ],
            timeout=10,
        ).decode()
    except Exception as exc:
        return [{"error": str(exc)[:160]}]
    out = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        out.append({"run": int(parts[0]), "strategy_id": int(parts[1] or 0), "symbol": parts[2]})
    return out


def _macro() -> dict:
    if not SWING_DB.is_file():
        return {"available": False}
    con = sqlite3.connect(f"file:{SWING_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT date, limit_up, limit_down, margin_balance, source "
            "FROM macro_slice ORDER BY date DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {"available": False}
    return {
        "available": True,
        "date": row["date"],
        "limit_up": row["limit_up"],
        "limit_down": row["limit_down"],
        "margin_balance": row["margin_balance"],
        "source": row["source"],
    }


def _index_close_cache(now: datetime | None = None) -> dict:
    """Read and validate the local cache without reaching any upstream."""
    current = now or datetime.now(TZ)
    try:
        expected_as_of = expected_market_as_of(current)
    except Exception:
        expected_as_of = None
    return read_state(INDEX_CLOSE_CACHE, expected_as_of, current)


def _render_index_cache(index_cache: dict) -> list[str]:
    lines = ["", "## Index closes (local cache; not exchange attestation)"]
    lines.append("- quality **%s** · integrity `%s` · available `%s`" % (
        index_cache["quality"], index_cache["integrity"], index_cache["available"]))
    lines.append("- cache `%s` · expected `%s` · source `%s` · fetched `%s`" % (
        index_cache["trade_date"], index_cache["expected_as_of"],
        index_cache["source"], index_cache["fetched_at"] or "unknown"))
    if index_cache["stale"]:
        lines.append("- **STALE / UNVERIFIED**: not usable as current verified market input.")
    if index_cache["issues"]:
        lines.append("- limitations: " + ", ".join(index_cache["issues"]))
    if not index_cache["provenance_verified"] and index_cache["closes"]:
        lines.append("- Historical/unverified values below are background only; no source or collection time has been inferred.")
    for row in index_cache["closes"]:
        lines.append("- `%s` **%s** (%s)" % (row["symbol"], row["close"], row["trade_date"]))
    if not index_cache["closes"]:
        lines.append("- cache unavailable or invalid")
    return lines


def main() -> int:
    now = datetime.now(TZ)
    macro = _macro()
    strategies = _pg_strategies()
    runs = _pg_last_runs()
    index_cache = _index_close_cache(now)
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "macro": macro,
        "strategies": strategies,
        "recent_runs": runs,
        "index_closes": index_cache["closes"],
        "index_close_cache": {
            key: value for key, value in index_cache.items() if key != "closes"
        },
        "note": "Not OpenBB. Local digest only. Frozen Quant frontend is unchanged.",
    }
    lines = [
        f"# Research digest  {now.strftime('%Y-%m-%d %H:%M CST')}",
        "",
        "## Macro (swing_macro.db)",
    ]
    if macro.get("available"):
        lines.append(
            f"- date **{macro['date']}** 涨停 {macro['limit_up']} / 跌停 {macro['limit_down']}  "
            f"source `{macro['source']}`"
        )
    else:
        lines.append("- macro unavailable")
    lines += _render_index_cache(index_cache)
    lines += ["", "## Strategies"]
    for item in strategies:
        if "error" in item:
            lines.append(f"- error: {item['error']}")
        else:
            lines.append(f"- id={item['id']} `{item['name']}`")
    lines += ["", "## Recent backtest runs"]
    for item in runs[:8]:
        if "error" in item:
            lines.append(f"- error: {item['error']}")
        else:
            lines.append(f"- run {item['run']} strategy={item['strategy_id']} {item['symbol']}")
    lines += [
        "",
        "_OpenBB was not installed. Use Quant 8888 for charts; Grafana for ops; this file for a one-screen catalog._",
        "",
    ]
    markdown = "\n".join(lines)
    OUT_MD.write_text(markdown, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUT_MD)
    print(OUT_JSON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
