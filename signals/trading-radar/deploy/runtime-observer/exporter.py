"""Read-only Quant runtime probes. No business imports, credentials or writes."""
import bisect
import json
import math
import os
import threading
import time
from datetime import date, datetime, time as daytime, timedelta
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
MAX_BYTES = 1024 * 1024
SYMBOLS = {"sh000001", "sh000300", "sz399001", "sz399006"}
TARGETS = {
    "backend": "http://quantdinger-backend:5000/api/health",
    "frontend": "http://quantdinger-frontend:80/",
    "grafana": "http://grafana:3000/api/health",
}


def day(value):
    if not isinstance(value, str):
        raise ValueError("date string required")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("noncanonical date")
    return parsed


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp string required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.timestamp()


def day_timestamp(value):
    return datetime.combine(value, daytime(), TZ).timestamp()


def read_json(path):
    # Reopen on every scrape: producers replace JSON files atomically.
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("oversize")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("object required")
    return data


def expected_trade_date(calendar, now):
    if (calendar.get("schema_version") != 1
            or calendar.get("source") != "akshare.tool_trade_date_hist_sina"
            or not 0 < timestamp(calendar["generated_at"]) <= now.timestamp() + 300):
        raise ValueError("invalid calendar metadata")
    dates = [day(item) for item in calendar["trading_dates"]]
    if (len(dates) < 100 or dates != sorted(set(dates))
            or calendar.get("count") != len(dates)
            or day(calendar["min_date"]) != dates[0]
            or day(calendar["max_date"]) != dates[-1]):
        raise ValueError("invalid trading calendar")
    today = now.astimezone(TZ).date()
    recent = [item for item in dates if item >= dates[-1] - timedelta(days=60)]
    # A future year-end boundary must not hide a corrupted session near today.
    relevant = [item for item in dates if today - timedelta(days=60) <= item <= today + timedelta(days=60)]
    if (dates[-1] > today + timedelta(days=550) or len(recent) < 20
            or any(item.weekday() >= 5 for item in recent)
            or len(relevant) < 20 or any(item.weekday() >= 5 for item in relevant)
            or max((b - a).days for a, b in zip(relevant, relevant[1:])) > 14
            or max((b - a).days for a, b in zip(recent, recent[1:])) > 14):
        raise ValueError("invalid recent calendar density or boundary")
    # The existing updater explicitly allows Friday to cover Saturday/Sunday,
    # but never assumes that an uncovered weekday is a holiday.
    last_covered = dates[-1]
    while last_covered < today and (last_covered + timedelta(days=1)).weekday() >= 5:
        last_covered += timedelta(days=1)
    if not dates[0] <= today <= last_covered:
        raise ValueError("calendar coverage unknown")
    cutoff = datetime.combine(today, daytime(15, 30), TZ)
    index = (bisect.bisect_right(dates, today) if now >= cutoff
             else bisect.bisect_left(dates, today)) - 1
    if index < 0:
        raise ValueError("no previous trading day")
    return dates[index]


def validate_closes(rows, trade_day):
    if not isinstance(rows, list) or len(rows) != len(SYMBOLS):
        raise ValueError("four indices required")
    if {row["symbol"] for row in rows} != SYMBOLS:
        raise ValueError("indices missing or duplicated")
    for row in rows:
        value = row["close"]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0
                or day(row["trade_date"]) != trade_day
                or not isinstance(row.get("source"), str) or not row["source"]):
            raise ValueError("invalid close")


def validate_cache(cache, now, rows=None, *, allow_declared_stale=False):
    trade_day = day(cache["trade_date"])
    expected_as_of = day(cache["expected_as_of"])
    stale_flag = cache["stale"]
    if not isinstance(stale_flag, bool):
        raise ValueError("stale flag required")
    if allow_declared_stale:
        if stale_flag is False and expected_as_of != trade_day:
            raise ValueError("fresh cache date mismatch")
    elif expected_as_of != trade_day or stale_flag is not False:
        raise ValueError("cache declares stale")
    if trade_day > now.astimezone(TZ).date():
        raise ValueError("future trade day")
    if not isinstance(cache.get("source"), str) or not cache["source"]:
        raise ValueError("missing source")
    fetched = timestamp(cache["fetched_at"])
    if fetched > now.timestamp() + 300 or fetched < day_timestamp(trade_day):
        raise ValueError("invalid fetch time")
    if rows is not None:
        validate_closes(rows, trade_day)
    return trade_day, fetched


