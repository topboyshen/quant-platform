"""Offline admission tests for complete, pinned old-anchor intervals."""

from datetime import date, timedelta
import unittest
from unittest.mock import patch

import run


REF = "a" * 32
SYMBOL = "SZ301266"
DAYS = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


class Client:
    def latest_ref(self):
        return REF


def row(day, *, symbol=SYMBOL, volume="100", close="103"):
    return dict(tradedate=day, symbol=symbol, open="102", high="110", low="100",
                close=close, adjclose="51.5", volume=volume, amount="10000")


def mapping(rows):
    return {(value["symbol"], value["tradedate"]): value for value in rows}


class AnchorIntervals(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.info = {SYMBOL: dict(new=False, short_history=False,
                                 last3_valid_dates=DAYS[-3:], last_date=DAYS[-1])}

    def call(self, raw, returned=(), calendar=None):
        with patch.object(run, "fetch_prices", return_value=list(returned)) as fetch:
            run.verify_anchor_intervals(self.client, self.info, calendar or DAYS, raw)
            return fetch

    def earlier(self):
        self.info[SYMBOL]["last3_valid_dates"] = [DAYS[0], DAYS[2], DAYS[3]]

    def full_earlier_rows(self):
        return [row(day, volume="0" if day in (DAYS[1], DAYS[4]) else "100") for day in DAYS]

    def test_recent_complete_bulk_needs_no_extra_query(self):
        raw = mapping([row(day) for day in DAYS[-3:]])
        before = dict(raw)
        fetch = self.call(raw)
        fetch.assert_not_called()
        self.assertEqual(raw, before)

    def test_early_anchors_query_entire_range_including_pause_slots(self):
        self.earlier()
        rows = self.full_earlier_rows()
        raw = mapping(rows[2:])
        fetch = self.call(raw, rows)
        fetch.assert_called_once_with(
            self.client,
            "symbol = 'SZ301266' AND tradedate >= '2026-08-31' AND tradedate <= '2026-09-04'",
        )
        self.assertEqual(raw, mapping(rows))

    def test_old_nan_tail_now_has_valid_source_close_is_rejected(self):
        # Previously this admitted the generation and computed 105/103 - 1
        # instead of 105/104 - 1 on the first new date. An old missing price
        # becoming valid is a historical revision, not an append-only update.
        self.info[SYMBOL]["last3_valid_dates"] = DAYS[1:4]
        rows = [row(day) for day in DAYS[1:4]] + [row(DAYS[4], close="104")]
        raw = mapping(rows[1:])
        with self.assertRaisesRegex(ValueError, "source_revises_old_valid_dates"):
            self.call(raw, rows)

    def test_old_interior_pause_now_valid_is_rejected(self):
        self.earlier()
        rows = self.full_earlier_rows()
        rows[1] = row(DAYS[1])
        with self.assertRaisesRegex(ValueError, "source_revises_old_valid_dates"):
            self.call(mapping(rows[2:]), rows)

    def test_missing_old_valid_row_is_rejected(self):
        raw = mapping([row(day) for day in DAYS[-2:]])
        with self.assertRaisesRegex(ValueError, "source_revises_old_valid_dates"):
            self.call(raw)

    def test_old_valid_row_now_zero_volume_is_rejected(self):
        rows = [row(day, volume="0" if day == DAYS[3] else "100") for day in DAYS[-3:]]
        with self.assertRaisesRegex(ValueError, "source_revises_old_valid_dates"):
            self.call(mapping(rows))

    def test_exhaustive_range_cannot_omit_an_already_observed_row(self):
        self.earlier()
        rows = self.full_earlier_rows()
        with self.assertRaisesRegex(ValueError, "anchor_range_absence_disagrees"):
            self.call(mapping(rows[2:]), rows[:-1])

    def test_same_commit_conflicting_row_is_rejected(self):
        self.earlier()
        rows = self.full_earlier_rows()
        raw = mapping(rows[2:])
        raw[(SYMBOL, DAYS[2])] = row(DAYS[2], close="999")
        with self.assertRaisesRegex(ValueError, "fixed_source_row_disagrees"):
            self.call(raw, rows)

    def test_outside_symbol_range_calendar_or_duplicate_is_rejected(self):
        self.earlier()
        cases = (
            row(DAYS[0], symbol="SH600000"),
            row("2026-08-28"),
            row("2026-09-07"),
        )
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "anchor_response_outside_request"):
                self.call({}, self.full_earlier_rows() + [extra])
        rows = self.full_earlier_rows()
        with self.assertRaisesRegex(ValueError, "anchor_response_outside_request"):
            self.call({}, rows + [rows[0]])

    def test_date_inside_bounds_but_not_exchange_calendar_rejected(self):
        calendar = ["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"]
        self.info[SYMBOL] = dict(new=False, short_history=False,
                                last3_valid_dates=calendar[:3], last_date=calendar[-1])
        with self.assertRaisesRegex(ValueError, "anchor_response_outside_request"):
            self.call({}, [row("2026-08-29")], calendar=calendar)

    def test_numeric_schema_checked_before_using_positive_volume(self):
        raw = mapping([row(day) for day in DAYS[-3:]])
        raw[(SYMBOL, DAYS[-1])]["close"] = "nan"
        with self.assertRaisesRegex(ValueError, "nonfinite_market_number"):
            self.call(raw)

    def test_query_failure_is_not_treated_as_proof_of_absence(self):
        self.earlier()
        with patch.object(run, "fetch_prices", side_effect=run.SourceError("query_not_success")):
            with self.assertRaises(run.SourceError):
                run.verify_anchor_intervals(self.client, self.info, DAYS, {})

    def test_long_old_anchor_interval_requires_manual_review_before_query(self):
        calendar = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(181)]
        self.info[SYMBOL]["last3_valid_dates"] = [calendar[0], calendar[-2], calendar[-1]]
        self.info[SYMBOL]["last_date"] = calendar[-1]
        with patch.object(run, "fetch_prices") as fetch:
            with self.assertRaisesRegex(ValueError, "requires_manual_review"):
                run.verify_anchor_intervals(self.client, self.info, calendar, {})
            fetch.assert_not_called()

    def test_malformed_old_anchor_order_is_rejected_before_query(self):
        for dates in (DAYS[-2:], DAYS[-3:][::-1], [DAYS[2], DAYS[2], DAYS[4]]):
            self.info[SYMBOL]["last3_valid_dates"] = dates
            with self.subTest(dates=dates), self.assertRaisesRegex(ValueError, "invalid_old_anchor_interval"):
                self.call({})

    def test_new_and_short_symbols_use_their_separate_full_history_contract(self):
        self.info = {"SZ001999": {"new": True}, SYMBOL: {"short_history": True}}
        self.call({}).assert_not_called()

    def test_other_symbols_and_new_days_do_not_enter_old_interval(self):
        rows = [row(day) for day in DAYS[-3:]] + [row(DAYS[-1], symbol="SH600000"), row("2026-09-07")]
        raw = mapping(rows)
        before = dict(raw)
        self.call(raw).assert_not_called()
        self.assertEqual(raw, before)


if __name__ == "__main__":
    unittest.main()
