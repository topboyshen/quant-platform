from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run

REF = "a" * 32
DAYS = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"]


class AbsenceProofTests(unittest.TestCase):
    def test_missing_old_active_is_kept_in_daily_proof(self):
        info = {"SH600825": {"new": False, "first_date": "2000-01-04"}}
        raw = {("SH600825", day): {} for day in DAYS[:3]}
        proof, fresh = run.derive_source_absence(info, {}, raw, DAYS, DAYS[3:], {}, REF)
        self.assertEqual(proof, {"sourceCommit": REF, "dates": {"SH600825": DAYS[3:]}})
        self.assertEqual(fresh, {"SH600825": tuple(DAYS[3:])})

    def test_ipo_before_true_first_date_is_not_absent(self):
        info = {"SZ001999": {"new": True}}
        proof, fresh = run.derive_source_absence(info, {"SZ001999": DAYS[-1]},
            {("SZ001999", DAYS[-1]): {}}, DAYS, DAYS[3:], {}, REF)
        self.assertEqual(proof["dates"], {})
        self.assertEqual(fresh, {})

    def test_old_history_proof_is_preserved_separately(self):
        info = {"SZ301266": {"new": False, "first_date": "2022-06-08"}}
        raw = {("SZ301266", day): {} for day in DAYS[3:]}
        proof, fresh = run.derive_source_absence(info, {}, raw, DAYS, DAYS[3:],
            {"SZ301266": ("2026-08-31", "2026-09-01", *DAYS[:3])}, REF)
        self.assertEqual(proof["dates"]["SZ301266"], ["2026-08-31", "2026-09-01", *DAYS[:3]])
        self.assertEqual(fresh, {})

    def test_short_history_complete_query_proves_stored_absence_before_bulk(self):
        symbol = "SZ001999"
        history = ["2026-08-28", "2026-08-31", "2026-09-01", *DAYS[:3]]
        info = {symbol: {"new": False, "short_history": True, "first_date": history[0],
                         "old_rows": [{"date": day} for day in history]}}
        raw = {(symbol, day): {} for day in [history[0], *DAYS[3:]]}
        proof, fresh = run.derive_source_absence(info, {}, raw, DAYS, DAYS[3:], {}, REF)
        self.assertEqual(proof["dates"][symbol], history[1:])
        self.assertEqual(fresh, {})

    def test_claim_cannot_conflict_with_actual_source_row(self):
        info = {"SH600000": {"new": False, "first_date": "2000-01-04"}}
        with self.assertRaisesRegex(ValueError, "absence_conflicts"):
            run.derive_source_absence(info, {}, {("SH600000", DAYS[0]): {}}, DAYS, DAYS[3:],
                {"SH600000": (DAYS[0],)}, REF)

    def legacy(self, source_symbols=(), raw=None):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "instruments").mkdir()
            (root / "instruments/all.txt").write_text(
                "SH600929\t2018-03-26\t2026-08-28\nSH600825\t2000-01-04\t2026-09-04\n")
            (root / "instruments/csi300.txt").write_text(
                "SH600929\t2026-08-31\t2026-09-04\nSH600825\t2026-08-31\t2026-09-04\n")
            with patch("indices.INDEX_MAP", {"csi300": "399300.SZ"}):
                return run.legacy_index_absence(root, DAYS[2], DAYS[3:], raw or {}, source_symbols, REF)

    def test_legacy_class_never_includes_old_active(self):
        proof = self.legacy()
        self.assertEqual(proof["dates"], {"SH600929": DAYS[3:]})
        self.assertNotIn("SH600825", proof["lastQuotedDates"])

    def test_resumed_legacy_returns_to_update_universe(self):
        self.assertEqual(self.legacy(source_symbols=["SH600929"])["dates"], {})

    def test_legacy_claim_conflicting_with_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "legacy_absence_conflicts"):
            self.legacy(raw={("SH600929", DAYS[-1]): {}})


if __name__ == "__main__":
    unittest.main()
