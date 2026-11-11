"""Synthetic, read-only projection tests; no provider, real database, or EOD run."""
import copy
import json
import sqlite3

import pytest

from dashboard import etf_pool
from tests.test_etf_pool import DATE, load
from tests.test_etf_pool import sample as sample


@pytest.fixture
def full(sample):
    config, con, audit = sample
    con.execute("CREATE TABLE swing_eod_run (id INTEGER PRIMARY KEY,run_id TEXT,decision_date TEXT,status TEXT,"
                "started_at TEXT,completed_at TEXT,target_count INTEGER,evaluated_count INTEGER,failed_count INTEGER,"
                "signal_count INTEGER,allowed_signal_count INTEGER,blocked_signal_count INTEGER,details TEXT)")
    for definition in ("decision_id TEXT", "decision_at TEXT", "raw_signals_json TEXT", "validation_split TEXT"):
        con.execute("ALTER TABLE eod_target_decision ADD COLUMN " + definition)
    con.execute("INSERT INTO swing_eod_run VALUES (100,'latest',?,'success',?, ?,10,10,0,0,0,0,'{}')",
                (DATE, DATE+'T15:30:00+08:00', audit['run']['completed_at']))
    return sample


def diagnostic(code):
    return {"code": code,
            "data": {"ready": True, "rows": 250, "minimum_rows": 65, "data_date": DATE,
                     "required_date": DATE, "reason_codes": []},
            "daily": {"evaluated": True, "reason_codes": ["close_not_above_ma", "volume_below_threshold"],
                      "ma_period": 60, "previous_close": .55, "close": .547, "previous_ma": .6, "ma": .6,
                      "previous_at_or_below_ma": True, "close_above_ma": False, "price_cross": False,
                      "volume": 100, "average_volume20": 100, "volume_ratio_raw": 1.0, "volume_ratio": 1.0,
                      "volume_threshold": 1.5, "volume_ok": False, "triggered": False},
            "weekly": {"evaluated": True, "reason_codes": ["no_weekly_golden_cross"], "completed_bars": 40,
                       "minimum_bars": 35, "previous_week_date": "2026-09-04", "week_date": DATE,
                       "previous_dif": .1, "previous_dea": .2, "dif": .1, "dea": .2,
                       "previous_at_or_below": True, "now_above": False, "golden_cross": False, "triggered": False}}


def record(full, mutate=None):
    config, con, _ = full
    raw = {"schema_version": "quant.swing-trigger-diagnostics.v1", "run_id": "latest", "decision_date": DATE,
           "targets": [diagnostic(t['code']) for t in config['targets']]}
    if mutate:
        mutate(raw)
    con.execute("UPDATE swing_eod_run SET details=? WHERE run_id='latest'", (json.dumps({"trigger_diagnostics": raw}),))


def historical(full, *, rid="old", decision_date="2026-08-21", data_date=None, bar="2026-08-21",
               code="510300", action="blocked", kind="weekly_macd_golden_cross", ident=1):
    _, con, _ = full
    data_date = data_date or decision_date
    con.execute("INSERT INTO swing_eod_run VALUES (?,?,?,?,?,?,10,10,0,1,?,?, '{}')",
                (ident, rid, decision_date, "success", decision_date+'T15:30:00+08:00',
                 decision_date+'T16:00:00+08:00', int(action == 'allowed'), int(action == 'blocked')))
    con.execute("INSERT INTO eod_risk_snapshot VALUES (?,?, 'critical',?)", (rid, 'risk-'+rid, decision_date+'T15:35:00+08:00'))
    signal = {"signal_type": kind, "period": etf_pool.SIGNAL_PERIODS[kind], "title": "PRIVATE_SECRET",
              "detail": {"data_date": bar, "token": "PRIVATE_SECRET"}}
    con.execute("INSERT INTO eod_target_decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, code, decision_date, data_date, 'ok', action, 1, action, 'critical', 'signal_allowed',
                 .5, 'risk-'+rid, 'decision-'+rid, decision_date+'T15:35:00+08:00', json.dumps([signal]), 'live_oos'))


def test_old_run_reports_not_recorded_without_false_conditions(full):
    item = load(full)['items'][0]
    assert item['trigger_diagnostics']['status'] == 'not_recorded'
    assert item['trigger_diagnostics']['daily'] is None
    assert item['history']['status'] == 'none_in_window'
    assert item['state'] == 'no_trigger'


