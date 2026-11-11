import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import core_market
import daily_snapshot
import snapshot_contract as selector
from test_core_market_contract import qlib, calendar


class ContractDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root / "policy.json"
        self.value = {"activeContract": selector.CORE, "effectiveTradeDate": "2026-09-28"}

    def test_missing_policy_is_exact_legacy_selection(self):
        self.assertEqual(selector.read_policy(self.policy), {"activeContract": selector.LEGACY, "effectiveTradeDate": None})
        self.assertEqual(selector.select(selector.read_policy(self.policy), dt.datetime(2026, 9, 28, 22, 30, tzinfo=selector.CST)), selector.LEGACY)

    def test_shanghai_midnight_controls_activation_not_host_utc(self):
        self.assertEqual(selector.select(self.value, dt.datetime(2026, 9, 27, 15, 59, 59, tzinfo=dt.timezone.utc)), selector.LEGACY)
        self.assertEqual(selector.select(self.value, dt.datetime(2026, 9, 27, 16, 0, tzinfo=dt.timezone.utc)), selector.CORE)
        self.assertEqual(selector.select(self.value, dt.datetime(2026, 9, 28, 22, 30, tzinfo=selector.CST)), selector.CORE)

    def test_invalid_present_policy_never_falls_back(self):
        for raw in ('{', '{"activeContract":"x","effectiveTradeDate":null}',
                    '{"activeContract":"research.core-market.v1","effectiveTradeDate":null}',
                    '{"activeContract":"research.core-market.v1","effectiveTradeDate":"2026-9-28"}',
                    '{"activeContract":"research.core-market.v1","activeContract":"research.full-snapshot.v1","effectiveTradeDate":null}'):
            self.policy.write_text(raw)
            with self.assertRaises(ValueError): selector.read_policy(self.policy)

    def test_symlink_directory_empty_oversized_policy_rejected(self):
        target = self.root / "other.json"
        target.write_text(json.dumps(self.value))
        self.policy.symlink_to(target)
        with self.assertRaises(ValueError): selector.read_policy(self.policy)
        self.policy.unlink(); self.policy.mkdir()
        with self.assertRaises(ValueError): selector.read_policy(self.policy)
        self.policy.rmdir()
        for raw in ("", " " * 4097):
            self.policy.write_text(raw)
            with self.assertRaises(ValueError): selector.read_policy(self.policy)

    def test_core_before_effective_target_defers_before_database_or_stage(self):
        db = self.root / "must-not-exist.db"
        with mock.patch.object(core_market, "qlib_market", return_value=qlib()), \
             mock.patch.object(core_market, "read_core") as read, \
             mock.patch.object(daily_snapshot, "load_akshare_market_stage") as load, \
             mock.patch.object(daily_snapshot, "capture_current_akshare_market") as collect:
            with self.assertRaises(daily_snapshot.SnapshotDeferredError) as caught:
                core_market.run_core(db_path=db, effective_trade_date="2026-09-28",
                                    now="2026-09-28T22:30:00+08:00", calendar_evidence=calendar("2026-09-28"))
            self.assertEqual(caught.exception.target, "2026-09-28")
            read.assert_not_called(); load.assert_not_called(); collect.assert_not_called()
        self.assertFalse(db.exists())

    def test_unzoned_clock_rejected(self):
        with self.assertRaises(ValueError): selector.select(self.value, dt.datetime(2026, 9, 28))

    def test_policy_format_shared_by_audit_and_dispatch(self):
        import data_health
        self.policy.write_text(json.dumps(self.value))
        self.assertEqual(data_health.read_contract_policy(self.policy), selector.read_policy(self.policy))
        self.policy.unlink()
        self.assertEqual(data_health.read_contract_policy(self.policy), selector.read_policy(self.policy))

    def test_wrapper_and_health_unit_use_same_policy_and_distinct_states(self):
        root = Path(__file__).resolve().parents[1]
        wrapper = (root / "scripts/run_daily_snapshot_with_alert.sh").read_text()
        unit = (root / "systemd/research-data-health.service").read_text()
        self.assertIn('CONTRACT_POLICY="${RD_ROOT}/config/snapshot-contract.json"', wrapper)
        self.assertIn('--contract-policy /home/ubuntu/research-data/config/snapshot-contract.json', unit)
        self.assertIn('LAST_RUN="${HOME}/.local/state/research-data/core-market/last-run.json"', wrapper)
        self.assertIn('--core-last-run /home/ubuntu/.local/state/research-data/core-market/last-run.json', unit)
        self.assertIn('else\n    "$PYTHON" "$SNAPSHOT"\n', wrapper)


if __name__ == "__main__":
    unittest.main()
