from datetime import datetime
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import atomic
import candidate
import run
from source import SourceError


REF = "a" * 32
DAYS = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"]


def exchange(left, right):
    temp = left.with_name("test-exchange-temp")
    left.rename(temp)
    right.rename(left)
    temp.rename(right)


class Client:
    def __init__(self, latest="2026-09-08", days=None):
        self.requests = 0
        self.latest = latest
        self.days = days or DAYS

    def latest_ref(self):
        return REF

    def table(self, name):
        return f"`{name}` AS OF '{REF}'"

    def query(self, sql):
        self.requests += 1
        if "symbol IN" in sql and "tradedate = " in sql:
            day = sql.split("tradedate = '")[1].split("'")[0]
            symbols = ("SH600000", "SZ000001", "SH600519", "SZ300750",
                       "SH000300", "SH000905", "SH000852", "SH000906", "SZ399300")
            if day == self.latest:
                return [{"symbol": symbol, "tradedate": day} for symbol in symbols]
            return []
        if "MAX(tradedate)" in sql:
            return [{"latest": self.latest}]
        raise AssertionError(sql)

    def paginated_rows(self, table, columns, where, fields):
        self.requests += 1
        if table == "ts_trade_day_calendar":
            days = ["2026-09-09", "2026-09-10"] if "date > '" in where else self.days
            return [{"exchange": "SSE", "date": day, "is_open": "1"} for day in days]
        if table == "final_a_stock_eod_price":
            return [{"tradedate": day, "symbol": "SH600000", "open": "10", "high": "11",
                     "low": "9", "close": "10", "adjclose": "20", "volume": "100", "amount": "100"}
                    for day in DAYS]
        raise AssertionError(table)


