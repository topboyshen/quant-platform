from pathlib import Path
import struct
import tempfile
import unittest

from indices import INDEX_MAP
from conversion import FIELDS
from alignment import validate_index_market_alignment

REF = "a" * 32
SYMBOL = "SH600000"


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "instruments").mkdir()
        (self.root / "features/sh600000").mkdir(parents=True)
        self.days = ["2026-09-04", "2026-09-07", "2026-09-08"]
        (self.root / "instruments/all.txt").write_text("SH600000\t2026-09-04\t2026-09-08\n")
        for name in INDEX_MAP:
            (self.root / "instruments" / (name + ".txt")).write_text("SH600000\t2026-09-04\t2026-09-08\n")
        self.values = {field: [1.0, 2.0, 3.0] for field in FIELDS}
        self.values.update(high=[2.0, 3.0, 4.0], low=[0.5, 1.0, 2.0], factor=[1.0] * 3,
                           volume=[100.0] * 3, amount=[1000.0] * 3, change=[0.1] * 3)
        for field, values in self.values.items():
            (self.root / "features/sh600000" / (field + ".day.bin")).write_bytes(struct.pack("<4f", 0, *values))

    def validate(self, proof=None, ref=None, legacy=None):
        return validate_index_market_alignment(self.root, self.days, source_absence=proof, expected_source_commit=ref,
                                                legacy_unquoted=legacy)

    def proof(self, *days):
        return {"sourceCommit": REF, "dates": {SYMBOL: list(days)}}

    def set_value(self, field, index, value):
        path = self.root / "features/sh600000" / (field + ".day.bin")
        raw = bytearray(path.read_bytes())
        struct.pack_into("<f", raw, (index + 1) * 4, value)
        path.write_bytes(raw)

    def missing_slot(self, index):
        for field in FIELDS:
            if field != "factor":
                self.set_value(field, index, float("nan"))

    def legacy_fixture(self):
        (self.root / "instruments/all.txt").write_text(f"{SYMBOL}\t{self.days[0]}\t{self.days[0]}\n")
        for field in FIELDS:
            path = self.root / "features/sh600000" / (field + ".day.bin")
            path.write_bytes(path.read_bytes()[:8])
        return {"sourceCommit": REF, "baseDate": self.days[1], "dates": {SYMBOL: [self.days[2]]},
                "lastQuotedDates": {SYMBOL: self.days[0]}}

    def test_all_indices_refer_to_current_prices(self):
        report = self.validate()
        self.assertEqual(report["uniqueMembersWithPrices"], 1)
        self.assertEqual(len(report["indexMemberCounts"]), 5)

    def test_closed_legacy_letters_need_no_fabricated_price_or_new_member(self):
        path = self.root / "instruments/csi300.txt"
        raw = ("SHT00018\t2005-04-08\t2005-12-30\n"
               "SHT00018\t2006-01-04\t2007-01-03\n").encode() + path.read_bytes()
        path.write_bytes(raw)
        report = self.validate()
        self.assertEqual(report["uniqueMembers"], 1)
        self.assertEqual(report["uniqueMembersWithPrices"], 1)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.root / "features/sht00018").exists())

    def test_legacy_letters_at_target_or_future_are_not_ignored(self):
        path = self.root / "instruments/csi300.txt"
        current = path.read_bytes()
        for end in (self.days[-1], "2026-09-09"):
            path.write_bytes(f"SHT00018\t2005-04-08\t{end}\n".encode() + current)
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, "invalid_index_member_row"):
                self.validate()

    def test_legacy_history_still_validates_dates_and_inclusive_overlap(self):
        path = self.root / "instruments/csi300.txt"
        current = path.read_bytes()
        for history in ("SHT00018\t2005-04-08\t2006-01-04\nSHT00018\t2006-01-04\t2007-01-03\n",
                        "SHT00018\t2007-01-03\t2005-04-08\n",
                        "SHT00018\t2005-04-08\t2007-02-30\n",
                        "SHT000180\t2005-04-08\t2007-01-03\n"):
            path.write_bytes(history.encode() + current)
            with self.subTest(history=history), self.assertRaises(ValueError):
                self.validate()

    def test_letters_cannot_enter_price_inventory_or_absence_proof(self):
        path = self.root / "instruments/all.txt"
        raw = path.read_bytes()
        path.write_bytes(b"SHT00018\t2005-04-08\t2007-01-03\n" + raw)
        with self.assertRaisesRegex(ValueError, "invalid_all_inventory"):
            self.validate()
        path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "absence_dates_invalid"):
            self.validate({"sourceCommit": REF, "dates": {"SHT00018": [self.days[-1]]}}, REF)

    def test_missing_inventory_member_rejected(self):
        (self.root / "instruments/csi300.txt").write_text("SH600001\t2026-09-04\t2026-09-08\n")
        with self.assertRaisesRegex(ValueError, "outside_price_inventory"):
            self.validate()

    def test_ended_member_rejected(self):
        (self.root / "instruments/all.txt").write_text("SH600000\t2026-09-04\t2026-09-07\n")
        with self.assertRaisesRegex(ValueError, "outside_price_inventory"):
            self.validate()

    def test_price_missing_target_rejected(self):
        (self.root / "features/sh600000/close.day.bin").write_bytes(struct.pack("<3f", 0, 1, 2))
        with self.assertRaisesRegex(ValueError, "feature_not_at_target"):
            self.validate()

    def test_nan_price_rejected(self):
        (self.root / "features/sh600000/close.day.bin").write_bytes(struct.pack("<4f", 0, 1, 2, float("nan")))
        with self.assertRaisesRegex(ValueError, "invalid_target_value"):
            self.validate()

    def test_missing_previous_close_rejected(self):
        (self.root / "features/sh600000/close.day.bin").write_bytes(struct.pack("<4f", 0, 1, float("nan"), 3))
        with self.assertRaisesRegex(ValueError, "previous_close_missing"):
            self.validate()

    def test_new_listing_first_day_needs_prices_not_fabricated_previous_close(self):
        (self.root / "instruments/all.txt").write_text("SH600000\t2026-09-08\t2026-09-08\n")
        for field in FIELDS:
            value = self.values[field][-1] if field != "change" else float("nan")
            (self.root / "features/sh600000" / (field + ".day.bin")).write_bytes(struct.pack("<2f", 2, value))
        result = self.validate()
        self.assertEqual(result["uniqueMembersWithPrices"], 1)
        self.assertEqual(result["usableDailyReturnMembers"], 0)
        self.assertEqual(result["firstListingDayMembers"], [SYMBOL])

    def test_old_membership_file_not_marked_current(self):
        (self.root / "instruments/csi300.txt").write_text("SH600000\t2026-09-04\t2026-09-07\n")
        with self.assertRaisesRegex(ValueError, "membership_not_at_target"):
            self.validate()

    def test_proven_current_absence_is_not_counted_as_quote_or_return(self):
        self.missing_slot(2)
        result = self.validate(self.proof(self.days[2]), REF)
        self.assertEqual(result["uniqueMembers"], 1)
        self.assertEqual(result["uniqueMembersWithPrices"], 0)
        self.assertEqual(result["usableDailyReturnMembers"], 0)
        self.assertEqual(result["missingQuoteMembers"], [SYMBOL])
        self.assertEqual(result["missingPreviousCloseMembers"], [])
        self.assertEqual(result["sourceAbsenceSlotsVerified"], 1)
        self.assertTrue(all(count == 1 for count in result["indexMemberCounts"].values()))

    def test_proven_previous_absence_keeps_real_quote_but_no_usable_return(self):
        self.missing_slot(1)
        result = self.validate(self.proof(self.days[1]), REF)
        self.assertEqual(result["uniqueMembersWithPrices"], 1)
        self.assertEqual(result["usableDailyReturnMembers"], 0)
        self.assertEqual(result["missingQuoteMembers"], [])
        self.assertEqual(result["missingPreviousCloseMembers"], [SYMBOL])

    def test_consecutive_absence_preserves_both_explicit_reasons(self):
        self.missing_slot(1)
        self.missing_slot(2)
        result = self.validate(self.proof(*self.days[1:]), REF)
        self.assertEqual(result["missingQuoteMembers"], [SYMBOL])
        self.assertEqual(result["missingPreviousCloseMembers"], [SYMBOL])
        self.assertEqual(result["uniqueMembersWithPrices"], 0)
        self.assertEqual(result["sourceAbsenceSlotsVerified"], 2)

    def test_no_proof_or_empty_exceptions_keep_strict_behavior(self):
        self.missing_slot(2)
        for proof, ref in ((None, None), ({"sourceCommit": REF, "dates": {}}, REF)):
            with self.subTest(proof=proof), self.assertRaisesRegex(ValueError, "invalid_target_value"):
                self.validate(proof, ref)

    def test_same_source_commit_required_for_absence(self):
        self.missing_slot(2)
        for ref in (None, "b" * 32, "master"):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "absence_identity_invalid"):
                self.validate(self.proof(self.days[2]), ref)

    def test_wrong_absence_date_cannot_authorize_current_missing_quote(self):
        self.missing_slot(2)
        with self.assertRaisesRegex(ValueError, "absence_bytes_disagree"):
            self.validate(self.proof(self.days[1]), REF)

    def test_real_quote_cannot_be_falsely_claimed_source_absent(self):
        with self.assertRaisesRegex(ValueError, "absence_bytes_disagree"):
            self.validate(self.proof(self.days[2]), REF)

    def test_single_missing_field_cannot_use_whole_quote_exception(self):
        self.set_value("close", 2, float("nan"))
        with self.assertRaisesRegex(ValueError, "absence_bytes_disagree"):
            self.validate(self.proof(self.days[2]), REF)

    def test_missing_quote_infinity_or_stray_finite_field_rejected(self):
        self.missing_slot(2)
        for field, value in (("volume", float("inf")), ("amount", 0.0), ("open", 2.0)):
            self.set_value(field, 2, value)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "absence_bytes_disagree"):
                self.validate(self.proof(self.days[2]), REF)
            self.set_value(field, 2, float("nan"))

    def test_missing_quote_factor_must_be_exactly_carried(self):
        self.missing_slot(2)
        for value in (2.0, 0.0, float("nan"), float("inf"), 1.0000001192092896):
            self.set_value("factor", 2, value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "factor_not_carried"):
                self.validate(self.proof(self.days[2]), REF)

    def test_prior_exception_cannot_hide_corrupt_current_noncore_field(self):
        self.missing_slot(1)
        self.set_value("volume", 2, float("nan"))
        with self.assertRaisesRegex(ValueError, "invalid_target_value"):
            self.validate(self.proof(self.days[1]), REF)

    def test_prior_absence_requires_entire_missing_slot_not_only_close_nan(self):
        self.set_value("close", 1, float("nan"))
        with self.assertRaisesRegex(ValueError, "absence_bytes_disagree"):
            self.validate(self.proof(self.days[1]), REF)

    def test_proof_cannot_hide_missing_field_file_or_wrong_boundary(self):
        self.missing_slot(2)
        path = self.root / "features/sh600000/volume.day.bin"
        raw = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "feature_inventory_invalid"):
            self.validate(self.proof(self.days[2]), REF)
        path.write_bytes(raw[:-4])
        with self.assertRaisesRegex(ValueError, "feature_not_at_target"):
            self.validate(self.proof(self.days[2]), REF)

    def test_proof_dates_must_be_unique_ordered_actual_calendar_dates(self):
        for days in ([self.days[2], self.days[2]], self.days[::-1], ["2026-09-05"], ["2026-09-09"], []):
            with self.subTest(days=days), self.assertRaisesRegex(ValueError, "absence_dates_invalid"):
                self.validate(self.proof(*days), REF)

    def test_no_prior_factor_means_no_absence_placeholder_permission(self):
        self.missing_slot(0)
        with self.assertRaisesRegex(ValueError, "no_prior_factor"):
            self.validate(self.proof(self.days[0]), REF)

    def test_real_current_field_corruption_has_no_blanket_missing_allowance(self):
        for field, value in (("factor", -1.0), ("volume", float("inf")), ("amount", -1.0)):
            self.set_value(field, 2, value)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "invalid_target_value"):
                self.validate()
            self.set_value(field, 2, self.values[field][2])

    def test_legacy_unquoted_keeps_original_physical_boundary_and_partial_status(self):
        legacy = self.legacy_fixture()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = self.validate(ref=REF, legacy=legacy)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["legacyUnquotedMembers"], [SYMBOL])
        self.assertEqual(result["uniqueMembers"], 1)
        self.assertEqual(result["uniqueMembersWithPrices"], 0)
        self.assertEqual(result["usableDailyReturnMembers"], 0)
        self.assertEqual(result["sourceAbsenceSlotsVerified"], 0)
        self.assertEqual(result["missingQuoteMembers"], [])
        self.assertTrue(all(count == 1 for count in result["legacyUnquotedCountsByIndex"].values()))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_legacy_absence_without_proof_remains_rejected(self):
        self.legacy_fixture()
        with self.assertRaisesRegex(ValueError, "outside_price_inventory"):
            self.validate()

    def test_legacy_ref_mismatch_rejected(self):
        legacy = self.legacy_fixture()
        with self.assertRaisesRegex(ValueError, "legacy_unquoted_identity_invalid"):
            self.validate(ref="b" * 32, legacy=legacy)

    def test_legacy_new_day_suffix_must_be_exact(self):
        legacy = self.legacy_fixture()
        for days in ([], self.days[1:], [self.days[2], self.days[2]], ["2026-09-09"]):
            legacy["dates"][SYMBOL] = days
            with self.subTest(days=days), self.assertRaisesRegex(ValueError, "legacy_unquoted_dates_invalid"):
                self.validate(ref=REF, legacy=legacy)

    def test_legacy_base_must_be_real_calendar_day_before_target(self):
        legacy = self.legacy_fixture()
        for base in ("2026-09-06", self.days[2], "2026-09-09"):
            legacy["baseDate"] = base
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "legacy_unquoted_base_invalid"):
                self.validate(ref=REF, legacy=legacy)

    def test_old_active_cannot_be_reclassified_as_legacy(self):
        legacy = self.legacy_fixture()
        legacy["lastQuotedDates"][SYMBOL] = legacy["baseDate"]
        with self.assertRaisesRegex(ValueError, "last_quote_not_before_base"):
            self.validate(ref=REF, legacy=legacy)

    def test_legacy_claim_cannot_hide_actual_current_feature_generation(self):
        legacy = {"sourceCommit": REF, "baseDate": self.days[1], "dates": {SYMBOL: [self.days[2]]},
                  "lastQuotedDates": {SYMBOL: self.days[0]}}
        with self.assertRaisesRegex(ValueError, "legacy_unquoted_inventory_disagrees"):
            self.validate(ref=REF, legacy=legacy)

    def test_legacy_and_new_source_absence_evidence_cannot_overlap(self):
        legacy = self.legacy_fixture()
        with self.assertRaisesRegex(ValueError, "evidence_overlap"):
            self.validate(self.proof(self.days[2]), REF, legacy)

    def test_legacy_cannot_hide_missing_or_misaligned_field(self):
        legacy = self.legacy_fixture()
        path = self.root / "features/sh600000/volume.day.bin"
        raw = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "feature_inventory_invalid"):
            self.validate(ref=REF, legacy=legacy)
        path.write_bytes(raw + struct.pack("<f", 100.0))
        with self.assertRaisesRegex(ValueError, "fields_misaligned"):
            self.validate(ref=REF, legacy=legacy)

    def test_legacy_declared_last_quote_must_be_valid(self):
        legacy = self.legacy_fixture()
        self.set_value("close", 0, float("nan"))
        with self.assertRaisesRegex(ValueError, "invalid_target_value"):
            self.validate(ref=REF, legacy=legacy)

    def test_legacy_proof_keys_must_match_and_be_active_csi_members(self):
        legacy = self.legacy_fixture()
        legacy["dates"]["SH600001"] = [self.days[2]]
        with self.assertRaisesRegex(ValueError, "legacy_unquoted_identity_invalid"):
            self.validate(ref=REF, legacy=legacy)
        legacy["lastQuotedDates"]["SH600001"] = self.days[0]
        with self.assertRaisesRegex(ValueError, "not_active_csi_member"):
            self.validate(ref=REF, legacy=legacy)


if __name__ == "__main__":
    unittest.main()
