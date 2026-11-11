"""Read-only API evidence; ephemeral dummy token only in isolated candidate."""
import collections
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else 'candidate'
if mode == 'candidate':
    token_path = Path('/tmp/etf-canary-token')
    token_path.write_text('etf-isolated-not-a-production-secret-123456789')
    token_path.chmod(0o600)
    os.environ['RADAR_INTEGRATION_TOKEN_FILE'] = str(token_path)
from dashboard import integration_app

def counts():
    with sqlite3.connect('file:/app/data/swing_macro.db?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        return {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('swing_eod_run', 'eod_target_decision', 'trend_signal')}

before = counts()
client = integration_app.app.test_client()
assert client.get('/health').status_code == 200
assert client.get('/ready').status_code == 200
assert client.get('/integration/v1/market-context').status_code == 401
token = integration_app._configured_token()
assert token
response = client.get('/integration/v1/market-context', headers={'Authorization': 'Bearer ' + token})
assert response.status_code == 200
pool = response.get_json()['research_pool']
items = pool['items']
assert len(items) == 10
assert all('trigger_diagnostics' in i and 'history' in i for i in items)
gold = next(i for i in items if i['code'] == '518880')
broker = next(i for i in items if i['code'] == '512000')
assert gold['history']['latest_raw_trigger'] is not None
assert broker['history']['excluded_stale_count'] >= 1
assert counts() == before, 'unexpected concurrent DB change; inspect before declaring pass'
print(json.dumps({'mode': mode, 'http': {'health': 200, 'ready': 200, 'unauthorized': 401, 'context': 200},
                  'configured_count': len(items), 'counts': pool['counts'],
                  'diagnostics': dict(collections.Counter(i['trigger_diagnostics']['status'] for i in items)),
                  'gold_history': gold['history'], 'broker_history': broker['history'],
                  'db_counts_unchanged': before,
                  'api_source_sha256': hashlib.sha256(Path('/app/dashboard/etf_pool.py').read_bytes()).hexdigest()},
                 ensure_ascii=False, indent=2))