class RunTests(unittest.TestCase):
    def test_source_diagnostic_whitelist_rejects_untrusted_and_malformed_fields(self):
        fields = ("queryKind", "table", "pageIndex", "elapsedMs")
        valid = {"queryKind": "page", "table": "final_a_stock_eod_price", "pageIndex": 1, "elapsedMs": 84000}
        self.assertEqual(run.safe_last_events([valid], fields, 1), [valid])
        for event in ({"queryKind": "SQL secret", "table": "token", "pageIndex": True, "elapsedMs": -1},
                      {"queryKind": {}, "table": [], "pageIndex": None, "elapsedMs": "secret"}):
            self.assertEqual(run.safe_last_events([event], fields, 1), [{}])

    def test_runtime_allows_observed_slow_count_but_keeps_global_bounds(self):
        client = run.source_client()
        self.assertEqual(client.transport.timeout, 40)
        self.assertEqual(client.transport.total_timeout, 84)
        self.assertEqual(client.transport.connect_timeout, 8)
        self.assertEqual(client.max_seconds, 1000)
        self.assertEqual(client.max_requests, 320)
        self.assertEqual(client.transport.proxy, "socks5h://127.0.0.1:7891")

    def test_transport_diagnostics_whitelist_rejects_unbounded_or_private_fields(self):
        fields = ("diagnosticStatus", "attemptTimeoutMs", "remainingBudgetMs", "attemptElapsedMs",
                  "nameLookupMs", "connectMs", "appConnectMs", "startTransferMs", "transferTotalMs", "numRedirects")
        good = {key: 0 for key in fields}
        good["diagnosticStatus"] = "available"
        self.assertEqual(run.safe_last_events([good], fields, 1), [good])
        for invalid in (True, -1, 86400001, float("nan"), float("inf"), "token=SECRET"):
            bad = {key: invalid for key in fields}
            self.assertEqual(run.safe_last_events([bad], fields, 1), [{}])
        self.assertEqual(run.safe_last_events([{"diagnosticStatus": "https://secret", "numRedirects": 100}], fields, 1), [{}])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name).resolve() / "data"
        self.active = self.data / "cn_data"
        (self.active / "calendars").mkdir(parents=True)
        (self.active / "features").mkdir()
        (self.active / "calendars/day.txt").write_text("\n".join(DAYS[:3]) + "\n")
        (self.active / "calendars/day_future.txt").write_text("\n".join(DAYS[3:]) + "\n")
        (self.active / ".qlib-release").write_text("2026-09-06\n")
        (self.data / ".tag").write_text("2026-09-06\n")
        self.inode = self.active.stat().st_ino
        self.events = []
        self.tree = types.SimpleNamespace(
            active_update_symbols=lambda root, base, symbols: symbols,
            inspect_symbols=lambda root, symbols, calendar: {"SH600000": {"new": False, "last3_valid_dates": DAYS[:3], "last_date": DAYS[2], "first_date": DAYS[0]}},
            apply_prices=lambda *a, **kw: {"coverage": {"eligible": 1, "valid": 1, "ratio": 1.0}},
            validate_tree=lambda *a, **kw: {"coverage": {"eligible": 1, "valid": 1, "ratio": 1.0}},
        )
        self.indices = types.SimpleNamespace(update_indices=lambda *args: {"validated": True})
        self.addCleanup(patch.stopall)
        patch.dict("sys.modules", {"tree": self.tree, "indices": self.indices}).start()
        patch.object(run, "Transaction", lambda data: atomic.Transaction(data, exchange)).start()
        patch.object(run, "preflight_indices", lambda *args: {}).start()
        patch.object(run, "validate_index_market_alignment", lambda *a, **kw: {"all_members_have_prices": True}).start()
        patch.object(run, "legacy_index_absence", lambda *a: {"sourceCommit": REF, "baseDate": DAYS[2], "dates": {}, "lastQuotedDates": {}}).start()

    def execute(self, **options):
        return run.run_update(self.data, client=options.pop("client", Client()),
            now=datetime(2026, 9, 9, 14, tzinfo=run.SHANGHAI), emit=self.events.append,
            min_eligible=1, **options)

    def test_validated_dry_run_does_not_promote(self):
        result = self.execute(dry_run=True)
        self.assertEqual(result["status"], "dry_run_validated")
        self.assertEqual(self.active.stat().st_ino, self.inode)
        self.assertEqual(run.read_calendar(self.active), DAYS[:3])
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_commit_keeps_release_identity_and_advances_calendar(self):
        result = self.execute()
        self.assertEqual(result["status"], "updated")
        self.assertEqual(run.read_calendar(self.active), DAYS)
        self.assertEqual((self.data / ".tag").read_text(), "2026-09-06\n")
        manifest = json.loads((self.active / atomic.MANIFEST).read_text())
        self.assertEqual(manifest["sourceCommit"], REF)
        self.assertEqual(manifest["targetDate"], "2026-09-08")

    def test_dry_run_checks_prepared_consumer_without_promoting(self):
        def check(stage, manifest):
            self.assertEqual(json.loads((stage / atomic.MANIFEST).read_text()), manifest)
            self.assertEqual((stage.parent / ".tag").read_bytes(), (self.data / ".tag").read_bytes())
            self.assertEqual(self.active.stat().st_ino, self.inode)
            return {"status": "passed"}
        self.execute(dry_run=True, candidate_check=check)
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_consumer_rejection_cannot_promote(self):
        def check(stage, manifest):
            raise ValueError("consumer_refuses_manifest")
        with self.assertRaisesRegex(ValueError, "consumer_refuses_manifest"):
            self.execute(candidate_check=check)
        self.assertEqual(self.active.stat().st_ino, self.inode)
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_candidate_subphase_failure_is_distinguishable(self):
        lifecycle = {"runId": "phase-test"}
        with patch.object(run, "validate_index_market_alignment", side_effect=ValueError("alignment_failed")):
            with self.assertRaisesRegex(ValueError, "alignment_failed"):
                self.execute(lifecycle=lifecycle)
        self.assertEqual(lifecycle["stage"], "candidate_index_alignment")
        def refuse(stage, manifest):
            raise ValueError("consumer_failed")
        with self.assertRaisesRegex(ValueError, "consumer_failed"):
            self.execute(lifecycle=lifecycle, candidate_check=refuse)
        self.assertEqual(lifecycle["stage"], "candidate_consumer_validation")
        self.assertIsInstance(lifecycle["stageStartedMonotonic"], float)
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_completed_candidate_phases_have_bounded_safe_timing(self):
        self.execute(candidate_check=lambda *args: {"status": "passed"})
        records = {row["event"]: row for row in map(json.loads, self.events)}
        for name in ("price_validation_passed", "candidate_index_alignment_passed",
                     "candidate_manifest_prepared", "candidate_consumer_validation_passed", "candidate_published"):
            self.assertIs(type(records[name]["phaseElapsedMs"]), int)
            self.assertGreaterEqual(records[name]["phaseElapsedMs"], 0)

    def test_manifest_substeps_are_distinct_for_prepare_and_commit(self):
        lifecycle = {"runId": "manifest-progress"}
        self.execute(lifecycle=lifecycle)
        records = [row for row in map(json.loads, self.events)
                   if row["event"] == "candidate_manifest_step"]
        self.assertEqual({row["operation"] for row in records}, {"prepare", "commit"})
        for operation in ("prepare", "commit"):
            starts = [row["phase"] for row in records
                      if row["operation"] == operation and row["state"] == "start"]
            completed = [row["phase"] for row in records
                         if row["operation"] == operation and row["state"] == "completed"]
            self.assertEqual(starts, completed)
            self.assertIn("manifest_historical_prefixes", starts)
        self.assertTrue(all(type(row["elapsedMs"]) is int and row["elapsedMs"] >= 0 for row in records))
        self.assertNotIn("substage", lifecycle)

    def test_manifest_interruption_keeps_exact_substage_without_promotion(self):
        lifecycle = {"runId": "manifest-interrupted"}
        with patch.object(atomic, "historical_prefixes", side_effect=run.InterruptedRun(15)):
            with self.assertRaises(run.InterruptedRun):
                self.execute(lifecycle=lifecycle)
        self.assertEqual(lifecycle["stage"], "candidate_manifest_prepare")
        self.assertEqual(lifecycle["substage"], "manifest_historical_prefixes")
        self.assertEqual(self.active.stat().st_ino, self.inode)
        self.assertEqual(run.read_calendar(self.active), DAYS[:3])
        records = list(map(json.loads, self.events))
        self.assertFalse(any(row["event"] == "candidate_manifest_prepared" for row in records))
        self.assertFalse(any(row["event"] == "candidate_manifest_step" and
                             row["phase"] == "manifest_historical_prefixes" and
                             row["state"] == "completed" for row in records))

    def test_required_index_missing_on_exact_day_fails_before_download(self):
        class Missing(Client):
            def query(self, sql):
                self.requests += 1
                if "symbol IN" in sql and "tradedate = " in sql:
                    day = sql.split("tradedate = '")[1].split("'")[0]
                    symbols = ("SH600000", "SZ000001", "SH600519", "SZ300750",
                               "SH000905", "SH000852", "SH000906", "SZ399300")
                    return [{"symbol": symbol, "tradedate": day} for symbol in symbols]
                if "MAX(tradedate)" in sql:
                    return [{"latest": self.latest}]
                raise AssertionError(sql)

            def paginated_rows(self, table, columns, where, fields):
                if table == "final_a_stock_eod_price":
                    raise AssertionError("download started")
                return super().paginated_rows(table, columns, where, fields)

        with self.assertRaisesRegex(ValueError, "required_reference_or_index_not_at_target:SH000300"):
            self.execute(client=Missing())
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_later_row_does_not_hide_a_missing_target_day(self):
        class LaterOnly(Client):
            def query(self, sql):
                self.requests += 1
                if "symbol IN" in sql and "tradedate = " in sql:
                    return []
                if "MAX(tradedate)" in sql:
                    return [{"latest": "2026-09-09"}]
                raise AssertionError(sql)

            def paginated_rows(self, table, columns, where, fields):
                if table == "final_a_stock_eod_price":
                    raise AssertionError("download started")
                return super().paginated_rows(table, columns, where, fields)

        with self.assertRaisesRegex(ValueError, "required_reference_or_index_not_at_target:"):
            self.execute(client=LaterOnly())

    def test_stale_source_never_promotes_or_fabricates_dates(self):
        with self.assertRaisesRegex(ValueError, "upstream_market_stale"):
            self.execute(client=Client(latest="2026-09-04"))
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_stale_source_records_fixed_commit_cutoff_and_failure_stage(self):
        lifecycle = {"runId": "offline-test", "sourceCommit": None, "cutoff": None,
                     "stage": "initializing"}
        with self.assertRaisesRegex(ValueError, "upstream_market_stale"):
            self.execute(client=Client(latest="2026-09-04"), lifecycle=lifecycle)
        self.assertEqual(lifecycle["sourceCommit"], REF)
        self.assertEqual(lifecycle["cutoff"], "2026-09-08")
        self.assertEqual(lifecycle["stage"], "market_freshness_check")
        records = [json.loads(value) for value in self.events]
        self.assertTrue(all(value["runId"] == "offline-test" for value in records))
        self.assertEqual(records[-1]["event"], "source_pinned")
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_calendar_source_failure_records_calendar_fetch_stage(self):
        class CalendarFailure(Client):
            def paginated_rows(self, *args):
                raise SourceError("query_not_success")

        lifecycle = {"runId": "offline-test", "sourceCommit": None, "cutoff": None,
                     "stage": "initializing"}
        with self.assertRaisesRegex(SourceError, "query_not_success"):
            self.execute(client=CalendarFailure(), lifecycle=lifecycle)
        self.assertEqual(lifecycle["stage"], "calendar_fetch")
        self.assertEqual(lifecycle["sourceCommit"], REF)
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_source_pin_failure_is_not_labelled_as_prior_success_event(self):
        class PinFailure(Client):
            def latest_ref(self):
                raise SourceError("source_pin_unavailable")

        lifecycle = {"runId": "offline-test", "sourceCommit": None, "cutoff": None,
                     "stage": "initializing"}
        with self.assertRaisesRegex(SourceError, "source_pin_unavailable"):
            self.execute(client=PinFailure(), lifecycle=lifecycle)
        self.assertEqual(json.loads(self.events[-1])["event"], "local_index_layout_validated")
        self.assertEqual(lifecycle["stage"], "source_pin")
        self.assertIsNone(lifecycle["sourceCommit"])
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_future_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "source_contains_future"):
            self.execute(client=Client(latest="2026-09-10"))

    def test_today_not_allowed_before_eod_cutoff(self):
        with self.assertRaisesRegex(ValueError, "target_not_completed_eod"):
            self.execute(through="2026-09-09")

    def test_pre_source_failure_still_records_computed_cutoff(self):
        lifecycle = {"runId": "offline-test", "sourceCommit": None, "cutoff": None,
                     "stage": "initializing"}
        with self.assertRaisesRegex(ValueError, "target_not_completed_eod"):
            self.execute(through="2026-09-09", lifecycle=lifecycle)
        self.assertEqual(lifecycle["cutoff"], "2026-09-08")
        self.assertIsNone(lifecycle["sourceCommit"])
        self.assertEqual(lifecycle["stage"], "run_setup")
        self.assertEqual(self.events, [])

    def test_calendar_response_cannot_override_completed_cutoff(self):
        with self.assertRaisesRegex(ValueError, "duplicate_exchange_calendar"):
            self.execute(client=Client(days=DAYS + ["2026-09-09"], latest="2026-09-09"))

    def test_calendar_revision_rejected(self):
        with self.assertRaisesRegex(ValueError, "source_calendar_revises"):
            self.execute(client=Client(days=[DAYS[0], DAYS[2], DAYS[3], DAYS[4]]))

    def test_current_calendar_still_requires_actual_price_validation(self):
        (self.active / "calendars/day.txt").write_text("\n".join(DAYS) + "\n")
        (self.active / atomic.MANIFEST).write_text(json.dumps({"sourceCommit": REF}))
        def fail(*a, **kw):
            raise ValueError("required_reference_not_current")
        self.tree.validate_tree = fail
        with self.assertRaisesRegex(ValueError, "required_reference_not_current"):
            self.execute()

    def test_current_state_is_readonly_and_validated(self):
        (self.active / "calendars/day.txt").write_text("\n".join(DAYS) + "\n")
        (self.active / atomic.MANIFEST).write_text(json.dumps({"sourceCommit": REF}))
        result = self.execute()
        self.assertEqual(result["status"], "already_current")
        self.assertIn("priceValidation", result)
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_missing_incremental_manifest_cannot_downgrade_to_legacy(self):
        (self.active / "calendars/day.txt").write_text("\n".join(DAYS) + "\n")
        with self.assertRaisesRegex(ValueError, "incremental_manifest_missing_cannot_downgrade"):
            self.execute()

    def test_current_but_malformed_manifest_must_pass_consumer(self):
        (self.active / "calendars/day.txt").write_text("\n".join(DAYS) + "\n")
        (self.active / atomic.MANIFEST).write_text("{}")
        def reject(root, manifest):
            self.assertEqual(root, self.active)
            raise ValueError("installed_manifest_rejected")
        with self.assertRaisesRegex(ValueError, "installed_manifest_rejected"):
            self.execute(candidate_check=reject)
        self.assertEqual((self.active / atomic.MANIFEST).read_text(), "{}")

    def test_price_failure_preserves_generation(self):
        def fail(*a, **kw):
            raise ValueError("overlap_mismatch")
        self.tree.apply_prices = fail
        lifecycle = {"runId": "offline-test", "sourceCommit": None, "cutoff": None,
                     "stage": "initializing"}
        with self.assertRaisesRegex(ValueError, "overlap_mismatch"):
            self.execute(lifecycle=lifecycle)
        self.assertEqual(json.loads(self.events[-1])["event"], "source_downloads_complete")
        self.assertEqual(lifecycle["stage"], "price_conversion")
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_partial_update_is_not_reported_as_complete(self):
        self.tree.apply_prices = lambda *a, **kw: {"status": "partial", "eligible": 1, "valid": 1, "invalid": 0}
        result = self.execute()
        self.assertEqual(result["status"], "updated_with_source_absence")
        self.assertEqual(result["dataQuality"], "partial")
        manifest = json.loads((self.active / atomic.MANIFEST).read_text())
        self.assertEqual(manifest["dataQuality"], "partial")
        self.assertEqual(manifest["sourceAbsence"]["sourceCommit"], REF)

    def test_current_source_observation_does_not_relabel_installed_source(self):
        (self.active / "calendars/day.txt").write_text("\n".join(DAYS) + "\n")
        (self.active / atomic.MANIFEST).write_text(json.dumps({"sourceCommit": "b" * 32, "dataQuality": "partial"}))
        result = self.execute()
        self.assertEqual(result["installedSourceCommit"], "b" * 32)
        self.assertEqual(result["observedSourceCommit"], REF)
        self.assertEqual(result["dataQuality"], "partial")

    def test_constituent_failure_preserves_generation(self):
        def fail(*a):
            raise ValueError("membership_mismatch")
        self.indices.update_indices = fail
        lifecycle = {"runId": "offline-test", "sourceCommit": None, "cutoff": None,
                     "stage": "initializing"}
        with self.assertRaisesRegex(ValueError, "membership_mismatch"):
            self.execute(lifecycle=lifecycle)
        self.assertEqual(json.loads(self.events[-1])["event"], "candidate_copied")
        self.assertEqual(lifecycle["stage"], "index_update")
        self.assertEqual(self.active.stat().st_ino, self.inode)

    def test_numeric_and_market_identity_rejected(self):
        row = Client().paginated_rows("final_a_stock_eod_price", (), "", ())[0]
        row["symbol"] = "../../bad"
        with self.assertRaises(ValueError):
            run.read_raw([row], REF)
        row["symbol"] = "SH600000"
        row["close"] = "nan"
        with self.assertRaisesRegex(ValueError, "nonfinite_market_number"):
            run.read_raw([row], REF)

    def test_cutoff_uses_shanghai_time(self):
        self.assertEqual(run.completed_cutoff(datetime(2026, 9, 9, 17, 59, tzinfo=run.SHANGHAI)), "2026-09-08")
        self.assertEqual(run.completed_cutoff(datetime(2026, 9, 9, 18, tzinfo=run.SHANGHAI)), "2026-09-09")


