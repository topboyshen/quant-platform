#!/usr/bin/env python3
"""Deterministic P0 acceptance tests for the central eligibility contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import research_eligibility as gate


SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_NOW = datetime(2026, 8, 15, 18, 56, tzinfo=SHANGHAI)


def _weekday_sessions(start: date, end: date, excluded: set[date] | None = None) -> list[date]:
    excluded = excluded or set()
    result = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in excluded:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def _calendar_payload(
    *,
    now: datetime = BASE_NOW,
    sessions: list[date] | None = None,
) -> dict[str, object]:
    sessions = sessions or _weekday_sessions(date(2022, 1, 3), date(2026, 12, 31))
    return {
        "schema_version": 1,
        "source": "akshare.tool_trade_date_hist_sina",
        "generated_at": (now - timedelta(days=1)).isoformat(),
        "count": len(sessions),
        "min_date": sessions[0].isoformat(),
        "max_date": sessions[-1].isoformat(),
        "trading_dates": [item.isoformat() for item in sessions],
    }


def _write_payload(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class CalendarMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot(
        self,
        now: datetime,
        *,
        sessions: list[date] | None = None,
        payload: dict[str, object] | None = None,
    ) -> gate.TradingCalendarSnapshot:
        target = self.root / "calendar.json"
        _write_payload(target, payload or _calendar_payload(now=now, sessions=sessions))
        return gate.load_trading_calendar(target, now)

    def test_cases_01_to_13_trading_day_boundaries(self) -> None:
        monday = date(2026, 8, 17)
        friday = date(2026, 8, 14)
        cases = (
            (time(0, 0), friday),
            (time(8, 30), friday),
            (time(9, 29, 59), friday),
            (time(9, 30), friday),
            (time(11, 30), friday),
            (time(12, 0), friday),
            (time(13, 0), friday),
            (time(14, 59, 59), friday),
            (time(15, 0), friday),
            (time(15, 4, 59), friday),
            (time(15, 5), monday),
            (time(15, 5, 1), monday),
            (time(23, 59), monday),
        )
        for index, (clock, expected) in enumerate(cases, 1):
            now = datetime.combine(monday, clock, tzinfo=SHANGHAI)
            snapshot = self._snapshot(now)
            with self.subTest(case=index):
                self.assertTrue(snapshot.valid, snapshot.error_code)
                self.assertEqual(gate.expected_last_closed_trade_date(snapshot, now), expected)

    def test_cases_14_to_22_weekends_and_holidays(self) -> None:
        excluded = {
            date(2026, 10, 1),
            date(2026, 10, 2),
            date(2026, 10, 5),
            date(2026, 10, 6),
            date(2026, 10, 7),
        }
        sessions = _weekday_sessions(date(2022, 1, 3), date(2026, 12, 31), excluded)
        friday = date(2026, 8, 14)
        holiday_prior = date(2026, 9, 30)
        reopen = date(2026, 10, 8)
        cases = (
            (datetime(2026, 8, 15, 0, 0, tzinfo=SHANGHAI), friday),
            (datetime(2026, 8, 15, 23, 59, tzinfo=SHANGHAI), friday),
            (datetime(2026, 8, 16, 8, 30, tzinfo=SHANGHAI), friday),
            (datetime(2026, 8, 16, 23, 59, tzinfo=SHANGHAI), friday),
            (datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI), holiday_prior),
            (datetime(2026, 10, 4, 9, 0, tzinfo=SHANGHAI), holiday_prior),
            (datetime(2026, 10, 7, 23, 59, tzinfo=SHANGHAI), holiday_prior),
            (datetime(2026, 10, 8, 8, 30, tzinfo=SHANGHAI), holiday_prior),
            (datetime(2026, 10, 8, 15, 5, tzinfo=SHANGHAI), reopen),
        )
        for index, (now, expected) in enumerate(cases, 14):
            snapshot = self._snapshot(now, sessions=sessions)
            with self.subTest(case=index):
                self.assertTrue(snapshot.valid, snapshot.error_code)
                self.assertEqual(gate.expected_last_closed_trade_date(snapshot, now), expected)

    def test_cases_23_to_36_calendar_fail_closed(self) -> None:
        target = self.root / "calendar.json"
        valid = _calendar_payload()

        # 23 missing file
        snapshot = gate.load_trading_calendar(target, BASE_NOW)
        self.assertFalse(snapshot.valid)
        self.assertIsNone(gate.expected_last_closed_trade_date(snapshot, BASE_NOW))

        # 24 non-regular path (portable substitute for unreadable file)
        target.mkdir()
        snapshot = gate.load_trading_calendar(target, BASE_NOW)
        self.assertFalse(snapshot.valid)
        target.rmdir()

        mutations: list[tuple[int, object]] = [
            (25, "{bad json"),
            (26, []),
            (27, {**valid, "schema_version": 2}),
            (28, {**valid, "source": "unknown"}),
            (29, {**valid, "generated_at": "2026-08-14T01:00:00"}),
            (30, {**valid, "generated_at": (BASE_NOW + timedelta(minutes=6)).isoformat()}),
            (31, {**valid, "generated_at": (BASE_NOW - timedelta(days=15)).isoformat()}),
            (32, {**valid, "count": 1, "min_date": "2026-08-14", "max_date": "2026-08-14", "trading_dates": ["2026-08-14"]}),
        ]
        reversed_dates = list(reversed(valid["trading_dates"]))
        mutations.append((33, {**valid, "trading_dates": reversed_dates}))
        weekend_dates = list(valid["trading_dates"])
        weekend_dates[-2] = "2026-12-26"
        mutations.append((34, {**valid, "trading_dates": weekend_dates}))
        mutations.append((35, {**valid, "count": int(valid["count"]) + 1}))
        cutoff = date(2026, 8, 13)
        old_sessions = _weekday_sessions(date(2022, 1, 3), cutoff)
        mutations.append((36, _calendar_payload(sessions=old_sessions)))

        for case, payload in mutations:
            if isinstance(payload, str):
                target.write_text(payload, encoding="utf-8")
            else:
                _write_payload(target, payload)
            snapshot = gate.load_trading_calendar(target, BASE_NOW)
            with self.subTest(case=case):
                self.assertFalse(snapshot.valid, snapshot.error_code)
                self.assertIsNone(gate.expected_last_closed_trade_date(snapshot, BASE_NOW))

    def test_cases_37_to_40_quote_integration(self) -> None:
        weekend = datetime(2026, 8, 15, 18, 56, tzinfo=SHANGHAI)
        weekend_calendar = self._snapshot(weekend)
        friday_close = datetime(2026, 8, 14, 15, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            gate.classify_closed_quote(weekend_calendar, weekend, friday_close, 26.91)[0],
            "fresh_close",
        )

        monday_intraday = datetime(2026, 8, 17, 11, 30, tzinfo=SHANGHAI)
        intraday_calendar = self._snapshot(monday_intraday)
        self.assertEqual(
            gate.classify_closed_quote(intraday_calendar, monday_intraday, friday_close, 26.91)[0],
            "fresh_close",
        )

        monday_after_close = datetime(2026, 8, 17, 15, 5, tzinfo=SHANGHAI)
        close_calendar = self._snapshot(monday_after_close)
        monday_close = datetime(2026, 8, 17, 14, 56, tzinfo=SHANGHAI)
        self.assertEqual(
            gate.classify_closed_quote(close_calendar, monday_after_close, monday_close, 27.10)[0],
            "fresh_close",
        )
        self.assertEqual(
            gate.classify_closed_quote(close_calendar, monday_after_close, friday_close, 26.91)[0],
            "stale",
        )

        self.assertEqual(
            gate.classify_closed_quote(close_calendar, monday_after_close, None, None)[0],
            "missing",
        )


class EligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calendar_path = self.root / "calendar.json"
        _write_payload(self.calendar_path, _calendar_payload())
        self.calendar = gate.load_trading_calendar(self.calendar_path, BASE_NOW)
        self.good = {
            "symbol": "600875",
            "calendar": self.calendar,
            "generated_at": BASE_NOW,
            "quote_observed_at": datetime(2026, 8, 14, 15, 0, tzinfo=SHANGHAI),
            "quote_price": 26.91,
            "quote_source": "腾讯",
            "market_data_lineage_complete": True,
            "strategy_hash": "a" * 64,
            "backtest_hash": "a" * 64,
            "strategy_fingerprint_complete": True,
            "backtest_id": 1,
            "indicator_id": 1,
            "expected_indicator_id": 1,
            "benchmark_present": True,
            "backtest_reproducible": True,
            "lookahead_proven": True,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_complete_p0_evidence_is_research_only_until_p2_and_review(self) -> None:
        decision = gate.evaluate_eligibility(**self.good)
        self.assertEqual(decision.qualification, "RESEARCH_ONLY")
        self.assertEqual(decision.reason_codes, ())
        self.assertIn("GATE_P2_VALIDATION", decision.advisory_codes)

    def test_each_hard_gate_fails_closed_and_order_is_stable(self) -> None:
        broken = dict(self.good)
        broken.update(
            quote_observed_at=None,
            quote_source=None,
            market_data_lineage_complete=False,
            strategy_hash="bad",
            backtest_hash=None,
            strategy_fingerprint_complete=False,
            backtest_id=None,
            indicator_id=None,
            expected_indicator_id=1,
            benchmark_present=False,
            backtest_reproducible=False,
            lookahead_proven=False,
        )
        decision = gate.evaluate_eligibility(**broken)
        self.assertEqual(decision.qualification, "BLOCKED")
        self.assertEqual(
            decision.reason_codes,
            (
                "GATE_MKT_FRESH",
                "GATE_DATA_LINEAGE",
                "GATE_STRATEGY_VERSION",
                "GATE_INDICATOR_ID",
                "GATE_BACKTEST_BASELINE",
                "GATE_BACKTEST_REPRO",
                "GATE_LOOKAHEAD",
            ),
        )

    def test_high_attention_cannot_override_gate(self) -> None:
        broken = dict(self.good)
        broken["backtest_id"] = None
        decision = gate.evaluate_eligibility(**broken)
        self.assertEqual(decision.qualification, "BLOCKED")
        self.assertNotEqual(decision.workflow, "MANUAL_REVIEW")

    def test_ai_stale_is_advisory_not_a_hard_gate(self) -> None:
        decision = gate.evaluate_eligibility(**self.good, ai_stale=True)
        self.assertEqual(decision.qualification, "RESEARCH_ONLY")
        self.assertIn("AI_STALE", decision.advisory_codes)

    def test_append_only_audit_never_overwrites(self) -> None:
        payload = {"rule": gate.GATE_RULE_VERSION, "decisions": []}
        first = gate.write_audit_record(self.root / "audit", payload, generated_at=BASE_NOW)
        second = gate.write_audit_record(self.root / "audit", payload, generated_at=BASE_NOW)
        self.assertNotEqual(first, second)
        self.assertEqual(len(list((self.root / "audit").glob("*.json"))), 2)
        self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["rule"], gate.GATE_RULE_VERSION)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("P0_ACCEPTANCE=PASS calendar_cases=40 hard_gate_leaks=0")
    raise SystemExit(0 if result.wasSuccessful() else 1)

