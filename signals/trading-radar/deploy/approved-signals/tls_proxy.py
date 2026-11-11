#!/usr/bin/env python3
"""TLS terminator + path-gated reverse proxy to loopback approved-signals."""
from __future__ import annotations

import os
import ssl
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/home/ubuntu/trading-radar")
PATH_TOKEN = (ROOT / "secrets/approved_signals_path_token").read_text().strip()
PREFIX = f"/{PATH_TOKEN}"
UPSTREAM = ("127.0.0.1", 8094)
CERT = ROOT / "deploy/approved-signals/tls/cert.pem"
KEY = ROOT / "deploy/approved-signals/tls/key.pem"
HOST = os.environ.get("APPROVED_TLS_HOST", "0.0.0.0")
PORT = int(os.environ.get("APPROVED_TLS_PORT", "8443"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        import sys
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _proxy(self):
        if not self.path.startswith(PREFIX + "/") and self.path != PREFIX:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # strip prefix
        rest = self.path[len(PREFIX):] or "/"
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        conn = HTTPConnection(UPSTREAM[0], UPSTREAM[1], timeout=30)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "transfer-encoding", "connection")}
        headers["Connection"] = "close"
        try:
            conn.request(self.command, rest, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        finally:
            conn.close()

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(CERT), keyfile=str(KEY))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"tls proxy on {HOST}:{PORT} prefix={PREFIX}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