def test_valid_current_diagnostic_whitelist_and_normalized_timestamp(full):
    record(full, lambda raw: raw['targets'][0]['daily'].update(secret="PRIVATE_SECRET"))
    full[2]['run']['completed_at'] = '2026-09-11T07:48:20Z'
    result = load(full)
    assert all(i['trigger_diagnostics']['status'] == 'recorded' for i in result['items'])
    assert result['items'][0]['trigger_diagnostics']['daily']['volume_ratio'] == 1
    assert 'PRIVATE_SECRET' not in json.dumps(result)


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(run_id='another-run'),
    lambda r: r.update(decision_date='2026-09-10'),
    lambda r: r['targets'].append(copy.deepcopy(r['targets'][0])),
    lambda r: r['targets'][0].update(code='588999'),
    lambda r: r.update(targets=r['targets'] * 21),
])
def test_unbound_or_unbounded_whole_payload_is_invalid(full, mutation):
    record(full, mutation)
    assert all(i['trigger_diagnostics']['status'] == 'invalid' for i in load(full)['items'])


@pytest.mark.parametrize('section,key,value', [
    ('daily', 'close', float('nan')), ('daily', 'close', float('inf')),
    ('daily', 'volume_ratio', 1.6), ('daily', 'volume_ok', True),
    ('daily', 'close_above_ma', True), ('weekly', 'golden_cross', True),
    ('weekly', 'week_date', '2026-09-18'), ('data', 'required_date', '2026-09-10'),
    ('data', 'rows', True), ('daily', 'reason_codes', ['PRIVATE_SECRET']),
    ('daily', 'triggered', True), ('weekly', 'minimum_bars', 20),
    ('daily', 'reason_codes', []), ('weekly', 'reason_codes', []),
    ('data', 'reason_codes', ['data_date_mismatch']),
])
def test_bad_values_never_expose_raw_details_or_change_decision(full, section, key, value):
    record(full, lambda raw: raw['targets'][0][section].update({key: value}))
    result = load(full)
    item = result['items'][0]
    assert item['trigger_diagnostics']['status'] == 'invalid'
    assert item['trigger_diagnostics']['daily'] is None
    assert item['state'] == 'no_trigger' and item['raw_trigger_count'] == 0
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_omitted_target_remains_not_recorded(full):
    record(full, lambda raw: raw['targets'].pop(0))
    items = load(full)['items']
    assert items[0]['trigger_diagnostics']['status'] == 'not_recorded'
    assert items[1]['trigger_diagnostics']['status'] == 'recorded'


def test_missing_volume_is_unknown_not_a_false_trigger(full):
    def mutate(raw):
        raw['targets'][0]['daily'].update(evaluated=False, reason_codes=['volume_missing', 'close_not_above_ma'],
                                        volume=None, average_volume20=None, volume_ratio=None, volume_ratio_raw=None,
                                        volume_ok=None, triggered=None)
    record(full, mutate)
    item = load(full)['items'][0]['trigger_diagnostics']
    assert item['status'] == 'recorded' and item['daily']['triggered'] is None
    assert item['daily']['price_cross'] is False


def test_details_byte_limit_and_duplicate_json_keys(full):
    full[1].execute("UPDATE swing_eod_run SET details=?", ('x'*(etf_pool.MAX_DIAGNOSTIC_BYTES+1),))
    assert load(full)['items'][0]['trigger_diagnostics']['status'] == 'invalid'
    full[1].execute("UPDATE swing_eod_run SET details=?", ('{"trigger_diagnostics":null,"trigger_diagnostics":{}}',))
    assert load(full)['items'][0]['trigger_diagnostics']['status'] == 'invalid'


def test_latest_weekly_history_retains_bar_and_first_observation_not_repeat_date(full):
    historical(full)
    historical(full, rid='repeat', decision_date='2026-08-25', ident=2)
    result = load(full)
    history = result['items'][0]['history']
    assert history['status'] == 'available'
    event = history['latest_raw_trigger']
    assert event['signal_data_date'] == event['first_observed_date'] == '2026-08-21'
    assert event['target_risk_action'] == 'blocked'
    assert history['latest_allowed_target'] is None
    assert result['items'][0]['state'] == 'no_trigger'
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_deduplicated_zero_retained_run_still_has_raw_history(full):
    historical(full, action='allowed')
    full[1].execute("UPDATE swing_eod_run SET signal_count=0,allowed_signal_count=0,blocked_signal_count=0 WHERE run_id='old'")
    history = load(full)['items'][0]['history']
    assert history['latest_raw_trigger']['signal_data_date'] == '2026-08-21'
    assert history['latest_allowed_target']['target_risk_action'] == 'allowed'


