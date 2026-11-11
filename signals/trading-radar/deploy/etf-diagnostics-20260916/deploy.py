"""Explicit, bounded release; no DB copies, business task execution or cleanup."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone

ROOT = Path('/home/ubuntu/trading-radar')
RELEASE = ROOT / 'releases/etf-diagnostics-20260916.1'
PROV = ROOT / 'deploy/etf-diagnostics-20260916'
BASE = 'b7e5c7b5fe4a07f278ef9ad34ab198b9b49c43b8'
TAG = 'personal-2026.09.16-etf-diagnostics.1'
OLDTAG = 'personal-2026.09.12-etf-pool.2'
FILES = {
    'radar/swing/trend_strategy.py': ('3081e51e5148444c293cbd0f1b30889308d60d0211f4823da0c40dd3c7737bd5', 'ba341122b0223a383b0b90c0cacad8a7abc67251fd1836940a3647e13389f876'),
    'radar_swing_eod.py': ('d6dde9665a68665f35c5c7a8c64171a60b9926597a818aa0c430309a17ee23c4', '7a94e0879e9b16903d5c4cfd49ee9e1bbd62a8d3020a3eb4995644e39dafcde2'),
    'dashboard/etf_pool.py': ('e99dd5d3648005878858269edf673dde11cb06d29f999e101f979a2ed5312117', 'cfe2e68bc3cd220893e89c23979dd2cb2b117232376346b79657f4fb132c3828'),
    'tests/test_swing_trigger_diagnostics.py': (None, '1cd82b2080d592b7b17b1647a6d0234895db895306b58895c1c2f17fe9424b97'),
    'tests/test_etf_pool_diagnostics.py': (None, '52879aaef4e1ec282050d48bbceef7e1a841bdd22943286b68cb9c8664ce7fc1'),
}
IMAGES = {
    'radar-integration-api': ('trading-radar-integration', 'sha256:96813d01ea51cbdbb1ad5f4151281ef2c8f55a87423c382a83e5375c89675b16', 'sha256:cee744177230063731d75097832586d661be77dc1d61e9a0418864246dc8bc27'),
    'radar-quantdinger-frontend': ('trading-radar-quantdinger-frontend', 'sha256:11efc8438993062010d9115fe6c36e0ed924643a2780de2a7320b73b1b8b526e', 'sha256:6aaa0024fad5e5ad26af458bfcfa0ae2899fcce1e444a25c8cf79d845057032a'),
}
UNITS = ['stock-radar-swing-eod', 'stock-radar-swing-eod-retry', 'stock-radar-swing-outcomes',
         'stock-radar-swing-total-amount-repair', 'stock-radar-swing-watchdog',
         'research-digest', 'research-index-close-cache']
FRONT_FILES = ['src/utils/etfDiagnostics.js', 'src/utils/etfPool.js',
               'src/views/personal-home/index.vue', 'tests/unit/etfDiagnostics.test.mjs',
               'tests/browser/etf-diagnostics.mjs']

def run(*args, check=True):
    return subprocess.run(args, cwd=ROOT, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120).stdout

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix='.etf-release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def snapshot():
    names = run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines()
    containers = {}
    for name in names:
        if not name.startswith('radar-'):
            continue
        fmt = '{{json .Id}} {{json .Image}} {{json .State.StartedAt}} {{json .RestartCount}} {{json .State.Health.Status}}'
        fields = run('docker', 'inspect', '--format', fmt, name).strip()
        containers[name] = json.loads('[' + ','.join(fields.split()) + ']')
    return {'time': datetime.now(timezone.utc).isoformat(), 'containers': containers,
            'config_sha256': sha(ROOT / 'config.swing.json'),
            'units_sha256': {u+s: hashlib.sha256(run('systemctl', 'cat', u+s).encode()).hexdigest()
                              for u in UNITS for s in ('.service', '.timer')},
            'invocations': {u: run('systemctl', 'show', u+'.service', '-p', 'InvocationID', '-p', 'ExecMainStartTimestamp', '-p', 'Result').strip() for u in UNITS}}

assert run('git', 'rev-parse', 'HEAD').strip() == BASE, 'Git HEAD drift'
assert not PROV.exists(), 'provenance destination already exists'
assert sha(RELEASE/'candidate.tar.gz') == '23584ad39066edfb2f33fcd7216e0e5f3d9c0caf18faf1db4aedf7ed2de59e51'
present_front = {str(p.relative_to(RELEASE/'frontend')) for p in (RELEASE/'frontend').rglob('*') if p.is_file()}
mac_metadata = {str(Path(n).parent / ('._'+Path(n).name)) for n in FRONT_FILES}
assert set(FRONT_FILES) <= present_front <= set(FRONT_FILES) | mac_metadata
assert all(not (RELEASE/'frontend'/n).is_symlink() for n in FRONT_FILES)
for name, (old, new) in FILES.items():
    assert sha(RELEASE/'candidate'/name) == new, name
    assert (sha(ROOT/name) == old) if old else not (ROOT/name).exists(), name

definitions = ['docker-compose.integration.override.yml', 'docker-compose.quantdinger.yml',
               'deploy/production_pin.json', 'deploy/PRODUCTION_PIN.md']
assert not run('git', 'status', '--porcelain', '--', *definitions), 'definition drift'
assert not run('git', 'diff', '--cached', '--name-only'), 'existing staged work'
pin = json.loads((ROOT/'deploy/production_pin.json').read_text())
for name, (image, old, new) in IMAGES.items():
    assert pin['services'][name] == {'image': image+':'+OLDTAG, 'image_id': old}
    assert run('docker', 'image', 'inspect', '--format', '{{.Id}}', image+':'+OLDTAG).strip() == old
    assert run('docker', 'image', 'inspect', '--format', '{{.Id}}', image+':'+TAG).strip() == new
run('python3', 'scripts/verify_production_pin.py')
before = snapshot()
atomic(RELEASE/'pre-deploy.json', json.dumps(before, indent=2)+'\n')
for unit in UNITS:
    assert run('systemctl', 'show', unit+'.service', '-p', 'ActiveState', '--value').strip() not in ('active', 'activating', 'deactivating'), unit+' active; wait naturally'

# All checks passed. Install only the reviewed source and release definitions.
for name in FILES:
    atomic(ROOT/name, (RELEASE/'candidate'/name).read_bytes())
tracked = list(FILES) + definitions
for name in FRONT_FILES:
    source = RELEASE/'frontend'/name
    destination = PROV/'frontend-overlay'/name
    atomic(destination, source.read_bytes())
    tracked.append(str(destination.relative_to(ROOT)))
for name in ('Dockerfile.integration', 'Dockerfile.frontend', 'smoke.py', 'deploy.py'):
    content = (RELEASE/name).read_text()
    if name == 'Dockerfile.integration':
        content = content.replace('COPY etf_pool.py ', 'COPY dashboard/etf_pool.py ')
    atomic(PROV/name, content)
    tracked.append(str((PROV/name).relative_to(ROOT)))

compose = ROOT/definitions[0]
body = compose.read_text()
assert body.count('trading-radar-integration:'+OLDTAG) == 1
body = body.replace('trading-radar-integration:'+OLDTAG, 'trading-radar-integration:'+TAG)
body = body.replace('dockerfile: deploy/etf-pool/Dockerfile.integration', 'dockerfile: deploy/etf-diagnostics-20260916/Dockerfile.integration')
atomic(compose, body)
compose = ROOT/definitions[1]
body = compose.read_text()
assert body.count('trading-radar-quantdinger-frontend:'+OLDTAG) == 1
atomic(compose, body.replace('trading-radar-quantdinger-frontend:'+OLDTAG, 'trading-radar-quantdinger-frontend:'+TAG))
for name, (image, old, new) in IMAGES.items():
    pin['services'][name] = {'image': image+':'+TAG, 'image_id': new}
atomic(ROOT/'deploy/production_pin.json', json.dumps(pin, indent=2)+'\n')

doc = (ROOT/'deploy/PRODUCTION_PIN.md').read_text()
lines = doc.splitlines()
for i, line in enumerate(lines):
    if line.startswith('| quantdinger-frontend |'):
        lines[i] = '| quantdinger-frontend | `trading-radar-quantdinger-frontend:'+TAG+'` | 9/12 frozen source + ETF diagnostics; source `b8913130…`, image `6aaa0024fad5` |'
    elif line.startswith('| integration-api |'):
        lines[i] = '| integration-api | `trading-radar-integration:'+TAG+'` | Read-only diagnostics/history overlay; image `cee744177230`, dependencies unchanged |'
doc = '\n'.join(lines)+'\n'
section = '''## ETF diagnostic evidence release (2026-09-16)

Tracked provenance: `deploy/etf-diagnostics-20260916/release.json` and frontend
overlay. Host producer records best-effort run-bound reasons only for future
natural EOD runs. No strategy/configuration/notification/timer changes, manual
runs, historical recomputation, schema changes, or database copies/backups.
Only integration-api and quantdinger-frontend containers are recreated.
Linux isolated regression: 270/270; frontend 218 pass (two external-fixture
tests excluded); 55 baseline-equivalence cases; synthetic browser checks pass.

Rollback is per component: restore only that component's Compose image and
matching pin entry from release.json, preserving the other services' entries;
use the canonical wrapper with `--no-deps --no-build --pull never` for that
service only. Restore both host producer files from base commit b7e5c7b only
while the Swing service is inactive. Do not restore or copy any database.
Old images remain present, but no new cleanup/retention policies were changed.

'''
doc = doc.replace('## Backend compatibility overlay (2026-09-14)', section+'## Backend compatibility overlay (2026-09-14)', 1)
atomic(ROOT/'deploy/PRODUCTION_PIN.md', doc)

manifest = {'release': TAG, 'base_git': BASE, 'source_files': {n:v[1] for n,v in FILES.items()},
            'images': {n:{'image':i+':'+TAG, 'image_id':new, 'rollback_image':i+':'+OLDTAG, 'rollback_image_id':old} for n,(i,old,new) in IMAGES.items()},
            'frontend_source_archive': 'releases/etf-diagnostics-20260916.1/frontend-source.tar.gz',
            'frontend_source_sha256': 'b89131302fb69fdedeeb014ac3e66e852db4d4b5e8e4fa6c5d1615da7fced315',
            'frontend_dist_sha256': 'c60baacd48490de2fd022d987cfdd3f1fc52c6e8a0b70fa7c42ea988461bc428',
            'index_html_sha256': 'f31752997430fd2a808f0db10e84f21fab5c3806ce290abf04d07692bee6fce9',
            'policy': 'NO_BACKUP; no replay/backfill/strategy/timer/proxy/notification changes',
            'natural_acceptance': 'pending next natural EOD; historical diagnostics intentionally not_recorded'}
atomic(PROV/'release.json', json.dumps(manifest, indent=2)+'\n')
tracked.append(str((PROV/'release.json').relative_to(ROOT)))
atomic(RELEASE/'tracked-files.json', json.dumps(tracked, indent=2)+'\n')
run('git', 'diff', '--check', '--', *tracked)
print('Source and exact image pins installed; switching only two containers.', flush=True)
result = subprocess.run(['/home/ubuntu/bin/trading-radar-compose', 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'integration-api', 'quantdinger-frontend'], cwd=ROOT, timeout=150)
assert result.returncode == 0, 'Canonical postflight failed; inspect before any rollback'
after = snapshot()
atomic(RELEASE/'post-deploy.json', json.dumps(after, indent=2)+'\n')
assert before['config_sha256'] == after['config_sha256']
assert before['units_sha256'] == after['units_sha256']
for name, state in before['containers'].items():
    if name not in IMAGES:
        assert after['containers'][name] == state, 'unrelated container changed: '+name
for name, (_, _, new) in IMAGES.items():
    assert after['containers'][name][1] == new
    assert after['containers'][name][-1] == 'healthy'
print(json.dumps({'release': TAG, 'state': 'deployed_healthy', 'other_containers_unchanged': len(before['containers'])-2,
                  'config_and_units_unchanged': True, 'new_source_sha256': {n:sha(ROOT/n) for n in FILES}}, indent=2), flush=True)
