"""Read-only proof that a frozen, platform-accepted summary covers one ETF run.

No sender, credentials, business producer or database mutation is used here.
Legacy summaries are preserved; missing batch identity is never backfilled.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import types
from zoneinfo import ZoneInfo

STATE = Path('/home/ubuntu/trading-radar/data/daily-research-summary/1')
CONTROL = Path('/home/ubuntu/releases/daily-research-summary-20261012.1/control.json')
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 16
SCHEMA = 'quant.etf-delivery-coverage.v1'


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate_key')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def _signature(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _read(path, limit=MAX_BYTES):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('linked_source')
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError('source_shape_or_limit')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if _signature(os.fstat(stream.fileno())) != _signature(before):
            raise ValueError('source_changed')
        raw = stream.read(limit + 1)
        if (len(raw) > limit or len(raw) != before.st_size
                or _signature(os.fstat(stream.fileno())) != _signature(before)
                or _signature(path.lstat()) != _signature(before)):
            raise ValueError('source_changed')
    return raw


def _identity_module():
    path = Path(__file__).with_name('etf_identity.py')
    raw = _read(path, 65536)
    module = types.ModuleType('_frozen_etf_identity')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def _binding_module():
    # The watchdog loads this reader by its fixed release path rather than
    # importing an arbitrary PYTHONPATH module. Keep the companion equally
    # fixed and use its pure, stdlib-only functions without writing bytecode.
    path = Path(__file__).with_name('report_binding.py')
    raw = _read(path, 65536)
    module = types.ModuleType('_frozen_report_binding')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def _instant(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone_missing')
    return parsed


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _database_boundary(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('linked_database')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('database_shape')
    for suffix in ('-wal', '-shm', '-journal'):
        # lstat also detects dangling links. Never open/remove/checkpoint a sidecar.
        try:
            Path(str(path) + suffix).lstat()
        except FileNotFoundError:
            continue
        raise ValueError('database_sidecar_present')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if _signature(os.fstat(fd)) != _signature(info):
            raise ValueError('database_changed')
        header = os.read(fd, 100)
    finally:
        os.close(fd)
    if header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01':
        raise ValueError('database_mode_unproven')
    return _signature(info)


def _rows(path, day):
    before = _database_boundary(path)
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA busy_timeout=1000')
        deadline = time.monotonic() + 3
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute('BEGIN')
        rows = connection.execute(
            'SELECT account_id,channel,business_date,notification_type,revision,content_sha256,'
            'status,message_id,target_fingerprint,completed_at,substr(receipt_json,1,32769) AS receipt_json '
            'FROM daily_summary_outbox WHERE account_id=? AND business_date=? '
            'AND notification_type=? ORDER BY revision LIMIT ?',
            ('1', day, 'daily_research_summary', MAX_ROWS + 1)).fetchall()
        if len(rows) > MAX_ROWS:
            raise ValueError('record_limit')
        result = [dict(row) for row in rows]
        if (any(len(str(row.get('receipt_json', ''))) > 32768 for row in result)
                or sum(len(str(v)) for row in result for v in row.values()) > MAX_BYTES):
            raise ValueError('record_budget')
        connection.rollback()
        if _database_boundary(path) != before:
            raise ValueError('database_changed')
        return result
    finally:
        if connection is not None:
            connection.close()


def _confirmed(row, frozen, target, day, now):
    """A local `sent` flag alone is insufficient; exact response/body binding is required."""
    revision = row['revision']
    if (row['account_id'] != '1' or row['business_date'] != day
            or row['notification_type'] != 'daily_research_summary'
            or row['channel'] != 'telegram:' + target or row['target_fingerprint'] != target
            or row['status'] != 'sent' or not isinstance(row['message_id'], str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', row['message_id'])
            or frozen.get('accountId') != '1' or frozen.get('businessDate') != day
            or frozen.get('revision') != revision or frozen.get('version') != 'daily-research-summary.v1'
            or not isinstance(frozen.get('text'), str)):
        return False
    digest = _sha(frozen['text'].encode('utf-8'))
    if not digest == frozen.get('textSha256') == row['content_sha256']:
        return False
    receipt = _json(row['receipt_json'])
    if (not isinstance(receipt, dict) or receipt.get('ok') is not True
            or type(receipt.get('http_status')) is not int or receipt['http_status'] != 200
            or type(receipt.get('message_id')) is not int
            or str(receipt['message_id']) != row['message_id']
            or receipt.get('targetFingerprint') != target):
        return False
    diagnostics = receipt.get('diagnostics', {})
    if (not isinstance(diagnostics, dict)
            or diagnostics.get('schema') != 'telegram-send-diagnostic.v1'
            or diagnostics.get('response_observed') is not True
            or not isinstance(diagnostics.get('body'), dict)
            or diagnostics['body'].get('sha256') != digest):
        return False
    presentation = frozen.get('presentationSchema')
    if presentation is not None:
        binding = _binding_module()
        if not binding.is_readable(frozen):
            return False
        binding.validate_provider_identity(frozen, receipt, target)
        binding.validate_receipt(frozen, receipt, target)
    elif 'evidenceBinding' in frozen or 'reportBinding' in receipt:
        return False
    generated, completed = _instant(frozen['generatedAt']), _instant(row['completed_at'])
    if presentation is not None:
        returned = _instant(receipt['senderReturnedAt'])
        if not generated <= returned <= completed <= now:
            return False
    return (generated.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() == day
            and completed.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() == day
            and generated <= completed <= now)


def _verified_coverage(frozen, identity, day):
    """Match reviewed ETF evidence against its legacy or readable binding.

    Readable receipt-to-binding agreement is separately required by _confirmed;
    an archive's self-consistent metadata digest alone proves no delivery.
    """
    details = frozen.get('details')
    if not isinstance(details, dict):
        raise ValueError('details_missing')
    checks = details.get('checks', {}).get('etf', {})
    source = details.get('sources', {}).get('etf', {})
    if (not isinstance(checks, dict) or checks.get('verified') is not True
            or checks.get('coverageIdentityVerified') is not True
            or not isinstance(source, dict) or source.get('verified') is not True
            or type(source.get('accountId')) not in (str, int)
            or str(source['accountId']) != '1' or source.get('decisionDate') != day):
        raise ValueError('etf_source_not_verified')
    value = identity.validate_coverage_identity(details.get('etfCoverage'), day)
    if identity.coverage_identity(source.get('firstRun'), source.get('finalRun'), day) != value:
        raise ValueError('etf_source_batch_mismatch')
    for key in ('firstRun', 'finalRun'):
        if checks.get('runs', {}).get(key, {}).get('verified') is not True:
            raise ValueError('etf_run_check_unverified')
        if _instant(value[key]['result']['completed_at']) > _instant(frozen['generatedAt']):
            raise ValueError('batch_after_freeze')
    references = frozen.get('sourceReferences')
    if not isinstance(references, list):
        raise ValueError('etf_reference_missing')
    refs = [r for r in references if isinstance(r, dict)
            and isinstance(r.get('sources'), list) and 'etf' in r['sources']]
    if len(refs) != 1:
        raise ValueError('etf_reference_count')
    ref = refs[0]
    if (ref.get('sources') != ['etf'] or type(ref.get('accountId')) not in (str, int)
            or str(ref['accountId']) != '1' or ref.get('decisionDate') != day):
        raise ValueError('etf_reference_scope')
    for prefix, key in (('first', 'firstRun'), ('final', 'finalRun')):
        run = value[key]
        if (ref.get(prefix + 'RunId') != run['runId']
                or ref.get(prefix + 'RunResultSha256') != run['resultSha256']
                or _instant(ref.get(prefix + 'RunCompletedAt')) != _instant(run['result']['completed_at'])):
            raise ValueError('etf_reference_binding')
    if frozen.get('presentationSchema') is not None:
        binding = _binding_module()
        if not binding.is_readable(frozen):
            raise ValueError('etf_presentation_schema_unknown')
        binding.validate(frozen, require_target=True)
    else:
        digest = _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':'), allow_nan=False).encode('utf-8'))
        matches = re.findall(r'^\[ETF-COVERAGE sha256=([0-9a-f]{64})\]$', frozen['text'], re.M)
        if matches != [digest]:
            raise ValueError('etf_sent_text_binding')
    return value


def _inspect(state, control_path, run, day, now):
    """Private filesystem seam for offline fixtures. Production wrapper has fixed paths."""
    result = {'schema': SCHEMA, 'status': 'unknown', 'reason': 'coverage_unavailable',
              'businessDate': day, 'priorSummaryDelivered': False,
              'confirmedSummaryCount': 0, 'unconfirmedSummaryCount': 0,
              'scope': 'dedicated_daily_summary_outbox',
              'allNotificationPathsAudited': False, 'doesNotAuthorizeResend': True}
    try:
        if (not isinstance(now, datetime) or now.tzinfo is None
                or now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() != day):
            raise ValueError('clock')
        identity = _identity_module()
        wanted = identity.run_identity(run, day)
        if _instant(wanted['result']['completed_at']) > now:
            raise ValueError('future_run')
        result.update(runId=wanted['runId'], resultSha256=wanted['resultSha256'])
        control = _json(_read(control_path, 16384))
        target = control.get('targetFingerprint')
        if (control.get('enabled') is not True or control.get('accountId') != '1'
                or control.get('contract') != 'daily-research-summary.v1'
                or not isinstance(target, str) or not re.fullmatch(r'[0-9a-f]{64}', target)
                or not isinstance(control.get('effectiveDate'), str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', control['effectiveDate'])
                or date.fromisoformat(control['effectiveDate']).isoformat() != control['effectiveDate']
                or control['effectiveDate'] > day):
            raise ValueError('control_identity')
        rows = _rows(state / 'notification-outbox.sqlite3', day)
        covered, older, ambiguous = [], 0, 0
        for row in rows:
            revision = row['revision']
            if not isinstance(revision, str) or not re.fullmatch(r'v[1-9][0-9]{0,2}', revision):
                ambiguous += 1
                continue
            try:
                raw = _read(state / (day + '-' + revision + '.json'))
                frozen = _json(raw)
                if not _confirmed(row, frozen, target, day, now):
                    result['unconfirmedSummaryCount'] += 1
                    continue
                result['confirmedSummaryCount'] += 1
                result['priorSummaryDelivered'] = True
                value = frozen.get('details', {}).get('etfCoverage')
                if value is None:
                    if _instant(frozen['generatedAt']) < _instant(wanted['result']['completed_at']):
                        older += 1
                    else:
                        ambiguous += 1
                    continue
                value = _verified_coverage(frozen, identity, day)
                if value['finalRun'] == wanted:
                    covered.append({'revision': revision, 'messageId': row['message_id'],
                                    'textSha256': row['content_sha256'],
                                    'frozenSha256': _sha(raw), 'completedAt': row['completed_at']})
                else:
                    older += 1
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError):
                ambiguous += 1
        result['unprovenSummaryCount'] = ambiguous
        if covered:
            result.update(status='covered', reason='exact_batch_platform_accepted', confirmations=covered)
        elif ambiguous or result['unconfirmedSummaryCount']:
            result.update(status='unknown', reason='batch_coverage_unproven' if ambiguous else 'delivery_unconfirmed')
        else:
            result.update(status='not_covered', reason='current_batch_not_covered',
                          priorDifferentOrEarlierCount=older)
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError, sqlite3.Error):
        # Fixed classification only; no database path, target or transport secrets.
        return result


def inspect_coverage(run, *, business_date, now):
    return _inspect(STATE, CONTROL, run, business_date, now)
