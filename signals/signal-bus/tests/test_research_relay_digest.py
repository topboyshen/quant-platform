"""Structured-digest consumer tests for the research relay (batch 2).

These tests are self-contained: they hand-build the sealed artifacts so the
consumer contract can be exercised without running the AStock compiler. The
cross-repo chain (real compile -> digest -> seal -> relay) is covered by the
release-package script; here the point is the consumer's own gates:

- a display-only body change must never block delivery (labels are irrelevant);
- the digest's byte hash, date, report and archive bindings are all enforced;
- a missing or corrupt digest holds with zero sender invocations;
- missing real counter-evidence stays a limitation instead of being invented.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

RELAY = Path(__file__).resolve().parents[1] / "scripts" / "research_relay.py"
spec = importlib.util.spec_from_file_location("research_relay_under_test", RELAY)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load relay module from {RELAY}")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

DAY = "2026-09-24"
SHANGHAI = timezone(timedelta(hours=8))
REPORT_BODY = "# AStock 数据快报｜2026-09-24\n\n## 2. 上一交易日 A 股行情（2026-09-23）\n\n> 展示改字：支持证据（成交额）：[来源 5]\n"


def digest_body(*, report_sha, input_sha, data_date="2026-09-23", items=None, facts=None, limitations=None):
    payload = {
        "schemaVersion": relay.DIGEST_SCHEMA,
        "kind": relay.DIGEST_KIND,
        "reportDate": DAY,
        "dataDate": data_date,
        "reportSha256": report_sha,
        "inputSha256": input_sha,
        "selectionSha256": "a" * 64,
        "reportView": "research_brief_v3",
        "conclusionLines": ["- [事实] 数据包结论行。"],
        "facts": facts if facts is not None else ["- [事实] 上一交易日行情：[来源 1]"],
        "items": items if items is not None else [{
            "kind": "theme", "label": "观察主题", "code": None,
            "inference": "下跌家数占优且成交额可核验，仍需继续核验",
            "evidence": [{"sourceId": "src-a", "reference": "[来源 1]", "role": "成交额"}],
            "counterEvidence": [], "periodNote": None,
            "limitation": "未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。",
        }],
        "sourceIndex": [{"number": 1, "sourceId": "src-a", "title": "交易所每日概况", "url": "https://example.invalid/a"}],
        "limitations": limitations if limitations is not None else ["未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。"],
        "generatedAt": "2026-09-24T00:00:33.455Z",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


class DigestConsumerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="relay-digest-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.project = self.root / "astock"
        (self.project / "config").mkdir(parents=True)
        (self.project / "work/orchestrator/states").mkdir(parents=True)
        (self.project / "reports/daily").mkdir(parents=True)
        (self.project / f"work/report-archives/{DAY}").mkdir(parents=True)
        (self.project / f"work/research-seal/{DAY}").mkdir(parents=True)
        astock = Path(os.environ.get("ASTOCK_TEST_ROOT", "/home/ubuntu/AStock_AI_System"))
        shutil.copy(astock / "config/risk.json", self.project / "config/risk.json")
        self.report = REPORT_BODY
        self.report_sha = hashlib.sha256(self.report.encode()).hexdigest()
        (self.project / f"reports/daily/{DAY}.md").write_text(self.report)
        self.input_sha = "b" * 64
        (self.project / f"work/report-archives/{DAY}/input.sha256").write_text(f"{self.input_sha}\n")
        (self.project / "work/last-run.json").write_text(json.dumps({
            "schemaVersion": "1.0", "targetDate": DAY,
            "startedAt": "2026-09-24T00:00:04.000Z", "frozenCutoffInstant": "2026-09-24T00:00:03.753Z",
            "cutoffLocal": "2026-09-24 08:00", "contextFile": "work/morning-context.json",
            "contextSha256": "c" * 64, "completedAt": "2026-09-24T00:00:33.455Z",
            "dataStatus": "partial", "publicationGate": "passed", "evidenceReview": "passed",
            "publicationBlockers": [], "researchSemanticReview": "not_human_reviewed",
            "status": "success", "phase": "publish", "reportFile": f"reports/daily/{DAY}.md",
        }, indent=2))
        (self.project / f"work/orchestrator/states/morning-report-{DAY}.json").write_text(json.dumps({
            "schemaVersion": "1.0", "task": "morning-report", "targetDate": DAY,
            "startedAt": "2026-09-24T00:00:02.699Z", "completedAt": "2026-09-24T00:00:33.900Z",
            "status": "success", "childExitCode": 0, "childSignal": None,
            "timedOut": False, "interrupted": False,
        }, indent=2))
        self.bus = self.root / "bus"
        (self.bus / "config").mkdir(parents=True)
        (self.bus / "config/pending.env").write_text(
            "export DEDIROCK_DIGEST_URL=https://receiver.invalid/ingest\n"
            "export DEDIROCK_PENDING_SECRET=test-secret\n"
            "export X_SOURCE=nng8\n"
            "export TLS_VERIFY=1\n")
        self.write_digest()

    def write_digest(self, body=None):
        body = body if body is not None else digest_body(report_sha=self.report_sha, input_sha=self.input_sha)
        self.digest_path = self.project / f"work/research-seal/{DAY}/{self.report_sha}.digest.json"
        self.digest_path.write_text(body)
        digest_sha = hashlib.sha256(self.digest_path.read_bytes()).hexdigest()
        result_dir = self.project / f"work/report-archives/{DAY}/result"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(json.dumps({
            "selectionSha256": "a" * 64,
        }))
        (self.project / f"work/research-seal/{DAY}/{self.report_sha}.json").write_text(json.dumps({
            "schemaVersion": "astock.research-seal.v1", "kind": "research_seal",
            "targetDate": DAY, "state": "SEALED", "generatedAt": "2026-09-24T00:00:33.455Z",
            "source": {
                "lastRunFile": "work/last-run.json",
                "lastRunSha256": hashlib.sha256((self.project / "work/last-run.json").read_bytes()).hexdigest(),
                "reportFile": f"reports/daily/{DAY}.md", "reportSha256": self.report_sha,
                "contextSha256": "c" * 64, "dataStatus": "partial",
                "researchSemanticReview": "not_human_reviewed",
                "frozenCutoffInstant": "2026-09-24T00:00:03.753Z", "cutoffLocal": "2026-09-24 08:00",
                "publicationGate": "passed", "evidenceReview": "passed",
                "digestFile": f"work/research-seal/{DAY}/{self.report_sha}.digest.json",
                "digestSha256": digest_sha,
            },
        }, indent=2))
        return digest_sha

    def sender(self, record):
        def send(payload, config, now):
            body = relay.encode_payload(payload)
            self.last_payload = payload
            self.last_body = body
            record.append(hashlib.sha256(body).hexdigest())
            return {"status": "confirmed", "reason": "accepted", "ack_scope": "http_accepted",
                    "receiver_storage": "receiver_reported_stored", "payload_sha256": record[-1],
                    "receiver_content_sha256": record[-1], "content_match": True,
                    "receiver_record_id": "rr_test_0001"}
        return send

    def run_relay(self):
        sent = []
        result = relay.run_relay(project_root=str(self.project), bus_root=str(self.bus),
                                 now=datetime(2026, 9, 24, 8, 5, tzinfo=SHANGHAI),
                                 dry_run=False, sender=self.sender(sent))
        return result, sent

    def test_display_label_change_does_not_block_delivery(self):
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(sent), 1)
        state = json.loads((self.bus / f"out/relay-delivery/{DAY}.json").read_text())
        self.assertEqual(state["payload_sha256"], sent[0])

    def test_tampered_sealed_report_body_is_rejected(self):
        """Tampering an already-sealed body must never pass, even with a valid digest."""
        (self.project / f"reports/daily/{DAY}.md").write_text(self.report + "\n未核验的额外正文\n")
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "held")
        self.assertIn(result["reason"], ("sealed_report_proof_invalid", "structured_digest_binding_invalid",
                                          "research_seal_missing"))
        self.assertEqual(sent, [])

    def test_resealed_new_sample_with_changed_labels_is_accepted(self):
        """A new sample sealed after a display-template change stays consumable."""
        changed = self.report.replace("支持证据（成交额）：", "佐证（成交额）：")
        self.assertNotEqual(changed, self.report)
        (self.project / f"reports/daily/{DAY}.md").write_text(changed)
        self.report = changed
        self.report_sha = hashlib.sha256(changed.encode()).hexdigest()
        self.write_digest(digest_body(report_sha=self.report_sha, input_sha=self.input_sha))
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(sent), 1)

    def test_url_credentials_and_private_hosts_never_reach_the_payload(self):
        """R07: the digest path shares the send-boundary URL sanitizer."""
        index = [
            {"number": 1, "sourceId": "src-a", "title": "交易所每日概况",
             "url": "https://user:pass@example.invalid/a?token=SECRET123#frag"},
            {"number": 2, "sourceId": "src-b", "title": "内部镜像",
             "url": "http://127.0.0.1:8080/private?key=SECRET456"},
        ]
        items = [{
            "kind": "theme", "label": "观察主题", "code": None, "inference": "双来源核验",
            "evidence": [{"sourceId": "src-a", "reference": "[来源 1]", "role": "成交额"},
                         {"sourceId": "src-b", "reference": "[来源 2]", "role": None}],
            "counterEvidence": [], "periodNote": None, "limitation": relay.INFERENCE_LIMIT,
        }]
        body = json.loads(digest_body(report_sha=self.report_sha, input_sha=self.input_sha, items=items))
        body["sourceIndex"] = index
        self.write_digest(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed", result)
        # 断言对象是发送器实际发出的字节，不是中间对象或状态文件
        self.assertTrue(hasattr(self, "last_body"), "sender must capture the encoded payload")
        for secret in (b"SECRET123", b"SECRET456", b"user:pass", b"127.0.0.1", b"token=", b"#frag"):
            self.assertNotIn(secret, self.last_body, secret)
        self.assertEqual(hashlib.sha256(self.last_body).hexdigest(), sent[0])

    def test_all_items_with_counter_evidence_may_have_empty_limitations(self):
        """R08: a legitimate all-counter-evidence digest is accepted."""
        items = [{
            "kind": "risk", "label": "风险", "code": None, "inference": "风险条目",
            "evidence": [{"sourceId": "src-a", "reference": "[来源 1]", "role": None}],
            "counterEvidence": [{"sourceId": "src-a", "reference": "[来源 1]"}],
            "periodNote": None, "limitation": None,
        }]
        self.write_digest(digest_body(report_sha=self.report_sha, input_sha=self.input_sha,
                                      items=items, limitations=[]))
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed", result)
        self.assertEqual(len(sent), 1)

    def test_stock_subject_and_period_survive_into_the_payload(self):
        """R09: a stock conclusion keeps its code and explicit period."""
        items = [{
            "kind": "stock", "label": None, "code": "002617",
            "inference": "单日行情与报告期利润同比方向不同，仍需继续核验",
            "evidence": [{"sourceId": "src-a", "reference": "[来源 1]", "role": "单日行情"}],
            "counterEvidence": [], "periodNote": "行情 2026-09-23；利润同比报告期 2026-06-30",
            "limitation": "未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。",
        }]
        self.write_digest(digest_body(report_sha=self.report_sha, input_sha=self.input_sha, items=items))
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed", result)
        state = json.loads((self.bus / f"out/relay-delivery/{DAY}.json").read_text())
        self.assertEqual(state["payload_sha256"], sent[0])
        encoded = relay.encode_payload(json.loads((self.bus / f"out/relay-delivery/{DAY}.json").read_text()))
        # 回执只存哈希；用发送器捕获的实际载荷核对主体与期间
        self.assertIn("002617", json.dumps(self.last_payload, ensure_ascii=False))
        self.assertIn("2026-06-30", json.dumps(self.last_payload, ensure_ascii=False))

    def test_missing_digest_holds_without_sending(self):
        self.digest_path.unlink()
        result, sent = self.run_relay()
        self.assertEqual((result["status"], result["reason"]), ("held", "structured_digest_missing"))
        self.assertEqual(sent, [])

    def test_tampered_digest_holds_without_sending(self):
        self.digest_path.write_text(self.digest_path.read_text() + " ")
        result, sent = self.run_relay()
        self.assertEqual((result["status"], result["reason"]), ("held", "structured_digest_hash_mismatch"))
        self.assertEqual(sent, [])

    def test_cross_date_digest_holds_without_sending(self):
        body = json.loads(digest_body(report_sha=self.report_sha, input_sha=self.input_sha))
        body["reportDate"] = "2026-09-23"
        self.write_digest(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "held")
        self.assertIn(result["reason"], ("structured_digest_binding_invalid", "digest_data_date_invalid"))
        self.assertEqual(sent, [])

    def test_archive_binding_mismatch_holds_without_sending(self):
        (self.project / f"work/report-archives/{DAY}/input.sha256").write_text("d" * 64 + "\n")
        result, sent = self.run_relay()
        self.assertEqual((result["status"], result["reason"]), ("held", "structured_digest_binding_invalid"))
        self.assertEqual(sent, [])

    def test_baseline_digest_without_items_is_accepted(self):
        self.write_digest(digest_body(report_sha=self.report_sha, input_sha=self.input_sha, items=[]))
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(sent), 1)

    def test_counter_evidence_is_carried_when_present(self):
        items = [{
            "kind": "risk", "label": "风险", "code": None, "inference": "风险条目",
            "evidence": [{"sourceId": "src-a", "reference": "[来源 1]", "role": None}],
            "counterEvidence": [{"sourceId": "src-a", "reference": "[来源 1]"}],
            "periodNote": None, "limitation": None,
        }]
        self.write_digest(digest_body(report_sha=self.report_sha, input_sha=self.input_sha, items=items))
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(sent), 1)

    def test_selection_or_generated_at_mismatch_holds_without_sending(self):
        body = json.loads(digest_body(report_sha=self.report_sha, input_sha=self.input_sha))
        body["selectionSha256"] = "e" * 64
        self.write_digest(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        result, sent = self.run_relay()
        self.assertEqual((result["status"], result["reason"]), ("held", "structured_digest_binding_invalid"))
        self.assertEqual(sent, [])
        body = json.loads(digest_body(report_sha=self.report_sha, input_sha=self.input_sha))
        body["generatedAt"] = "2026-09-23T00:00:00.000Z"
        self.write_digest(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        result, sent = self.run_relay()
        self.assertEqual((result["status"], result["reason"]),
                         ("held", "structured_digest_generated_at_mismatch"))
        self.assertEqual(sent, [])

    def test_unknown_reference_number_holds_without_sending(self):
        items = [{
            "kind": "theme", "label": "观察主题", "code": None, "inference": "引用未登记编号",
            "evidence": [{"sourceId": "src-a", "reference": "[来源 9]", "role": "成交额"}],
            "counterEvidence": [], "periodNote": None, "limitation": relay.INFERENCE_LIMIT,
        }]
        self.write_digest(digest_body(report_sha=self.report_sha, input_sha=self.input_sha, items=items))
        result, sent = self.run_relay()
        self.assertEqual((result["status"], result["reason"]), ("held", "source_reference_missing"))
        self.assertEqual(sent, [])

    def test_digest_inference_preserves_the_receiver_v2_contract(self):
        result, sent = self.run_relay()
        self.assertEqual(result["status"], "confirmed", result)
        contract = self.last_payload["research_summary"]["analysis_contract"]
        self.assertEqual(contract, {
            "version": 2, "claim_type": "inference", "inference_status": "unverified",
            "counter_evidence_status": "explicitly_missing",
            "evidence_roles": [{"source_id": "1", "role": "成交额"}],
            "period_note": None, "inference_limit": relay.INFERENCE_LIMIT})
        self.assertIn("推断限制：" + relay.INFERENCE_LIMIT,
                      self.last_payload["research_summary"]["limitations"])
        self.assertEqual(len(sent), 1)
        gate = os.environ.get("SIGNAL_GATE_TEST_ROOT")
        if gate:
            spec = importlib.util.spec_from_file_location("saved_receiver_v2", Path(gate) / "app/research_contract.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            accepted = module.validate(self.last_payload, datetime(2026, 9, 24, 8, 5, tzinfo=SHANGHAI))
            self.assertEqual(accepted["analysis"], contract)

    def test_same_day_research_is_held_until_receiver_contract_is_verified(self):
        self.write_digest(digest_body(report_sha=self.report_sha, input_sha=self.input_sha, data_date=DAY))
        result, sent = self.run_relay()
        self.assertEqual((result["status"], result["reason"]),
                         ("held", "receiver_data_date_incompatible"))
        self.assertEqual(sent, [])

    def test_digest_unknown_role_bad_period_and_contradictory_counter_hold(self):
        for over, reason in (
                ({"evidence": [{"sourceId": "src-a", "reference": "[来源 1]", "role": "未经定义"}]},
                 "analysis_evidence_role_invalid"),
                ({"periodNote": "利润同比报告期 2026-06-30"}, "analysis_period_invalid"),
                ({"counterEvidence": [{"sourceId": "src-a", "reference": "[来源 1]"}]},
                 "analysis_limit_conflict")):
            with self.subTest(reason=reason):
                body = json.loads(digest_body(report_sha=self.report_sha, input_sha=self.input_sha))
                body["items"][0].update(over)
                self.write_digest(json.dumps(body, ensure_ascii=False) + "\n")
                result, sent = self.run_relay()
                self.assertEqual((result["status"], result["reason"]), ("held", reason))
                self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