def test_ancient_friday_is_not_a_new_qualified_weekly_trigger(full):
    historical(full, bar='2026-07-31')
    assert load(full)['items'][0]['history']['latest_raw_trigger'] is None


def test_real_synthetic_strategy_output_passes_api_schema(full):
    import pandas as pd

    from radar.swing.trend_strategy import TrendConfirmStrategy
    frame = pd.DataFrame({'date': pd.bdate_range(end=DATE, periods=250),
                          'close': [.547] * 250, 'volume': [100.0] * 250})
    class Fetcher:
        def get_historical_data(self, code, **kwargs):
            return frame.copy()
    strategy = TrendConfirmStrategy(Fetcher(), required_data_date=DATE)
    assert strategy.evaluate('510300') == []
    record(full, lambda raw: raw['targets'].__setitem__(0, copy.deepcopy(strategy.last_diagnostics)))
    result = load(full)['items'][0]['trigger_diagnostics']
    assert result['status'] == 'recorded'
    assert result['weekly']['triggered'] is False


def test_partially_unavailable_weekly_numbers_remain_unknown(full):
    def mutate(raw):
        raw['targets'][0]['weekly'].update(evaluated=False, reason_codes=['weekly_values_unavailable'],
                                          dif=None, previous_at_or_below=None, now_above=None,
                                          golden_cross=None, triggered=None)
    record(full, mutate)
    result = load(full)['items'][0]['trigger_diagnostics']
    assert result['status'] == 'recorded' and result['weekly']['triggered'] is None


@pytest.mark.parametrize('case', ['positive', 'negative', 'missing_volume', 'short_weeks', 'stale_data'])
def test_actual_producer_diagnostic_through_run_details_to_pool(full, case):
    from tests.test_swing_trigger_diagnostics import frame, strategy
    data = frame(170 if case == 'short_weeks' else 175, end='2026-09-10' if case == 'stale_data' else DATE)
    if case == 'negative':
        data['close'] = 10.0
    if case == 'missing_volume':
        data = data.drop(columns=['volume'])
    producer = strategy(data, required_data_date=DATE)
    signals = producer.evaluate('510300')
    item = producer.last_diagnostics
    # This matches the existing EOD target writer's input and audit contract;
    # the test does not invoke EOD, a provider, or any production database.
    ready = producer.last_data_ok
    state = 'data_failed' if not ready else 'blocked' if signals else 'no_trigger'
    action = 'blocked' if signals else 'not_applicable'
    full[1].execute("UPDATE eod_target_decision SET data_status=?,decision_state=?,risk_action=?,"
                    "raw_trigger_count=?,reference_close=?,data_date=? WHERE run_id='latest' AND code='510300'",
                    ('ok' if ready else 'failed', state, action, len(signals),
                     producer.last_close if ready else None, producer.last_data_date))
    full[2]['decision'].update(signal_count=len(signals), allowed_signal_count=0, blocked_signal_count=len(signals))
    if not ready:
        full[2]['coverage'].update(evaluated_count=9, failed_count=1)
        full[2]['run']['status'] = 'partial'
        full[1].execute("UPDATE swing_eod_run SET status='partial' WHERE run_id='latest'")
    record(full, lambda raw: raw['targets'].__setitem__(0, copy.deepcopy(item)))
    projected = load(full)['items'][0]['trigger_diagnostics']
    assert projected['status'] == 'recorded'
    assert projected['data']['ready'] is ready
    assert projected['daily']['triggered'] is item['daily']['triggered']
    assert projected['weekly']['triggered'] is item['weekly']['triggered']


def test_rejected_current_audit_does_not_hide_independent_qualified_history(full):
    historical(full)
    full[2]['coverage']['target_count'] = 9
    item = load(full)['items'][0]
    assert item['state'] == 'unknown' and not item['is_current']
    assert item['history']['latest_raw_trigger']['signal_data_date'] == '2026-08-21'


def test_same_day_duplicate_uses_first_observed_time_not_run_id_sort(full):
    historical(full, rid='z-earlier', action='blocked')
    historical(full, rid='a-later', action='allowed', ident=2)
    full[1].execute("UPDATE eod_target_decision SET decision_at='2026-08-21T15:45:00+08:00' WHERE run_id='a-later'")
    full[1].execute("UPDATE eod_risk_snapshot SET decision_at='2026-08-21T15:45:00+08:00' WHERE run_id='a-later'")
    history = load(full)['items'][0]['history']
    assert history['latest_raw_trigger']['run_id'] == 'z-earlier'
    assert history['latest_raw_trigger']['target_risk_action'] == 'blocked'
    assert history['latest_allowed_target']['run_id'] == 'a-later'


