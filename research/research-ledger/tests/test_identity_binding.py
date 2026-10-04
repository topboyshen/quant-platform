#!/usr/bin/env python3
"""Stateless regression tests: review/veto identity binding and request body bounds.

No database is used. A fake connection returns a verified capability row; the test
asserts which actor_id reaches the INSERT and audit statements.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import ledger_app as app  # noqa: E402

ROUND_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
CASE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class _Result:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [] if self._row is None else [self._row]


class FakeConn:
    def __init__(self, cap_actor="verified-bull", cap_role="BULL"):
        self.cap = {"capability_id": uuid.uuid4(), "actor_id": cap_actor, "role_name": cap_role, "scope_id": ROUND_ID}
        self.inserts: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        if "FROM ledger.capability" in text:
            return _Result(self.cap if params[1] == self.cap["role_name"] else None)
        if text.startswith("SELECT status,deadline_at FROM ledger.review_round"):
            return _Result({"status": "BLIND", "deadline_at": app.now_utc() + timedelta(hours=1)})
        if text.startswith("SELECT case_id,status FROM ledger.review_round"):
            return _Result({"case_id": CASE_ID, "status": "BLIND"})
        if text.startswith("INSERT"):
            self.inserts.append((text, tuple(params)))
            return _Result()
        raise AssertionError(f"unexpected SQL: {text}")

    def actors(self):
        out = []
        for text, params in self.inserts:
            if "review_submission" in text:
                out.append(("submission", params[3]))
            elif "risk_decision" in text:
                out.append(("veto", params[-1]))
            elif "audit_event" in text:
                out.append(("audit", params[1]))
        return out


class ReviewIdentityBindingTests(unittest.TestCase):
    def test_submit_records_capability_identity_when_actor_omitted(self):
        conn = FakeConn()
        app.submit_review(conn, "tok", ROUND_ID, "BULL", "h" * 64, None)
        self.assertEqual(conn.actors(), [("submission", "verified-bull"), ("audit", "verified-bull")])

    def test_submit_rejects_forged_identity(self):
        conn = FakeConn()
        with self.assertRaises(PermissionError):
            app.submit_review(conn, "tok", ROUND_ID, "BULL", "h" * 64, "someone-else")
        self.assertEqual(conn.inserts, [], "forged identity must not reach any INSERT")

    def test_submit_accepts_matching_identity(self):
        conn = FakeConn()
        app.submit_review(conn, "tok", ROUND_ID, "BULL", "h" * 64, "verified-bull")
        self.assertEqual({a for _, a in conn.actors()}, {"verified-bull"})

    def test_veto_records_capability_identity_when_actor_omitted(self):
        conn = FakeConn("verified-risk", "RISK_OFFICER")
        app.risk_veto(conn, "tok", ROUND_ID, "r" * 64, None)
        self.assertEqual(conn.actors(), [("veto", "verified-risk"), ("audit", "verified-risk")])

    def test_veto_rejects_forged_identity(self):
        conn = FakeConn("verified-risk", "RISK_OFFICER")
        with self.assertRaises(PermissionError):
            app.risk_veto(conn, "tok", ROUND_ID, "r" * 64, "risk_officer")
        self.assertEqual(conn.inserts, [])

    def test_capability_without_identity_fails_closed(self):
        conn = FakeConn("", "BULL")
        with self.assertRaises(PermissionError):
            app.submit_review(conn, "tok", ROUND_ID, "BULL", "h" * 64, None)
        self.assertEqual(conn.inserts, [])


class _Handler(app.LedgerHandler):
    """Drive the real do_POST without sockets."""

    def __init__(self, path, headers, body=b""):
        self.path = path
        self.headers = headers
        self.rfile = _BoundedReader(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.close_connection = False
        self.request_version = "HTTP/1.1"

    def send_response(self, code, message=None):
        self.status = int(code)

    def send_header(self, *_):
        pass

    def end_headers(self):
        pass


class _BoundedReader:
    """Records the size argument; a negative read would mean 'read until EOF'."""

    def __init__(self, body):
        self._buf = io.BytesIO(body)
        self.sizes = []

    def read(self, size=-1):
        self.sizes.append(size)
        if size is None or size < 0:
            raise AssertionError("unbounded read requested")
        return self._buf.read(size)


class _Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return default if value is None else [value]


class _MultiHeaders(_Headers):
    def __init__(self, values):
        super().__init__({"Content-Length": values[0]})
        self._values = values

    def get_all(self, name, default=None):
        return list(self._values) if name == "Content-Length" else default


def _post(path, headers, body=b"", conn=None):
    handler = _Handler(path, headers, body)
    conn = conn or FakeConn()
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    with mock.patch.object(app, "connect", return_value=ctx):
        handler.do_POST()
    payload = json.loads(handler.wfile.getvalue() or b"{}")
    return handler, payload, conn


class HttpBodyAndIdentityTests(unittest.TestCase):
    def _review_body(self, **extra):
        body = {"round_id": str(ROUND_ID), "role": "BULL", "content_hash": "h" * 64}
        body.update(extra)
        return json.dumps(body).encode()

    def test_negative_content_length_rejected_without_unbounded_read(self):
        handler, payload, conn = _post("/v1/review-submissions", _Headers({"Content-Length": "-1", "X-Ledger-Capability": "tok"}), self._review_body())
        self.assertEqual(handler.status, 400)
        self.assertTrue(all(s is not None and s >= 0 for s in handler.rfile.sizes))
        self.assertEqual(conn.inserts, [])

    def test_non_numeric_and_signed_lengths_rejected(self):
        for value in ("abc", "+5", " 5 5", "1e3", "\u0661"):
            handler, _, conn = _post("/v1/review-submissions", _Headers({"Content-Length": value, "X-Ledger-Capability": "tok"}), self._review_body())
            self.assertEqual(handler.status, 400, value)
            self.assertEqual(conn.inserts, [], value)

    def test_oversized_length_rejected_before_read(self):
        handler, _, conn = _post("/v1/review-submissions", _Headers({"Content-Length": str(10 ** 9)}), b"")
        self.assertEqual(handler.status, 413)
        self.assertEqual(handler.rfile.sizes, [])

    def test_conflicting_duplicate_content_length_rejected(self):
        handler, _, conn = _post("/v1/review-submissions", _MultiHeaders(["10", "20"]), b"x" * 20)
        self.assertEqual(handler.status, 400)
        self.assertEqual(conn.inserts, [])

    def test_transfer_encoding_rejected(self):
        handler, _, conn = _post("/v1/review-submissions", _Headers({"Transfer-Encoding": "chunked", "X-Ledger-Capability": "tok"}), b"")
        self.assertEqual(handler.status, 400)

    def test_short_body_rejected(self):
        body = self._review_body()
        handler, _, conn = _post("/v1/review-submissions", _Headers({"Content-Length": str(len(body) + 50), "X-Ledger-Capability": "tok"}), body)
        self.assertEqual(handler.status, 400)
        self.assertEqual(conn.inserts, [])

    def test_http_review_without_actor_uses_capability_identity(self):
        body = self._review_body()
        handler, payload, conn = _post("/v1/review-submissions", _Headers({"Content-Length": str(len(body)), "X-Ledger-Capability": "tok"}), body)
        self.assertEqual(handler.status, 201, payload)
        self.assertEqual({a for _, a in conn.actors()}, {"verified-bull"})

    def test_http_review_forged_actor_forbidden(self):
        body = self._review_body(actor_id="someone-else")
        handler, payload, conn = _post("/v1/review-submissions", _Headers({"Content-Length": str(len(body)), "X-Ledger-Capability": "tok"}), body)
        self.assertEqual(handler.status, 403)
        self.assertEqual(conn.inserts, [])

    def test_http_veto_forged_actor_forbidden_and_default_not_used(self):
        conn = FakeConn("verified-risk", "RISK_OFFICER")
        body = json.dumps({"round_id": str(ROUND_ID), "reason_hash": "r" * 64, "actor_id": "risk_officer"}).encode()
        handler, _, conn = _post("/v1/risk-vetoes", _Headers({"Content-Length": str(len(body)), "X-Ledger-Capability": "tok"}), body, conn)
        self.assertEqual(handler.status, 403)
        self.assertEqual(conn.inserts, [])
        conn = FakeConn("verified-risk", "RISK_OFFICER")
        body = json.dumps({"round_id": str(ROUND_ID), "reason_hash": "r" * 64}).encode()
        handler, _, conn = _post("/v1/risk-vetoes", _Headers({"Content-Length": str(len(body)), "X-Ledger-Capability": "tok"}), body, conn)
        self.assertEqual(handler.status, 201)
        self.assertEqual({a for _, a in conn.actors()}, {"verified-risk"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
