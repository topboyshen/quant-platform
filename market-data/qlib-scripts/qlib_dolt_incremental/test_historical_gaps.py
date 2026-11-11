import unittest
from unittest.mock import patch

import run


class Gaps(unittest.TestCase):
    def setUp(self):
        self.calendar = ["2026-09-02", "2026-09-03", "2026-09-04"]
        self.info = {"SZ301266": {"last_date": self.calendar[0], "gap_dates": self.calendar[1:]}}
        self.rows = {}

    def call(self, returned):
        with patch.object(run, "fetch_prices", return_value=returned) as fetch:
            result = run.fetch_historical_gaps(object(), self.info, self.calendar, self.rows)
            self.assertIn("tradedate <= '2026-09-04'", fetch.call_args.args[1])
            return result

    def test_empty_complete_response_proves_only_old_gap(self):
        missing, proof = self.call([])
        self.assertEqual(missing, {"SZ301266": tuple(self.calendar[1:])})
        self.assertEqual(proof["SZ301266"]["meaning"], "source_absent_not_confirmed_suspension")

    def test_real_historical_rows_not_replaced_by_missing(self):
        row = {"symbol": "SZ301266", "tradedate": "2026-09-03"}
        missing, _ = self.call([row])
        self.assertEqual(missing["SZ301266"], ("2026-09-04",))
        self.assertEqual(self.rows[("SZ301266", "2026-09-03")], row)

    def test_complete_real_historical_rows_need_no_absence_claim(self):
        rows = [{"symbol": "SZ301266", "tradedate": day} for day in self.calendar[1:]]
        missing, proof = self.call(rows)
        self.assertEqual(missing, {})
        self.assertEqual(proof["SZ301266"]["absentDates"], [])

    def test_new_day_cannot_be_gap(self):
        self.info["SZ301266"]["gap_dates"].append("2026-09-07")
        with self.assertRaisesRegex(ValueError, "invalid_historical_gap"):
            self.call([])

    def test_extra_symbol_or_day_rejected(self):
        for row in ({"symbol": "SZ000001", "tradedate": "2026-09-03"},
                    {"symbol": "SZ301266", "tradedate": "2026-09-07"}):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, "outside_request"):
                self.call([row])

    def test_conflicting_absence_rejected(self):
        self.rows[("SZ301266", "2026-09-03")] = {"close": "20"}
        with self.assertRaisesRegex(ValueError, "absence_disagrees"):
            self.call([])

    def test_duplicate_dates_rejected(self):
        row = {"symbol": "SZ301266", "tradedate": "2026-09-03"}
        with self.assertRaisesRegex(ValueError, "outside_request"):
            self.call([row, row])

    def test_query_failure_not_converted_to_empty_evidence(self):
        with patch.object(run, "fetch_prices", side_effect=run.SourceError("query_not_success")):
            with self.assertRaises(run.SourceError):
                run.fetch_historical_gaps(object(), self.info, self.calendar, self.rows)


if __name__ == "__main__":
    unittest.main()
