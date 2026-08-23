import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(
    os.environ.get("PHASE2_PROJECT_ROOT", Path(__file__).resolve().parents[1])
).resolve()
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from phase2 import init  # noqa: E402
from server import H  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class ServerEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tempdir.name) / "phase2.db")
        cls.protocol_path = str(PROJECT_ROOT / "protocol" / "calibration_protocol_v1.json")
        init(cls.db_path)
        H.db_path = cls.db_path
        H.protocol_path = cls.protocol_path
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tempdir.cleanup()

    def test_healthz(self):
        with urlopen(f"{self.base_url}/healthz", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.load(response),
                {"scope": "loopback_phase2", "status": "ok"},
            )

    def test_dashboard_api(self):
        with urlopen(f"{self.base_url}/api/dashboard", timeout=5) as response:
            payload = json.load(response)
        self.assertEqual(response.status, 200)
        self.assertIn("protocol_hash", payload)
        self.assertIn("batches", payload)
        self.assertIn("stats", payload)

    def test_read_only_routing(self):
        with urlopen(f"{self.base_url}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Phase 2", page)

        with self.assertRaises(HTTPError) as post_error:
            urlopen(
                Request(f"{self.base_url}/api/dashboard", data=b"{}", method="POST"),
                timeout=5,
            )
        self.assertEqual(post_error.exception.code, 403)

        with self.assertRaises(HTTPError) as missing_error:
            urlopen(f"{self.base_url}/missing", timeout=5)
        self.assertEqual(missing_error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