class MainLoggingTests(unittest.TestCase):
    def invoke(self, source_factory, run_impl=None):
        output = StringIO()
        if run_impl is None:
            run_impl = lambda *args, **kwargs: self.fail("run_update must not run")
        with (patch.object(run, "source_client", source_factory),
              patch.object(run, "run_update", run_impl),
              patch.object(run.signal, "signal"),
              patch.object(candidate, "consumer_check", return_value=lambda *_: None),
              patch("sys.argv", ["run.py"]), redirect_stdout(output)):
            rc = run.main()
        return rc, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_client_initialization_failure_has_complete_terminal_record(self):
        def fail_init():
            raise SourceError("unsupported_proxy")

        rc, records = self.invoke(fail_init)
        self.assertEqual(rc, 1)
        self.assertEqual(len(records), 1)
        result = records[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["event"], "failed")
        self.assertEqual(result["error"], "unsupported_proxy")
        self.assertEqual(result["failureStage"], "source_client_init")
        self.assertIsNone(result["sourceCommit"])
        self.assertIsNone(result["cutoff"])
        self.assertEqual(result["sourceRequests"], 0)
        self.assertRegex(result["runId"], r"^[0-9a-f-]{36}$")
        self.assertEqual(datetime.fromisoformat(result["time"]).utcoffset().total_seconds(), 8 * 3600)

    def test_pinned_provider_failure_has_identity_but_no_raw_response(self):
        client = types.SimpleNamespace(
            requests=15,
            last_source_event={"request_i": 15, "http": 200, "rows": 0, "status": "failed",
                               "sql_query": "SELECT secret FROM provider", "token": "secret-token"},
            last_transport_events=[{"transport": "direct", "attempt": 1, "curl_rc": 0,
                                    "http": 200, "bytes": 95, "url": "secret-token"}],
            transport=types.SimpleNamespace(events=[
                {"transport": "direct", "attempt": 1, "curl_rc": 0, "http": 200,
                 "bytes": 95, "url": "https://provider.invalid/?token=secret-token"}]))

        def fail_run(*args, **kwargs):
            kwargs["lifecycle"].update({"sourceCommit": REF, "cutoff": "2026-09-30",
                                        "stage": "calendar_fetch"})
            raise SourceError("query_not_success")

        rc, records = self.invoke(lambda: client, fail_run)
        self.assertEqual(rc, 1)
        self.assertEqual(len(records), 1)
        result = records[0]
        self.assertEqual(result["event"], "failed")
        self.assertEqual(result["sourceCommit"], REF)
        self.assertEqual(result["cutoff"], "2026-09-30")
        self.assertEqual(result["failureStage"], "calendar_fetch")
        self.assertEqual(result["sourceRequests"], 15)
        self.assertEqual(result["lastSourceEvent"],
                         [{"request_i": 15, "http": 200, "rows": 0, "status": "failed"}])
        self.assertEqual(result["lastTransportEvents"],
                         [{"transport": "direct", "attempt": 1, "curl_rc": 0,
                           "http": 200, "bytes": 95}])
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertNotIn("SELECT", json.dumps(result))

    def test_failure_without_current_transport_does_not_log_prior_success(self):
        client = types.SimpleNamespace(requests=15, last_source_event=None, last_transport_events=[],
            events=[{"request_i": 15, "http": 200, "rows": 500, "status": "Success"}],
            transport=types.SimpleNamespace(events=[{"transport": "direct", "attempt": 1, "http": 200}]))
        def fail_run(*args, **kwargs):
            raise SourceError("request_budget_exhausted")
        rc, records = self.invoke(lambda: client, fail_run)
        self.assertEqual(rc, 1)
        self.assertEqual(records[0]["lastSourceEvent"], [])
        self.assertEqual(records[0]["lastTransportEvents"], [])

    def test_interruption_and_lock_preserve_existing_exit_codes(self):
        client = types.SimpleNamespace(requests=0)
        cases = [(lambda: run.InterruptedRun(15), 143, "interrupted"),
                 (BlockingIOError, 75, "locked")]
        for factory, expected_rc, status in cases:
            with self.subTest(status=status):
                def fail_run(*args, **kwargs):
                    raise factory()

                rc, records = self.invoke(lambda: client, fail_run)
                self.assertEqual(rc, expected_rc)
                self.assertEqual(records[0]["event"], status)
                self.assertEqual(records[0]["status"], status)
                self.assertEqual(records[0]["failureStage"], "candidate_check_init")

    def test_terminal_interruption_records_parent_and_manifest_substage(self):
        def interrupted(*args, **kwargs):
            now = run.time.monotonic()
            kwargs["lifecycle"].update(stage="candidate_manifest_prepare",
                stageStartedMonotonic=now - 2, substage="manifest_historical_prefixes",
                substageStartedMonotonic=now - 1)
            raise run.InterruptedRun(15)
        rc, records = self.invoke(lambda: types.SimpleNamespace(requests=0), interrupted)
        self.assertEqual(rc, 143)
        self.assertEqual(records[0]["failureStage"], "candidate_manifest_prepare")
        self.assertEqual(records[0]["failureSubstage"], "manifest_historical_prefixes")
        self.assertGreaterEqual(records[0]["stageElapsedMs"], 2000)
        self.assertGreaterEqual(records[0]["substageElapsedMs"], 1000)

    def test_interrupted_stage_timing_excludes_cleanup_after_signal(self):
        clock = [100.0]
        def interrupted(*args, **kwargs):
            kwargs["lifecycle"].update(stage="candidate_manifest_prepare",
                stageStartedMonotonic=90.0, substage="manifest_historical_prefixes",
                substageStartedMonotonic=95.0)
            signal = run.InterruptedRun(15)
            clock[0] = 107.0  # Synthetic context-unwind/cleanup, not work time.
            raise signal
        with patch.object(run.time, "monotonic", side_effect=lambda: clock[0]):
            rc, records = self.invoke(lambda: types.SimpleNamespace(requests=0), interrupted)
        self.assertEqual(rc, 143)
        self.assertEqual(records[0]["stageElapsedMs"], 10000)
        self.assertEqual(records[0]["substageElapsedMs"], 5000)
        self.assertEqual(records[0]["postInterruptionElapsedMs"], 7000)
        self.assertIsNotNone(datetime.fromisoformat(records[0]["interruptedAt"]).tzinfo)


if __name__ == "__main__":
    unittest.main()
