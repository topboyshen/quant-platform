"""Temporary Qlib fixtures only; no database, subprocess, or network permitted."""

import copy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

import data_health as health


def price_claim(added, absence=None, invalid=None, last_quotes=None, gaps=None):
    absence, invalid = absence or {}, invalid or {}
    days = []
    for day in added:
        reasons = invalid.get(day, {})
        count = len(reasons)
        days.append({"target_date": day, "status": "partial" if count else "complete",
                     "eligible": 5000, "valid": 5000 - count, "invalid": count,
                     "coverage": (5000 - count) / 5000, "invalid_symbols": sorted(reasons),
                     "invalid_reasons": copy.deepcopy(reasons), "first_day_listings_checked": 0,
                     "reference_dates": {symbol.upper(): day for symbol in health.REFERENCES}})
    return dict(copy.deepcopy(days[-1]), status="partial" if any(d["invalid"] for d in days) else "complete",
                daily_coverage=days, sourceAbsentDates=copy.deepcopy(absence),
                lastQuotedDate=copy.deepcopy(last_quotes or {}), historical_tail_gaps=copy.deepcopy(gaps or {}))


def index_claim(target, *, missing_quotes=(), missing_previous=(), legacy=(), absence=None):
    names = {"csi300": 300, "csi500": 500, "csi800": 800, "csi1000": 1000, "csiall": 5000}
    quotes, previous, old = set(missing_quotes), set(missing_previous), set(legacy)
    by_index = lambda values: {name: len(values) if name == "csiall" else 0 for name in names}
    return {"status": "partial" if quotes or previous or old else "complete", "targetDate": target,
            "uniqueMembers": 5000, "uniqueMembersWithPrices": 5000 - len(quotes | old),
            "usableDailyReturnMembers": 5000 - len(quotes | previous | old), "indexMemberCounts": names,
            "missingQuoteMembers": sorted(quotes), "missingPreviousCloseMembers": sorted(previous),
            "firstListingDayMembers": [], "sourceAbsenceSlotsVerified": sum(len((absence or {})[s]) for s in quotes | previous),
            "legacyUnquotedMembers": sorted(old),
            "legacyUnquotedMeaning": "unchanged_historical_feature_boundary_no_physical_target_quote",
            "legacyUnquotedBaseDate": "2026-09-04", "sourceAbsenceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq",
            "missingQuoteCountsByIndex": by_index(quotes), "missingPreviousCloseCountsByIndex": by_index(previous),
            "legacyUnquotedCountsByIndex": by_index(old), "absenceMeaning": "source_absent_not_confirmed_suspension"}


def legacy_claim(added, last_quotes=None):
    return {"sourceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq", "baseDate": "2026-09-04",
            "dates": {symbol: list(added) for symbol in (last_quotes or {})}, "lastQuotedDates": dict(last_quotes or {})}


class IncrementalQlibTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qlib-health-incremental-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cn_data"
        (self.root / "calendars").mkdir(parents=True)
        self.days = ["2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"]
        (self.root / "calendars/day.txt").write_text("\n".join(self.days) + "\n")
        for path in (self.root.parent / ".tag", self.root / ".qlib-release"):
            path.write_text("2026-09-06\n")
        for symbol in health.REFERENCES:
            (self.root / "features" / symbol).mkdir(parents=True)
            self.features(symbol, len(self.days))
        self.path = self.root / health.INCREMENTAL_MANIFEST
        self.document = {
            "schemaVersion": 1, "kind": "same_source_dolt_incremental", "status": "validated",
            "sourceRepository": "chenditc/investment_data",
            "sourceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq", "generationId": "a" * 32,
            "baseReleaseTag": "2026-09-06", "baseDate": "2026-09-04", "targetDate": "2026-09-08",
            "calendarAdded": ["2026-09-07", "2026-09-08"], "sourceRowsSha256": "b" * 64,
            "historicalPrefixes": {"unchanged": True, "files": 17, "bytes": 2000, "sha256": "c" * 64},
            "sourceAbsence": {"sourceCommit": "80q7jtqjrga15h61uufm60h6uqirk6iq", "dates": {}},
            "priceValidation": price_claim(["2026-09-07", "2026-09-08"]),
            "legacyUnquotedCSI": legacy_claim(["2026-09-07", "2026-09-08"]),
            "indexPriceValidation": index_claim("2026-09-08"),
        }
        self.write()
        for target in ((sqlite3, "connect"), (socket.socket, "connect"), (subprocess, "run"), (subprocess, "Popen")):
            guard = mock.patch.object(*target, side_effect=AssertionError("forbidden external operation"))
            guard.start()
            self.addCleanup(guard.stop)

    def write(self, document=None):
        self.path.write_text(json.dumps(self.document if document is None else document))

    def features(self, symbol, size):
        for field in health.FIELDS:
            (self.root / "features" / symbol / (field + ".day.bin")).write_bytes(
                struct.pack("<" + str(size + 1) + "f", 0, *range(10, 10 + size)))

    def fingerprint(self):
        return {str(p.relative_to(self.root.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.parent.rglob("*") if p.is_file() and not p.is_symlink()}

    def partial(self, recovered=False):
        document = copy.deepcopy(self.document)
        missing = {"SH600825": ["2026-09-07"] if recovered else ["2026-09-07", "2026-09-08"]}
        invalid = {"2026-09-07": {"SH600825": ["source_absent_today"]},
                   "2026-09-08": {"SH600825": ["prior_close_source_absent"] if recovered else
                                  ["source_absent_today", "prior_close_source_absent"]}}
        document["sourceAbsence"]["dates"] = missing
        document["priceValidation"] = price_claim(document["calendarAdded"], missing, invalid,
                                                 {"SH600825": "2026-09-08" if recovered else "2026-09-04"})
        return document

    def test_valid_increment_preserves_true_release_and_all_files(self):
        before = self.fingerprint()
        calendar, complete, gen = health.qlib_evidence(self.root)
        self.assertEqual(calendar, self.days)
        self.assertEqual(complete, "2026-09-08")
        self.assertEqual(gen[1], "2026-09-06")
        self.assertEqual(gen[2], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(gen[3][1], self.document["sourceCommit"])
        self.assertEqual(before, self.fingerprint())

    def test_partial_source_absence_is_readonly_and_not_complete(self):
        self.write(self.partial())
        before = self.fingerprint()
        _, _, gen = health.qlib_evidence(self.root)
        self.assertEqual(gen[3][9]["status"], "partial")
        self.assertEqual([r["invalid"] for r in gen[3][9]["dailyCoverage"]], [1, 1])
        self.assertEqual(before, self.fingerprint())

    def test_recovered_target_quote_still_has_unavailable_previous_close(self):
        self.write(self.partial(recovered=True))
        _, _, gen = health.qlib_evidence(self.root)
        self.assertEqual(gen[3][9]["status"], "partial")
        self.assertEqual(gen[3][9]["dailyCoverage"][-1]["invalid_reasons"],
                         {"SH600825": ["prior_close_source_absent"]})

    def test_earlier_daily_partial_cannot_be_hidden_by_complete_target(self):
        self.days.append("2026-09-09")
        (self.root / "calendars/day.txt").write_text("\n".join(self.days) + "\n")
        for symbol in health.REFERENCES:
            self.features(symbol, len(self.days))
        document = self.partial(recovered=True)
        document["targetDate"] = "2026-09-09"
        document["calendarAdded"].append("2026-09-09")
        document["indexPriceValidation"]["targetDate"] = "2026-09-09"
        document["priceValidation"] = price_claim(document["calendarAdded"], document["sourceAbsence"]["dates"],
            {row["target_date"]: row["invalid_reasons"] for row in document["priceValidation"]["daily_coverage"]},
            {"SH600825": "2026-09-09"})
        self.write(document)
        _, _, gen = health.qlib_evidence(self.root)
        self.assertEqual(gen[3][9]["status"], "partial")
        self.assertEqual(gen[3][9]["dailyCoverage"][-1]["invalid"], 0)

    def test_partial_without_daily_invalid_or_absence_proof_rejected(self):
        cases = []
        empty = copy.deepcopy(self.document); empty["priceValidation"]["status"] = "partial"; cases.append(empty)
        no_proof = self.partial(); no_proof["sourceAbsence"]["dates"] = {}; cases.append(no_proof)
        wrong_ref = self.partial(); wrong_ref["sourceAbsence"]["sourceCommit"] = "a" * 32; cases.append(wrong_ref)
        for document in cases:
            with self.subTest(document=document):
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_daily_counts_types_ratios_and_flat_claim_must_agree(self):
        for field, value in (("eligible", True), ("valid", -1), ("invalid", 0), ("coverage", 1),
                             ("invalid_symbols", []), ("invalid_symbols", ["SH600825", "SH600825"]),
                             ("first_day_listings_checked", False), ("reference_dates", {}),
                             ("status", "complete"), ("target_date", "2026-09-06")):
            with self.subTest(field=field):
                document = self.partial(); document["priceValidation"]["daily_coverage"][0][field] = value
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)
        for field, value in (("invalid", True), ("coverage", True), ("valid", 5000), ("status", "complete")):
            with self.subTest(flat=field):
                document = self.partial(); document["priceValidation"][field] = value
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_reasons_require_exact_symbol_sets_known_codes_and_matching_dates(self):
        for reasons in ({}, {"SH600826": ["source_absent_today"]}, {"SH600825": []},
                        {"SH600825": ["unproven_nan"]}, {"SH600825": [[]]},
                        {"SH600825": ["source_absent_today", "source_absent_today"]},
                        {"SH600825": ["prior_close_source_absent"]}):
            with self.subTest(reasons=reasons):
                document = self.partial(); document["priceValidation"]["daily_coverage"][0]["invalid_reasons"] = reasons
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_absence_dates_last_quotes_and_sparse_proof_must_match(self):
        for dates in (["2026-09-06"], ["2026-09-09"], ["2026-09-07", "2026-09-07"],
                      ["2026-09-08", "2026-09-07"], [], [True]):
            with self.subTest(dates=dates):
                document = self.partial()
                document["sourceAbsence"]["dates"]["SH600825"] = dates
                document["priceValidation"]["sourceAbsentDates"]["SH600825"] = dates
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)
        for quotes in ({}, {"SH600825": "2026-09-08"}, {"SH600825": "2026-09-09"},
                       {"SH600825": "2026-09-06"}, {"sh600825": "2026-09-04"}):
            with self.subTest(quotes=quotes):
                document = self.partial(); document["priceValidation"]["lastQuotedDate"] = quotes
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_source_quality_change_during_read_is_detected(self):
        real = health.read_file
        changed = False
        def mutate(path, *args, **kwargs):
            nonlocal changed
            if Path(path).name == "close.day.bin" and not changed:
                self.write(self.partial()); changed = True
            return real(path, *args, **kwargs)
        with mock.patch.object(health, "read_file", side_effect=mutate):
            with self.assertRaisesRegex(health.EvidenceError, "generation_changed_or_invalid"):
                health.qlib_evidence(self.root)

    def test_known_absence_cannot_be_omitted_from_daily_invalid_claims(self):
        for hidden_day in ("2026-09-07", "2026-09-08"):
            with self.subTest(hidden_day=hidden_day):
                document = self.partial()
                old = document["priceValidation"]
                invalid = {row["target_date"]: row["invalid_reasons"] for row in old["daily_coverage"]
                           if row["target_date"] != hidden_day}
                document["priceValidation"] = price_claim(document["calendarAdded"], old["sourceAbsentDates"],
                                                         invalid, old["lastQuotedDate"])
                self.write(document)
                with self.assertRaisesRegex(health.EvidenceError, "absence_not_disclosed"):
                    health.qlib_evidence(self.root)

    def test_older_tail_gap_proof_is_bounded_and_does_not_invent_current_invalid(self):
        self.days = ["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02"] + self.days
        (self.root / "calendars/day.txt").write_text("\n".join(self.days) + "\n")
        for symbol in health.REFERENCES:
            self.features(symbol, len(self.days))
        document = copy.deepcopy(self.document)
        absence = {"SZ301266": ["2026-08-31", "2026-09-01"]}
        document["sourceAbsence"]["dates"] = absence
        document["priceValidation"] = price_claim(document["calendarAdded"], absence,
            last_quotes={"SZ301266": "2026-09-08"}, gaps=absence)
        self.write(document)
        self.assertEqual(health.qlib_evidence(self.root)[2][3][9]["status"], "complete")
        document["priceValidation"]["historical_tail_gaps"] = {}
        self.write(document)
        with self.assertRaisesRegex(health.EvidenceError, "old_absence_unproven"):
            health.qlib_evidence(self.root)

    def test_each_price_claim_required_field_is_required(self):
        for field in self.document["priceValidation"]:
            with self.subTest(field=field):
                document = copy.deepcopy(self.document)
                document["priceValidation"].pop(field)
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_legacy_csi_absence_is_separate_from_current_inventory(self):
        document = copy.deepcopy(self.document)
        document["legacyUnquotedCSI"] = legacy_claim(document["calendarAdded"], {"SH600929": "2026-09-03"})
        document["indexPriceValidation"] = index_claim(document["targetDate"], legacy=["SH600929"])
        self.write(document)
        quality = health.qlib_evidence(self.root)[2][3][9]
        self.assertEqual(quality["status"], "partial")
        self.assertEqual(quality["priceStatus"], "complete")
        self.assertEqual(quality["index"]["legacyLastQuotedDates"], {"SH600929": "2026-09-03"})

    def test_current_csi_missing_quote_and_previous_close_require_same_proof(self):
        document = self.partial()
        document["indexPriceValidation"] = index_claim(document["targetDate"], missing_quotes=["SH600825"],
            missing_previous=["SH600825"], absence=document["sourceAbsence"]["dates"])
        self.write(document)
        self.assertEqual(health.qlib_evidence(self.root)[2][3][9]["index"]["validation"]["status"], "partial")
        document["sourceAbsence"]["sourceCommit"] = "b" * 32
        self.write(document)
        with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_legacy_csi_wrong_ref_dates_quotes_and_overlap_are_rejected(self):
        for changes in ({"sourceCommit": "b" * 32}, {"baseDate": "2026-09-03"},
                        {"dates": {"SH600929": ["2026-09-08"]}},
                        {"lastQuotedDates": {"SH600929": "2026-09-04"}},
                        {"lastQuotedDates": {"SH600929": "2026-09-02"}}, {"lastQuotedDates": {}}):
            with self.subTest(changes=changes):
                document = copy.deepcopy(self.document)
                document["legacyUnquotedCSI"] = dict(legacy_claim(document["calendarAdded"], {"SH600929": "2026-09-03"}), **changes)
                document["indexPriceValidation"] = index_claim(document["targetDate"], legacy=["SH600929"])
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)
        document = self.partial()
        document["legacyUnquotedCSI"] = legacy_claim(document["calendarAdded"], {"SH600825": "2026-09-03"})
        document["indexPriceValidation"] = index_claim(document["targetDate"], legacy=["SH600825"])
        self.write(document)
        with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_index_partial_counts_lists_meaning_and_nested_fields_are_strict(self):
        cases = [("status", "partial"), ("uniqueMembers", True), ("uniqueMembersWithPrices", 0),
                 ("usableDailyReturnMembers", -1), ("sourceAbsenceSlotsVerified", 1),
                 ("sourceAbsenceCommit", "a" * 32), ("legacyUnquotedBaseDate", "2026-09-03"),
                 ("absenceMeaning", "confirmed_suspension"), ("missingQuoteMembers", ["SH600825"]),
                 ("indexMemberCounts", {}), ("missingQuoteCountsByIndex", {"csiall": 0})]
        for field, value in cases:
            with self.subTest(field=field):
                document = copy.deepcopy(self.document); document["indexPriceValidation"][field] = value
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_producer_source_and_combined_quality_cannot_contradict_validated_claims(self):
        for key, value in (("source_commit", "a" * 32), ("base_date", "2026-09-03")):
            document = self.partial(); document["priceValidation"][key] = value
            self.write(document)
            with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)
        document = self.partial(); document["dataQuality"] = "complete"
        self.write(document)
        with self.assertRaisesRegex(health.EvidenceError, "quality_claim_disagrees"):
            health.qlib_evidence(self.root)

    def stored_short_history(self, dates=None):
        self.days = ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
                     "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
                     "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
                     "2026-09-07", "2026-09-08"]
        (self.root / "calendars/day.txt").write_text("\n".join(self.days) + "\n")
        for symbol in health.REFERENCES:
            self.features(symbol, len(self.days))
        document = copy.deepcopy(self.document)
        absent = {"SZ301266": ["2026-08-25", "2026-08-26"] if dates is None else dates}
        document["sourceAbsence"]["dates"] = copy.deepcopy(absent)
        document["priceValidation"] = price_claim(document["calendarAdded"], absent,
            last_quotes={"SZ301266": "2026-09-08"})
        document["storedShortHistoryEvidence"] = {"sourceCommit": document["sourceCommit"], "dates": copy.deepcopy(absent)}
        return document

    def test_stored_short_history_has_separate_readonly_identity_not_tail_gap(self):
        document = self.stored_short_history()
        self.write(document)
        before = self.fingerprint()
        quality = health.qlib_evidence(self.root)[2][3][9]
        self.assertEqual(quality["storedShortHistoryDates"], document["storedShortHistoryEvidence"]["dates"])
        self.assertEqual(quality["historicalTailGaps"], {})
        self.assertEqual(quality["status"], "complete")  # The newly added days, not a historical full-market claim.
        self.assertEqual(before, self.fingerprint())

    def test_optional_stored_evidence_absence_is_empty_but_cannot_authorize_old_slot(self):
        self.assertEqual(health.qlib_evidence(self.root)[2][3][9]["storedShortHistoryDates"], {})
        document = self.stored_short_history()
        document.pop("storedShortHistoryEvidence")
        self.write(document)
        with self.assertRaisesRegex(health.EvidenceError, "old_absence_unproven"):
            health.qlib_evidence(self.root)

    def test_stored_short_history_identity_shape_and_mapping_subset_are_strict(self):
        document = self.stored_short_history()
        cases = [None, {}, {"sourceCommit": "a" * 32, "dates": {"SZ301266": ["2026-08-25"]}},
                 {"sourceCommit": document["sourceCommit"], "dates": []},
                 {"sourceCommit": document["sourceCommit"], "dates": {"SH600825": ["2026-08-25"]}},
                 {"sourceCommit": document["sourceCommit"], "dates": {"SZ301266": []}},
                 {"sourceCommit": document["sourceCommit"], "dates": {"SZ301266": ["2026-08-24"]}},
                 {"sourceCommit": document["sourceCommit"], "dates": {"SZ301266": ["2026-08-26", "2026-08-25"]}},
                 {"sourceCommit": document["sourceCommit"], "dates": {"SZ301266": ["2026-08-25", "2026-08-25"]}},
                 {"sourceCommit": document["sourceCommit"], "dates": {"SZ301266": [True]}}]
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.write(dict(document, storedShortHistoryEvidence=evidence))
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_stored_short_history_only_accepts_latest_ten_old_calendar_days(self):
        self.write(self.stored_short_history(["2026-08-24"]))  # Tenth old trading day, inclusive of base.
        health.qlib_evidence(self.root)
        for dates in (["2026-08-21"], ["2026-08-23"], ["2026-09-07"],
                      ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
                       "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]):
            with self.subTest(dates=dates):
                self.write(self.stored_short_history(dates))
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_stored_short_history_cannot_claim_last_quoted_date_absent(self):
        document = self.stored_short_history()
        document["priceValidation"]["lastQuotedDate"]["SZ301266"] = "2026-08-25"
        self.write(document)
        with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_stored_short_history_removal_during_read_is_a_generation_change(self):
        document = self.stored_short_history()
        self.write(document)
        original = health.read_file
        changed = False
        def remove(path, *args, **kwargs):
            nonlocal changed
            if Path(path).name == "close.day.bin" and not changed:
                document.pop("storedShortHistoryEvidence"); self.write(document); changed = True
            return original(path, *args, **kwargs)
        with mock.patch.object(health, "read_file", side_effect=remove):
            with self.assertRaisesRegex(health.EvidenceError, "generation_changed_or_invalid"):
                health.qlib_evidence(self.root)
        for field in self.document["indexPriceValidation"]:
            with self.subTest(missing=field):
                document = copy.deepcopy(self.document); document["indexPriceValidation"].pop(field)
                self.write(document)
                with self.assertRaises(health.EvidenceError): health.qlib_evidence(self.root)

    def test_missing_manifest_cannot_authorize_date_after_release(self):
        self.path.unlink()
        with self.assertRaisesRegex(health.EvidenceError, "generation_changed_or_invalid"):
            health.qlib_evidence(self.root)

    def test_legacy_calendar_may_lead_reference_as_before(self):
        self.path.unlink()
        for symbol in health.REFERENCES:
            self.features(symbol, 2)
        calendar, complete, gen = health.qlib_evidence(self.root)
        self.assertEqual(calendar[-1], "2026-09-08")
        self.assertEqual(complete, "2026-09-04")
        self.assertIsNone(gen[2])
        self.assertIsNone(gen[3])

    def test_malformed_manifest_never_falls_back_to_legacy(self):
        for symbol in health.REFERENCES:
            self.features(symbol, 2)
        for raw in (b"", b"[]", b"null", b"{broken", b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(health.EvidenceError):
                    health.qlib_evidence(self.root)

    def test_all_required_fields_are_required(self):
        for field in self.document:
            document = copy.deepcopy(self.document)
            document.pop(field)
            with self.subTest(field=field):
                self.write(document)
                with self.assertRaises(health.EvidenceError):
                    health.qlib_evidence(self.root)

    def test_identity_type_and_value_tampering_rejected(self):
        cases = [("schemaVersion", True), ("schemaVersion", 2), ("kind", "release"),
                 ("status", "updated"), ("sourceRepository", "someone/else"),
                 ("sourceCommit", "master"), ("sourceCommit", "w" * 32),
                 ("sourceCommit", "a" * 32 + "\n"), ("generationId", "z" * 32),
                 ("generationId", None), ("baseReleaseTag", "2026-09-05"),
                 ("sourceRowsSha256", "B" * 64), ("sourceRowsSha256", None)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.write(dict(self.document, **{field: value}))
                with self.assertRaises(health.EvidenceError):
                    health.qlib_evidence(self.root)

    def test_prefix_claim_is_required_and_strictly_shaped(self):
        cases = [None, {}, {"unchanged": False},
                 dict(self.document["historicalPrefixes"], unchanged=1),
                 dict(self.document["historicalPrefixes"], files=True),
                 dict(self.document["historicalPrefixes"], bytes=0),
                 dict(self.document["historicalPrefixes"], sha256="bad")]
        for value in cases:
            with self.subTest(value=value):
                self.write(dict(self.document, historicalPrefixes=value))
                with self.assertRaises(health.EvidenceError):
                    health.qlib_evidence(self.root)

    def test_calendar_added_must_equal_entire_actual_suffix(self):
        cases = [[], ["2026-09-08"], ["2026-09-04", "2026-09-07", "2026-09-08"],
                 ["2026-09-08", "2026-09-07"], ["2026-09-07", "2026-09-07", "2026-09-08"],
                 "2026-09-08", [123], ["20260907", "2026-09-08"]]
        for added in cases:
            with self.subTest(added=added):
                self.write(dict(self.document, calendarAdded=added))
                with self.assertRaises(health.EvidenceError):
                    health.qlib_evidence(self.root)

    def test_base_target_and_calendar_disagreements_rejected(self):
        for changes in ({"baseDate": "2026-09-08"}, {"baseDate": "2026-09-09"},
                        {"baseDate": "2026-09-02"}, {"baseDate": "2026-09-03"},
                        {"targetDate": "2026-09-07"}, {"targetDate": "2026-09-09"}):
            with self.subTest(changes=changes):
                self.write(dict(self.document, **changes))
                with self.assertRaises(health.EvidenceError):
                    health.qlib_evidence(self.root)

    def test_incremental_reference_physical_tail_cannot_lag_or_exceed_calendar(self):
        for size in (3, 5):
            self.features(health.REFERENCES[0], size)
            with self.subTest(size=size), self.assertRaisesRegex(health.EvidenceError, "physical_tail"):
                health.qlib_evidence(self.root)

    def test_complete_reference_date_must_equal_manifest_target(self):
        for symbol in health.REFERENCES:
            for field in health.FIELDS:
                path = self.root / "features" / symbol / (field + ".day.bin")
                raw = path.read_bytes()
                path.write_bytes(raw[:-4] + struct.pack("<f", float("nan")))
        with self.assertRaisesRegex(health.EvidenceError, "reference_date_disagrees"):
            health.qlib_evidence(self.root)

    def test_manifest_in_place_change_detected_despite_same_inode_and_tag(self):
        real = health.read_file
        changed = False
        def mutate(path, *args, **kwargs):
            nonlocal changed
            if Path(path).name == "close.day.bin" and not changed:
                self.write(dict(self.document, sourceRowsSha256="d" * 64))
                changed = True
            return real(path, *args, **kwargs)
        with mock.patch.object(health, "read_file", side_effect=mutate):
            with self.assertRaisesRegex(health.EvidenceError, "generation_changed_or_invalid"):
                health.qlib_evidence(self.root)

    def test_manifest_removal_mid_read_rejected(self):
        real = health.read_file
        removed = False
        def remove(path, *args, **kwargs):
            nonlocal removed
            if Path(path).name == "close.day.bin" and not removed:
                self.path.unlink()
                removed = True
            return real(path, *args, **kwargs)
        with mock.patch.object(health, "read_file", side_effect=remove):
            with self.assertRaises(health.EvidenceError):
                health.qlib_evidence(self.root)

    def test_manifest_digest_covers_ignored_metadata_and_whitespace(self):
        old = health.generation(self.root)
        self.write(dict(self.document, diagnostic="additional local metadata"))
        changed = health.generation(self.root)
        self.assertEqual(old[0:2], changed[0:2])
        self.assertEqual(old[3], changed[3])
        self.assertNotEqual(old[2], changed[2])

    def test_manifest_symlink_and_oversize_rejected(self):
        raw = self.path.read_bytes()
        self.path.unlink()
        self.path.symlink_to(self.root / "missing-target")
        with self.assertRaises((health.EvidenceError, OSError)):
            health.qlib_evidence(self.root)
        self.path.unlink()
        self.path.write_bytes(raw + b" " * 65536)
        with self.assertRaises(health.EvidenceError):
            health.qlib_evidence(self.root)


class TailGapExceptionTests(unittest.TestCase):
    """SZ301139 21-day exchange suspension: exact allowlisted claim only."""

    GAP = list(health.QLIB_TAIL_GAP_EXCEPTIONS["qlib-tail-gap-SZ301139-20260831-v1"]["gapDates"])
    write = IncrementalQlibTests.write
    features = IncrementalQlibTests.features

    def setUp(self):
        IncrementalQlibTests.setUp(self)
        self.days = ["2026-08-27", "2026-08-28"] + self.GAP + ["2026-09-30"]
        (self.root / "calendars/day.txt").write_text("\n".join(self.days) + "\n")
        for symbol in health.REFERENCES:
            self.features(symbol, len(self.days))
        expected = health.QLIB_TAIL_GAP_EXCEPTIONS["qlib-tail-gap-SZ301139-20260831-v1"]
        document = copy.deepcopy(self.document)
        document.update(baseDate="2026-09-29", targetDate="2026-09-30", calendarAdded=["2026-09-30"])
        absence = {"SZ301139": list(self.GAP)}
        document["sourceAbsence"]["dates"] = absence
        document["priceValidation"] = price_claim(["2026-09-30"], absence,
            {"2026-09-30": {"SZ301139": ["prior_close_source_absent"]}},
            {"SZ301139": "2026-09-30"}, gaps={"SZ301139": list(self.GAP)})
        document["legacyUnquotedCSI"] = dict(legacy_claim(["2026-09-30"]), baseDate="2026-09-29")
        document["indexPriceValidation"] = dict(index_claim("2026-09-30"), legacyUnquotedBaseDate="2026-09-29")
        document["historicalTailGapException"] = {
            "policyId": "qlib-tail-gap-SZ301139-20260831-v1", "recordSha256": expected["recordSha256"],
            "symbol": "SZ301139", "lastStoredDate": "2026-08-28", "gapDates": list(self.GAP),
            "resumeDate": "2026-09-30", "sourceCommit": document["sourceCommit"],
            "resumeRowSha256": expected["resumeRowSha256"], "lastStoredRowSha256": expected["lastStoredRowSha256"],
            "officialEvidenceSha256": list(expected["officialEvidenceSha256"]), "meaning": expected["meaning"]}
        self.document = document
        self.write()

    def test_exact_exception_accepted_and_reported_partial(self):
        _, complete, gen = health.qlib_evidence(self.root)
        self.assertEqual(complete, "2026-09-30")
        quality = gen[3][9]
        self.assertEqual(quality["status"], "partial")
        self.assertEqual(quality["historicalTailGaps"], {"SZ301139": self.GAP})

    def test_same_long_gap_without_exception_still_rejected(self):
        document = copy.deepcopy(self.document)
        document.pop("historicalTailGapException")
        self.write(document)
        with self.assertRaisesRegex(health.EvidenceError, "historical_tail_claim_invalid"):
            health.qlib_evidence(self.root)

    def test_altered_or_stale_exception_rejected(self):
        changes = [("recordSha256", "0" * 64), ("gapDates", self.GAP[1:]), ("resumeDate", "2026-10-08"),
                   ("sourceCommit", "0" * 32), ("symbol", "SZ301266"), ("policyId", "other"),
                   ("resumeRowSha256", "0" * 64), ("officialEvidenceSha256", [])]
        for key, value in changes:
            with self.subTest(key=key):
                document = copy.deepcopy(self.document)
                document["historicalTailGapException"][key] = value
                self.write(document)
                with self.assertRaisesRegex(health.EvidenceError, "tail_gap_exception_invalid"):
                    health.qlib_evidence(self.root)
        document = copy.deepcopy(self.document)
        document["historicalTailGapException"]["extra"] = True
        self.write(document)
        with self.assertRaisesRegex(health.EvidenceError, "tail_gap_exception_invalid"):
            health.qlib_evidence(self.root)
        # Stale reuse: claim present but the generation has no such long gap.
        document = copy.deepcopy(self.document)
        document["priceValidation"]["historical_tail_gaps"] = {}
        self.write(document)
        with self.assertRaisesRegex(health.EvidenceError, "tail_gap_exception_invalid"):
            health.qlib_evidence(self.root)

    def test_exception_does_not_cover_another_long_gap(self):
        document = copy.deepcopy(self.document)
        other = self.GAP[-11:]
        document["sourceAbsence"]["dates"]["SZ301266"] = other
        price = document["priceValidation"]
        price["sourceAbsentDates"]["SZ301266"] = other
        price["lastQuotedDate"]["SZ301266"] = "2026-09-30"
        price["historical_tail_gaps"]["SZ301266"] = other
        for row in [price] + price["daily_coverage"]:
            row["invalid_symbols"] = sorted(row["invalid_symbols"] + ["SZ301266"])
            row["invalid_reasons"]["SZ301266"] = ["prior_close_source_absent"]
            row["invalid"] += 1; row["valid"] -= 1; row["coverage"] = row["valid"] / row["eligible"]
        self.write(document)
        with self.assertRaisesRegex(health.EvidenceError, "historical_tail_claim_invalid"):
            health.qlib_evidence(self.root)


if __name__ == "__main__":
    unittest.main()
