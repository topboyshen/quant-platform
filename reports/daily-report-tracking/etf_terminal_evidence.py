"""Pure validation of the already-installed immutable EOD journal receipt.

No collector, producer, network, filesystem, clock or database calls. Receipt
counts describe log observations; database result counts remain a separate
source and are never inferred from those observations.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import re

from etf_identity import run_identity

ACTIVATION_SHA = '60d903df8b2dde2bd211fca34f8ea583cfb268db21904197cc1bc87909cc7f93'
COLLECTOR_SHA = '289c375b44b2322446f7a44177922e770250f277db74cf83082c2305d80b9778'
PRODUCER_SHA = 'b98f64c9643f3e57de121e49b26d61be506066a5623db8ab1bbfce27f710f708'
RECEIPT_SCHEMA = 'quant.eod-journal-receipt.v1'
COUNT_KEYS = frozenset({'kline_fail','kline_ok','kline_samples','total_amount_fail',
    'fallback_kline','fallback_macro','fallback','sina_ok','sina_fail','akshare_ok',
    'akshare_fail','observation_count'})
TZ = timezone(timedelta(hours=8))


def require(condition, code):
    if not condition:
        raise ValueError(code)


def canonical(value):
    # Existing collector contract uses ensure_ascii=True; do not change it.
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, item in items:
            require(key not in result, 'terminal_duplicate_json_key')
            result[key] = item
        return result
    def reject(_):
        raise ValueError('terminal_nonfinite_json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def instant(raw):
    require(isinstance(raw, str), 'terminal_timestamp')
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    require(value.tzinfo is not None, 'terminal_timestamp')
    return value


def sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def activation(raw):
    require(hashlib.sha256(raw).hexdigest() == ACTIVATION_SHA, 'terminal_activation_hash')
    value = strict_json(raw)
    require(value.get('schema') == 'quant.eod-retention-activation.v1'
        and value.get('release') == 'etf-controlled-release-20261027.1'
        and value.get('collector_sha256') == COLLECTOR_SHA
        and value.get('producer_sha256') == PRODUCER_SHA, 'terminal_activation_binding')
    instant(value['activated_at'])
    return value


def rule_attribution(run, day):
    raw = run.get('rule_attribution_json')
    require(isinstance(raw, str) and len(raw.encode('utf-8')) <= 16384, 'terminal_rule_unavailable')
    rule = strict_json(raw)
    require(isinstance(rule, dict) and rule.get('schema_version') == 'quant.etf-run-rule.v1'
        and rule.get('scope') == 'diagnostic_switch_and_pool_not_full_strategy'
        and rule.get('run_id') == run['run_id'] and rule.get('decision_date') == day
        and rule.get('decision_source_sha256') == PRODUCER_SHA, 'terminal_rule_binding')
    codes = rule.get('target_codes')
    require(isinstance(codes, list) and 0 < len(codes) <= 2000
        and all(isinstance(code, str) and re.fullmatch('[A-Za-z0-9_.:-]{1,128}', code) for code in codes)
        and codes == sorted(set(codes)) and type(rule.get('target_count')) is int
        and rule['target_count'] == len(codes) and type(run.get('target_count')) is int
        and run['target_count'] == len(codes), 'terminal_rule_pool')
    require(type(rule.get('daily_volume_breakout')) is bool
        and type(rule.get('breakout_ma')) is int and 0 < rule['breakout_ma'] <= 100000
        and type(rule.get('volume_window')) is int and 0 < rule['volume_window'] <= 100000,
        'terminal_rule_values')
    projection = {key: rule[key] for key in ('daily_volume_breakout','target_codes','breakout_ma','volume_window')}
    require(rule.get('rule_sha256') == digest(projection), 'terminal_rule_hash')
    return rule


def validate_receipt(value, filename, enabled, run, day, cutoff):
    """Same-run terminal corroboration, never a substitute for DB count proof."""
    result = run_identity(run, day)
    require(isinstance(cutoff, datetime) and cutoff.tzinfo is not None, 'terminal_cutoff')
    expected = {'schema','activation_sha256','collector_sha256','identity','event',
                'started_unixtime','unit','input_journal_sha256','counts','has_samples','receipt_sha256'}
    require(isinstance(value, dict) and set(value) == expected, 'terminal_receipt_shape')
    require(value['receipt_sha256'] == digest({k: v for k, v in value.items() if k != 'receipt_sha256'}),
            'terminal_receipt_hash')
    require(value['schema'] == RECEIPT_SCHEMA and value['activation_sha256'] == ACTIVATION_SHA
        and value['collector_sha256'] == COLLECTOR_SHA
        and value['unit'] in ('stock-radar-swing-eod.service','stock-radar-swing-eod-retry.service')
        and sha(value['input_journal_sha256']), 'terminal_receipt_binding')
    identity = value['identity']
    require(isinstance(identity, dict) and set(identity) == {'schema','run_id','decision_date',
        'status','outcome','rule_sha256','decision_source_sha256','effective_config_sha256'},
        'terminal_identity_shape')
    require(identity['schema'] == 'quant.eod-completion.v1'
        and isinstance(identity['run_id'], str) and re.fullmatch('[0-9a-f]{32}', identity['run_id'])
        and identity['run_id'] == run['run_id'] and identity['decision_date'] == day
        and identity['status'] == run['status'] and identity['outcome'] == run['decision_outcome']
        and identity['status'] in ('success','partial','failed')
        and identity['decision_source_sha256'] == PRODUCER_SHA
        and sha(identity['effective_config_sha256']) and sha(identity['rule_sha256']),
        'terminal_identity_binding')
    rule = rule_attribution(run, day)
    require(rule['rule_sha256'] == identity['rule_sha256'], 'terminal_rule_receipt_mismatch')
    event = value['event']
    require(isinstance(event, dict) and set(event) == {'invocation_id','event_date','event_time','unixtime','result'}
        and isinstance(event['invocation_id'], str) and re.fullmatch('[0-9a-f]{32}', event['invocation_id'])
        and event['result'] in ('success','failed'), 'terminal_event_shape')
    start, end = value['started_unixtime'], event['unixtime']
    require(type(start) in (int,float) and math.isfinite(start)
        and type(end) in (int,float) and math.isfinite(end)
        and instant(enabled['activated_at']).timestamp() <= start
        <= instant(run['started_at']).timestamp() <= instant(run['completed_at']).timestamp()
        <= end <= cutoff.timestamp(), 'terminal_event_time')
    expected_time = datetime.fromtimestamp(end, TZ)
    require(event['event_date'] == day == expected_time.date().isoformat()
        and event['event_time'] == expected_time.isoformat(timespec='seconds')
        and filename == day + '-' + run['run_id'] + '-' + event['invocation_id'] + '.json',
        'terminal_event_filename')
    # A manager success proves process exit only. It does not rewrite a failed
    # business result; both fields remain explicit in the retained receipt.
    counts = value['counts']
    require(isinstance(counts, dict) and set(counts) - {'_obs'} == COUNT_KEYS, 'terminal_counts_shape')
    require(all(type(counts[k]) is int and 0 <= counts[k] <= 5000 for k in COUNT_KEYS), 'terminal_counts')
    require(counts['kline_samples'] == counts['kline_fail'] + counts['kline_ok']
        and counts['fallback'] == counts['fallback_kline'] + counts['fallback_macro'], 'terminal_counts_sum')
    observations = counts.get('_obs', [])
    require(isinstance(observations, list) and len(observations) <= 5000, 'terminal_observation_shape')
    for row in observations:
        require(isinstance(row, list) and len(row) == 4 and isinstance(row[0], str)
            and re.fullmatch('[A-Za-z0-9_.-]{1,64}', row[0])
            and row[1] in ('adapter','direct','proxy','cache','other')
            and row[2] in ('ready','stale','invalid','empty','error','other')
            and type(row[3]) is int and 0 < row[3] <= 5000, 'terminal_observation')
    require(sum(row[3] for row in observations) == counts['observation_count'], 'terminal_observation_count')
    require(type(value['has_samples']) is int and value['has_samples'] == int(
        counts['kline_samples'] + counts['total_amount_fail'] + counts['fallback'] + counts['observation_count'] > 0),
        'terminal_has_samples')
    return {'runIdentity': result, 'receiptSha256': value['receipt_sha256'],
        'invocationId': event['invocation_id'], 'terminalAt': event['event_time'],
        'processResult': event['result'], 'ruleSha256': identity['rule_sha256'],
        'sourceSha256': identity['decision_source_sha256'],
        'effectiveConfigSha256': identity['effective_config_sha256'],
        'inputJournalSha256': value['input_journal_sha256']}
