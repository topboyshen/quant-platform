"""Local process fault injection using fixed functions, no network or database."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import bounded_source


def success(): return {"n": 1}
def never_returns(): time.sleep(30)
def fail(): raise ValueError("SECRET_RESPONSE")
def helper(): subprocess.run(["false"], check=False)
def huge(): return "x" * (bounded_source.MAX_BYTES + 1)
def noisy():
    # More diagnostics than MAX_BYTES must not become an in-memory buffer or
    # leak into the parent result. Only the small JSON result is retained.
    for _ in range(100):
        print("PRIVATE_SOURCE_DIAGNOSTIC" * 4096)
        sys.stderr.write("PRIVATE_SOURCE_DIAGNOSTIC" * 4096)
    return {"n": 1}


class BoundedSourceTests(unittest.TestCase):
    def test_bad_child_result_is_boundary_error_not_attribute_error(self):
        for raw in (b'[]', b'null', b'1', b'{', b'{"ok":true}', b'{"ok":false,"errorType":[]}',
                    b'{"ok":false,"errorType":"ValueError","endpoint":"private-url"}',
                    b'{"ok":false,"errorType":"ValueError","endpoint":[]}'):
            with self.subTest(raw=raw), self.assertRaises(bounded_source.CollectionBoundaryError):
                bounded_source._decode_result(raw)

    def test_vendor_output_is_discarded_not_accumulated(self):
        sink = bounded_source._Discard()
        self.assertFalse(hasattr(sink, "getvalue"))
        self.assertEqual(sink.write("abc"), 3)
        self.assertEqual(bounded_source.call(noisy, timeout=2), {"n": 1})

    def test_json_result_is_preserved_and_private_scratch_removed(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(bounded_source.call(success, state_dir=root), {"n": 1})
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_timeout_is_bounded_without_retry(self):
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(bounded_source.CollectionBoundaryError, "deadline_exceeded"):
                bounded_source.call(never_returns, timeout=.1, state_dir=root)
            self.assertEqual(list(Path(root).iterdir()), [])
        self.assertLess(time.monotonic() - started, 3)

    def test_error_text_is_never_returned(self):
        with self.assertRaises(bounded_source.CollectionFailure) as caught:
            bounded_source.call(fail, timeout=2)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_child_cannot_launch_helpers(self):
        with self.assertRaises(bounded_source.CollectionFailure):
            bounded_source.call(helper, timeout=2)

    def test_response_size_is_bounded(self):
        with self.assertRaises(bounded_source.CollectionFailure):
            bounded_source.call(huge, timeout=2)
