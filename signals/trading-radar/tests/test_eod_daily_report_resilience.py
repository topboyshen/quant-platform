"""Daily report must degrade visibly (never crash) on empty/short/missing macro collector fields.

Regression for 2026-10-08 15:50: margin_balance/margin_growth_pct were stale with
message "" -> `"".splitlines()[0]` raised IndexError, no report, no outbox, no alert.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from radar.swing import daily_report as report
from tests.test_eod_daily_report import _build, _decision, _result, _run


def _seed_extra(extra):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-partial", status="partial", reason_codes='["macro_data_partial"]')
        _decision(connection, "d-partial", "run-partial")
        connection.execute(
            "UPDATE macro_slice SET extra=? WHERE date='2026-09-18'",
            (extra if isinstance(extra, str) else json.dumps(extra),),
        )
    return seed


def _partial_result():
    return _result(run_id="run-partial", status="partial", reason_codes=["macro_data_partial"])


def test_2026_10_08_incident_empty_message_stale_factors(tmp_path):
    extra = {"collect_status": {
        "total_amount": {"available": True, "source": "tencent", "message": "", "observed_date": "2026-09-18",
                         "stale": False, "anomalous": False},
        "margin_balance": {"available": True, "source": "akshare", "message": "", "observed_date": "2026-09-30",
                           "stale": True, "anomalous": False},
        "margin_growth_pct": {"available": True, "source": "akshare", "message": "", "observed_date": "2026-09-30",
                              "stale": True, "anomalous": False},
        "limit_ratio": {"available": True, "source": "akshare", "message": "", "observed_date": "2026-09-18",
                        "stale": False, "anomalous": False},
    }}
    payload = _build(tmp_path, _seed_extra(extra), _partial_result())
    assert payload["reading"]["macro_missing"] == [
        "margin_balance（akshare：数据过期（观测日 2026-09-30））",
        "margin_growth_pct（akshare：数据过期（观测日 2026-09-30））",
    ]
    assert "部分缺失" in payload["text"] and "margin_balance" in payload["text"]
    assert "total_amount" not in payload["reading"]["macro"]


@pytest.mark.parametrize("message", ["", None, "   ", "\n", "\n\n", 0, False])
def test_blank_or_missing_message_never_raises_and_gap_stays_visible(tmp_path, message):
    extra = {"collect_status": {"total_amount": {
        "available": False, "source": "tencent", "message": message, "stale": False, "anomalous": False}}}
    payload = _build(tmp_path, _seed_extra(extra), _partial_result())
    assert payload["reading"]["macro_missing"] == ["total_amount（tencent：不可用）"]
    assert "部分缺失" in payload["reading"]["macro"]


def test_missing_message_key_and_missing_optional_fields(tmp_path):
    extra = {"collect_status": {
        "margin_balance": {"available": True, "stale": True},                       # no message/source/observed
        "limit_ratio": {"available": True, "anomalous": True, "source": "akshare"},  # anomalous, no message
        "total_amount": {},                                                           # no 'available' at all
    }}
    payload = _build(tmp_path, _seed_extra(extra), _partial_result())
    assert payload["reading"]["macro_missing"] == [
        "total_amount（unknown：不可用）",
        "margin_balance（unknown：数据过期）",
        "limit_ratio（akshare：数据异常）",
    ]


def test_multiline_message_uses_first_nonblank_line_and_is_truncated(tmp_path):
    extra = {"collect_status": {"total_amount": {
        "available": False, "source": "tencent", "stale": False, "anomalous": False,
        "message": "\n  \nConnection aborted " + "x" * 200 + "\nsecond line"}}}
    payload = _build(tmp_path, _seed_extra(extra), _partial_result())
    (entry,) = payload["reading"]["macro_missing"]
    assert entry.startswith("total_amount（tencent：Connection aborted ")
    assert "second line" not in entry
    assert len(entry) < 120


@pytest.mark.parametrize("extra", ["{}", "null", "[]", '{"collect_status": null}', '{"collect_status": []}',
                                   '{"collect_status": {}}', "not json", None])
def test_short_or_absent_collect_status_is_not_a_crash(tmp_path, extra):
    payload = _build(tmp_path, _seed_extra(extra if extra is not None else "{}"), _partial_result())
    assert payload["reading"]["macro_missing"] == []
    assert payload["text"].startswith("EOD 日报")


def test_missing_macro_slice_is_reported_not_crash(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        _run(connection, "run-partial", status="partial", reason_codes='["macro_data_partial"]')
        connection.execute("DELETE FROM macro_slice")
    payload = _build(tmp_path, seed, _partial_result())
    assert payload["reading"]["macro_missing"] == ["当日宏观切片缺失"]
    assert "宏观切片：当日缺失" in payload["text"]


def test_complete_factors_report_complete(tmp_path):
    ok = {"available": True, "source": "akshare", "message": "", "stale": False, "anomalous": False}
    extra = {"collect_status": {name: dict(ok) for name in report.FACTORS}}
    payload = _build(tmp_path, _seed_extra(extra), _partial_result())
    assert payload["reading"]["macro_missing"] == []
    assert payload["reading"]["macro"] == "宏观数据：完整"


def test_empty_run_rows_and_no_decisions_still_build(tmp_path):
    def seed(connection: sqlite3.Connection) -> None:
        pass  # no swing_eod_run / decisions at all
    result = _result(run_id="absent", status="failed")
    result["ok"] = False
    payload = _build(tmp_path, seed, result)
    assert payload["terminal_ok"] is False
    assert "不支持推荐" in payload["conclusion"]


@pytest.mark.parametrize("value,limit,expected", [
    ("", 60, ""), (None, 60, ""), ("   \n \n", 60, ""), ("a\nb", 60, "a"), ("\n\n b ", 60, "b"),
    ("x" * 100, 10, "x" * 10), (123, 60, "123"),
])
def test_first_line_helper(value, limit, expected):
    assert report._first_line(value, limit) == expected
