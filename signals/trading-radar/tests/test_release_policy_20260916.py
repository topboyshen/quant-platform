"""Offline-only checks: no Docker, DB, SSH, cloud or production paths opened."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / 'deploy/backend-compat-20260914'
ENTRIES = [ROOT / 'scripts/backup_quantdinger_postgres.sh',
           ROOT / 'deploy/retired/backup_sqlite_to_cos.sh']


class ReleasePolicyTests(unittest.TestCase):
    def test_retired_entries_refuse_before_any_external_command(self):
        for entry in ENTRIES:
            source = entry.read_text()
            prefix = source.split('exit 78\n', 1)[0]
            self.assertIn('exit 78\n', source)
            statements = [line for line in prefix.splitlines()
                          if line.strip() and not line.lstrip().startswith('#')]
            self.assertEqual(len(statements), 2)
            self.assertTrue(statements[0].startswith('set -'))
            self.assertTrue(statements[1].startswith("printf '%s\\n' 'REFUSED: Quant NO_BACKUP"))
            for args in ([], ['--upload'], ['--force'], ['--help']):
                with self.subTest(entry=entry.name, args=args):
                    proc = subprocess.run(['/bin/bash', '--noprofile', '--norc', str(entry), *args],
                                          env={'PATH': '/nonexistent', 'ALLOW_BACKUP': '1',
                                               'NO_BACKUP': '0'}, capture_output=True, text=True, timeout=3)
                    self.assertEqual(proc.returncode, 78)
                    self.assertEqual(proc.stdout, '')
                    self.assertEqual(proc.stderr.count('\n'), 1)
                    self.assertIn('REFUSED: Quant NO_BACKUP', proc.stderr)

    def test_shell_syntax_only(self):
        for entry in ENTRIES:
            proc = subprocess.run(['/bin/bash', '-n', str(entry)], capture_output=True, timeout=3)
            self.assertEqual(proc.returncode, 0)

    def test_manifest_matches_current_backend_pin_and_build_base(self):
        release = json.loads((RELEASE / 'release.json').read_text())
        pin = json.loads((ROOT / 'deploy/production_pin.json').read_text())['services']['radar-quantdinger-backend']
        pin_doc = (ROOT / 'deploy/PRODUCTION_PIN.md').read_text()
        self.assertIn(pin['image'], pin_doc)
        self.assertIn(pin['image_id'], pin_doc)
        self.assertIn('image: ' + pin['image'], (ROOT / 'docker-compose.quantdinger.yml').read_text())
        self.assertTrue((RELEASE / 'Dockerfile').read_text().startswith('FROM ' + release['base_image_id'] + '\n'))
        # The compat overlay is a historical record, not the current pin: it may
        # only claim the pin while production still runs it (the 2026-11-06
        # catch-up moved the backend to security-hardening-20261029.1).
        if pin['image'] == release['image']:
            self.assertEqual(release['image_id'], pin['image_id'])
        self.assertEqual(set(release['files']), {'app/data_sources/cn_stock.py',
                         'app/data_sources/tencent.py', 'app/services/backtest.py'})
        patch = (RELEASE / 'overlay.patch').read_text()
        self.assertEqual(patch.count('\n+++ b/'), 3)
        for rel, hashes in release['files'].items():
            self.assertIn('+++ b/' + rel + '\n', patch)
            for value in hashes.values():
                self.assertRegex(value, r'^[0-9a-f]{64}$')

    def test_backup_policy_is_explicit_and_does_not_authorize_deletion(self):
        text = (ROOT / 'deploy/QUANT_BACKUP_POLICY.md').read_text()
        for term in ('PostgreSQL', 'pre-release', 'exit 78', 'separate, explicit authorization',
                     'not arbitrary root commands', 'NOT future templates'):
            self.assertIn(term, text)
        self.assertIn('NO_BACKUP', (ROOT / 'AGENTS.md').read_text())


if __name__ == '__main__':
    unittest.main()
