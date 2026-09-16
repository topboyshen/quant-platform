"""Offline fixtures only: no models, production paths, HTTP or notification I/O."""
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error
import errno

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "research_relay.py"
spec = importlib.util.spec_from_file_location("research_relay", MODULE)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)
DAY = "2026-09-09"
NOW = dt.datetime(2026, 9, 9, 0, 5, tzinfo=dt.timezone.utc)
REPORT = """# A 股基础数据快报｜2026-09-09
> 数据截止：2026-09-09 08:00（Asia/Shanghai）
> 数据状态：部分可用
## 一句话结论
[事实] 基础行情已核验，不补造因果关系。[来源 1]
## 1. 数据覆盖与可用性
部分字段缺失。
## 2. 上一交易日 A 股行情（2026-09-08）
- [事实] 三项指数表现分化。[来源 1]
## 3. 隔夜数据与事件
没有编入事件判断。
## 4. 板块与股票池
略。
## 5. 今日政策、公告与事件
暂无可核验事件。
## 6. 结构化分析覆盖层
- [推断] 情景：这个块不应替代摘要。
  支持证据：[来源 1]
  反证：[来源 2]
- [推断] 摘要：市场线索可能分化，仍需观察反证。
  支持证据：[来源 2]
  反证：[来源 3]
  反证：[来源 4]
## 7. 风险提示与数据缺口
- 财务及公告字段不完整。[来源 1]
- 行情只对应上一交易日，并非盘中实时价格。
- 未核验的数据不能用于交易指令。
## 来源
1. 基础行情 — market_data；数据时间：2026-09-08 15:00；https://example.org/base
2. 上证指数历史日线 — market_data；数据时间：2026-09-08 15:00；https://example.org/sh
3. 深证成指历史日线 — market_data；数据时间：2026-09-08 15:00；https://example.org/sz
4. 创业板历史日线 — market_data；数据时间：2026-09-08 15:00；https://example.org/cy
---
不构成个性化投资建议。
"""


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project"
        self.bus = Path(self.temp.name).resolve() / "bus"
        self.root.mkdir()
        self.bus.mkdir()
        self.run = {"schemaVersion": "1.0", "targetDate": DAY,
                    "startedAt": "2026-09-09T00:00:04.910Z", "completedAt": "2026-09-09T00:00:24.000Z",
                    "frozenCutoffInstant": "2026-09-09T00:00:04.910Z", "cutoffLocal": DAY + " 08:00",
                    "status": "success", "phase": "publish", "dataStatus": "partial",
                    "publicationGate": "passed", "publicationBlockers": [], "evidenceReview": "passed",
                    "evidenceReviewScope": "structure_source_freshness_only", "researchSemanticReview": "not_human_reviewed",
                    "contextSha256": "a" * 64, "reportFile": f"reports/daily/{DAY}.md"}
        self.occurrence = {"schemaVersion": "1.0", "task": "morning-report", "targetDate": DAY,
                           "startedAt": "2026-09-09T00:00:04.000Z", "completedAt": "2026-09-09T00:00:25.000Z",
                           "status": "success", "childExitCode": 0, "childSignal": None,
                           "timedOut": False, "interrupted": False}
        self.report = REPORT
        self.write_fixture()
        # These guards make accidental use of any standard Python socket fail.
        self.network_guard = patch.object(socket, "socket", side_effect=AssertionError("test attempted network"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def write(self, relative, value):
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value if isinstance(value, bytes) else relay.encode_payload(value))
        return destination

    def write_fixture(self, source_patch=None, seal_patch=None):
        run_body = relay.encode_payload(self.run)
        report_body = self.report.encode()
        self.write("work/last-run.json", run_body)
        self.write(f"work/orchestrator/states/morning-report-{DAY}.json", self.occurrence)
        self.write(f"reports/daily/{DAY}.md", report_body)
        self.source = {"lastRunFile": "work/last-run.json", "reportFile": f"reports/daily/{DAY}.md",
                       "lastRunSha256": relay.sha256(run_body), "reportSha256": relay.sha256(report_body),
                       **{key: self.run[key] for key in ("contextSha256", "dataStatus", "researchSemanticReview",
                                                        "frozenCutoffInstant", "cutoffLocal", "publicationGate", "evidenceReview")}}
        self.source.update(source_patch or {})
        self.seal = {"schemaVersion": "astock.xhs-draft.v1", "kind": "xhs_draft", "targetDate": DAY,
                     "state": "DRAFT_ONLY", "publishEligible": False, "humanApprovalRequired": True,
                     "source": self.source, "content": {"body": "MUST_NOT_READ_XHS_BODY"}}
        self.seal.update(seal_patch or {})
        self.seal_relative = f"work/xhs/drafts/{DAY}/{relay.sha256(report_body)}.json"
        self.write(self.seal_relative, self.seal)

    def config(self, text=None):
        (self.bus / "config").mkdir(exist_ok=True)
        (self.bus / "config/pending.env").write_text(text or (
            "DEDIROCK_DIGEST_URL=https://example.org/PRIVATE_ENDPOINT\n"
            "DEDIROCK_PENDING_SECRET=" + "SUPER_SECRET_MUST_NOT_LEAK\nX_SOURCE=nng8\nTLS_VERIFY=1\n"))

    def execute(self, **kwargs):
        return relay.run_relay(self.root, self.bus, now=NOW, **kwargs)

    def assert_hold(self, reason):
        result = self.execute(dry_run=True)
        self.assertEqual(result["status"], "held", result)
        self.assertEqual(result["reason"], reason)

    def test_sealed_summary_uses_formal_report_complete_counterevidence(self):
        payload = relay.build_payload(self.root, NOW)
        self.assertEqual(payload["target_date"], DAY)
        self.assertEqual(payload["as_of"], "2026-09-08")
        self.assertEqual(payload["source"], "nng8")
        self.assertEqual(payload["signal_type"], "research_summary")
        summary = payload["research_summary"]
        self.assertEqual(summary["conclusion"], "[推断] 摘要：市场线索可能分化，仍需观察反证。\n支持证据引用：[来源 2]")
        self.assertEqual(summary["counter_evidence"], ["反证：[来源 3]", "反证：[来源 4]"])
        self.assertEqual(len(summary["sources"]), 4)
        self.assertNotIn("MUST_NOT_READ_XHS_BODY", json.dumps(payload))
        self.assertEqual(len(summary["risks"]), 3)
        self.assertIn("partial", "\n".join(summary["limitations"]))
        self.assertIn("人工审核", "\n".join(summary["limitations"]))
        self.assertIn("其余 1 个", "\n".join(summary["limitations"]))

    def test_dry_run_no_config_lock_state_or_network_daytime(self):
        before = sorted(str(p) for p in self.bus.rglob("*"))
        with patch.object(relay, "read_post_config", side_effect=AssertionError("config read")), \
                patch.object(relay, "_state_directory", side_effect=AssertionError("state write")):
            result = relay.run_relay(self.root, self.bus, now=NOW + dt.timedelta(hours=8), dry_run=True,
                                     sender=Mock(side_effect=AssertionError("send")))
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["would_send"])
        self.assertFalse(result["within_send_window"])
        self.assertEqual(before, sorted(str(p) for p in self.bus.rglob("*")))
        self.assertNotIn("推断", json.dumps(result, ensure_ascii=False))

    def test_window_hold_does_not_read_credentials(self):
        with patch.object(relay, "read_post_config", side_effect=AssertionError("config read")):
            result = relay.run_relay(self.root, self.bus, now=NOW.replace(hour=2), dry_run=False)
        self.assertEqual(result["reason"], "outside_send_window")

    def test_no_prior_day_fallback(self):
        self.run["targetDate"] = "2026-09-08"
        self.write_fixture()
        self.assert_hold("today_run_missing")

    def test_missing_occurrence_never_uses_last_run(self):
        (self.root / f"work/orchestrator/states/morning-report-{DAY}.json").unlink()
        self.assert_hold("required_artifact_or_config_invalid")

    def test_success_status_without_exit_proof_fails(self):
        for field, value in [("childExitCode", 1), ("childExitCode", False), ("childSignal", "SIGTERM"),
                             ("timedOut", True), ("interrupted", True), ("status", "running")]:
            with self.subTest(field=field, value=value):
                old = self.occurrence[field]
                self.occurrence[field] = value
                self.write_fixture()
                self.assert_hold("source_occurrence_not_successful")
                self.occurrence[field] = old

    def test_time_order_and_future_completion(self):
        self.run["completedAt"] = "2026-09-09T00:06:00Z"
        self.write_fixture()
        self.assert_hold("run_time_order_invalid")

    def test_gate_and_blockers_require_passed(self):
        for key, value in [("publicationGate", "blocked"), ("evidenceReview", "failed"),
                           ("publicationBlockers", ["missing"]), ("dataStatus", "fixture")]:
            old = self.run[key]
            self.run[key] = value
            self.write_fixture()
            self.assert_hold("content_gate_not_passed")
            self.run[key] = old

    def test_fixed_path_only(self):
        self.run["reportFile"] = "reports/daily/../secret.md"
        self.write_fixture()
        self.assert_hold("formal_report_path_invalid")

    def test_cutoff_must_match_day_and_local(self):
        self.run["cutoffLocal"] = "2026-09-08 08:00"
        self.write_fixture()
        self.assert_hold("frozen_cutoff_invalid")

    def test_seal_hash_and_metadata_mismatches(self):
        for key, value in [("lastRunSha256", "b" * 64), ("reportSha256", "b" * 64),
                           ("contextSha256", "b" * 64), ("dataStatus", "live"),
                           ("researchSemanticReview", "human_passed"), ("cutoffLocal", "2026-09-08 08:00")]:
            self.write_fixture(source_patch={key: value})
            self.assert_hold("sealed_report_proof_invalid")

    def test_seal_human_gate_cannot_be_bypassed(self):
        for key, value in [("publishEligible", True), ("humanApprovalRequired", False), ("state", "PUBLISHED")]:
            self.write_fixture(seal_patch={key: value})
            self.assert_hold("sealed_report_proof_invalid")

    def test_current_mutable_context_is_not_read(self):
        self.write("work/context.json", b"not-json-and-mutated")
        self.assertEqual(self.execute(dry_run=True)["status"], "ready")

    def test_market_date_must_precede_target(self):
        self.report = REPORT.replace("A 股行情（2026-09-08）", "A 股行情（2026-09-09）")
        self.write_fixture()
        self.assert_hold("market_date_invalid")

    def test_basic_report_labels_missing_analysis_without_invention(self):
        start = self.report.index("- [推断] 情景")
        end = self.report.index("## 7.")
        self.report = self.report[:start] + "本次没有程序证明合格的语义候选。\n" + self.report[end:]
        self.write_fixture()
        summary = relay.build_payload(self.root, NOW)["research_summary"]
        self.assertIn("基础版", "\n".join(summary["limitations"]))
        self.assertIn("[事实]", summary["conclusion"])
        self.assertEqual(summary["counter_evidence"], [])

    def basic_fixture(self, conclusion):
        """Synthetic sealed basic report; never borrow live report contents."""
        start, end = REPORT.index("- [推断] 情景"), REPORT.index("## 7.")
        basic = REPORT[:start] + "本次没有程序证明合格的语义候选。\n" + REPORT[end:]
        begin, end = basic.index("## 一句话结论\n"), basic.index("## 1.")
        self.report = basic[:begin] + "## 一句话结论\n" + conclusion + "\n" + basic[end:]
        self.write_fixture()

    def test_basic_list_conclusion_preserves_unlabelled_original_followup(self):
        original = "[事实] 合成基础行情已核验。[来源 1]\n- 无标签原说明，不增加事实标签。\n+ [推断] 原文补充仍保留列表。"
        self.basic_fixture("\n \t\n- " + original)
        summary = relay.build_payload(self.root, NOW)["research_summary"]
        self.assertEqual(summary["conclusion"], original)
        self.assertEqual(summary["evidence"], ["- [事实] 三项指数表现分化。[来源 1]"])
        self.assertEqual(summary["counter_evidence"], [])

    def test_basic_list_conclusion_accepts_only_single_markers_and_horizontal_space(self):
        for marker in ("-", "+", "*"):
            for whitespace in (" ", "\t", " \t "):
                for tag in ("事实", "推断"):
                    with self.subTest(marker=marker, whitespace=repr(whitespace), tag=tag):
                        original = f"[{tag}] 合成原始结论。[来源 1]"
                        self.basic_fixture(marker + whitespace + original)
                        summary = relay.build_payload(self.root, NOW)["research_summary"]
                        self.assertEqual(summary["conclusion"], original)

    def test_basic_plain_labelled_conclusion_is_unchanged(self):
        for tag in ("事实", "推断"):
            with self.subTest(tag=tag):
                original = f"[{tag}] 合成原始结论。[来源 1]\n- 无标签原说明。"
                self.basic_fixture(original)
                summary = relay.build_payload(self.root, NOW)["research_summary"]
                self.assertEqual(summary["conclusion"], original)

    def test_basic_list_conclusion_does_not_invent_or_unwrap_labels(self):
        for invalid in ("- 没有标签。[来源 1]", "没有标签。[来源 1]",
                        "- - [事实] 双层标记。[来源 1]", "> - [事实] 引用前缀。[来源 1]",
                        "- **[事实]** 加粗标签。[来源 1]", "-[事实] 缺少空白。[来源 1]",
                        "-\u00a0[事实] 不换行空格。[来源 1]", "-\u200b[事实] 零宽字符。[来源 1]",
                        "-\v[事实] 纵向空白。[来源 1]", "-\f[事实] 换页字符。[来源 1]"):
            with self.subTest(conclusion=repr(invalid)):
                self.basic_fixture(invalid)
                self.assert_hold("conclusion_label_missing")

    def test_basic_list_conclusion_still_requires_resolvable_source_references(self):
        self.basic_fixture("- [事实] 没有结论出处。")
        self.report = self.report.replace("- [事实] 三项指数表现分化。[来源 1]", "- [事实] 无引用的行情。").replace(
            "- 财务及公告字段不完整。[来源 1]", "- 财务及公告字段不完整。")
        self.write_fixture()
        self.assert_hold("conclusion_reference_missing")
        self.basic_fixture("- [事实] 引用不存在的来源。[来源 99]")
        self.assert_hold("source_reference_missing")

    def test_basic_list_conclusion_preserves_partial_and_human_review_limits(self):
        self.basic_fixture("- [事实] 合成基础行情已核验。[来源 1]\n- 不能据此推断因果。")
        payload = relay.build_payload(self.root, NOW)
        summary, contract = payload["research_summary"], payload["research_contract"]
        self.assertEqual(contract["data_status"], "partial")
        self.assertEqual(contract["semantic_review"], "not_human_reviewed")
        self.assertEqual(contract["report_date"], DAY)
        self.assertEqual(payload["as_of"], "2026-09-08")
        limitations = "\n".join(summary["limitations"])
        for text in ("基础版", "部分来源或字段不可用", "研究语义未通过人工审核", "不是交易指令"):
            self.assertIn(text, limitations)
        self.assertEqual(len(summary["risks"]), 3)
        self.assertEqual(summary["counter_evidence"], [])

    def test_basic_list_dry_run_does_not_read_credentials_write_state_or_send(self):
        self.basic_fixture("- [事实] 合成基础行情已核验。[来源 1]\n- 无标签原说明。")
        self.config()
        before = {str(p.relative_to(Path(self.temp.name))): p.read_bytes()
                  for p in Path(self.temp.name).rglob("*") if p.is_file()}
        sender = Mock(side_effect=AssertionError("send"))
        with patch.object(relay, "read_post_config", side_effect=AssertionError("config read")), \
                patch.object(relay, "_state_directory", side_effect=AssertionError("state directory")), \
                patch.object(relay, "_write_state", side_effect=AssertionError("state write")):
            for now, within_window in ((NOW, True), (NOW + dt.timedelta(hours=8), False)):
                with self.subTest(within_window=within_window):
                    result = relay.run_relay(self.root, self.bus, now=now, dry_run=True, sender=sender)
                    self.assertEqual(result["status"], "ready", result)
                    self.assertFalse(result["would_send"])
                    self.assertEqual(result["within_send_window"], within_window)
                    self.assertNotIn("SUPER_SECRET", json.dumps(result))
        sender.assert_not_called()
        after = {str(p.relative_to(Path(self.temp.name))): p.read_bytes()
                 for p in Path(self.temp.name).rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_basic_list_normalization_does_not_replace_structured_analysis(self):
        original = relay.build_payload(self.root, NOW)["research_summary"]
        self.report = REPORT.replace("[事实] 基础行情已核验，不补造因果关系。[来源 1]",
                                     "- [事实] 不应替代结构化研究的基础说明。[来源 1]")
        self.write_fixture()
        summary = relay.build_payload(self.root, NOW)["research_summary"]
        self.assertEqual(summary, original)

    def test_missing_source_reference_blocks(self):
        self.report = REPORT.replace("  反证：[来源 4]", "  反证：[来源 99]")
        self.write_fixture()
        self.assert_hold("source_reference_missing")

    def test_compound_references_resolve_every_member(self):
        self.report = REPORT.replace("  反证：[来源 3]\n  反证：[来源 4]", "  反证：[来源 3、4]")
        self.write_fixture()
        payload = relay.build_payload(self.root, NOW)
        self.assertEqual(payload["research_summary"]["counter_evidence"], ["反证：[来源 3、4]"])
        self.assertEqual(len(payload["research_summary"]["sources"]), 4)

    def test_compound_reference_unknown_member_is_not_ignored(self):
        for citation in ("[来源 2、999]", "[来源 2,999]", "[来源 2，999]", "[来源 2 999]"):
            self.report = REPORT.replace("  反证：[来源 4]", "  反证：" + citation)
            self.write_fixture()
            self.assert_hold("source_reference_missing")

    def test_malformed_reference_fails_closed(self):
        for citation in ("[来源 2、]", "[来源 2–4]", "[来源 ]", "[来源 2", "[来源 0]", "[来源 02]"):
            self.report = REPORT.replace("  反证：[来源 4]", "  反证：" + citation)
            self.write_fixture()
            self.assert_hold("source_reference_invalid")

    def test_inference_support_requires_real_citation(self):
        self.report = REPORT.replace("  支持证据：[来源 2]", "  支持证据：没有可核验引用")
        self.write_fixture()
        self.assert_hold("analysis_support_missing")

    def test_basic_conclusion_without_reference_uses_retained_fact_reference(self):
        start = self.report.index("- [推断] 情景")
        end = self.report.index("## 7.")
        self.report = self.report[:start] + "本次没有程序证明合格的语义候选。\n" + self.report[end:]
        self.report = self.report.replace("[事实] 基础行情已核验，不补造因果关系。[来源 1]", "[事实] 本版只报告基础行情。")
        self.write_fixture()
        summary = relay.build_payload(self.root, NOW)["research_summary"]
        self.assertIn("支持证据引用：[来源 1]", summary["conclusion"])

    def test_list_limit_holds_without_truncation(self):
        self.report = REPORT.replace("- 行情只对应上一交易日，并非盘中实时价格。", "\n".join(f"- 第 {index} 个风险。" for index in range(25)))
        self.write_fixture()
        self.assert_hold("research_summary_too_large")

    def test_single_item_limit_holds_without_truncation(self):
        self.report = REPORT.replace("- 行情只对应上一交易日，并非盘中实时价格。", "- " + "风险" * 601)
        self.write_fixture()
        self.assert_hold("research_summary_too_large")

    def test_missing_counterevidence_is_explicit_not_invented(self):
        self.report = REPORT.replace("  反证：[来源 3]\n  反证：[来源 4]\n", "")
        self.write_fixture()
        self.assert_hold("analysis_counter_missing")

    def test_only_original_risk_placeholder_is_preserved(self):
        self.report = REPORT.replace("[推断] 摘要：", "[推断] 风险：").replace(
            "  反证：[来源 3]\n  反证：[来源 4]", "  反证：未提供结构化反证来源，不升级为事实结论。")
        # Make this the chosen first block (there is deliberately no 摘要).
        self.report = self.report.replace("- [推断] 情景：这个块不应替代摘要。\n  支持证据：[来源 1]\n  反证：[来源 2]\n", "")
        self.write_fixture()
        summary = relay.build_payload(self.root, NOW)["research_summary"]
        self.assertEqual(summary["counter_evidence"], ["反证：未提供结构化反证来源，不升级为事实结论。"])
        self.assertIn("不是已核验反证", "\n".join(summary["limitations"]))

    def test_placeholder_cannot_bypass_missing_counter_on_summary(self):
        self.report = REPORT.replace("  反证：[来源 3]\n  反证：[来源 4]", "  反证：未提供结构化反证来源，不升级为事实结论。")
        self.write_fixture()
        self.assert_hold("analysis_counter_missing")

    def test_uncited_second_support_item_is_not_hidden_by_first(self):
        self.report = REPORT.replace("  支持证据：[来源 2]", "  支持证据：[来源 2]\n  支持证据：没有引用的另一理由")
        self.write_fixture()
        self.assert_hold("analysis_support_missing")

    def test_source_query_credentials_removed(self):
        self.report = REPORT.replace("https://example.org/sz", "https://example.org/sz?token=PRIVATE_TOKEN#secret")
        self.write_fixture()
        payload = relay.build_payload(self.root, NOW)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(payload))
        self.assertNotIn("#secret", json.dumps(payload))
        self.assertIn("链接已移除", "\n".join(payload["research_summary"]["limitations"]))

    def test_private_or_userinfo_source_url_hidden(self):
        for url in ("https://user:pw@example.org/a", "https://127.0.0.1/private", "https://service.internal/a"):
            self.report = REPORT.replace("https://example.org/sz", url)
            self.write_fixture()
            payload = relay.build_payload(self.root, NOW)
            self.assertNotIn(url, json.dumps(payload))
            self.assertIn("链接已隐藏", payload["text"])

    def test_oversized_full_block_fails_without_dropping_counterevidence(self):
        self.report = REPORT.replace("  反证：[来源 4]", "  反证：" + "保留反证" * 5000 + "[来源 4]")
        self.write_fixture()
        self.assert_hold("research_summary_too_large")

    def test_symlink_file_rejected(self):
        report = self.root / f"reports/daily/{DAY}.md"
        saved = report.with_suffix(".real")
        report.rename(saved)
        report.symlink_to(saved)
        self.assert_hold("required_artifact_or_config_invalid")

    def test_symlink_parent_rejected(self):
        original = self.root / "reports/daily"
        replacement = self.root / "reports/actual"
        original.rename(replacement)
        original.symlink_to(replacement, target_is_directory=True)
        self.assert_hold("required_artifact_or_config_invalid")

    def test_fifo_artifact_rejected_without_blocking(self):
        report = self.root / f"reports/daily/{DAY}.md"
        report.unlink()
        os.mkfifo(report, 0o600)
        self.assert_hold("artifact_file_invalid")

    def test_duplicate_json_fields_fail_closed(self):
        self.write("work/last-run.json", b'{"status":"success","status":"failed"}')
        self.assert_hold("artifact_json_invalid")

    def test_bundle_change_during_read_fails(self):
        original = relay.read_artifact
        counter = [0]

        def changing(root, relative, limit=65536):
            result = original(root, relative, limit)
            counter[0] += 1
            if counter[0] == 4:
                self.write("work/last-run.json", relay.encode_payload(self.run) + b"\n")
            return result

        with patch.object(relay, "read_artifact", side_effect=changing):
            self.assert_hold("artifact_changed")

    def test_oversize_or_non_object_json_rejected(self):
        self.write("work/last-run.json", b" " * 65537)
        self.assert_hold("artifact_file_invalid")
        self.write("work/last-run.json", b"[]")
        self.assert_hold("artifact_json_invalid")

    def test_multiline_counterevidence_not_truncated(self):
        self.report = REPORT.replace("  反证：[来源 3]", "  反证：[来源 3]\n    不能排除另一解释；仍需后续数据验证。")
        self.write_fixture()
        summary = relay.build_payload(self.root, NOW)["research_summary"]
        self.assertEqual(len(summary["counter_evidence"]), 2)
        self.assertIn("不能排除另一解释", "\n".join(summary["counter_evidence"]))
        self.assertTrue(all(relay.references_in(item) for item in summary["counter_evidence"]))

    def test_missing_risks_fails_without_fabricating_completeness(self):
        begin = REPORT.index("## 7.")
        end = REPORT.index("## 来源")
        self.report = REPORT[:begin] + "## 7. 风险提示与数据缺口\n" + REPORT[end:]
        self.write_fixture()
        self.assert_hold("risk_section_empty")

    def test_confirmed_delivery_deduplicated(self):
        self.config()
        sender = Mock(return_value={"status": "confirmed", "reason": "accepted"})
        self.assertEqual(self.execute(sender=sender)["status"], "confirmed")
        self.assertEqual(self.execute(sender=sender)["status"], "already_confirmed")
        self.assertEqual(sender.call_count, 1)
        state = (self.bus / f"out/relay-delivery/{DAY}.json").read_text()
        self.assertNotIn("SUPER_SECRET", state)
        self.assertNotIn("PRIVATE_ENDPOINT", state)
        self.assertNotIn("推断", state)

    def test_ambiguous_delivery_never_auto_retried(self):
        self.config()
        sender = Mock(side_effect=TimeoutError("PRIVATE_URL SUPER_SECRET"))
        self.assertEqual(self.execute(sender=sender)["status"], "ambiguous")
        self.assertEqual(self.execute(sender=sender)["reason"], "delivery_requires_manual_review")
        self.assertEqual(sender.call_count, 1)

    def test_explicit_rejection_can_retry(self):
        self.config()
        sender = Mock(side_effect=[{"status": "rejected", "reason": "http_rejected"},
                                   {"status": "confirmed", "reason": "accepted"}])
        self.assertEqual(self.execute(sender=sender)["status"], "rejected")
        self.assertEqual(self.execute(sender=sender)["status"], "confirmed")
        self.assertEqual(sender.call_count, 2)

    def test_changed_same_day_report_does_not_send(self):
        self.config()
        sender = Mock(return_value={"status": "confirmed", "reason": "accepted"})
        self.execute(sender=sender)
        self.report += "\n"
        self.write_fixture()
        self.assertEqual(self.execute(sender=sender)["reason"], "same_day_report_changed")
        self.assertEqual(sender.call_count, 1)

    def test_inflight_state_does_not_retry_after_crash(self):
        self.config()
        self.execute(sender=Mock(return_value={"status": "confirmed", "reason": "accepted"}))
        state_path = self.bus / f"out/relay-delivery/{DAY}.json"
        state = json.loads(state_path.read_text())
        state["status"] = "sending"
        state_path.write_bytes(relay.encode_payload(state))
        sender = Mock(side_effect=AssertionError("not allowed"))
        self.assertEqual(self.execute(sender=sender)["reason"], "delivery_requires_manual_review")
        sender.assert_not_called()

    def test_concurrent_lock_holds_without_sender(self):
        self.config()
        sender = Mock(side_effect=AssertionError("not allowed"))
        with patch.object(relay.fcntl, "flock", side_effect=BlockingIOError(errno.EAGAIN, "locked")):
            result = self.execute(sender=sender)
        self.assertEqual(result["reason"], "delivery_busy")
        sender.assert_not_called()

    def test_pre_send_state_write_failure_never_posts(self):
        self.config()
        sender = Mock(side_effect=AssertionError("not allowed"))
        with patch.object(relay, "_write_state", side_effect=OSError("PRIVATE ERROR")):
            result = self.execute(sender=sender)
        self.assertEqual(result["status"], "held")
        sender.assert_not_called()
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_confirmation_state_write_failure_leaves_sending_for_manual_review(self):
        self.config()
        sender = Mock(return_value={"status": "confirmed", "reason": "accepted"})
        original = relay._write_state
        count = [0]

        def fail_confirmation(*args):
            count[0] += 1
            if count[0] > 1:
                raise OSError("write error")
            return original(*args)

        with patch.object(relay, "_write_state", side_effect=fail_confirmation):
            self.assertEqual(self.execute(sender=sender)["status"], "held")
        self.assertEqual(self.execute(sender=sender)["reason"], "delivery_requires_manual_review")
        self.assertEqual(sender.call_count, 1)

    def test_no_auto_send_weekend_or_after_cutoff(self):
        prepared = relay.prepare_research(self.root, NOW)
        self.assertTrue(relay.send_window(NOW.replace(minute=13, second=59), prepared))
        self.assertFalse(relay.send_window(NOW.replace(minute=14), prepared))
        self.assertFalse(relay.send_window(NOW.replace(day=12), prepared))

    def test_send_boundary_rechecks_window(self):
        self.config()
        sender = Mock(side_effect=AssertionError("not allowed"))
        result = self.execute(sender=sender, clock=lambda: NOW.replace(minute=14))
        self.assertEqual(result["reason"], "outside_send_window")
        sender.assert_not_called()

    def test_tls_verification_cannot_be_disabled(self):
        self.config("DEDIROCK_DIGEST_URL=https://example.org/private\nDEDIROCK_PENDING_SECRET=test-secret\nTLS_VERIFY=0\n")
        self.assertEqual(self.execute()["reason"], "post_tls_pin_missing_or_invalid")

    def pin_fixture(self, host="example.org", certificate_hash=None):
        self.config("DEDIROCK_DIGEST_URL=https://example.org/private\nDEDIROCK_PENDING_SECRET=test-secret\nTLS_VERIFY=0\n")
        pin = {"version": 1, "host": host, "certificate_sha256": certificate_hash or relay.sha256(b"test-public-der")}
        (self.bus / "config/relay-tls-pin.json").write_bytes(relay.encode_payload(pin))
        # Explicitly synthetic PEM: parser/context must be mocked in tests; this
        # material is never a deployable trust certificate.
        (self.bus / "config/dedirock-relay-ca.crt").write_text("-----BEGIN CERTIFICATE-----\nTEST_PUBLIC_CERTIFICATE\n-----END CERTIFICATE-----\n")

    def test_valid_explicit_pin_mode_keeps_certificate_verification(self):
        self.pin_fixture()
        context = ssl.create_default_context()
        with patch.object(relay.ssl, "PEM_cert_to_DER_cert", return_value=b"test-public-der"), \
                patch.object(relay.ssl, "create_default_context", return_value=context):
            config = relay.read_post_config(self.bus)
            handler = relay._PinnedHTTPSHandler(ca_pem=config["ca_pem"], pin_host=config["pin_host"],
                                                certificate_sha256=config["certificate_sha256"])
        self.assertEqual(config["tls_mode"], "pinned")
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(handler._context.check_hostname)
        self.assertEqual(handler._pin_host, "example.org")

    def test_pin_host_must_exactly_match_endpoint(self):
        self.pin_fixture(host="elsewhere.invalid")
        self.assertEqual(self.execute()["reason"], "post_tls_pin_missing_or_invalid")

    def test_local_public_certificate_hash_must_match_pin(self):
        self.pin_fixture(certificate_hash="b" * 64)
        with patch.object(relay.ssl, "PEM_cert_to_DER_cert", return_value=b"test-public-der"):
            self.assertEqual(self.execute()["reason"], "post_tls_pin_missing_or_invalid")

    def test_pin_mode_missing_ca_never_posts(self):
        self.pin_fixture()
        (self.bus / "config/dedirock-relay-ca.crt").unlink()
        sender = Mock(side_effect=AssertionError("not allowed"))
        self.assertEqual(self.execute(sender=sender)["reason"], "post_tls_pin_missing_or_invalid")
        sender.assert_not_called()

    def test_strict_mode_does_not_read_pin_files(self):
        self.config()
        original = relay.read_artifact

        def no_pin(root, relative, limit=65536):
            self.assertEqual(relative, "config/pending.env")
            return original(root, relative, limit)

        with patch.object(relay, "read_artifact", side_effect=no_pin):
            config = relay.read_post_config(self.bus)
        self.assertEqual(config["tls_mode"], "strict")
        self.assertNotIn("ca_pem", config)

    def pinned_connection(self, *, host="example.org", actual_der=b"test-public-der"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        connection = relay._PinnedHTTPSConnection(host, context=context, pin_host="example.org",
                                                  certificate_sha256=relay.sha256(b"test-public-der"))
        peer = Mock()
        peer.getpeercert.return_value = actual_der
        return connection, peer

    def test_mismatched_peer_pin_writes_zero_http_bytes(self):
        connection, peer = self.pinned_connection(actual_der=b"different-public-der")

        def successful_handshake():
            connection.sock = peer

        with patch.object(relay.http.client.HTTPSConnection, "connect", side_effect=successful_handshake):
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.request("POST", "/PRIVATE_PATH", b"PRIVATE_BODY", {"Authorization": "PRIVATE_HEADER"})
        peer.sendall.assert_not_called()
        peer.close.assert_called_once()
        self.assertIsNone(connection.sock)

    def test_matched_peer_pin_is_checked_before_http_bytes(self):
        connection, peer = self.pinned_connection()

        def successful_handshake():
            connection.sock = peer

        with patch.object(relay.http.client.HTTPSConnection, "connect", side_effect=successful_handshake):
            connection.request("POST", "/test", b"test-body")
        self.assertTrue(peer.sendall.called)
        self.assertEqual(peer.method_calls[0], unittest.mock.call.getpeercert(binary_form=True))

    def test_chain_verification_failure_writes_zero_http_bytes(self):
        connection, peer = self.pinned_connection()
        with patch.object(relay.http.client.HTTPSConnection, "connect", side_effect=ssl.SSLCertVerificationError("fixed")):
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.request("POST", "/private", b"private")
        peer.sendall.assert_not_called()

    def test_pin_endpoint_mismatch_does_not_connect(self):
        connection, peer = self.pinned_connection(host="elsewhere.invalid")
        with patch.object(relay.http.client.HTTPSConnection, "connect") as connect:
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.request("POST", "/private", b"private")
        connect.assert_not_called()
        peer.sendall.assert_not_called()

    def test_pinned_connection_forbids_proxy_tunnel(self):
        connection, _ = self.pinned_connection()
        with self.assertRaises(ssl.SSLCertVerificationError):
            connection.set_tunnel("example.org", 443)

    def test_http_post_config_rejected(self):
        self.config("DEDIROCK_DIGEST_URL=http://example.org/private\nDEDIROCK_PENDING_SECRET=test-secret\n")
        self.assertEqual(self.execute()["reason"], "post_config_invalid")

    def test_transport_validates_fixed_ack_only_and_hmac(self):
        payload = relay.build_payload(self.root, NOW)
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"status":"accepted","source":"nng8","day":"2026-09-08","path":"PRIVATE_RESPONSE_PATH"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(relay.urllib.request, "build_opener", return_value=opener) as build_opener:
            result = relay.post_payload(payload, {"url": "https://example.org/SECRET_PATH", "secret": "test"}, NOW)
        self.assertEqual(result, {"status": "confirmed", "reason": "accepted",
                                  "ack_scope": "http_accepted",
                                  "receiver_storage": "not_in_acknowledgement"})
        request = opener.open.call_args.args[0]
        timestamp = str(int(NOW.timestamp()))
        expected = "sha256=" + relay.hmac.new(b"test", timestamp.encode() + b"." + request.data, relay.hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-signature"), expected)
        self.assertTrue(request.get_header("X-signature").startswith("sha256="))
        self.assertEqual(request.get_header("X-source"), "nng8")
        self.assertEqual(request.data, relay.encode_payload(payload))
        self.assertNotIn("PRIVATE_RESPONSE_PATH", json.dumps(result))
        proxy = next(handler for handler in build_opener.call_args.args if isinstance(handler, relay.urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})

    def test_ack_stored_flag_is_classified_without_copying_extra_fields(self):
        payload = relay.build_payload(self.root, NOW)
        response = Mock(status=200)
        response.read.return_value = b'{"status":"accepted","source":"nng8","day":"2026-09-08","stored":true,"path":"SECRET_STORE"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(relay.urllib.request, "build_opener", return_value=opener):
            result = relay.post_payload(payload, {"url": "https://example.org/a", "secret": "test"}, NOW)
        self.assertEqual(result["receiver_storage"], "receiver_reported_stored")
        self.assertEqual(result["ack_scope"], "http_accepted")
        self.assertNotIn("SECRET_STORE", json.dumps(result))

    def test_transport_mismatched_ack_is_ambiguous(self):
        response = Mock(status=200)
        response.read.return_value = b'{"status":"accepted","source":"jd","day":"2026-09-08"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(relay.urllib.request, "build_opener", return_value=opener):
            result = relay.post_payload(relay.build_payload(self.root, NOW), {"url": "https://example.org/a", "secret": "test"}, NOW)
        self.assertEqual(result["status"], "ambiguous")

    def test_redirect_is_never_followed(self):
        self.assertIsNone(relay._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_transport_dns_failure_known_unsent(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(socket.gaierror("PRIVATE_DNS_NAME"))
        with patch.object(relay.urllib.request, "build_opener", return_value=opener):
            result = relay.post_payload(relay.build_payload(self.root, NOW), {"url": "https://example.org/a", "secret": "test"}, NOW)
        self.assertEqual(result["status"], "not_sent")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_transport_rejection_and_unknown_do_not_include_errors(self):
        opener = Mock()
        for code, expected in [(401, "rejected"), (429, "rejected"), (500, "ambiguous"), (302, "ambiguous")]:
            opener.open.side_effect = urllib.error.HTTPError("PRIVATE_URL", code, "SUPER_SECRET", {}, None)
            with patch.object(relay.urllib.request, "build_opener", return_value=opener):
                result = relay.post_payload(relay.build_payload(self.root, NOW), {"url": "https://example.org/a", "secret": "test"}, NOW)
            self.assertEqual(result["status"], expected)
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_cli_dry_run_only_exposes_safe_metadata(self):
        result = self.execute(dry_run=True)
        with patch.object(relay, "run_relay", return_value=result) as execute, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(relay.main(["--dry-run", "--json"]), 0)
        execute.assert_called_once_with(dry_run=True)
        self.assertEqual(json.loads(output.getvalue())["sent"], 0)

    def test_optional_sibling_consumer_contract_in_memory(self):
        """Run in this integration worktree; production standalone install skips.

        No inputs are written to the consumer, and only its pure renderer method
        is called. All payloads still come from temporary sealed fixtures.
        """
        consumer_path = MODULE.parents[2] / "signal-gate/app/digest_content.py"
        if not consumer_path.is_file():
            self.skipTest("standalone relay install: sibling consumer source not present")
        consumer_spec = importlib.util.spec_from_file_location("relay_consumer_contract", consumer_path)
        consumer = importlib.util.module_from_spec(consumer_spec)
        consumer_spec.loader.exec_module(consumer)
        basic_start, basic_end = REPORT.index("- [推断] 情景"), REPORT.index("## 7.")
        basic = REPORT[:basic_start] + "本次没有程序证明合格的语义候选。\n" + REPORT[basic_end:]
        risk = REPORT.replace("- [推断] 情景：这个块不应替代摘要。\n  支持证据：[来源 1]\n  反证：[来源 2]\n", "")
        risk = risk.replace("[推断] 摘要：", "[推断] 风险：").replace(
            "  反证：[来源 3]\n  反证：[来源 4]", "  反证：未提供结构化反证来源，不升级为事实结论。")
        cases = {
            "production_shape": REPORT,
            "basic": basic,
            "basic_list": basic.replace(
                "[事实] 基础行情已核验，不补造因果关系。[来源 1]",
                "- [事实] 基础行情已核验，不补造因果关系。[来源 1]\n- 无标签原说明，仍须人工复核。"),
            "compound_reference": REPORT.replace("  反证：[来源 3]\n  反证：[来源 4]", "  反证：[来源 3 4]"),
            "risk_placeholder": risk,
            "counter_continuation": REPORT.replace("  反证：[来源 3]", "  反证：[来源 3]\n    不能排除另一解释；仍需后续数据验证。"),
        }
        for label, report in cases.items():
            with self.subTest(case=label):
                self.report = report
                self.write_fixture()
                payload = relay.build_payload(self.root, NOW)
                renderer = consumer.Renderer(self.root, self.root / "unused.db", NOW.astimezone(relay.SHANGHAI))
                lines = renderer.sealed_research(payload)
                self.assertEqual(renderer.issues, [], label)
                self.assertNotIn("旧稿未纳入", "\n".join(lines))
                if label == "basic_list":
                    self.assertIn("无标签原说明，仍须人工复核。", "\n".join(lines))
                if label == "counter_continuation":
                    self.assertIn("不能排除另一解释", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