def collect(data_dir, now=None):
    now = now or datetime.now(TZ)
    metrics = {
        "radar_runtime_expected_date_known": 0,
        "radar_runtime_index_cache_valid": 0,
        "radar_runtime_index_cache_stale": 1,
        "radar_runtime_digest_valid": 0,
        "radar_runtime_digest_inputs_stale": 1,
    }
    loaded = {}
    for label, filename in (("calendar", "trading_calendar.json"),
                            ("index", "index_closes_latest.json"),
                            ("digest", "research_digest.json")):
        try:
            loaded[label] = read_json(data_dir / filename)
            metrics['radar_runtime_file_read_success{file="%s"}' % label] = 1
        except (OSError, ValueError, TypeError, RecursionError):
            metrics['radar_runtime_file_read_success{file="%s"}' % label] = 0
    expected = None
    try:
        expected = expected_trade_date(loaded["calendar"], now)
        metrics["radar_runtime_expected_date_known"] = 1
        metrics["radar_runtime_expected_trade_date_timestamp_seconds"] = day_timestamp(expected)
        metrics["radar_runtime_calendar_age_seconds"] = max(0, now.timestamp() - timestamp(loaded["calendar"]["generated_at"]))
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, OverflowError):
        pass
    try:
        cache = loaded["index"]
        if cache.get("schema_version") != "quant.index-close-cache.v1":
            raise ValueError("unknown cache schema")
        trade_day, fetched = validate_cache(cache, now, cache["closes"])
        metrics["radar_runtime_index_cache_valid"] = 1
        metrics["radar_runtime_index_cache_stale"] = int(expected is None or trade_day != expected)
        metrics["radar_runtime_index_trade_date_timestamp_seconds"] = day_timestamp(trade_day)
        metrics["radar_runtime_index_fetched_timestamp_seconds"] = fetched
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, OverflowError):
        pass
    try:
        digest = loaded["digest"]
        generated = timestamp(digest["generated_at"])
        if not 0 < generated <= now.timestamp() + 300:
            raise ValueError("invalid digest timestamp")
        metrics["radar_runtime_digest_age_seconds"] = max(0, now.timestamp() - generated)
        cache = digest["index_close_cache"]
        if cache.get("available") is not True:
            raise ValueError("digest cache unavailable")
        trade_day, fetched = validate_cache(
            cache, now, digest["index_closes"], allow_declared_stale=True)
        if generated + 300 < fetched:
            raise ValueError("digest generated before input")
        metrics["radar_runtime_digest_valid"] = 1
        metrics["radar_runtime_digest_inputs_stale"] = int(expected is None or trade_day != expected)
        metrics["radar_runtime_digest_generated_timestamp_seconds"] = generated
        metrics["radar_runtime_digest_trade_date_timestamp_seconds"] = day_timestamp(trade_day)
    except (KeyError, ValueError, TypeError, IndexError, AttributeError, OverflowError):
        pass
    return metrics


def render(metrics):
    # Only fixed metric names and fixed labels; never emit file data as labels.
    return "".join("%s %s\n" % (key, value) for key, value in sorted(metrics.items())).encode()


def probe(target):
    if target not in TARGETS:
        raise ValueError("unknown target")
    started = time.monotonic()
    code = 0
    connection = None
    try:
        url = urlsplit(TARGETS[target])
        if url.scheme != "http":
            raise ValueError("HTTP probe targets required")
        # All targets are fixed internal HTTP endpoints. Avoid urllib's default
        # HTTPS/CA context per probe; direct HTTP also has no proxy or redirects.
        connection = HTTPConnection(url.hostname, url.port, timeout=2)
        connection.request("GET", url.path or "/", headers={
            "User-Agent": "quant-runtime-observer/1", "Connection": "close"})
        with connection.getresponse() as response:
            code = response.status
    except (HTTPException, OSError, ValueError):
        pass
    finally:
        if connection is not None:
            connection.close()
    return {"probe_success": int(code == 200), "probe_http_status_code": code,
            "probe_duration_seconds": time.monotonic() - started}


class Handler(BaseHTTPRequestHandler):
    slots = threading.BoundedSemaphore(4)
    data_dir = Path(os.environ.get("DATA_DIR", "/app/data"))

    def respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/health" and not parsed.query:
            self.respond(200, b"ok\n")
            return
        if not self.slots.acquire(blocking=False):
            self.respond(503, b"busy\n")
            return
        try:
            if parsed.path == "/metrics" and not parsed.query:
                self.respond(200, render(collect(self.data_dir)))
            elif parsed.path == "/probe":
                args = parse_qs(parsed.query, keep_blank_values=True)
                if set(args) != {"target"} or len(args["target"]) != 1 or args["target"][0] not in TARGETS:
                    self.respond(400, b"unknown target\n")
                else:
                    self.respond(200, render(probe(args["target"][0])))
            else:
                self.respond(404, b"not found\n")
        finally:
            self.slots.release()

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    # 2026-10-17: clients (e.g. the healthcheck) may close before the response
    # is fully written; that is not an exporter error, so skip the traceback.
    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    Server(("0.0.0.0", 9116), Handler).serve_forever()
