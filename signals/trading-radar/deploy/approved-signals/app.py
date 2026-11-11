#!/usr/bin/env python3
"""Approved-signal intake (stdlib only): HMAC, idempotent, risk gate stub."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(os.environ.get("APPROVED_SIGNALS_ROOT", "/home/ubuntu/trading-radar"))
SECRET_FILE = Path(os.environ.get("APPROVED_SIGNALS_HMAC_FILE", str(ROOT / "secrets/approved_signals_hmac")))
DB_PATH = Path(os.environ.get("APPROVED_SIGNALS_DB", str(ROOT / "data/approved-signals/intake.sqlite3")))
MAX_SKEW_SEC = int(os.environ.get("APPROVED_SIGNALS_MAX_SKEW_SEC", "300"))
LOCK = threading.Lock()


def load_secret() -> bytes:
    return SECRET_FILE.read_text(encoding="utf-8").strip().encode("utf-8")


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intakes (
          idempotency_key TEXT PRIMARY KEY,
          received_at TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          detail TEXT
        )
        """
    )
    conn.commit()
    return conn


def parse_expires(raw):
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def risk_check(payload: dict):
    required = ["idempotency_key", "signal_type", "symbol", "confidence", "expires_at", "provenance", "decision"]
    missing = [k for k in required if k not in payload]
    if missing:
        return False, "missing_fields:" + ",".join(missing)
    if payload.get("decision") != "approve":
        return False, "decision_not_approve"
    if payload.get("review_status") == "pending":
        return False, "still_pending"
    try:
        conf_f = float(payload.get("confidence"))
    except Exception:
        return False, "bad_confidence"
    if conf_f < 0.5:
        return False, "confidence_below_floor"
    exp = parse_expires(str(payload.get("expires_at")))
    if exp is None:
        return False, "bad_expires_at"
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return False, "expired"
    if payload.get("side") not in (None, "long", "short", "flat"):
        return False, "bad_side"
    return True, "ok"


class Handler(BaseHTTPRequestHandler):
    server_version = "approved-signals/0.1"

    def log_message(self, fmt, *args):
        # keep journal tidy
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._json(200, {"ok": True, "service": "approved-signals"})
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/hooks/v1/approved-signals":
            return self._json(404, {"ok": False, "error": "not_found"})
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length)
        ts = self.headers.get("X-Timestamp", "")
        sig = self.headers.get("X-Signature", "")
        source = self.headers.get("X-Source", "")
        if source != "dedirock":
            return self._json(401, {"ok": False, "error": "bad_source"})
        try:
            ts_i = int(ts)
        except Exception:
            return self._json(401, {"ok": False, "error": "bad_timestamp"})
        if abs(int(time.time()) - ts_i) > MAX_SKEW_SEC:
            return self._json(401, {"ok": False, "error": "timestamp_skew"})
        expect = "sha256=" + hmac.new(load_secret(), f"{ts}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return self._json(401, {"ok": False, "error": "bad_signature"})
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json(400, {"ok": False, "error": "invalid_json"})
        if not isinstance(payload, dict):
            return self._json(400, {"ok": False, "error": "payload_not_object"})
        key = payload.get("idempotency_key")
        if not key or not isinstance(key, str):
            return self._json(400, {"ok": False, "error": "missing_idempotency_key"})

        with LOCK:
            conn = db()
            try:
                row = conn.execute("SELECT status, detail FROM intakes WHERE idempotency_key=?", (key,)).fetchone()
                if row:
                    return self._json(409, {"ok": True, "duplicate": True, "status": row[0], "detail": row[1], "idempotency_key": key})
                passed, detail = risk_check(payload)
                status = "accepted" if passed else "rejected"
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "INSERT INTO intakes(idempotency_key, received_at, status, payload_json, detail) VALUES (?,?,?,?,?)",
                    (key, now, status, json.dumps(payload, ensure_ascii=False), detail),
                )
                conn.commit()
            finally:
                conn.close()

        if not passed:
            return self._json(422, {"ok": False, "status": "rejected", "detail": detail, "idempotency_key": key})
        return self._json(200, {"ok": True, "status": "accepted", "detail": detail, "idempotency_key": key, "order": "queued_stub"})


def main():
    host = os.environ.get("APPROVED_SIGNALS_HOST", "127.0.0.1")
    port = int(os.environ.get("APPROVED_SIGNALS_PORT", "8094"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"approved-signals listening on {host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
