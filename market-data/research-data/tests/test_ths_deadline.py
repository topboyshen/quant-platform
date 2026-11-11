"""Real short POSIX alarms, synthetic HTTP only: no network or business IO."""
import signal
import socket
import threading
import time
import unittest
from unittest import mock

import requests

import ths_source as source


REAL_SLEEP = time.sleep
HTML = b'''<html><head><meta charset="utf-8">
<title>Example(600875) \xe6\xa6\x82\xe5\xbf\xb5\xe9\xa2\x98\xe6\x9d\x90_F10_\xe5\x90\x8c\xe8\x8a\xb1\xe9\xa1\xba\xe9\x87\x91\xe8\x9e\x8d\xe6\x9c\x8d\xe5\x8a\xa1\xe7\xbd\x91</title></head>
<body><input id="stockCode" value="600875"><input id="marketId" value="17"></body></html>'''


class Response:
    status_code = 200
    headers = {"Content-Type": "text/html"}

    def __init__(self, stream=None):
        self.stream = stream
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        if self.stream is None:
            yield HTML
        else:
            yield from self.stream()


@unittest.skipUnless(hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM"), "POSIX alarm required")
class DeadlineTests(unittest.TestCase):
    def setUp(self):
        if any(signal.getitimer(signal.ITIMER_REAL)):
            self.skipTest("do not disturb an existing test-runner timer")
        self.original_handler = signal.getsignal(signal.SIGALRM)
        self.session = mock.Mock(headers={})
        self.response = Response()
        self.session.get.return_value = self.response
        for patch in (
            mock.patch.object(source.requests, "Session", return_value=self.session),
            mock.patch.object(source, "PAGE_DEADLINE_SECONDS", 0.05),
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")),
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        remaining = signal.getitimer(signal.ITIMER_REAL)
        handler = signal.getsignal(signal.SIGALRM)
        # Avoid a faulty implementation leaking a live timer into other tests.
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.original_handler)
        self.assertEqual(remaining, (0.0, 0.0))
        self.assertIs(handler, self.original_handler)

    def page(self):
        with source.F10Client("concepts") as client:
            return client.page("600875", "concept")

    def expires(self):
        started = time.monotonic()
        with self.assertRaises(source.SourceError) as caught:
            self.page()
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.diagnostic, {
            "source": "concepts", "stage": "concept_page", "errorType": "DeadlineExceeded",
            "provider": "ths_f10", "code": "600875"})
        self.assertLess(elapsed, 0.8, "blocking code exceeded the hard test deadline")
        self.assertGreater(elapsed, 0.02)
        self.session.close.assert_called_once()

    def test_slow_headers_are_interrupted_before_requests_inactivity_timeout(self):
        def slow_headers(*args, **kwargs):
            REAL_SLEEP(2)
            return self.response
        self.session.get.side_effect = slow_headers
        self.expires()
        self.assertEqual(self.session.get.call_count, 1)

    def test_continuous_body_drip_does_not_reset_deadline(self):
        def drip():
            for _ in range(1000):
                REAL_SLEEP(0.005)
                yield b" "
        self.response.stream = drip
        self.expires()
        self.assertTrue(self.response.closed)

    def test_blocked_body_read_is_interrupted_without_waiting_for_next_chunk(self):
        def blocked():
            REAL_SLEEP(2)
            yield HTML
        self.response.stream = blocked
        self.expires()
        self.assertTrue(self.response.closed)

    def test_parser_is_inside_the_same_deadline(self):
        def slow_parser(*args, **kwargs):
            REAL_SLEEP(2)
        with mock.patch.object(source, "BeautifulSoup", side_effect=slow_parser):
            self.expires()
        self.assertTrue(self.response.closed)

    def test_broad_exception_handler_cannot_swallow_the_expiry(self):
        caught = []
        def broad_supplier(*args, **kwargs):
            try:
                REAL_SLEEP(2)
            except Exception:
                caught.append(True)
                return self.response
        self.session.get.side_effect = broad_supplier
        self.expires()
        self.assertEqual(caught, [])

    def test_retry_backoff_shares_the_page_deadline(self):
        self.session.get.side_effect = requests.ReadTimeout("SECRET")
        self.expires()  # First default one-second retry sleep is interrupted.
        self.assertEqual(self.session.get.call_count, 1)

    def test_multiple_attempts_do_not_get_fresh_deadlines(self):
        def transient(*args, **kwargs):
            REAL_SLEEP(0.02)
            raise requests.ReadTimeout("SECRET")
        self.session.get.side_effect = transient
        with mock.patch.object(source.time, "sleep", side_effect=lambda _: REAL_SLEEP(0.01)):
            self.expires()
        self.assertEqual(self.session.get.call_count, 2)

    def test_success_clears_timer_and_restores_custom_handler(self):
        def previous(signum, frame):
            raise AssertionError("unexpected old signal")
        signal.signal(signal.SIGALRM, previous)
        try:
            _, evidence = self.page()
            self.assertEqual(evidence["code"], "600875")
            self.assertIs(signal.getsignal(signal.SIGALRM), previous)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        finally:
            signal.signal(signal.SIGALRM, self.original_handler)

    def test_ordinary_failure_clears_timer_and_preserves_source_diagnostic(self):
        self.response.status_code = 403
        with self.assertRaises(source.SourceError) as caught:
            self.page()
        self.assertEqual(caught.exception.diagnostic["errorType"], "HTTPStatus")
        self.assertEqual(caught.exception.diagnostic["httpStatus"], 403)
        self.assertEqual(caught.exception.diagnostic["attempt"], 1)
        self.assertTrue(self.response.closed)

    def test_existing_timer_is_rejected_without_replacing_it(self):
        def previous(signum, frame):
            raise AssertionError("existing timer should not fire in this short test")
        signal.signal(signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, 5, 7)
        try:
            with self.assertRaises(source.SourceError) as caught:
                self.page()
            self.assertEqual(caught.exception.diagnostic["errorType"], "DeadlineUnavailable")
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining, 4)
            self.assertEqual(interval, 7)
            self.assertIs(signal.getsignal(signal.SIGALRM), previous)
            self.session.get.assert_not_called()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.original_handler)

    def test_non_main_thread_rejected_before_http(self):
        errors = []
        def worker():
            try:
                self.page()
            except source.SourceError as exc:
                errors.append(exc.diagnostic)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["errorType"], "DeadlineUnavailable")
        self.session.get.assert_not_called()

    def test_unavailable_timer_implementation_rejects_before_http(self):
        with mock.patch.object(source.signal, "getitimer", side_effect=OSError("SECRET")):
            with self.assertRaises(source.SourceError) as caught:
                self.page()
        self.assertEqual(caught.exception.diagnostic["errorType"], "DeadlineUnavailable")
        self.assertNotIn("SECRET", str(caught.exception))
        self.session.get.assert_not_called()

    def test_timer_setup_failure_restores_handler(self):
        original = signal.setitimer
        def unavailable(which, seconds, *args):
            if seconds:
                raise OSError("SECRET")
            return original(which, seconds, *args)
        with mock.patch.object(source.signal, "setitimer", side_effect=unavailable):
            with self.assertRaises(source.SourceError) as caught:
                self.page()
        self.assertEqual(caught.exception.diagnostic["errorType"], "DeadlineUnavailable")
        self.assertNotIn("SECRET", str(caught.exception))
        self.session.get.assert_not_called()

    @unittest.skipUnless(hasattr(signal, "pthread_sigmask"), "signal mask API required")
    def test_blocked_alarm_rejects_without_changing_signal_mask(self):
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            with self.assertRaises(source.SourceError) as caught:
                self.page()
            self.assertEqual(caught.exception.diagnostic["errorType"], "DeadlineUnavailable")
            self.assertIn(signal.SIGALRM, signal.pthread_sigmask(signal.SIG_BLOCK, []))
            self.session.get.assert_not_called()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


if __name__ == "__main__":
    unittest.main()
