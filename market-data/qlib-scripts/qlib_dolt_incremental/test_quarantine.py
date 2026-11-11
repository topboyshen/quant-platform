"""Offline fault matrix for the narrowly bounded closed-index quarantine.

No transport, production files, business tasks or real consumer database are
used. Transaction tests reuse only RunTests.setUp, not its inherited test cases.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import unittest
from unittest.mock import patch

import atomic
import candidate
import run
import test_candidate as consumer_fixtures
import test_run as fixtures


REF = "a" * 32
CLOSED = "2026-09-12"
INDICES = ("SH000300", "SH000852", "SH000905", "SH000906")
OPEN_DAYS = ["2026-09-08", "2026-09-09", "2026-09-10",
             "2026-09-11", "2026-09-14", "2026-09-15"]


def row(day=CLOSED, symbol=INDICES[0], **changes):
    value = dict(tradedate=day, symbol=symbol, open="10", high="11",
                 low="9", close="10", adjclose="20", volume="100", amount="1000")
    value.update(changes)
    return value


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class ClosedProofClient:
    """The only query allowed by the standalone gate is the closed-day proof."""
    def __init__(self, proof=None):
        self.ref = REF
        self.proof = ([{"exchange": "SSE", "date": CLOSED, "is_open": "0"}]
                      if proof is None else proof)
        self.queries = []
        self.requests = 0
        self.failure = None
        self.drift_after_query = False

    def latest_ref(self):
        return self.ref

    def table(self, name):
        if name != "ts_trade_day_calendar":
            raise AssertionError("quarantine queried an unexpected table")
        return f"`{name}` AS OF '{self.ref}'"

    def query(self, sql):
        self.requests += 1
        self.queries.append(sql)
        if self.failure:
            raise self.failure
        if self.drift_after_query:
            self.ref = "b" * 32
        return deepcopy(self.proof)


class QuarantineUnitTests(unittest.TestCase):
    def setUp(self):
        self.client = ClosedProofClient()
        self.events = []
        self.gate = run.ClosedIndexQuarantine(self.client, REF, OPEN_DAYS,
                                               emit=self.events.append)

    def filter(self, values, **options):
        return self.gate.filter_rows(values, start=options.pop("start", OPEN_DAYS[0]),
                                     end=options.pop("end", OPEN_DAYS[-1]), **options)

    def assert_pinned_closed_query(self):
        self.assertEqual(len(self.client.queries), 1)
        sql = " ".join(self.client.queries[0].split())
        self.assertRegex(sql, r"SELECT exchange,\s*date,\s*is_open FROM")
        self.assertIn(f"`ts_trade_day_calendar` AS OF '{REF}'", sql)
        self.assertRegex(sql, r"exchange\s*=\s*'SSE'")
        self.assertRegex(sql, r"date\s*=\s*'2026-09-12'")
        self.assertRegex(sql, r"LIMIT\s+2\s*$")

    def test_open_day_rows_are_preserved_without_extra_query_or_evidence(self):
        values = [row("2026-09-11", symbol) for symbol in INDICES]
        values += [row("2026-09-14", "SH600000")]
        kept = self.filter(values)
        self.assertEqual(kept, values)
        for original, returned in zip(values, kept):
            self.assertIs(original, returned)
        self.assertIsNone(self.gate.evidence())
        self.assertEqual(self.client.queries, [])
        self.assertEqual(self.events, [])

    def test_exact_four_closed_rows_removed_but_open_day_indices_kept(self):
        closed = [row(symbol=symbol) for symbol in INDICES]
        valid = [row("2026-09-14", symbol) for symbol in INDICES]
        original = deepcopy(closed + valid)
        self.assertEqual(self.filter(closed + valid), valid)
        self.assertEqual(closed + valid, original)
        self.assert_pinned_closed_query()
        proof = self.gate.evidence()
        self.assertEqual(proof["policyId"], "qlib-closed-index-20260912-v1")
        self.assertEqual(proof["sourceCommit"], REF)
        self.assertEqual(proof["count"], 4)
        self.assertEqual(proof["calendarEvidence"],
            {"exchange": "SSE", "date": CLOSED, "is_open": "0", "sourceCommit": REF})
        self.assertEqual(proof["records"], [
            {"date": CLOSED, "symbol": value["symbol"], "rowSha256": canonical_digest(value)}
            for value in closed])
        self.assertRegex(proof["rowsSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(proof["rowsSha256"], canonical_digest(closed))
        self.assertNotIn("adjclose", json.dumps(proof))
        self.assertNotIn("volume", json.dumps(proof))

    def test_unknown_off_calendar_pairs_fail_before_any_proof_query(self):
        cases = [row(symbol="SH600000"), row(symbol="SZ000300"),
                 row(symbol="SH000985"), row("2026-09-13"),
                 row("2026-09-05")]
        for value in cases:
            with self.subTest(pair=(value["tradedate"], value["symbol"])):
                with self.assertRaises(ValueError):
                    self.filter([row(), value])
                self.assertEqual(self.client.queries, [])
                self.assertIsNone(self.gate.evidence())

    def test_known_closed_date_falsely_marked_open_is_not_admitted(self):
        gate = run.ClosedIndexQuarantine(self.client, REF, sorted(OPEN_DAYS + [CLOSED]))
        with self.assertRaisesRegex(ValueError, "quarantine_calendar_conflicts_with_known_closed_date"):
            gate.filter_rows([row()], start=OPEN_DAYS[0], end=OPEN_DAYS[-1])
        self.assertEqual(self.client.queries, [])
        self.assertIsNone(gate.evidence())

    def test_requested_range_and_symbol_rejected_before_proof_query(self):
        for options in ({"start": "2026-09-14"}, {"end": "2026-09-11"},
                        {"symbol": "SH000905"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.filter([row()], **options)
        self.assertEqual(self.client.queries, [])
        self.assertIsNone(self.gate.evidence())

    def test_duplicate_pair_in_one_response_is_not_silently_deduplicated(self):
        for duplicate in (row(), row(close="10.25")):
            with self.subTest(duplicate=duplicate["close"]), self.assertRaises(ValueError):
                self.filter([row(), duplicate])
        self.assertEqual(self.client.queries, [])
        self.assertIsNone(self.gate.evidence())

    def test_incomplete_extra_or_bad_numeric_closed_row_cannot_hide_in_quarantine(self):
        missing = row()
        del missing["amount"]
        cases = [missing, row(extra="unapproved"), row(close="nan"),
                 row(adjclose="inf"), row(volume=None), row(high="not-a-number"),
                 row(tradedate="2026-9-12"), row(symbol="../../SH000300")]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.filter([value])
        self.assertEqual(self.client.queries, [])
        self.assertIsNone(self.gate.evidence())

    def test_open_rows_remain_subject_to_the_unchanged_read_raw_validator(self):
        for value in (row("2026-09-11", close="nan"), row("2026-09-11", extra="x")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run.read_raw(self.filter([value]), REF)
        self.assertEqual(self.client.queries, [])

    def test_nullable_amount_remains_legal_as_in_read_raw(self):
        self.assertEqual(self.filter([row(amount=None)]), [])
        self.assertEqual(self.gate.evidence()["count"], 1)

    def test_missing_duplicate_malformed_open_or_wrong_calendar_proof_rejected(self):
        valid = {"exchange": "SSE", "date": CLOSED, "is_open": "0"}
        cases = [[], [valid, valid], [dict(valid, is_open="1")],
                 [dict(valid, is_open=0)], [dict(valid, is_open=False)],
                 [dict(valid, exchange="SZSE")], [dict(valid, date="2026-09-13")],
                 [dict(valid, extra="x")], [{"exchange": "SSE", "date": CLOSED}]]
        for proof in cases:
            client = ClosedProofClient(proof)
            gate = run.ClosedIndexQuarantine(client, REF, OPEN_DAYS)
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                gate.filter_rows([row()], start=OPEN_DAYS[0], end=OPEN_DAYS[-1])
            self.assertIsNone(gate.evidence())
            self.assertEqual(len(client.queries), 1)

    def test_calendar_transport_error_is_not_treated_as_closed_day_evidence(self):
        self.client.failure = run.SourceError("query_not_success")
        with self.assertRaises(run.SourceError):
            self.filter([row()])
        self.assertIsNone(self.gate.evidence())
        self.assertEqual(self.events, [])

    def test_source_ref_drift_before_query_is_rejected(self):
        self.client.ref = "b" * 32
        with self.assertRaises(ValueError):
            self.filter([row()])
        self.assertEqual(self.client.queries, [])
        self.assertIsNone(self.gate.evidence())

    def test_source_ref_drift_while_proving_closed_day_is_rejected(self):
        self.client.drift_after_query = True
        with self.assertRaises(ValueError):
            self.filter([row()])
        self.assertIsNone(self.gate.evidence())

    def test_cached_proof_never_authorizes_a_later_different_source_ref(self):
        self.filter([row()])
        before = deepcopy(self.gate.evidence())
        self.client.ref = "b" * 32
        with self.assertRaises(ValueError):
            self.filter([row(symbol=INDICES[1])])
        self.assertEqual(self.gate.evidence(), before)
        self.assertEqual(len(self.client.queries), 1)

    def test_same_pair_reobserved_in_supplemental_query_is_counted_once(self):
        value = row()
        self.filter([value])
        initial = deepcopy(self.gate.evidence())
        initial_events = len(self.events)
        self.filter([dict(reversed(list(value.items())))], symbol=INDICES[0])
        self.assertEqual(self.gate.evidence(), initial)
        self.assertEqual(len(self.events), initial_events)
        self.assertEqual(len(self.client.queries), 1)

    def test_same_pair_different_values_cannot_replace_prior_evidence(self):
        self.filter([row()])
        before = deepcopy(self.gate.evidence())
        with self.assertRaises(ValueError):
            self.filter([row(close="10.01")])
        self.assertEqual(self.gate.evidence(), before)

    def test_order_independent_evidence_and_hashes_for_same_raw_rows(self):
        values = [row(symbol=symbol) for symbol in INDICES]
        self.filter(values)
        second = run.ClosedIndexQuarantine(ClosedProofClient(), REF, OPEN_DAYS)
        second.filter_rows([dict(reversed(list(value.items()))) for value in reversed(values)],
                           start=OPEN_DAYS[0], end=OPEN_DAYS[-1])
        self.assertEqual(self.gate.evidence(), second.evidence())

    def test_evidence_emission_grows_only_for_new_pairs_without_raw_values(self):
        self.filter([row()])
        first = deepcopy(self.gate.evidence())
        first_events = len(self.events)
        self.filter([row(symbol=INDICES[1])])
        self.assertGreater(first_events, 0)
        self.assertGreater(len(self.events), first_events)
        self.assertEqual(first["count"], 1)
        self.assertEqual(self.gate.evidence()["count"], 2)
        self.assertNotEqual(first["rowsSha256"], self.gate.evidence()["rowsSha256"])
        self.assertNotIn("adjclose", json.dumps(self.events))
        self.assertNotIn("volume", json.dumps(self.events))
        self.assertEqual(len(self.client.queries), 1)

    def test_later_mixed_invalid_batch_cannot_partially_add_new_evidence(self):
        self.filter([row()])
        before = deepcopy(self.gate.evidence())
        event_count = len(self.events)
        with self.assertRaises(ValueError):
            self.filter([row(symbol=INDICES[1]), row(symbol="SH600000")])
        self.assertEqual(self.gate.evidence(), before)
        self.assertEqual(len(self.events), event_count)


class QuarantineSupplementalTests(unittest.TestCase):
    def setUp(self):
        self.client = ClosedProofClient()
        self.gate = run.ClosedIndexQuarantine(self.client, REF, OPEN_DAYS)
        self.gate.filter_rows([row(symbol=symbol) for symbol in INDICES],
                              start=OPEN_DAYS[0], end=OPEN_DAYS[-1])

    def test_historical_gap_supplement_uses_same_gate_and_does_not_add_closed_day(self):
        symbol = INDICES[0]
        info = {symbol: {"last_date": OPEN_DAYS[2], "gap_dates": OPEN_DAYS[3:]}}
        returned = [row(day, symbol) for day in OPEN_DAYS[3:]] + [row(symbol=symbol)]
        raw_map = {}
        with patch.object(run, "fetch_prices", return_value=returned):
            missing, evidence = run.fetch_historical_gaps(
                self.client, info, OPEN_DAYS, raw_map, quarantine=self.gate)
        self.assertEqual(missing, {})
        self.assertEqual(evidence[symbol]["returnedDates"], OPEN_DAYS[3:])
        self.assertNotIn((symbol, CLOSED), raw_map)
        self.assertEqual(self.gate.evidence()["count"], 4)
        self.assertEqual(len(self.client.queries), 1)

    def test_historical_gap_wrong_symbol_still_fails_after_known_quarantine(self):
        symbol = INDICES[0]
        info = {symbol: {"last_date": OPEN_DAYS[2], "gap_dates": OPEN_DAYS[3:]}}
        with patch.object(run, "fetch_prices", return_value=[row(symbol=INDICES[1])]):
            with self.assertRaises(ValueError):
                run.fetch_historical_gaps(self.client, info, OPEN_DAYS, {}, quarantine=self.gate)
        self.assertEqual(len(self.client.queries), 1)

    def test_anchor_supplement_uses_same_gate_and_keeps_valid_date_alignment(self):
        symbol = INDICES[0]
        expected = [OPEN_DAYS[0], OPEN_DAYS[3], OPEN_DAYS[-1]]
        info = {symbol: {"new": False, "short_history": False,
                         "last3_valid_dates": expected, "last_date": OPEN_DAYS[-1]}}
        returned = [row(day, symbol, volume="100" if day in expected else "0")
                    for day in OPEN_DAYS] + [row(symbol=symbol)]
        raw_map = {(value["symbol"], value["tradedate"]): value for value in returned
                   if value["tradedate"] in OPEN_DAYS[-3:]}
        with patch.object(run, "fetch_prices", return_value=returned):
            run.verify_anchor_intervals(self.client, info, OPEN_DAYS, raw_map, quarantine=self.gate)
        self.assertNotIn((symbol, CLOSED), raw_map)
        self.assertEqual(self.gate.evidence()["count"], 4)
        self.assertEqual(len(self.client.queries), 1)

    def test_anchor_supplement_unknown_off_calendar_date_still_fails(self):
        symbol = INDICES[0]
        info = {symbol: {"new": False, "short_history": False,
                         "last3_valid_dates": [OPEN_DAYS[0], OPEN_DAYS[3], OPEN_DAYS[-1]],
                         "last_date": OPEN_DAYS[-1]}}
        with patch.object(run, "fetch_prices", return_value=[row("2026-09-13", symbol)]):
            with self.assertRaises(ValueError):
                run.verify_anchor_intervals(self.client, info, OPEN_DAYS, {}, quarantine=self.gate)
        self.assertEqual(len(self.client.queries), 1)


class TransactionClient(ClosedProofClient):
    def __init__(self, *, include_closed=True, extra_rows=()):
        super().__init__()
        self.include_closed = include_closed
        self.extra_rows = list(extra_rows)
        self.supplemental_rows = None
        self.price_predicates = []

    def table(self, name):
        return f"`{name}` AS OF '{self.ref}'"

    def query(self, sql):
        if "MAX(tradedate)" in sql:
            self.requests += 1
            return [{"latest": OPEN_DAYS[-1]}]
        if "MIN(tradedate)" in sql:
            self.requests += 1
            return [{"first_date": OPEN_DAYS[0]}]
        return super().query(sql)

    def paginated_rows(self, table, columns, where, fields):
        self.requests += 1
        if table == "ts_trade_day_calendar":
            days = ["2026-09-16", "2026-09-17"] if "date > '" in where else OPEN_DAYS
            return [{"exchange": "SSE", "date": day, "is_open": "1"} for day in days]
        if table == "final_a_stock_eod_price":
            self.price_predicates.append(where)
            if "symbol = '" in where and self.supplemental_rows is not None:
                return deepcopy(self.supplemental_rows)
            values = [row(day, "SH600000") for day in OPEN_DAYS]
            values += [row("2026-09-14", INDICES[0])]
            if self.include_closed:
                values += [row(symbol=symbol) for symbol in INDICES]
            return values + deepcopy(self.extra_rows)
        raise AssertionError(table)


class QuarantineTransactionTests(unittest.TestCase):
    def setUp(self):
        fixtures.RunTests.setUp(self)
        (self.active / "calendars/day.txt").write_text("\n".join(OPEN_DAYS[:3]) + "\n")
        (self.active / "calendars/day_future.txt").write_text("\n".join(OPEN_DAYS[3:]) + "\n")
        (self.active / "features/historical-sentinel").write_bytes(b"untouched-history\x00\x01")
        self.tree.inspect_symbols = lambda *args: {
            "SH600000": {"new": False, "last3_valid_dates": OPEN_DAYS[:3],
                         "last_date": OPEN_DAYS[2], "first_date": OPEN_DAYS[0]}}
        self.captured_raw = []

        def apply(*args, **kwargs):
            self.captured_raw[:] = deepcopy(kwargs["raw_rows"])
            return {"coverage": {"eligible": 1, "valid": 1, "ratio": 1.0}}

        self.tree.apply_prices = apply
        self.before = {path.relative_to(self.active).as_posix(): path.read_bytes()
                       for path in self.active.rglob("*") if path.is_file()}
        self.release_before = (self.data / ".tag").read_bytes()

    def execute(self, **options):
        return run.run_update(self.data, client=options.pop("client", TransactionClient()),
            now=datetime(2026, 9, 16, 8, tzinfo=run.SHANGHAI), emit=self.events.append,
            min_eligible=options.pop("min_eligible", 1), **options)

    def assert_active_unchanged(self):
        self.assertEqual(self.active.stat().st_ino, self.inode)
        self.assertEqual({path.relative_to(self.active).as_posix(): path.read_bytes()
                          for path in self.active.rglob("*") if path.is_file()}, self.before)
        self.assertEqual((self.data / ".tag").read_bytes(), self.release_before)
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_dry_run_records_quarantine_and_checks_consumer_without_exchange(self):
        captured = []

        def check(stage, manifest):
            self.assertEqual(json.loads((stage / atomic.MANIFEST).read_text()), manifest)
            captured.append(deepcopy(manifest))
            self.assertEqual(self.active.stat().st_ino, self.inode)
            return {"status": "passed"}

        client = TransactionClient()
        result = self.execute(client=client, dry_run=True, candidate_check=check)
        self.assertEqual(result["status"], "dry_run_validated")
        self.assertEqual(result["dataQuality"], "complete")
        self.assertEqual(result["sourceIntegrity"], "partial")
        self.assertEqual(result["sourceRowQuarantine"]["count"], 4)
        self.assertEqual(captured[0]["sourceRowQuarantine"], result["sourceRowQuarantine"])
        self.assertEqual(captured[0]["dataQuality"], "complete")
        self.assertEqual(captured[0]["sourceIntegrity"], "partial")
        self.assertEqual(captured[0]["sourceRows"], len(self.captured_raw))
        self.assertFalse(any(value["date"] == CLOSED for value in self.captured_raw))
        self.assertTrue(any(value["date"] == "2026-09-14" and value["symbol"] == INDICES[0]
                            for value in self.captured_raw))
        self.assertEqual(len(client.queries), 1)
        self.assert_active_unchanged()

    def test_promotion_contains_evidence_and_preserves_history_and_release_tag(self):
        result = self.execute()
        self.assertNotEqual(self.active.stat().st_ino, self.inode)
        self.assertEqual(result["dataQuality"], "complete")
        self.assertEqual(result["sourceIntegrity"], "partial")
        manifest = json.loads((self.active / atomic.MANIFEST).read_text())
        self.assertEqual(manifest["sourceIntegrity"], "partial")
        self.assertEqual(manifest["sourceRowQuarantine"], result["sourceRowQuarantine"])
        self.assertEqual(manifest["sourceRowQuarantine"]["count"], 4)
        self.assertEqual(run.read_calendar(self.active), OPEN_DAYS)
        self.assertNotIn(CLOSED, run.read_calendar(self.active))
        for name, value in self.before.items():
            if name.startswith("features/"):
                self.assertEqual((self.active / name).read_bytes(), value)
        self.assertEqual((self.data / ".tag").read_bytes(), self.release_before)
        self.assertTrue(manifest["historicalPrefixes"]["unchanged"])

    def test_no_anomaly_does_not_add_quarantine_or_change_complete_quality(self):
        result = self.execute(client=TransactionClient(include_closed=False))
        manifest = json.loads((self.active / atomic.MANIFEST).read_text())
        self.assertEqual(result["dataQuality"], "complete")
        self.assertNotIn("sourceRowQuarantine", result)
        self.assertNotIn("sourceRowQuarantine", manifest)
        self.assertNotIn("sourceIntegrity", result)
        self.assertNotIn("sourceIntegrity", manifest)

    def test_already_current_preserves_installed_quarantine_not_new_observed_source(self):
        first = self.execute()
        installed_inode = self.active.stat().st_ino
        installed_bytes = {path.relative_to(self.active).as_posix(): path.read_bytes()
                           for path in self.active.rglob("*") if path.is_file()}
        client = TransactionClient()
        client.ref = "b" * 32
        result = self.execute(client=client)
        self.assertEqual(result["status"], "already_current")
        self.assertEqual(result["installedSourceCommit"], REF)
        self.assertEqual(result["observedSourceCommit"], client.ref)
        self.assertEqual(result["sourceRowQuarantine"], first["sourceRowQuarantine"])
        self.assertEqual(result["sourceIntegrity"], "partial")
        self.assertEqual(result["dataQuality"], "complete")
        self.assertEqual(client.queries, [])
        self.assertEqual(self.active.stat().st_ino, installed_inode)
        self.assertEqual({path.relative_to(self.active).as_posix(): path.read_bytes()
                          for path in self.active.rglob("*") if path.is_file()}, installed_bytes)
        self.assertFalse(list(self.data.glob(".qlib-incremental.*")))

    def test_later_coverage_failure_keeps_active_and_audit_evidence(self):
        def fail(*args, **kwargs):
            raise ValueError("coverage_below_threshold")

        self.tree.apply_prices = fail
        with self.assertRaisesRegex(ValueError, "coverage_below_threshold"):
            self.execute()
        self.assert_active_unchanged()
        records = [json.loads(value) for value in self.events]
        quarantine_events = [value for value in records if value["event"] == "closed_index_rows_quarantined"]
        self.assertEqual(quarantine_events[-1]["count"], 4)
        self.assertEqual(quarantine_events[-1]["sourceCommit"], REF)

    def test_later_index_alignment_failure_cannot_exchange(self):
        with patch.object(run, "validate_index_market_alignment",
                          side_effect=ValueError("missing_required_reference")):
            with self.assertRaisesRegex(ValueError, "missing_required_reference"):
                self.execute()
        self.assert_active_unchanged()

    def test_consumer_rejection_of_new_manifest_field_cannot_exchange(self):
        def reject(stage, manifest):
            self.assertEqual(manifest["sourceRowQuarantine"]["count"], 4)
            raise ValueError("consumer_refuses_quarantine")

        with self.assertRaisesRegex(ValueError, "consumer_refuses_quarantine"):
            self.execute(candidate_check=reject)
        self.assert_active_unchanged()

    def test_unknown_nontrading_stock_still_aborts_without_proof_or_exchange(self):
        client = TransactionClient(extra_rows=[row(symbol="SH600000")])
        with self.assertRaises(ValueError):
            self.execute(client=client)
        self.assertEqual(client.queries, [])
        self.assert_active_unchanged()

    def test_quarantine_does_not_bypass_daily_population_threshold(self):
        with self.assertRaisesRegex(ValueError, "market_day_population_incomplete"):
            self.execute(min_eligible=2)
        self.assert_active_unchanged()

    def test_nonfinite_open_row_is_rejected_before_candidate_copy(self):
        client = TransactionClient(extra_rows=[row("2026-09-15", "SZ000001", close="nan")])
        with self.assertRaisesRegex(ValueError, "nonfinite_market_number"):
            self.execute(client=client)
        self.assertNotIn("candidate_copied", "\n".join(self.events))
        self.assert_active_unchanged()

    def test_raw_download_count_failure_never_reaches_quarantine_or_candidate(self):
        client = TransactionClient()
        with patch.object(run, "fetch_prices", side_effect=run.SourceError("pagination_count_mismatch")):
            with self.assertRaisesRegex(run.SourceError, "pagination_count_mismatch"):
                self.execute(client=client)
        self.assertEqual(client.queries, [])
        self.assertNotIn("closed_index_rows_quarantined", "\n".join(self.events))
        self.assertNotIn("candidate_copied", "\n".join(self.events))
        self.assert_active_unchanged()

    def test_short_history_supplement_cannot_reintroduce_closed_index(self):
        client = TransactionClient()
        symbol = INDICES[0]
        client.supplemental_rows = [row(day, symbol) for day in OPEN_DAYS] + [row(symbol=symbol)]
        self.tree.inspect_symbols = lambda *args: {
            symbol: {"new": False, "short_history": True, "first_date": OPEN_DAYS[0],
                     "last_date": OPEN_DAYS[2],
                     "old_rows": [{"date": day} for day in OPEN_DAYS[:3]]}}
        result = self.execute(client=client, dry_run=True)
        self.assertEqual(len(client.price_predicates), 2)
        self.assertTrue(any("symbol = 'SH000300'" in value for value in client.price_predicates))
        self.assertFalse(any(value["date"] == CLOSED for value in self.captured_raw))
        self.assertEqual(result["sourceRowQuarantine"]["count"], 4)
        self.assertEqual(len(client.queries), 1)
        self.assert_active_unchanged()

    def test_short_history_supplement_changed_quarantined_value_aborts(self):
        client = TransactionClient()
        symbol = INDICES[0]
        client.supplemental_rows = [row(day, symbol) for day in OPEN_DAYS]
        client.supplemental_rows += [row(symbol=symbol, close="10.01")]
        self.tree.inspect_symbols = lambda *args: {
            symbol: {"new": False, "short_history": True, "first_date": OPEN_DAYS[0],
                     "last_date": OPEN_DAYS[2],
                     "old_rows": [{"date": day} for day in OPEN_DAYS[:3]]}}
        with self.assertRaises(ValueError):
            self.execute(client=client)
        self.assert_active_unchanged()


class QuarantineActualConsumerTests(unittest.TestCase):
    # Reuse fixture construction, not the original suite's test methods.
    write_manifest = consumer_fixtures.CandidateConsumerTests.write_manifest
    fingerprint = consumer_fixtures.CandidateConsumerTests.fingerprint

    def setUp(self):
        consumer_fixtures.CandidateConsumerTests.setUp(self)
        self.days = ["2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14"]
        (self.root / "calendars/day.txt").write_text("\n".join(self.days) + "\n")
        added = self.days[2:]
        self.manifest.update({
            "baseDate": self.days[1], "targetDate": self.days[-1], "calendarAdded": added,
            "priceValidation": self.helpers.price_claim(added),
            "legacyUnquotedCSI": self.helpers.legacy_claim(added),
            "indexPriceValidation": self.helpers.index_claim(self.days[-1]),
            "dataQuality": "complete", "sourceIntegrity": "partial",
        })
        self.manifest["legacyUnquotedCSI"]["baseDate"] = self.days[1]
        self.manifest["indexPriceValidation"]["legacyUnquotedBaseDate"] = self.days[1]
        client = ClosedProofClient()
        client.ref = self.manifest["sourceCommit"]
        gate = run.ClosedIndexQuarantine(client, client.ref, self.days)
        gate.filter_rows([row(symbol=symbol) for symbol in INDICES],
                         start=self.days[0], end=self.days[-1])
        self.manifest["sourceRowQuarantine"] = gate.evidence()
        self.write_manifest()

    def test_real_consumer_accepts_additive_source_integrity_with_complete_prices(self):
        before = self.fingerprint()
        result = candidate.consumer_check(self.health_file)(self.root, self.manifest)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["referenceCompleteDate"], self.days[-1])
        self.assertFalse(result["networkOrDatabaseCalls"])
        self.assertEqual(self.fingerprint(), before)

    def test_quarantine_fields_are_bound_by_real_consumer_manifest_hash(self):
        manifest_bytes = (self.root / candidate.MANIFEST).read_bytes()
        _, complete, generation = self.real_health.qlib_evidence(self.root)
        self.assertEqual(complete, self.days[-1])
        self.assertEqual(generation[2], hashlib.sha256(manifest_bytes).hexdigest())
        for key, replacement in (("count", 3), ("rowsSha256", "d" * 64)):
            altered = deepcopy(self.manifest)
            altered["sourceRowQuarantine"][key] = replacement
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "candidate_manifest_argument_disagrees"):
                candidate.consumer_check(self.health_file)(self.root, altered)

    def test_price_quality_cannot_be_falsely_changed_just_because_source_was_quarantined(self):
        self.manifest["dataQuality"] = "partial"
        self.write_manifest()
        with self.assertRaises(self.real_health.EvidenceError):
            self.real_health.qlib_evidence(self.root)


if __name__ == "__main__":
    unittest.main()