def test_stale_broker_trigger_is_excluded_and_cannot_green_current_row(full):
    historical(full, decision_date='2026-08-27', data_date='2026-08-26', bar='2026-08-26', kind='volume_breakout_ma60')
    result = load(full)
    h = result['items'][0]['history']
    assert h['excluded_stale_count'] == 1 and h['latest_raw_trigger'] is None
    assert h['status'] == 'none_in_window' and result['items'][0]['state'] == 'no_trigger'


def test_history_remains_separate_when_current_attempt_is_failed(full):
    historical(full)
    full[1].execute("UPDATE eod_target_decision SET data_status='failed',decision_state='data_failed',reference_close=NULL WHERE run_id='latest'")
    full[1].execute("UPDATE swing_eod_run SET status='failed' WHERE run_id='latest'")
    full[2]['run']['status'] = 'failed'
    full[2]['coverage'].update(evaluated_count=0, failed_count=10)
    item = load(full)['items'][0]
    assert item['state'] == 'data_failed' and not item['is_current']
    assert item['history']['latest_raw_trigger']['signal_data_date'] == '2026-08-21'


def test_mixed_target_gate_does_not_claim_each_signal_was_allowed(full):
    historical(full, action='allowed')
    raw = json.loads(full[1].execute("SELECT raw_signals_json FROM eod_target_decision WHERE run_id='old'").fetchone()[0])
    raw.append({'signal_type': 'volume_breakout_ma60', 'period': 'D', 'detail': {'data_date': '2026-08-21'}})
    full[1].execute("UPDATE eod_target_decision SET raw_trigger_count=2,raw_signals_json=? WHERE run_id='old'", (json.dumps(raw),))
    full[1].execute("UPDATE swing_eod_run SET signal_count=2,allowed_signal_count=1,blocked_signal_count=1 WHERE run_id='old'")
    h = load(full)['items'][0]['history']
    assert h['latest_allowed_target']['risk_scope'] == 'target'
    assert h['latest_allowed_target']['mixed_signal_gating_possible'] is True


@pytest.mark.parametrize('fault', ['missing_risk', 'wrong_snapshot', 'unbound_date', 'premarket', 'wrong_count', 'future_bar'])
def test_history_requires_real_run_and_bound_valid_evidence(full, fault):
    historical(full)
    if fault == 'missing_risk': full[1].execute("DELETE FROM eod_risk_snapshot WHERE run_id='old'")
    if fault == 'wrong_snapshot': full[1].execute("UPDATE eod_target_decision SET snapshot_id='other' WHERE run_id='old'")
    if fault == 'unbound_date': full[1].execute("UPDATE eod_target_decision SET decision_date='2026-08-20',data_date='2026-08-20' WHERE run_id='old'")
    if fault == 'premarket': full[1].execute("UPDATE swing_eod_run SET started_at='2026-08-21T10:00:00+08:00' WHERE run_id='old'")
    if fault == 'wrong_count': full[1].execute("UPDATE eod_target_decision SET raw_trigger_count=2 WHERE run_id='old'")
    if fault == 'future_bar': full[1].execute("UPDATE eod_target_decision SET raw_signals_json=? WHERE run_id='old'", (json.dumps([{'signal_type':'weekly_macd_golden_cross','period':'W','detail':{'data_date':'2026-08-28'}}]),))
    assert load(full)['items'][0]['history']['latest_raw_trigger'] is None


def test_history_run_window_and_result_rows_are_capped(full, monkeypatch):
    historical(full)
    historical(full, rid='repeat', decision_date='2026-08-25', ident=2)
    monkeypatch.setattr(etf_pool, 'HISTORY_RUN_LIMIT', 2)
    assert load(full)['items'][0]['history']['status'] == 'truncated'
    monkeypatch.setattr(etf_pool, 'HISTORY_RUN_LIMIT', 400)
    monkeypatch.setattr(etf_pool, 'HISTORY_ROW_LIMIT', 1)
    assert load(full)['items'][0]['history']['status'] == 'truncated'


def test_reads_are_constant_count_bounded_and_query_only(full):
    historical(full)
    record(full)
    con = full[1]
    con.execute('PRAGMA query_only=ON')
    queries = []
    con.set_trace_callback(queries.append)
    result = load(full)
    assert result['items'][0]['history']['status'] == 'available'
    assert len(queries) == 5 and all(q.startswith('SELECT') and 'LIMIT' in q for q in queries)
    with pytest.raises(sqlite3.OperationalError):
        con.execute('DELETE FROM swing_eod_run')
