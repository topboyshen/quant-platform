"""Compiler-compatible evidence/limit protocol; temporary fixtures, no network."""
import errno
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_research_relay as relay_cases
from tests.test_research_relay import DAY, NOW, REPORT, relay


THEME = """- [推断] 主题 观察主题：下跌家数占优且成交额可核验，仍需继续核验
  - 支持证据（下跌占优）：[来源 2][陈旧]
  - 支持证据（成交额）：[来源 3][陈旧]
  - 推断限制：未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。
"""


def with_block(block):
    return (REPORT[:REPORT.index("## 6.")] + "## 6. 结构化分析覆盖层\n"
            + block + REPORT[REPORT.index("## 7."):])


class ResearchProtocolTests(unittest.TestCase):
    def summary(self, block=THEME):
        return relay.extract_summary(with_block(block), DAY, "partial", "not_human_reviewed")[1]

    def test_current_theme_preserves_support_roles_and_explicit_missing_counter(self):
        summary = self.summary()
        self.assertEqual(summary["counter_evidence"], [])
        self.assertEqual(summary["analysis_contract"], {
            "version": 2, "claim_type": "inference", "inference_status": "unverified",
            "counter_evidence_status": "explicitly_missing", "period_note": None,
            "inference_limit": relay.INFERENCE_LIMIT,
            "evidence_roles": [{"source_id": "2", "role": "下跌占优"},
                               {"source_id": "3", "role": "成交额"}]})
        self.assertIn("推断限制：" + relay.INFERENCE_LIMIT, summary["limitations"])
        self.assertTrue(all("[陈旧]" in item for item in summary["evidence"]))
        self.assertIn("[推断]", summary["conclusion"])
        self.assertEqual(len(summary["sources"]), 3)  # risk source + both support sources

    def test_current_stock_period_is_preserved_independently_from_support(self):
        block = THEME.replace("下跌占优", "单日行情").replace("成交额", "报告期利润同比")
        period = "行情 2026-09-08；利润同比报告期 2026-06-30"
        block = block.replace("  - 推断限制", "  - 期间：" + period + "。\n  - 推断限制")
        summary = self.summary(block)
        self.assertEqual(summary["analysis_contract"]["period_note"], period)
        self.assertIn("期间：" + period + "。", summary["limitations"])
        self.assertTrue(all("期间" not in item for item in summary["evidence"]))

    def test_role_support_and_real_counter_keep_counter_as_evidence(self):
        block = THEME.replace("  - 推断限制：" + relay.INFERENCE_LIMIT, "  - 反证：[来源 4]")
        summary = self.summary(block)
        self.assertEqual(summary["counter_evidence"], ["反证：[来源 4]"])
        self.assertEqual(summary["analysis_contract"]["counter_evidence_status"], "provided")
        self.assertIsNone(summary["analysis_contract"]["inference_limit"])

    def test_unknown_role_source_missing_limit_and_invalid_period_hold(self):
        for block, reason in (
                (THEME.replace("下跌占优", "任意标签"), "analysis_evidence_role_invalid"),
                (THEME.replace("[来源 2]", "[来源 99]"), "source_reference_missing"),
                (THEME.replace("  - 推断限制：" + relay.INFERENCE_LIMIT, ""), "analysis_counter_missing"),
                (THEME.replace(relay.INFERENCE_LIMIT, "没有反证所以已经验证。"), "analysis_limit_invalid"),
                (THEME + "  - 期间：行情 2026-02-30；利润同比报告期 2026-06-30。\n", "date_invalid"),
                (THEME + "  - 反证：[来源 4]\n", "analysis_limit_conflict")):
            with self.subTest(reason=reason), self.assertRaises(relay.Held) as raised:
                self.summary(block)
            self.assertEqual(str(raised.exception), reason)

    def test_new_invalid_seal_cannot_fall_back_to_valid_legacy(self):
        case = relay_cases.RelayTests()
        case.setUp()
        try:
            relative = case.write_research_seal(keep_legacy=True)
            real = case.root / relative
            real.unlink()
            real.symlink_to(case.root / case.seal_relative)
            self.assertEqual(case.execute(dry_run=True)["status"], "held")
            # Permission/I/O failures must be held even with valid legacy proof.
            original = relay.read_json
            for code in (errno.EACCES, errno.EIO, errno.ELOOP, errno.ENOTDIR):
                def read(root, name, limit=65536):
                    if name == relative:
                        raise OSError(code, "synthetic read failure")
                    return original(root, name, limit)
                with self.subTest(errno=code), patch.object(relay, "read_json", side_effect=read):
                    self.assertEqual(case.execute(dry_run=True)["status"], "held")
            real.unlink()
            self.assertEqual(case.execute(dry_run=True)["status"], "ready")
        finally:
            case.doCleanups()

    def test_v2_summary_is_accepted_by_the_actual_sibling_renderer(self):
        gate = Path(os.environ.get("SIGNAL_GATE_TEST_ROOT", str(Path(__file__).resolve().parents[2] / "signal-gate")))
        source = gate / "app/digest_content.py"
        if not source.exists():
            self.skipTest("sibling Signal Gate integration source is absent")
        case = relay_cases.RelayTests()
        case.setUp()
        try:
            case.report = with_block(THEME)
            case.write_fixture()
            payload = relay.build_payload(case.root, NOW)
            spec = importlib.util.spec_from_file_location("v2_consumer", source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            inbox = case.bus / "inbox/nng8"
            inbox.mkdir(parents=True)
            (inbox / "digest-latest.json").write_bytes(relay.encode_payload(payload))
            renderer = module.Renderer(case.bus, case.bus / "missing.db", NOW.astimezone(module.TZ))
            rendered = "\n".join(renderer.highlights("nng8"))
            self.assertEqual(renderer.issues, [])
            self.assertIn("未提供结构化反证来源", rendered)
            self.assertIn("下跌占优", rendered)
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
