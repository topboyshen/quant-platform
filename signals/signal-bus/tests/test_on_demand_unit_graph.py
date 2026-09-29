"""Cross-repo templates, no systemctl or sends: XHS cannot trigger delivery.

ASTOCK_TEMPLATE_ROOT may select an isolated AStock template tree. In production
the sibling AStock_AI_System is read. Missing templates fail; they never skip.
The deployer must additionally compare the effective installed systemd graph.
"""
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
ASTOCK = Path(os.environ.get('ASTOCK_TEMPLATE_ROOT', str(ROOT.parent / 'AStock_AI_System')))


def merged_values(paths, key):
    values = []
    for path in paths:
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith(('#', ';')) or '=' not in line:
                continue
            name, value = line.split('=', 1)
            if name != key:
                continue
            if not value.strip():
                values = []
            else:
                values.extend(value.split())
    return set(values)


def unit_paths(unit):
    base = ASTOCK / 'systemd' / unit
    if not base.is_file():
        raise AssertionError(f'missing AStock unit template: {base}')
    # If both repos provide a same-named drop-in, deployment copies the
    # signal-bus template last (unified alert wiring). Do not concatenate it.
    fragments = {}
    for root in (ASTOCK / 'systemd', ROOT / 'systemd'):
        for path in (root / f'{unit}.d').glob('*.conf'):
            fragments[path.name] = path
    return [base] + [fragments[name] for name in sorted(fragments)]


class OnDemandUnitGraphTests(unittest.TestCase):
    def test_source_success_only_triggers_the_seal(self):
        self.assertEqual(merged_values(unit_paths('astock-xhs-source.service'), 'OnSuccess'),
                         {'astock-research-seal.service'})

    def test_publish_consumers_retain_the_same_closed_marker_condition(self):
        expected = {'!/home/ubuntu/AStock_AI_System/work/orchestrator/publish-branch.closed'}
        for unit in ('astock-research-seal.service', 'astock-morning-notify.service', 'astock-xhs-draft.service'):
            with self.subTest(unit=unit):
                self.assertEqual(merged_values(unit_paths(unit), 'ConditionPathExists'), expected)

    def test_seal_has_both_automatic_delivery_success_targets(self):
        self.assertEqual(merged_values(unit_paths('astock-research-seal.service'), 'OnSuccess'),
                         {'astock-morning-notify.service', 'nng8-research-digest.service'})

    def test_draft_merged_graph_has_no_automatic_delivery(self):
        self.assertEqual(merged_values(unit_paths('astock-xhs-draft.service'), 'OnSuccess'), set())

    def test_on_demand_draft_merged_graph_has_no_automatic_core_alert(self):
        self.assertEqual(merged_values(unit_paths('astock-xhs-draft.service'), 'OnFailure'), set())
        self.assertEqual(merged_values(unit_paths('astock-research-seal.service'), 'OnFailure'),
                         {'research-chain-alert-post.service'})

    def test_the_installable_signal_gate_is_inert_not_absent(self):
        path = ROOT / 'systemd/astock-xhs-draft.service.d/signal-gate.conf'
        self.assertTrue(path.is_file())
        self.assertEqual(merged_values([path], 'OnSuccess'), set())

    def test_existing_bounded_relay_timers_are_preserved(self):
        path = ROOT / 'systemd/nng8-research-digest.timer'
        self.assertEqual(merged_values([path], 'OnCalendar'),
                         {'Mon..Fri', '*-*-*', '08:05:00', '08:10:00', 'Asia/Shanghai'})
        text = path.read_text(encoding='utf-8')
        self.assertIn('OnCalendar=Mon..Fri *-*-* 08:05:00 Asia/Shanghai', text)
        self.assertIn('OnCalendar=Mon..Fri *-*-* 08:10:00 Asia/Shanghai', text)


if __name__ == '__main__':
    unittest.main()
