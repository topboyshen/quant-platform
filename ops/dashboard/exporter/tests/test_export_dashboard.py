#!/usr/bin/env python3
"""Offline test matrix for the dashboard exporter.

Covers the agreed judge rules plus the five defects found in review:
  1. missing seal evidence must be 'insufficient', never a pass (None == None)
  2. Telegram pass requires a platform receipt (message id, not skipped) and a
     full-length hash comparison
  3. (client-side staleness is covered in prototype/tests/app.test.mjs)
  4. one malformed source degrades one panel, never the whole batch
  5. output files/directories use the dedicated-account modes and the whole
     output path is checked for symlinked components
Fixtures only: no database, no subprocess, no network, no production paths.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import export_dashboard as exporter  # noqa: E402
exporter.DELIVERY_CONTRACT_PATH = Path(os.environ.get("DELIVERY_CONTRACT_TEST_MODULE",
    "/home/ubuntu/signal-bus/scripts/delivery_evidence.py"))
assert exporter.DELIVERY_CONTRACT_PATH.is_file(), \
    f"shared delivery contract missing: {exporter.DELIVERY_CONTRACT_PATH}"
EVIDENCE = json.loads((ROOT.parent / "fixtures/gap_evidence_20260924.json").read_text(encoding="utf-8"))
BRIDGE_EVIDENCE = json.loads((ROOT.parent / "fixtures/snapshot_diagnostic_chain.json")
                             .read_text(encoding="utf-8"))

CST = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 9, 21, 9, 0, tzinfo=CST)
DATE = "2026-09-21"
PREVIOUS = "2026-09-18"
REPORT = "\n".join([
    f"# AStock 数据快报（语义未审）｜{DATE}", "",
    f"> 数据截止：{DATE} 08:00（Asia/Shanghai）",
    "> 数据状态：部分可用", "",
    f"## 2. 上一交易日 A 股行情（{PREVIOUS}）",
    "- [事实] 三项指数表现分化。[来源 1]", "",
    "## 一句话结论", "",
    "- [事实] 002617 露笑科技：涨跌幅 +10.03%（未核验为涨跌停）。[来源 9]",
    "- [事实] 危险文本 <script>alert(1)</script> 仅作为文本展示。",
    "- [事实] 与 [错误链接](javascript:alert(1)) 和 [数据链接](data:text/html;base64,QQ==) 无关。", "",
    "## 1. 数据覆盖与可用性", "",
    "> 模块状态分数：行情 96%", "",
    "## 7. 风险提示与数据缺口", "",
    "- 三大指数各自成交额字段缺失；本报告未据此作指数成交额比较。",
    "- 北向资金实时净流入不可用；本报告未填入估算值。",
    "- 财务口径：营收金额及同比为供应商口径，尚未与公告核对；不据此推断经营杠杆。", "",
    "## 来源", "",
    "> 以下仅列出本报告实际引用的固定数据包来源。", "",
    "9. 新浪财经露笑科技未复权历史日线 — market_data（新浪财经）；数据时间：2026-09-18 15:00（Asia/Shanghai）；https://finance.sina.com.cn/realstock/company/sz002617/nc.shtml",
    "10. [陈旧] 2026年半年度报告摘要 — official_announcement（巨潮资讯网）；披露日期：2026-08-26；https://static.cninfo.com.cn/finalpage/2026-08-26/1225501876.PDF",
    "", "---", "", "本晨报用于信息整理与研究支持，不构成个性化投资建议；市场有风险，决策需独立核验。", "",
])


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def build_fixture(root: Path, *, with_receipts: bool = True, telegram_receipt: dict | None = None,
                  health_payload=None, published: dict | None = None, write_published: bool = True,
                  drop_seal_files: bool = False, health_generated: str = f"{DATE}T00:40:00+08:00",
                  history: tuple[str, ...] = (DATE, PREVIOUS)):
    project = root / "AStock_AI_System"
    (project / "reports/daily").mkdir(parents=True, exist_ok=True)
    for date in history:
        body = REPORT.replace(DATE, date)
        (project / "reports/daily" / f"{date}.md").write_text(body, encoding="utf-8")
    report_path = project / "reports/daily" / f"{DATE}.md"
    (project / "work").mkdir(parents=True, exist_ok=True)
    (project / "work/last-run.json").write_text(json.dumps({
        "targetDate": DATE, "status": "success", "publicationGate": "passed", "dataStatus": "partial",
        "publicationLevel": "可发布（结构、来源与时效门禁通过；研究语义未审校）",
        "reportVariant": "structured_enhanced", "completedAt": f"{DATE}T00:01:15.000Z"}), encoding="utf-8")
    archive = project / f"work/report-archives/{DATE}"
    (archive / "result").mkdir(parents=True, exist_ok=True)
    if not drop_seal_files:
        (archive / "input.json").write_text(json.dumps({
        "schemaVersion": "1.0", "targetDate": DATE,
        "compilerContext": {"data": {"previousAShareSession": {"sessionDate": PREVIOUS}}}}) + "\n", encoding="utf-8")
        (archive / "result/result.json").write_text('{"status": "validated"}\n', encoding="utf-8")
    # Production convention (run-archive.mjs): the file is canonicalJson + "\n"
    # and the recorded resultSha256 hashes the canonical bytes without it.
    result_bytes = (archive / "result/result.json").read_bytes()
    canonical_result = result_bytes[:-1] if result_bytes.endswith(b"\n") else result_bytes
    record = {"status": "sealed",
              "inputSha256": sha((archive / "input.json").read_bytes()) if (archive / "input.json").is_file() else None,
              "reportSha256": sha(report_path.read_bytes()),
              "resultSha256": sha(canonical_result)
              if (archive / "result/result.json").is_file() else None}
    if published is not None:
        record = published
    if write_published:
        (archive / "published.json").write_text(json.dumps(record), encoding="utf-8")
    (project / "work/notifications/sent").mkdir(parents=True, exist_ok=True)
    payload_dir = project / "work/notifications/payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    event_id = f"astock-{DATE}-success-telegram-v1"
    payload_bytes = report_path.read_bytes()
    (payload_dir / f"{event_id}.md").write_bytes(payload_bytes)
    notification = {"schemaVersion": "1.0", "source": "AStock_AI_System",
                    "eventId": f"astock-{DATE}-success-telegram-v1",
                    "targetDate": DATE, "outcome": "success", "route": "telegram",
                    "report": {"reportFile": f"reports/daily/{DATE}.md",
                               "payloadFile": f"payloads/{event_id}.md",
                               "sha256": record.get("reportSha256"),
                               "bytes": len(payload_bytes)}}
    if telegram_receipt:
        notification.update(telegram_receipt)
    (project / f"work/notifications/sent/astock-{DATE}-success-telegram-v1.json") \
        .write_text(json.dumps(notification), encoding="utf-8")
    bus = root / "signal-bus/out/relay-delivery"
    bus.mkdir(parents=True, exist_ok=True)
    (bus / f"{DATE}.json").write_text(json.dumps({
        "status": "confirmed", "reason": "accepted", "target_date": DATE, "as_of": PREVIOUS,
        "ack_scope": "http_accepted", "receiver_storage": "not_in_acknowledgement",
        "receiver_record_id": "rr_fixture_baseline", "content_match": True,
        "payload_sha256": "c" * 64,
        "report_sha256": record.get("reportSha256")}), encoding="utf-8")
    (root / "qlib/data/cn_data/calendars").mkdir(parents=True, exist_ok=True)
    (root / "qlib/data/cn_data/calendars/day.txt").write_text(
        "2026-09-16\n2026-09-17\n2026-09-18\n", encoding="ascii")
    (root / "qlib/data/cn_data/.qlib-dolt-incremental.json").write_text(json.dumps({
        "targetDate": PREVIOUS, "dataQuality": "partial"}), encoding="utf-8")
    for name, head in (("AStock_AI_System", "a" * 40), ("signal-bus", "b" * 40),
                       ("research-data", "c" * 40)):
        git = root / name / ".git"
        (git / "refs/heads").mkdir(parents=True, exist_ok=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs/heads/main").write_text(head + "\n", encoding="utf-8")
    health = root / "health.json"
    payload = health_payload if health_payload is not None else {
        "status": "critical", "generatedAt": health_generated,
        "issues": [{"code": "unresolved_trading_day_gaps", "severity": "critical",
                    "dates": ["2026-09-17"]},
                   {"code": "qlib_source_absence", "severity": "warning"}],
        "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}}
    health.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    acceptance = root / "ops/acceptance/out"
    acceptance.mkdir(parents=True, exist_ok=True)
    (acceptance / "night-20260920T230000.json").write_text(json.dumps({
        "phase": "night", "generatedAt": "2026-09-20T23:00:00+08:00",
        "checks": {"snapshot": {"status": "pass", "limitations": []},
                   "health_alert": {"status": "insufficient", "limitations": ["缺少可用回执"]}}}),
        encoding="utf-8")
    services = root / "services.json"
    services.write_text(json.dumps({
        "checkedAt": f"{DATE}T08:35:00+08:00",
        "units": [{"unit": "astock-xhs-source.timer", "activeState": "active", "subState": "waiting",
                   "enabled": "enabled", "next": "Tue 2026-09-22 08:00:00 CST", "last": f"{DATE} 08:00:14 CST"},
                  {"unit": "not-allowlisted.service", "activeState": "active"}]}), encoding="utf-8")
    if not with_receipts:
        (project / f"work/notifications/sent/astock-{DATE}-success-telegram-v1.json").unlink()
        (bus / f"{DATE}.json").unlink()
    return {"root": root, "health": health, "services": services, "record": record,
            "report": report_path, "archive": archive}


def _make_stub_publisher():
        def _stub(root, target, body, **kw):
            target.write_bytes(body)
            os.chmod(target, 0o640)
        return _stub


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "src"
        self.out = Path(self.temp.name) / "out"
        self.fixture = build_fixture(self.root)
        self.relay_baseline = (self.root / f"signal-bus/out/relay-delivery/{DATE}.json").read_text()

    def export(self, now=NOW, **kwargs):
        # The test temp tree sits under a symlinked system prefix on macOS, so
        # local/dev mode is explicit here; production defaults to strict.
        kwargs.setdefault("allow_symlinked_ancestors", True)
        kwargs.setdefault("_publisher", _make_stub_publisher())
        return exporter.export_all(self.root, self.out, now, self.fixture["health"],
                                   self.fixture["services"], **kwargs)

    def page(self, name):
        return json.loads((self.out / f"{name}.json").read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- basics

    def test_normal_export_pages_history_manifest_and_modes(self):
        manifest = self.export()
        self.assertEqual(sorted(manifest["files"]),
                         ["delivery.json", "progress.json", "projects.json", "today.json"])
        self.assertEqual(manifest["builderFailures"], {})
        for name, entry in manifest["files"].items():
            body = (self.out / name).read_bytes()
            self.assertEqual(entry["sha256"], sha(body), name)
            self.assertEqual(entry["bytes"], len(body), name)
            self.assertEqual(os.stat(self.out / name).st_mode & 0o777, exporter.FILE_MODE, name)
        self.assertEqual(os.stat(self.out).st_mode & 0o777, exporter.DIR_MODE)
        self.assertEqual(os.stat(self.out / "reports").st_mode & 0o777, exporter.DIR_MODE)
        self.assertEqual(os.stat(self.out / "reports" / f"{DATE}.json").st_mode & 0o777,
                         exporter.FILE_MODE)
        self.assertEqual(sorted(manifest["history"]["pages"]), sorted([DATE, PREVIOUS]))
        today = self.page("today")
        self.assertEqual(today["report"]["targetDate"], DATE)
        self.assertEqual(today["availableDates"], [DATE, PREVIOUS])
        self.assertTrue(today["report"]["conclusion"])
        self.assertTrue(today["report"]["risks"])
        self.assertFalse(today["stale"])
        kinds = [block["kind"] for block in today["report"]["blocks"]]
        self.assertIn("h1", kinds)
        self.assertIn("quote", kinds)
        self.assertEqual([item["id"] for item in today["report"]["sources"]], [9, 10])

    def test_long_body_sources_still_extracted(self):
        """Sources must survive truncation limits: long body, numbered refs at tail."""
        long_parts = []
        for i in range(1, 32):
            long_parts.extend([f"## 第{i}节", "", f"段落{i}：" + "延长文本。" * 20, ""])
        long_parts.extend(["## 来源", "", "> 仅列实际引用来源。",
            "1. 来源一 — s1；https://example.com/1",
            "2. 来源二 — s2；https://example.com/2"])
        blocks, sources, completeness = exporter.complete_report_blocks("\n".join(long_parts))
        self.assertGreater(len(blocks), 60, "fixture too short")
        self.assertEqual(len(sources), 2)
        self.assertEqual([s["id"] for s in sources], [1, 2])
        self.assertTrue(all(s["url"].startswith("https://") for s in sources))
        self.assertTrue(completeness["complete"])
        self.assertEqual(completeness["reasons"], [])

    def test_history_report_page_is_self_contained(self):
        self.export()
        page = json.loads((self.out / "reports" / f"{PREVIOUS}.json").read_text(encoding="utf-8"))
        self.assertEqual(page["date"], PREVIOUS)
        self.assertEqual(page["page"], "report")
        self.assertEqual(page["report"]["targetDate"], PREVIOUS)
        self.assertIn("stale", page)

    def test_focus_lists_risks_delivery_gaps_and_gap_days(self):
        self.export()
        today = self.page("today")
        kinds = {item["kind"] for item in today["focus"]}
        self.assertIn("report_limitation", kinds)
        self.assertNotIn("risk", kinds)
        self.assertIn("delivery", kinds)
        # 历史缺口单独分类：不再与“当前需要处理”的问题混在一起
        self.assertIn("historical_gap", kinds)
        self.assertNotIn("gap", kinds)
        gap_item = next(item for item in self.page("today")["focus"] if item["kind"] == "historical_gap")
        self.assertIn("保留", gap_item["text"])
        self.assertEqual(gap_item["source"], "dashboard", "历史缺口由看板自核")
        # 报告来源的风险行必须标出来源；被新规则取代的措辞要带说明
        risks = [item for item in self.page("today")["focus"] if item["kind"] == "report_limitation"]
        self.assertEqual(len(risks), 3)
        self.assertTrue(all(item["source"].startswith("report") for item in risks))
        self.assertTrue(all(item["sealVerified"] for item in risks))
        delivery = [item for item in self.page("today")["focus"] if item["kind"] == "delivery"]
        self.assertTrue(all(item["source"] == "dashboard" for item in delivery))

    def test_old_report_wording_never_invents_current_verification(self):
        """旧报告只带当时的日期/哈希，不凭关键词生成现行规则或已核验断言。"""
        self.export()
        focus = self.page("today")["focus"]
        northbound = next(item for item in focus if "北向" in item["text"])
        financial = next(item for item in focus if "尚未与公告核对" in item["text"])
        for item in (northbound, financial):
            self.assertEqual(item["kind"], "report_limitation")
            self.assertEqual(item["reportDate"], DATE)
            self.assertEqual(item["reportSha256"], sha(self.fixture["report"].read_bytes()))
            self.assertEqual(item["sourceTime"], f"{DATE}T08:00:00+08:00")
            self.assertNotIn("supersededBy", item)
        self.assertNotIn("现行规则", json.dumps(focus, ensure_ascii=False))
        self.assertNotIn("营收已按", json.dumps(focus, ensure_ascii=False))
        self.assertTrue(all(item["text"] for item in focus), "每条都必须有正文，不写空推断")

    def test_focus_does_not_label_unverified_report_as_sealed(self):
        (self.fixture["archive"] / "published.json").unlink()
        self.export()
        rows = [item for item in self.page("today")["focus"] if item["kind"] == "report_limitation"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(item["sealVerified"] is False for item in rows))

    def test_focus_seal_must_bind_the_same_report_date_and_hash(self):
        today = {"report": {"targetDate": DATE, "sha256": "a" * 64,
                            "risks": [{"text": "成交额缺失"}]}}
        delivery = {"targetDate": PREVIOUS, "layers": {"seal": {
            "status": "pass", "evidence": {"recomputed": {"report": "a" * 64}}}}}
        for wrong_date, wrong_hash in ((PREVIOUS, "a" * 64), (DATE, "b" * 64)):
            delivery["targetDate"] = wrong_date
            delivery["layers"]["seal"]["evidence"]["recomputed"]["report"] = wrong_hash
            rows = exporter.build_focus(self.root, NOW, today, delivery, {}, {"health": {}})
            self.assertFalse(rows[0]["sealVerified"])

    def test_inference_in_old_report_is_not_promoted_to_a_verified_gap(self):
        today = {"report": {"targetDate": DATE, "risks": [
            {"text": "[推断] 数据缺失可能意味着业务停摆"},
            {"text": "建议因成交额不足降低仓位"},
            {"text": "成交额字段缺失，本报告未进行比较"}]}}
        original = json.dumps(today, ensure_ascii=False)
        rows = exporter.build_focus(self.root, NOW, today, {}, {}, {"health": {}})
        self.assertEqual([item["text"] for item in rows], ["成交额字段缺失，本报告未进行比较"])
        self.assertEqual(json.dumps(today, ensure_ascii=False), original)

    def test_delivery_focus_is_bound_to_target_day_and_actual_check_time(self):
        self.export()
        delivery = self.page("delivery")
        rows = [item for item in self.page("today")["focus"] if item["kind"] == "delivery"]
        self.assertTrue(rows)
        for item in rows:
            self.assertEqual(item["targetDate"], DATE)
            self.assertEqual(item["sourceTime"], delivery["sourceTime"])
            self.assertEqual(item["checkedAt"], NOW.isoformat())
            self.assertEqual(item["sourceFile"], "delivery.json")
            self.assertEqual(item["status"], "insufficient")

    def test_next_tenth_gap_is_not_silently_accepted_as_retained(self):
        new_day = "2030-04-03"  # synthetic future date, absent from the fixed evidence
        dates = sorted([*EVIDENCE["currentAudit"]["issues"][1]["dates"], new_day])
        health = {"status": "critical", "generatedAt": NOW.isoformat(), "issues": [
            {"code": "unresolved_trading_day_gaps", "severity": "critical", "dates": dates}]}
        self.fixture["health"].write_text(json.dumps(health))
        self.export()
        progress = self.page("progress")
        self.assertEqual(len(progress["historicalGaps"]["criticalDates"]), 9)
        self.assertEqual(progress["criticalDates"], dates)
        self.assertEqual(progress["currentIssues"][0]["dates"], [new_day])
        self.assertEqual(progress["currentIssues"][0]["classification"], "not_in_retained_baseline")
        row = next(item for item in self.page("today")["focus"] if item["kind"] == "audit_unconfirmed")
        self.assertIn(new_day, row["text"])
        self.assertIn("待核", row["text"])

    def test_retained_date_severity_change_remains_unconfirmed(self):
        baseline, addenda, current = exporter.split_retained_issues([
            {"code": "legacy_gaps_or_invalid_rows", "severity": "critical", "dates": ["2026-08-21"]}])
        self.assertEqual(baseline["legacy_gaps_or_invalid_rows"], [])
        self.assertEqual(addenda, [])
        self.assertEqual(current[0]["classification"], "not_in_retained_baseline")

    def test_fixed_audit_fixture_preserves_original_anchor_and_separate_addendum(self):
        self.fixture["health"].write_text(json.dumps(EVIDENCE["currentAudit"]))
        self.export(now=dt.datetime(2026, 9, 24, 9, 45, tzinfo=CST))
        gaps = self.page("progress")["historicalGaps"]
        self.assertEqual((len(gaps["criticalDates"]), len(gaps["legacyDates"]), gaps["count"]), (9, 10, 19))
        self.assertEqual(gaps["observedAt"], EVIDENCE["currentAudit"]["generatedAt"])
        self.assertEqual(gaps["baseline"], EVIDENCE["baseline"])
        self.assertEqual(gaps["baselineDate"], EVIDENCE["baseline"]["baselineDate"])
        self.assertEqual(gaps["baselineSha256"], EVIDENCE["baseline"]["baselineSha256"])
        self.assertEqual(gaps["addenda"], [EVIDENCE["addendum"]])
        self.assertNotIn("acceptedAt", gaps["addenda"][0])
        self.assertEqual(gaps["addenda"][0]["firstObservedAt"],
                         "2026-09-22T22:45:00.813661+08:00")
        self.assertEqual(gaps["addenda"][0]["firstObservedSource"],
                         "research-data-health.service journal_reconstruction")
        self.assertEqual(gaps["criticalDates"], sorted([*EVIDENCE["baseline"]["criticalDates"],
                                                         EVIDENCE["addendum"]["date"]]))
        day = next(row for row in gaps["evidence"] if row["date"] == "2026-09-22")
        self.assertEqual((day["snapshot"], day["stage"], day["recoveryReadiness"]),
                         ("missing", "missing", "missing_or_invalid_market_evidence"))
        self.assertIsNone(day["payloadSha256"])
        historical = [item for item in self.page("today")["focus"] if item["kind"] == "historical_gap"]
        self.assertEqual([len(item["dates"]) for item in historical], [8, 10, 1])
        self.assertEqual([item["scope"] for item in historical],
                         ["original_baseline", "original_baseline", "later_addendum"])
        self.assertIn("日志重建", historical[-1]["text"])
        self.assertEqual(historical[-1]["firstObservedAt"], EVIDENCE["addendum"]["firstObservedAt"])
        self.assertEqual(self.page("progress")["health"]["status"], "critical")
        self.assertEqual(self.page("progress")["currentIssues"], [])

    def test_original_registry_survives_dates_disappearing_from_the_current_audit(self):
        health = json.loads(json.dumps(EVIDENCE["currentAudit"]))
        health["issues"][0]["dates"].remove("2026-08-21")
        health["issues"][1]["dates"].remove("2026-09-07")
        health["issues"][1]["dates"].remove("2026-09-22")
        self.fixture["health"].write_text(json.dumps(health))
        self.export()
        gaps = self.page("progress")["historicalGaps"]
        self.assertEqual(gaps["baseline"], EVIDENCE["baseline"])
        self.assertEqual(gaps["addenda"], [EVIDENCE["addendum"]])
        self.assertEqual(gaps["baselineSha256"], EVIDENCE["baseline"]["baselineSha256"])
        self.assertEqual((len(gaps["current"]["criticalDates"]),
                          len(gaps["current"]["legacyDates"]), gaps["current"]["count"]), (7, 9, 16))
        self.assertNotIn("2026-09-22", gaps["current"]["criticalDates"])
        self.assertNotIn("2026-09-07", gaps["current"]["criticalDates"])
        self.assertNotIn("2026-08-21", gaps["current"]["legacyDates"])
        self.assertNotIn("2026-09-22", [row["date"] for row in gaps["evidence"]])
        focus = [item for item in self.page("today")["focus"] if item["kind"] == "historical_gap"]
        self.assertEqual([len(item["dates"]) for item in focus], [8, 10, 1])
        self.assertTrue(all("当前状态以最新审计为准" in item["text"] for item in focus[:2]))

    def test_changed_addendum_severity_keeps_provenance_but_requires_review(self):
        self.fixture["health"].write_text(json.dumps({
            "status": "warning", "generatedAt": NOW.isoformat(), "issues": [
                {"code": "unresolved_trading_day_gaps", "severity": "warning", "dates": ["2026-09-22"]}]}))
        self.export()
        progress = self.page("progress")
        gaps = progress["historicalGaps"]
        self.assertEqual(gaps["baseline"], EVIDENCE["baseline"])
        self.assertEqual(gaps["addenda"], [EVIDENCE["addendum"]])
        self.assertEqual(gaps["current"], {"criticalDates": [], "legacyDates": [], "count": 0})
        self.assertEqual(progress["currentIssues"][0]["dates"], ["2026-09-22"])
        self.assertEqual(progress["currentIssues"][0]["classification"], "not_in_retained_baseline")

    def test_missing_issue_list_is_unknown_while_explicit_empty_is_observed_zero(self):
        for health in ({"status": "critical", "generatedAt": NOW.isoformat()},
                       {"status": "critical", "generatedAt": NOW.isoformat(), "issues": None}):
            self.fixture["health"].write_text(json.dumps(health))
            self.export()
            progress = self.page("progress")
            gaps = progress["historicalGaps"]
            self.assertIn("health_issues_unavailable", progress["readErrors"])
            self.assertEqual(gaps["baseline"], EVIDENCE["baseline"])
            self.assertEqual(gaps["addenda"], [EVIDENCE["addendum"]])
            self.assertIsNone(gaps["current"]["count"])
            self.assertIsNone(gaps["current"]["criticalDates"])
            self.assertIsNone(gaps["criticalDates"])
            self.assertIsNone(progress["currentIssues"])
            self.assertIsNone(progress["health"]["issues"])
            self.assertIsNone(progress["coverage"]["criticalDates"])
            self.assertIsNone(progress["coverage"]["legacyDates"])
            self.assertTrue(any("当前缺口日期与数量无法确认" in item["text"]
                                for item in self.page("today")["focus"]))
        self.fixture["health"].write_text(json.dumps({
            "status": "ok", "generatedAt": NOW.isoformat(), "issues": []}))
        self.export()
        self.assertEqual(self.page("progress")["historicalGaps"]["current"],
                         {"criticalDates": [], "legacyDates": [], "count": 0})

    def test_addendum_requires_matching_aggregate_issue_severity(self):
        baseline, addenda, current = exporter.split_retained_issues([
            {"code": "unresolved_trading_day_gaps", "severity": "warning", "dates": ["2026-09-22"]}])
        self.assertEqual(baseline["unresolved_trading_day_gaps"], [])
        self.assertEqual(addenda, [])
        self.assertEqual(current[0]["dates"], ["2026-09-22"])

    def test_missing_health_is_unknown_not_zero_or_no_new_failure(self):
        self.fixture["health"].unlink()
        self.export()
        self.assertIsNone(self.page("progress")["historicalGaps"])
        rows = self.page("today")["focus"]
        self.assertTrue(any(item["kind"] == "audit_unconfirmed" and "审计不可用" in item["text"] for item in rows))

    # ------------------------------------------------- defect 1: seal evidence

    def test_missing_seal_evidence_is_insufficient_never_pass(self):
        self.fixture = build_fixture(self.root, published={})
        self.export()
        seal = self.page("delivery")["layers"]["seal"]
        self.assertEqual(seal["status"], "insufficient")
        self.assertIsNone(seal["evidence"]["matches"])
        self.assertTrue(any("不完整" in item for item in seal["limitations"]))

    def test_absent_seal_files_with_null_record_are_insufficient(self):
        self.fixture = build_fixture(self.root, drop_seal_files=True)
        record = dict(self.fixture["record"])
        record["inputSha256"] = None
        record["resultSha256"] = None
        (self.fixture["archive"] / "published.json").write_text(json.dumps(record), encoding="utf-8")
        self.export()
        seal = self.page("delivery")["layers"]["seal"]
        self.assertEqual(seal["status"], "insufficient")
        self.assertIsNone(seal["evidence"]["matches"])

    def test_seal_uses_the_archive_result_hash_convention(self):
        """Production writes canonicalJson + newline and records the hash of the
        canonical bytes, so the exporter must accept that convention."""
        self.export()
        seal = self.page("delivery")["layers"]["seal"]
        self.assertEqual(seal["status"], "pass")
        self.assertEqual(seal["evidence"]["resultHashConvention"],
                         "canonical-json-without-trailing-newline")
        self.assertTrue(all(seal["evidence"]["matches"].values()))

    def test_seal_also_accepts_a_raw_byte_hash_convention(self):
        archive = self.fixture["archive"]
        raw = (archive / "result/result.json").read_bytes()
        record = json.loads((archive / "published.json").read_text())
        record["resultSha256"] = sha(raw)
        (archive / "published.json").write_text(json.dumps(record), encoding="utf-8")
        self.export()
        seal = self.page("delivery")["layers"]["seal"]
        self.assertEqual(seal["status"], "pass")
        self.assertEqual(seal["evidence"]["resultHashConvention"], "raw-bytes")

    def test_tampered_result_json_is_fail_even_with_the_convention(self):
        archive = self.fixture["archive"]
        (archive / "result/result.json").write_text('{"status": "tampered", "x": 1}\n', encoding="utf-8")
        self.export()
        seal = self.page("delivery")["layers"]["seal"]
        self.assertEqual(seal["status"], "fail")
        self.assertFalse(seal["evidence"]["matches"]["result"])

    def test_real_hash_mismatch_is_fail(self):
        self.export()
        self.fixture["report"].write_text(REPORT + "\n被改写\n", encoding="utf-8")
        self.export()
        seal = self.page("delivery")["layers"]["seal"]
        self.assertEqual(seal["status"], "fail")
        self.assertFalse(seal["evidence"]["matches"]["report"])
        self.assertTrue(seal["evidence"]["declared"]["report"])

    # --------------------------------------------- defect 2: telegram receipt

    def test_telegram_requires_platform_receipt_message_id_and_full_hash(self):
        cases = [
            ("no receipt", {}, "insufficient"),
            ("skipped receipt", {"message_id": "77", "skipped": True}, "insufficient"),
            ("message id without success flag", {"message_id": "77"}, "insufficient"),
            ("platform success=false", {"message_id": "77", "success": False}, "fail"),
            ("platform success=false without id", {"success": False}, "fail"),
            ("nested receipt success=false", {"receipt": {"success": False}}, "fail"),
            # 内嵌回执是历史形态：没有独立回执文件就只能到 insufficient，
            # 通过路径由回执文件承担（见 test_send_receipt_is_the_only_telegram_pass）。
            ("embedded receipt only", {"message_id": "77", "success": True}, "insufficient"),
            ("prefix-only hash", {"message_id": "77", "success": True,
                                  "report": {"sha256": "short"}}, "fail"),
        ]
        for label, extra, expected in cases:
            with self.subTest(label=label):
                self.fixture = build_fixture(self.root, telegram_receipt=extra or None)
                if label == "prefix-only hash":
                    record = dict(self.fixture["record"])
                    (self.fixture["archive"] / "published.json").write_text(json.dumps(record),
                                                                           encoding="utf-8")
                    path = (self.root / "AStock_AI_System/work/notifications/sent"
                            / f"astock-{DATE}-success-telegram-v1.json")
                    payload = json.loads(path.read_text())
                    payload["report"]["sha256"] = str(record["reportSha256"])[:16]
                    path.write_text(json.dumps(payload), encoding="utf-8")
                self.export()
                telegram = self.page("delivery")["layers"]["telegram"]
                self.assertEqual(telegram["status"], expected, label)
                if expected == "pass":
                    self.assertEqual(telegram["evidence"]["messageId"], "77")
                    self.assertTrue(telegram["evidence"]["fullHashMatch"])

    def test_telegram_local_failure_outcome_is_fail(self):
        self.fixture = build_fixture(self.root, telegram_receipt={"message_id": "77",
                                                                 "outcome": "failed"})
        self.export()
        self.assertEqual(self.page("delivery")["layers"]["telegram"]["status"], "fail")

    def relay_path(self):
        return self.root / f"signal-bus/out/relay-delivery/{DATE}.json"

    def send_receipt_path(self):
        return (self.root / "AStock_AI_System/work/notifications/receipts/send"
                / f"astock-{DATE}-success-telegram-v1.json")

    def write_send_receipt(self, **changes):
        """新的发送回执：默认是一份与密封报告一致、字段完整的回执。"""
        report = (self.root / "AStock_AI_System/reports/daily" / f"{DATE}.md").read_bytes()
        payload = {"schemaVersion": "1.0", "eventId": f"astock-{DATE}-success-telegram-v1",
                   "targetDate": DATE, "route": "telegram",
                   "payloadSha256": sha(report), "payloadBytes": len(report),
                   "platformMessageId": "4242", "ackKind": "hermes_cli_json_message_id",
                   "confirmedAt": f"{DATE}T00:02:00.000Z"}
        payload.update(changes)
        self.send_receipt_path().parent.mkdir(parents=True, exist_ok=True)
        self.send_receipt_path().write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_send_receipt_is_the_only_telegram_pass(self):
        """发送记录 + 平台回执（消息 ID、内容哈希、带时区确认时间）才能判通过。"""
        self.write_send_receipt()
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "pass")
        self.assertEqual(layer["evidence"]["receiptSource"], "send_receipt")
        self.assertEqual(layer["evidence"]["messageId"], "4242")
        self.assertEqual(layer["evidence"]["platformConfirmedAt"], f"{DATE}T00:02:00.000Z")
        # 完整报告事件：附件哈希 == 事件哈希 == 文件重算 == 密封报告重算
        self.assertEqual(layer["evidence"]["payloadChecks"],
                         {"receipt_vs_event": True, "event_vs_file": True, "receipt_vs_file": True})
        self.assertEqual(layer["evidence"]["bytesChecks"],
                         {"receipt_vs_event": True, "event_vs_file": True, "receipt_vs_file": True})
        self.assertIsNone(layer["evidence"]["sourceChecks"])
        self.assertEqual(layer["evidence"]["recomputedPayloadSha256"],
                         layer["evidence"]["sealedReportSha256"])
        self.assertTrue(any("不代表用户已读" in item for item in layer["limitations"]))

    def test_sent_record_without_receipt_reports_receipt_missing(self):
        """生产里的九条历史记录：有发送记录、没有回执，只能记“回执缺失”。"""
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "insufficient")
        self.assertIsNone(layer["evidence"]["messageId"])
        self.assertIsNone(layer["evidence"]["receiptSource"])
        self.assertEqual(layer["evidence"]["receiptReason"], "receipt_missing")
        self.assertTrue(any("回执缺失" in item for item in layer["limitations"]))

    def test_send_receipt_hash_mismatch_is_a_contradiction(self):
        """回执哈希与密封报告不一致＝矛盾证据，判失败并写明原因。"""
        self.write_send_receipt(payloadSha256="b" * 64)
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "fail")
        self.assertTrue(any("不一致" in item for item in layer["limitations"]))

    def test_malformed_send_receipt_is_insufficient_never_pass(self):
        cases = [
            ("unknown field", {"token": "x"}, "receipt_unknown_fields"),
            ("zone-less confirmedAt", {"confirmedAt": f"{DATE} 00:02"}, "receipt_confirmed_at_invalid"),
            ("non numeric message id", {"platformMessageId": "abc"}, "receipt_message_id_invalid"),
            ("zero message id", {"platformMessageId": "0"}, "receipt_message_id_invalid"),
            ("invalid event", {"eventId": "garbage"}, "receipt_event_mismatch"),
            ("wrong ack kind", {"ackKind": "hermes_cli_exit_0"}, "receipt_ack_kind_unknown"),
            ("bad payload hash", {"payloadSha256": "short"}, "receipt_payload_hash_invalid"),
            ("bad source hash", {"sourceSha256": "short"}, "receipt_source_hash_invalid"),
        ]
        for label, changes, expected_reason in cases:
            with self.subTest(label=label):
                self.write_send_receipt(**changes)
                self.export()
                layer = self.page("delivery")["layers"]["telegram"]
                self.assertEqual(layer["status"], "insufficient", label)
                self.assertEqual(layer["evidence"]["receiptReason"], expected_reason, label)
                self.assertIsNone(layer["evidence"]["receiptSource"], label)

    def write_result_view_event(self):
        """结果版事件：附件是投影，来源是完整报告，两者哈希本来就不同。"""
        report = (self.root / "AStock_AI_System/reports/daily" / f"{DATE}.md").read_bytes()
        projection = f"结果版投影（合成样本）｜{DATE}\n".encode()
        payload_path = (self.root / "AStock_AI_System/work/notifications/payloads"
                        / f"astock-{DATE}-success-telegram-v1.md")
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(projection)
        sent = self.root / "AStock_AI_System/work/notifications/sent" / f"astock-{DATE}-success-telegram-v1.json"
        record = json.loads(sent.read_text())
        record["report"] = {"reportFile": f"reports/daily/{DATE}.md",
                            "payloadFile": f"payloads/astock-{DATE}-success-telegram-v1.md",
                            "sha256": sha(projection), "bytes": len(projection),
                            "presentation": "results_only_v1",
                            "sourceSha256": sha(report), "sourceBytes": len(report)}
        sent.write_text(json.dumps(record), encoding="utf-8")
        return {"payload": sha(projection), "payloadBytes": len(projection), "source": sha(report)}

    def test_result_view_event_needs_both_the_attachment_and_the_source(self):
        """附件与来源分别核验：不能要求两者相等，也不能任一匹配就通过。"""
        hashes = self.write_result_view_event()
        # 正确附件 + 正确来源 → 通过
        self.write_send_receipt(payloadSha256=hashes["payload"], payloadBytes=hashes["payloadBytes"],
                                sourceSha256=hashes["source"])
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "pass")
        self.assertEqual(layer["evidence"]["payloadChecks"],
                         {"receipt_vs_event": True, "event_vs_file": True, "receipt_vs_file": True})
        self.assertEqual(layer["evidence"]["bytesChecks"],
                         {"receipt_vs_event": True, "event_vs_file": True, "receipt_vs_file": True})
        self.assertEqual(layer["evidence"]["sourceChecks"],
                         {"receipt_vs_event": True, "event_vs_sealed": True,
                          "receipt_vs_sealed": True})
        # 附件哈希错了，来源哈希对 → 必须失败（旧实现会误判通过）
        self.write_send_receipt(payloadSha256="d" * 64, payloadBytes=hashes["payloadBytes"],
                                sourceSha256=hashes["source"])
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "fail")
        self.assertTrue(any("发送附件哈希与回执/事件/文件不一致" in item
                            for item in layer["limitations"]))
        # 回执字节数不符 → 失败
        self.write_send_receipt(payloadSha256=hashes["payload"], payloadBytes=1,
                                sourceSha256=hashes["source"])
        self.export()
        self.assertEqual(self.page("delivery")["layers"]["telegram"]["status"], "fail")
        # 事件字节数被改错 → 失败（旧实现只比回执与文件，会漏掉这一项）
        self.write_send_receipt(payloadSha256=hashes["payload"], payloadBytes=hashes["payloadBytes"],
                                sourceSha256=hashes["source"])
        sent_path = (self.root / "AStock_AI_System/work/notifications/sent"
                     / f"astock-{DATE}-success-telegram-v1.json")
        record = json.loads(sent_path.read_text())
        record["report"]["bytes"] = record["report"]["bytes"] + 1
        sent_path.write_text(json.dumps(record), encoding="utf-8")
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "fail")
        self.assertTrue(any("字节数" in item and "不一致" in item for item in layer["limitations"]))
        self.assertEqual(layer["evidence"]["bytesChecks"],
                         {"receipt_vs_event": False, "event_vs_file": False, "receipt_vs_file": True})
        # 事件字节数被删除 → 证据不足（不能因为缺一项就当通过）
        del record["report"]["bytes"]
        sent_path.write_text(json.dumps(record), encoding="utf-8")
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "insufficient")
        self.assertIsNone(layer["evidence"]["bytesChecks"])
        self.assertTrue(any("缺少附件字节数" in item for item in layer["limitations"]))
        # 附件对、来源错 → 失败（先把上一步删掉的字节数恢复，否则先命中“字节数缺失”）
        record["report"]["bytes"] = hashes["payloadBytes"]
        sent_path.write_text(json.dumps(record), encoding="utf-8")
        self.write_send_receipt(payloadSha256=hashes["payload"], payloadBytes=hashes["payloadBytes"],
                                sourceSha256="e" * 64)
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "fail")
        self.assertTrue(any("来源报告与回执/事件/密封件不一致" in item for item in layer["limitations"]))
        # 附件对但回执缺 sourceSha256 → 证据不足（不能拿附件顶替来源）
        self.write_send_receipt(payloadSha256=hashes["payload"], payloadBytes=hashes["payloadBytes"])
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "insufficient")
        self.assertTrue(any("缺少回执 sourceSha256" in item for item in layer["limitations"]))

    def test_result_view_event_without_payload_file_is_insufficient(self):
        """附件文件缺失时不能只靠事件自述判通过。"""
        self.write_result_view_event()
        (self.root / "AStock_AI_System/work/notifications/payloads"
         / f"astock-{DATE}-success-telegram-v1.md").unlink()
        self.write_send_receipt()
        self.export()
        layer = self.page("delivery")["layers"]["telegram"]
        self.assertEqual(layer["status"], "insufficient")
        self.assertTrue(any("附件文件缺失" in item for item in layer["limitations"]))

    def write_relay(self, **changes):
        """Always rebuild from the pristine record: one sub-case must never leak
        its change (wrong date, failed state) into the next one."""
        payload = json.loads(self.relay_baseline)
        # 新协议：发送端持久化本次发送字节的哈希，接收端返回的哈希与它比较（同一对象）
        payload.setdefault("payload_sha256", "c" * 64)
        payload.update(changes)
        self.relay_path().write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def relay_status(self, **changes):
        self.write_relay(**changes)
        self.export()
        return self.page("delivery")["layers"]["relay"]

    def test_relay_local_hash_and_stored_flag_cannot_certify_delivery(self):
        self.write_relay(receiver_storage="receiver_reported_stored")
        self.export()
        layer = self.page("delivery")["layers"]["relay"]
        self.assertEqual(layer["status"], "insufficient")
        self.assertIsNone(layer["evidence"]["receiverContentSha256"])
        self.assertTrue(any("不能自证送达" in item for item in layer["limitations"]))

    def test_relay_pass_requires_receiver_returned_content_hash(self):
        """同一对象：接收端返回的哈希必须等于本次发送字节的哈希。"""
        sent = "c" * 64                      # write_relay 的默认 payload_sha256
        self.write_relay(receiver_storage="receiver_reported_stored",
                         receiver_content_sha256=sent)
        self.export()
        layer = self.page("delivery")["layers"]["relay"]
        self.assertEqual(layer["status"], "pass")
        self.assertEqual(layer["evidence"]["receiverHashField"], "receiver_content_sha256")
        # 存储已自报但哈希不符 → 矛盾，判失败
        self.write_relay(receiver_storage="receiver_reported_stored",
                         receiver_content_sha256="0" * 64)
        self.export()
        self.assertEqual(self.page("delivery")["layers"]["relay"]["status"], "fail")
        # 旧记录：接收端返回了哈希，但本地没有发送字节哈希 → 证据不足，不得判通过
        self.write_relay(receiver_storage="receiver_reported_stored",
                         receiver_content_sha256="c" * 64, payload_sha256=None)
        self.export()
        layer = self.page("delivery")["layers"]["relay"]
        self.assertEqual(layer["status"], "insufficient")
        self.assertTrue(any("同一对象" in item for item in layer["limitations"]))

    def test_relay_contradictions_fail_with_a_specific_reason(self):
        stored = {"receiver_storage": "receiver_reported_stored",
                  "receiver_content_sha256": "c" * 64}
        for label, changes, marker in (
                ("wrong date", {"target_date": "2026-09-20"}, "记录日期"),
                ("failed state", {"status": "failed"}, "终态为 failed"),
                ("rejected state", {"status": "rejected"}, "终态为 rejected"),
                ("receiver hash mismatch", {"receiver_content_sha256": "0" * 64},
                 "与本次发送字节不一致"),
        ):
            with self.subTest(label=label):
                layer = self.relay_status(**{**stored, **changes})
                self.assertEqual(layer["status"], "fail", label)
                self.assertTrue(any(marker in item for item in layer["limitations"]),
                                f"{label}: {layer['limitations']}")

    def test_relay_missing_evidence_is_insufficient_with_a_specific_reason(self):
        stored = {"receiver_storage": "receiver_reported_stored",
                  "receiver_content_sha256": "c" * 64}
        for label, changes, marker in (
                ("as_of missing", {"as_of": None}, "as_of 缺失"),
                ("as_of invalid", {"as_of": "2026/09/17"}, "as_of 缺失或不是有效日期"),
                ("target_date missing", {"target_date": None}, "缺少 target_date"),
                ("not accepted", {"reason": "http_rejected"}, "未确认为 accepted"),
                ("unknown state", {"status": "sending"}, "未确认为 accepted"),
                ("not stored", {"receiver_storage": "receiver_reported_not_stored"}, "未自报已存储"),
                ("no receiver hash", {"receiver_content_sha256": None}, "不能自证送达"),
        ):
            with self.subTest(label=label):
                layer = self.relay_status(**{**stored, **changes})
                self.assertEqual(layer["status"], "insufficient", label)
                self.assertTrue(any(marker in item for item in layer["limitations"]),
                                f"{label}: {layer['limitations']}")

    def test_relay_data_date_must_equal_the_independently_bound_expectation(self):
        """Prior/current data dates only pass against their own archived proof."""
        archive = self.root / f"AStock_AI_System/work/report-archives/{DATE}"
        frozen = archive / "input.json"
        published_path = archive / "published.json"
        for expected in (PREVIOUS, DATE):
            value = json.loads(frozen.read_bytes())
            value["compilerContext"]["data"]["previousAShareSession"]["sessionDate"] = expected
            frozen.write_text(json.dumps(value), encoding="utf-8")
            published = json.loads(published_path.read_bytes())
            published["inputSha256"] = sha(frozen.read_bytes())
            published_path.write_text(json.dumps(published), encoding="utf-8")
            for actual in ("2026-09-17", PREVIOUS, DATE, None):
                with self.subTest(expected=expected, actual=actual):
                    layer = self.relay_status(receiver_storage="receiver_reported_stored",
                                              receiver_content_sha256="c" * 64,
                                              as_of=actual, target_date=DATE)
                    self.assertEqual(layer["evidence"]["expectedDataDate"], expected)
                    status = "insufficient" if actual is None else "pass" if actual == expected else "fail"
                    self.assertEqual(layer["status"], status)
                    if status == "fail":
                        self.assertTrue(any("与独立封存的期望数据日" in item
                                            for item in layer["limitations"]))

    def test_relay_receipt_cannot_supply_a_missing_or_unbound_expected_data_date(self):
        frozen = self.root / f"AStock_AI_System/work/report-archives/{DATE}/input.json"
        original = frozen.read_bytes()
        for missing in (True, False):
            with self.subTest(missing=missing):
                if missing:
                    frozen.unlink()
                else:
                    # A plausible date from changed bytes is not independent proof.
                    frozen.write_bytes(original.replace(PREVIOUS.encode(), DATE.encode()))
                layer = self.relay_status(receiver_storage="receiver_reported_stored",
                                          receiver_content_sha256="c" * 64, as_of=DATE)
                self.assertIsNone(layer["evidence"]["expectedDataDate"])
                self.assertEqual(layer["status"], "insufficient")
                self.assertIn("缺少独立且已绑定的期望数据日", layer["limitations"])

    def test_production_field_shape_without_receiver_hash_stays_insufficient(self):
        """Synthetic regression in the production record's field shape.

        Not a replay of the original 2026-09-18 file: the shipped shape is
        rebuilt from the documented fields (status/reason/as_of/target_date/
        ack_scope/receiver_storage) to prove that a record with no receiver hash
        can never be a pass.
        """
        self.write_relay(status="confirmed", reason="accepted", as_of=PREVIOUS,
                         target_date=DATE, ack_scope="http_accepted",
                         receiver_storage="not_in_acknowledgement")
        self.export()
        layer = self.page("delivery")["layers"]["relay"]
        self.assertEqual(layer["status"], "insufficient")
        self.assertEqual(layer["evidence"]["localReportSha256"], layer["evidence"]["sealedReportSha256"])
        self.assertIsNone(layer["evidence"]["receiverContentSha256"])

    # ------------------------------------------------ defect 4: source isolation

    def test_unknown_severity_is_not_silently_informational(self):
        for value in (None, "", "urgent", False, {"bad": True}):
            with self.subTest(severity=value):
                self.fixture = build_fixture(self.root, health_payload={
                    "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
                    "issues": [{"code": "synthetic_unrecognized", "severity": value}],
                    "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
                self.export()
                progress = self.page("progress")
                self.assertEqual(progress["currentIssues"][0]["severity"], "unknown")
                self.assertIn("health_issue_severity_unrecognized", progress["readErrors"])

    def test_explicit_information_and_warning_remain_source_records(self):
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [{"code": "qlib_source_absence", "severity": "information",
                        "affectedDates": [PREVIOUS]},
                       {"code": "synthetic_info_alias", "severity": "info"},
                       {"code": "qlib_legacy_unquoted_csi", "severity": "warning",
                        "symbols": ["SZ301139"]}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
        self.export()
        progress = self.page("progress")
        by_code = {item["code"]: item for item in progress["currentIssues"]}
        self.assertEqual(by_code["qlib_source_absence"]["severity"], "information")
        self.assertEqual(by_code["qlib_source_absence"]["dates"], [PREVIOUS])
        self.assertEqual(by_code["synthetic_info_alias"]["severity"], "info")
        self.assertEqual(by_code["qlib_legacy_unquoted_csi"]["severity"], "warning")
        self.assertEqual(by_code["qlib_legacy_unquoted_csi"]["symbols"], ["SZ301139"])
        self.assertNotIn("health_issue_severity_unrecognized", progress["readErrors"])

    def test_explicit_failure_survives_informational_severity(self):
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [{"code": "synthetic_conflict", "severity": "information", "status": "fail"}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
        self.export()
        issue = self.page("progress")["currentIssues"][0]
        self.assertEqual(issue["severity"], "information")
        self.assertEqual(issue["status"], "fail")

    def test_snapshot_source_failure_passes_only_bounded_audit_evidence(self):
        record = BRIDGE_EVIDENCE["deferredLastRun"]
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [{"code": "last_snapshot_not_successful", "severity": "warning",
                        "status": record["status"], "target": record["target"],
                        "runAt": record["timestamp"], "runId": record["runId"],
                        "sourceFailure": record["sourceFailure"]}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
        self.export()
        progress = self.page("progress")
        issue = progress["currentIssues"][0]
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["runStatus"], "deferred")
        self.assertEqual(issue["targetDate"], record["target"])
        self.assertEqual(issue["runAt"], record["timestamp"])
        self.assertEqual(issue["runId"], "a" * 32)
        self.assertEqual(issue["sourceFailure"]["attemptCount"], 2)
        self.assertEqual(issue["sourceFailure"]["attempts"][1]["endpoint"], "stock_market_activity_legu")
        self.assertEqual(issue["sourceFailure"]["attempts"][1]["errorType"], "OtherError")
        self.assertNotIn("UPSTREAM_RAW_RESPONSE_NOT_PUBLIC", json.dumps(progress))
        self.assertNotIn("SensitiveVendorCodeDoNotPublish", json.dumps(progress))

    def test_successful_snapshot_stage_warning_exports_without_claiming_run_failed(self):
        record = BRIDGE_EVIDENCE["insertedWithStageWarning"]
        for status in ("inserted", "skipped_existing"):
            with self.subTest(status=status):
                self.fixture = build_fixture(self.root, health_payload={
                    "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
                    "issues": [{"code": "last_snapshot_material_warning", "severity": "warning",
                                "runStatus": status, "target": record["target"],
                                "runAt": record["timestamp"], "runId": record["runId"],
                                "stageUnavailable": True,
                                "stageSourceFailure": record["akshareStage"]["sourceFailure"]}],
                    "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
                self.export()
                progress = self.page("progress")
                issue = progress["currentIssues"][0]
                self.assertEqual(issue["code"], "last_snapshot_material_warning")
                self.assertEqual(issue["severity"], "warning")
                self.assertEqual(issue["runStatus"], status)
                self.assertTrue(issue["stageUnavailable"])
                self.assertEqual(issue["targetDate"], record["target"])
                self.assertEqual(issue["stageSourceFailure"]["attempts"][1]["errorType"], "OtherError")
                self.assertNotIn("UPSTREAM_RAW_RESPONSE_NOT_PUBLIC", json.dumps(progress))
                self.assertNotIn("SensitiveVendorCodeDoNotPublish", json.dumps(progress))

    def test_missing_or_malformed_snapshot_source_failure_never_changes_severity(self):
        for failure in (None, [], {"provider": "akshare", "attemptCount": 1,
                                   "attempts": [{"attempt": 1, "endpoint": ["bad"], "errorType": "ValueError"}]}):
            with self.subTest(failure=failure):
                issue = {"code": "last_snapshot_not_successful", "severity": "warning",
                         "status": "deferred", "target": PREVIOUS}
                if failure is not None:
                    issue["sourceFailure"] = failure
                self.fixture = build_fixture(self.root, health_payload={
                    "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
                    "issues": [issue], "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
                self.export()
                exported = self.page("progress")["currentIssues"][0]
                self.assertEqual(exported["severity"], "warning")
                self.assertEqual(exported["targetDate"], PREVIOUS)
                self.assertNotIn("sourceFailure", exported)

    def test_missing_or_malformed_stage_failure_never_changes_success_warning(self):
        for failure in (None, [], {"provider": "akshare", "attemptCount": 1,
                                   "attempts": [{"attempt": 1, "endpoint": ["bad"], "errorType": "ValueError"}]}):
            with self.subTest(failure=failure):
                issue = {"code": "last_snapshot_material_warning", "severity": "warning",
                         "runStatus": "inserted", "target": PREVIOUS, "stageUnavailable": True}
                if failure is not None:
                    issue["stageSourceFailure"] = failure
                self.fixture = build_fixture(self.root, health_payload={
                    "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
                    "issues": [issue], "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
                self.export()
                exported = self.page("progress")["currentIssues"][0]
                self.assertEqual(exported["severity"], "warning")
                self.assertEqual(exported["runStatus"], "inserted")
                self.assertTrue(exported["stageUnavailable"])
                self.assertEqual(exported["targetDate"], PREVIOUS)
                self.assertNotIn("stageSourceFailure", exported)

    def test_snapshot_warning_rejects_unrecognized_run_status_and_raw_error(self):
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [{"code": "last_snapshot_material_warning", "severity": "warning",
                        "runStatus": "UPSTREAM_RAW_RESPONSE_NOT_PUBLIC", "target": PREVIOUS,
                        "stageUnavailable": True,
                        "stageSourceFailure": {"provider": "akshare", "attemptCount": 1,
                                               "attempts": [{"attempt": 1,
                                                             "endpoint": "stock_market_activity_legu",
                                                             "errorType": "SensitiveVendorCodeDoNotPublish"}]}}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
        self.export()
        issue = self.page("progress")["currentIssues"][0]
        self.assertEqual(issue["severity"], "warning")
        self.assertIsNone(issue["runStatus"])
        self.assertEqual(issue["stageSourceFailure"]["attempts"][0]["errorType"], "OtherError")
        self.assertNotIn("UPSTREAM_RAW_RESPONSE_NOT_PUBLIC", json.dumps(issue))
        self.assertNotIn("SensitiveVendorCodeDoNotPublish", json.dumps(issue))

    def test_affected_dates_spelling_is_understood(self):
        """Production qlib_source_absence uses affectedDates, not dates."""
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [
                {"code": "qlib_source_absence", "severity": "warning",
                 "affectedDates": ["2026-09-16", "2026-09-17"]},
                {"code": "unresolved_trading_day_gaps", "severity": "critical",
                 "dates": ["2026-09-17"]}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
        self.export()
        progress = self.page("progress")
        issue = next(item for item in progress["health"]["issues"]
                     if item["code"] == "qlib_source_absence")
        self.assertEqual(issue["dates"], ["2026-09-16", "2026-09-17"])
        self.assertEqual(progress["criticalDates"], ["2026-09-17"])

    def test_coverage_passes_through_only_real_audit_numbers(self):
        """The progress page draws bars only from numbers the audit itself
        produced: the qlib daily coverage ratio, the snapshot row hashes, the
        staged dates and the two gap lists. Nothing is derived or estimated."""
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "auditSince": "2026-08-21", "monitorSince": "2026-09-07",
            "referenceCompleteDate": PREVIOUS,
            "issues": [
                {"code": "unresolved_trading_day_gaps", "severity": "critical",
                 "dates": ["2026-09-17"]},
                {"code": "legacy_gaps_or_invalid_rows", "severity": "warning",
                 "dates": ["2026-08-21"]}],
            "snapshot": {"rowCount": 2, "latestValidDate": PREVIOUS,
                         "rowHashes": {"2026-08-28": "aa", PREVIOUS: "bb"}},
            "stages": {"2026-09-10": {"state": "valid"},
                       "2026-09-15": {"state": "valid"},
                       "2026-09-17": {"state": "missing"}},
            "ledger": [{"tradeDate": "2026-08-21", "classification": "legacy",
                        "snapshotState": "invalid", "stageState": "missing"},
                       {"tradeDate": PREVIOUS, "classification": "monitored",
                        "snapshotState": "valid", "stageState": "valid"},
                       {"tradeDate": "2026-09-17", "classification": "monitored",
                        "snapshotState": "missing", "stageState": "valid"}],
            "qlib": {"incremental": {"dailyCoverage": [
                {"coverage": 0.9983, "eligible": 5567, "valid": 5558,
                 "target_date": PREVIOUS, "status": "partial"}]}}})
        self.export()
        coverage = self.page("progress")["coverage"]
        self.assertEqual(coverage["snapshotValiditySource"], "audit_ledger")
        self.assertEqual(coverage["validSnapshotDates"], 1)
        self.assertEqual(coverage["recordedSnapshotDates"], 2)
        self.assertEqual(coverage["stagedDates"], 2)
        self.assertEqual(coverage["criticalDates"], 1)
        self.assertEqual(coverage["legacyDates"], 1)
        self.assertEqual(coverage["knownDates"], len(coverage["dates"]))
        self.assertEqual(coverage["qlibCoverage"]["ratio"], 0.9983)
        self.assertEqual(coverage["qlibCoverage"]["eligible"], 5567)
        self.assertEqual(coverage["qlibCoverage"]["valid"], 5558)
        by_date = {item["date"]: item for item in coverage["dates"]}
        self.assertEqual([item["date"] for item in coverage["dates"]], sorted(by_date))
        self.assertEqual(by_date[PREVIOUS]["snapshot"], "valid")
        self.assertEqual(by_date["2026-08-28"]["snapshot"], "recorded")
        self.assertTrue(by_date["2026-08-28"]["snapshotRecorded"])
        self.assertEqual(by_date["2026-08-21"]["gap"], "legacy")
        self.assertEqual(by_date["2026-08-21"]["classification"], "legacy")
        self.assertEqual(by_date["2026-08-21"]["snapshot"], "invalid")
        self.assertEqual(by_date["2026-09-17"]["gap"], "critical")
        self.assertEqual(by_date["2026-09-17"]["stage"], "valid")
        self.assertIsNone(by_date["2026-09-10"]["gap"])

    def test_row_hash_alone_is_never_counted_as_valid(self):
        """Production shape: the ledger keeps a hash and still says invalid.

        `snapshot.rowHashes` proves the record exists; only the audit's own
        `snapshotState` may call it valid. Before this rule the page counted
        such dates as valid and painted them green.
        """
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [{"code": "legacy_gaps_or_invalid_rows", "severity": "warning",
                        "dates": ["2026-08-21"]}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS,
                         "rowHashes": {"2026-08-21": "e05cd020", PREVIOUS: "16073733"}},
            "ledger": [{"tradeDate": "2026-08-21", "classification": "legacy",
                        "payloadSha256": "e05cd020", "reason": "non_finite_json",
                        "snapshotState": "invalid", "stageState": "missing"},
                       {"tradeDate": PREVIOUS, "classification": "monitored",
                        "payloadSha256": "16073733", "snapshotState": "valid",
                        "stageState": "valid"}]})
        self.export()
        coverage = self.page("progress")["coverage"]
        by_date = {item["date"]: item for item in coverage["dates"]}
        self.assertEqual(by_date["2026-08-21"]["snapshot"], "invalid")
        self.assertTrue(by_date["2026-08-21"]["snapshotRecorded"])
        self.assertEqual(by_date["2026-08-21"]["snapshotReason"], "non_finite_json")
        self.assertEqual(by_date[PREVIOUS]["snapshot"], "valid")
        self.assertEqual(coverage["validSnapshotDates"], 1)
        self.assertEqual(coverage["recordedSnapshotDates"], 2)

    def test_missing_collections_are_null_while_empty_ones_are_zero(self):
        """字段缺失＝未知，明确空集合＝真的 0。

        The audit omitting `snapshot.rowHashes` / `stages` means it did not
        enumerate them, not that nothing was recorded; only an explicit empty
        dictionary may be reported as zero.
        """
        missing = build_fixture(self.root, health_payload={
            "status": "warning", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [{"code": "unresolved_trading_day_gaps", "severity": "critical",
                        "dates": ["2026-09-17"]}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
        self.fixture = missing
        self.export()
        coverage = self.page("progress")["coverage"]
        self.assertIsNone(coverage["recordedSnapshotDates"], "missing rowHashes is unknown")
        self.assertIsNone(coverage["stagedDates"], "missing stages is unknown")
        self.assertEqual(coverage["knownDates"], 1, "the gap list still names a real date")
        self.assertIsNone(coverage["dates"][0]["snapshotRecorded"], "no claim without rowHashes")

        empty = build_fixture(self.root, health_payload={
            "status": "warning", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [],
            "snapshot": {"rowCount": 0, "latestValidDate": None, "rowHashes": {}},
            "stages": {}, "ledger": []})
        self.fixture = empty
        self.export()
        coverage = self.page("progress")["coverage"]
        self.assertEqual(coverage["recordedSnapshotDates"], 0, "an explicit empty dict is a real zero")
        self.assertEqual(coverage["stagedDates"], 0)
        self.assertEqual(coverage["knownDates"], 0)
        self.assertIsNone(coverage["validSnapshotDates"], "no ledger verdicts means validity is unknown")

    def test_historical_gaps_are_listed_with_evidence_and_split_from_current_issues(self):
        """历史缺口（保留）与当前问题分开；逐日给出真实观察时间与哈希。"""
        self.fixture = build_fixture(self.root, health_payload={
            "status": "critical", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [
                {"code": "unresolved_trading_day_gaps", "severity": "critical",
                 "dates": ["2026-09-17"]},
                {"code": "legacy_gaps_or_invalid_rows", "severity": "warning",
                 "dates": ["2026-08-21"]},
                {"code": "qlib_source_absence", "severity": "warning",
                 "affectedDates": [PREVIOUS]}],
            "snapshot": {"rowCount": 1, "latestValidDate": PREVIOUS,
                         "rowHashes": {PREVIOUS: "aa"}},
            "stages": {"2026-09-17": {"state": "valid",
                                      "observedAt": "2026-09-18T09:04:12+08:00"}},
            "ledger": [{"tradeDate": "2026-08-21", "classification": "legacy",
                        "snapshotState": "invalid", "stageState": "missing",
                        "payloadSha256": "bb", "reason": "non_finite_json",
                        "recoveryReadiness": "missing_or_invalid_market_evidence"}]})
        self.export()
        progress = self.page("progress")
        gaps = progress["historicalGaps"]
        self.assertEqual(gaps["count"], 2)
        self.assertEqual(gaps["criticalDates"], ["2026-09-17"])
        self.assertEqual(gaps["legacyDates"], ["2026-08-21"])
        by_date = {item["date"]: item for item in gaps["evidence"]}
        self.assertEqual(by_date["2026-09-17"]["stageObservedAt"], "2026-09-18T09:04:12+08:00")
        # 审计没有给该日的快照判决，也没有行哈希：只能是 unknown，不替它下结论
        self.assertEqual(by_date["2026-09-17"]["snapshot"], "unknown")
        self.assertEqual(by_date["2026-09-17"]["gap"], "critical")
        self.assertEqual(by_date["2026-08-21"]["payloadSha256"], "bb")
        self.assertEqual(by_date["2026-08-21"]["snapshotReason"], "non_finite_json")
        self.assertEqual(by_date["2026-08-21"]["recoveryReadiness"],
                         "missing_or_invalid_market_evidence")
        # 当前问题只留非历史项（qlib_source_absence），历史缺口不重复出现在这里
        self.assertEqual([item["code"] for item in progress["currentIssues"]],
                         ["qlib_source_absence"])

    def test_coverage_without_audit_numbers_is_null_not_guessed(self):
        """A sparse audit must leave the bars empty instead of inventing one."""
        self.fixture = build_fixture(self.root, health_payload={
            "status": "warning", "generatedAt": f"{DATE}T00:40:00+08:00",
            "issues": [{"code": "unresolved_trading_day_gaps", "severity": "critical",
                        "dates": ["2026-09-17"]}],
            "snapshot": {"rowCount": 7, "latestValidDate": PREVIOUS}})
        self.export()
        coverage = self.page("progress")["coverage"]
        self.assertIsNone(coverage["qlibCoverage"])
        self.assertIsNone(coverage["validSnapshotDates"])
        self.assertIsNone(coverage["snapshotValiditySource"])
        self.assertIsNone(coverage["recordedSnapshotDates"])
        self.assertIsNone(coverage["stagedDates"])
        self.assertEqual([item["date"] for item in coverage["dates"]], ["2026-09-17"])
        self.assertEqual(coverage["dates"][0]["snapshot"], "unknown")
        self.assertEqual(coverage["dates"][0]["gap"], "critical")

    def test_all_five_collector_states_survive_the_export(self):
        """collector -> export: every health value keeps its note and field."""
        states = [("ok", "服务在运行"), ("failed", "Result=failed ActiveState=failed"),
                  ("stopped", "常驻服务未在运行（Type=simple）"),
                  ("disabled", "单元未启用（UnitFileState=disabled）"),
                  ("unknown", "oneshot 从未执行（无完成记录）")]
        snapshot = json.loads(self.fixture["services"].read_text())
        snapshot["units"] = [
            {"unit": "astock-xhs-source.timer", "scope": "user", "kind": "timer",
             "health": health, "note": note, "activeState": "active", "subState": "waiting",
             "unitFileState": "enabled"} for health, note in states]
        self.fixture["services"].write_text(json.dumps(snapshot), encoding="utf-8")
        self.export()
        exported = self.page("projects")["services"]
        self.assertEqual([item["health"] for item in exported], [state for state, _ in states])
        self.assertEqual([item["note"] for item in exported], [note for _, note in states])

    def test_malformed_health_does_not_break_other_pages(self):
        self.fixture = build_fixture(self.root, health_payload=[])
        manifest = self.export()
        self.assertEqual(sorted(manifest["files"]),
                         ["delivery.json", "progress.json", "projects.json", "today.json"])
        progress = self.page("progress")
        self.assertIn("health_json_not_object", progress["readErrors"])
        self.assertIsNone(progress["health"])
        self.assertTrue(progress["stale"])
        self.assertEqual(self.page("today")["report"]["targetDate"], DATE)
        self.assertEqual(self.page("projects")["versions"]["signal-bus"]["head"], "b" * 40)

    def test_builder_exception_degrades_only_that_page(self):
        original = exporter.build_projects
        exporter.build_projects = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(setattr, exporter, "build_projects", original)
        manifest = self.export()
        self.assertEqual(manifest["builderFailures"], {"projects": "RuntimeError"})
        projects = self.page("projects")
        self.assertEqual(projects["reason"], "builder_failed")
        self.assertTrue(projects["stale"])
        self.assertEqual(self.page("today")["report"]["targetDate"], DATE)

    def test_malformed_qlib_manifest_degrades_only_progress(self):
        (self.root / "qlib/data/cn_data/.qlib-dolt-incremental.json").write_text("[]", encoding="utf-8")
        self.export()
        progress = self.page("progress")
        self.assertIn("qlib_manifest_not_object", progress["readErrors"])
        self.assertIsNone(progress["qlib"])
        self.assertEqual(self.page("today")["report"]["targetDate"], DATE)

    # ------------------------------------------------- defect 5: output safety

    def symlinked_prefix_root(self):
        """A root whose *ancestor* is a symlink (the root itself is a real dir)."""
        base = Path(self.temp.name) / "base"
        real = base / "real-out"
        real.mkdir(parents=True)
        link = base / "link-out"
        link.symlink_to(real)
        root = link / "out"
        root.mkdir(parents=True)
        return root, real / "out"

    def test_symlinked_ancestor_above_the_output_root_is_refused_by_default(self):
        root, real = self.symlinked_prefix_root()
        self.assertFalse(root.is_symlink())
        with self.assertRaisesRegex(ValueError, "export_root_ancestor_symlink"):
            exporter.export_all(self.root, root, NOW, self.fixture["health"],
                                self.fixture["services"])
        self.assertEqual(list(real.iterdir()), [])

    def test_explicit_local_mode_allows_a_symlinked_prefix_and_records_it(self):
        root, real = self.symlinked_prefix_root()
        manifest = exporter.export_all(self.root, root, NOW, self.fixture["health"],
                                       self.fixture["services"],
                                       allow_symlinked_ancestors=True,
                                       _publisher=_make_stub_publisher())
        self.assertEqual(manifest["pathCheck"], "resolved-symlink-allowed")
        self.assertTrue((real / "today.json").is_file())
        self.assertTrue((real / "manifest.json").is_file())

    def test_production_default_is_strict(self):
        args = exporter.build_parser().parse_args([])
        self.assertFalse(args.allow_symlinked_ancestors)
        self.assertEqual(exporter.THRESHOLDS_HOURS["today"], 26.0)

    def test_symlinked_parent_component_is_refused(self):
        self.out.mkdir(parents=True, exist_ok=True)
        decoy = Path(self.temp.name) / "decoy"
        decoy.mkdir()
        (self.out / "reports").symlink_to(decoy)
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(list(decoy.iterdir()), [])
        self.assertFalse((self.out / "manifest.json").exists())

    def test_symlinked_output_root_is_refused(self):
        real = Path(self.temp.name) / "real-out"
        real.mkdir()
        self.out.symlink_to(real)
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(list(real.iterdir()), [])

    def test_symlinked_target_file_is_refused(self):
        self.export()
        decoy = Path(self.temp.name) / "decoy.json"
        decoy.write_text("keep")
        (self.out / "today.json").unlink()
        (self.out / "today.json").symlink_to(decoy)
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(decoy.read_text(), "keep")

    # ------------------------------------------------------- content handling

    def test_stale_sources_are_flagged_including_export_stall(self):
        self.export(now=dt.datetime(2026, 9, 24, 9, 0, tzinfo=CST))
        today = self.page("today")
        self.assertTrue(today["stale"])
        self.assertGreater(today["ageSeconds"], 26 * 3600)
        progress = self.page("progress")
        self.assertTrue(progress["stale"])
        self.assertEqual(progress["criticalDates"], ["2026-09-17"])
        self.assertEqual(progress["snapshot"]["rowCount"], 7)

    def test_missing_evidence_is_insufficient_and_recorded(self):
        self.fixture = build_fixture(self.root, with_receipts=False)
        self.export()
        delivery = self.page("delivery")
        self.assertEqual(delivery["layers"]["seal"]["status"], "pass")
        self.assertEqual(delivery["layers"]["telegram"]["status"], "insufficient")
        self.assertEqual(delivery["layers"]["relay"]["status"], "insufficient")
        self.assertFalse(any("telegram" in error for error in delivery["readErrors"]))
        self.assertIn("尚无当日发送记录", delivery["layers"]["telegram"]["limitations"][0])
        # 缺失的 relay 记录同样是层判定而非读取失败；仅尝试观测记录可读性才进 readErrors。
        self.assertEqual(delivery["layers"]["relay"]["limitations"], ["尚无当日 relay 记录"])
        self.assertFalse(any("relay" in error for error in delivery["readErrors"]))

    def test_malicious_markdown_is_inert(self):
        self.export()
        today = self.page("today")
        for block in today["report"]["blocks"]:
            self.assertLessEqual(len(block["text"]), exporter.MAX_TEXT_CHARS)
            for url in block.get("urls", []):
                self.assertTrue(url.startswith(("http://", "https://")), url)
        for source in today["report"]["sources"]:
            if source["url"] is not None:
                self.assertTrue(source["url"].startswith(("http://", "https://")), source["url"])
        dumped = json.dumps(today, ensure_ascii=False)
        self.assertNotIn("javascript:", dumped)
        self.assertNotIn("data:text/html", dumped)
        self.assertIn("<script>", dumped)  # stays inert text, rendered via textContent

    def test_exporter_has_no_database_subprocess_or_network_usage(self):
        source = (ROOT / "export_dashboard.py").read_text(encoding="utf-8")
        for forbidden in ("import sqlite3", "import subprocess", "import socket", "import requests",
                          "os.system", "urllib.request"):
            self.assertNotIn(forbidden, source, forbidden)

    @unittest.skipUnless((ROOT.parent / "prototype" / "assets" / "app.js").is_file(),
                         "prototype pages are not deployed next to the exporter")
    def test_client_recomputes_freshness_and_avoids_html_parsing(self):
        app = (ROOT.parent / "prototype" / "assets" / "app.js").read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", app, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        for forbidden in ("innerHTML", "outerHTML", "document.write", "eval(", "new Function",
                          "insertAdjacentHTML", "//cdn", '<script src=', 'fetch("http'):
            self.assertNotIn(forbidden, code, forbidden)
        # The only absolute URL allowed is the internal base used by new URL().
        schemes = re.findall(r"https?://[\w.-]+", code)
        self.assertEqual(sorted(set(schemes)), ["http://localhost"])
        self.assertIn("recomputeFreshness", code)

    def test_services_allowlist_and_pending_authorization(self):
        self.export()
        projects = self.page("projects")
        self.assertEqual([unit["unit"] for unit in projects["services"]], ["astock-xhs-source.timer"])
        self.assertIn("公网发布（含认证、证书与最小暴露面）", projects["pendingAuthorization"])

    def test_service_snapshot_fields_pass_through_with_a_fixed_allowlist(self):
        snapshot = json.loads(self.fixture["services"].read_text())
        snapshot["units"] = [
            {"unit": "astock-xhs-source.timer", "scope": "user", "kind": "timer",
             "health": "stopped", "note": "定时器已启用但当前未在运行", "activeState": "inactive",
             "subState": "dead", "unitFileState": "enabled", "result": None,
             "execMainStatus": None, "nextElapse": None, "nextElapseSource": None,
             "loadState": "loaded", "restarts": "0", "lastStart": None, "lastExit": None,
             "Environment": "SECRET=1", "ExecStart": "/opt/secret"},
            {"unit": "astock-acceptance-monday.timer", "scope": "user", "kind": "timer",
             "health": "ok", "note": "定时器在运行", "activeState": "active",
             "subState": "waiting", "unitFileState": "enabled",
             "nextElapse": "2026-09-21T08:20:00+08:00", "nextElapseSource": "realtime"}]
        self.fixture["services"].write_text(json.dumps(snapshot), encoding="utf-8")
        self.export()
        services = {item["unit"]: item for item in self.page("projects")["services"]}
        self.assertEqual(sorted(services), ["astock-acceptance-monday.timer", "astock-xhs-source.timer"])
        stopped = services["astock-xhs-source.timer"]
        self.assertEqual(stopped["health"], "stopped")
        self.assertIn("未在运行", stopped["note"])
        self.assertEqual(stopped["scope"], "user")
        self.assertNotIn("Environment", stopped)
        self.assertNotIn("ExecStart", stopped)
        self.assertEqual(services["astock-acceptance-monday.timer"]["nextElapse"],
                         "2026-09-21T08:20:00+08:00")

    def test_history_zero_writes_nothing_extra(self):
        manifest = self.export(history=1)
        self.assertEqual(sorted(manifest["history"]["pages"]), [DATE])


class TradingCalendarFreshnessTests(unittest.TestCase):
    """Synthetic sealed calendars; never consult a live calendar or alter an archive."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "AStock_AI_System"
        self.date = "2026-09-18"
        self.source = dt.datetime(2026, 9, 18, 8, tzinfo=CST)
        self.now = dt.datetime(2026, 9, 20, 16, 48, tzinfo=CST)
        self.calendar = {"targetDate": self.date, "status": "trading_day",
                         "nextTradingDate": "2026-09-21",
                         "dataAsOf": "2026-09-18T08:00:17+08:00",
                         "sourceIds": ["akshare:sina:trade-calendar"]}
        self.archive = self.project / "work/report-archives" / self.date
        self.archive.mkdir(parents=True)
        self.report_path = self.project / "reports/daily" / f"{self.date}.md"
        self.report_path.parent.mkdir(parents=True)
        self.report_path.write_text(REPORT.replace(DATE, self.date))
        self.write_calendar()
        (self.project / "work/last-run.json").write_text(json.dumps({
            "targetDate": self.date, "status": "success",
            "completedAt": "2026-09-18T08:00:52.672+08:00"}))

    def write_calendar(self, *, target=None):
        frozen = {"targetDate": target or self.date,
                  "compilerContext": {"data": {"tradingCalendar": self.calendar}}}
        raw = json.dumps(frozen).encode()
        (self.archive / "input.json").write_bytes(raw)
        (self.archive / "published.json").write_text(json.dumps({
            "status": "sealed", "inputSha256": sha(raw),
            "reportSha256": sha(self.report_path.read_bytes())}))

    def freshness(self, **kwargs):
        return exporter.report_freshness(self.project, self.date,
            kwargs.get("now", self.now), kwargs.get("source", self.source),
            kwargs.get("page", "today"))

    def test_sunday_keeps_friday_time_and_uses_sealed_next_trading_date(self):
        result = self.freshness()
        self.assertFalse(result["stale"])
        self.assertEqual(result["reason"], "latest_scheduled_report")
        self.assertEqual(result["sourceTime"], self.source.isoformat())
        self.assertGreater(result["ageSeconds"], 56 * 3600)
        self.assertEqual(result["freshnessPolicy"]["expectedAt"], "2026-09-21T08:00:00+08:00")
        self.assertEqual(result["freshnessPolicy"]["deadlineAt"], "2026-09-21T09:01:00+08:00")
        self.assertEqual(result["freshnessPolicy"]["calendarObservedAt"], self.calendar["dataAsOf"])

    def test_monday_before_during_and_after_run_window(self):
        for hour, minute, stale, reason in ((7, 59, False, "latest_scheduled_report"),
                                          (8, 0, False, "awaiting_scheduled_report"),
                                          (9, 0, False, "awaiting_scheduled_report"),
                                          (9, 1, True, "expected_report_overdue")):
            with self.subTest(time=(hour, minute)):
                result = self.freshness(now=dt.datetime(2026, 9, 21, hour, minute, tzinfo=CST))
                self.assertEqual(result["stale"], stale)
                self.assertEqual(result["reason"], reason)

    def test_missing_friday_is_not_excused_by_sunday(self):
        # Thursday report knows next Friday; a weekend never extends that deadline.
        self.date = "2026-09-17"
        self.source -= dt.timedelta(days=1)
        self.calendar.update(targetDate=self.date, nextTradingDate="2026-09-18",
                             dataAsOf="2026-09-17T08:00:17+08:00")
        self.archive = self.project / "work/report-archives" / self.date
        self.archive.mkdir()
        self.report_path = self.report_path.with_name(f"{self.date}.md")
        self.report_path.write_text(REPORT.replace(DATE, self.date))
        self.write_calendar()
        self.assertTrue(self.freshness()["stale"])
        self.assertEqual(self.freshness()["reason"], "expected_report_overdue")

    def test_long_break_uses_calendar_not_weekday_guessing(self):
        # Synthetic holiday interval: no hardcoded exchange holiday list is involved.
        self.calendar["nextTradingDate"] = "2026-09-28"
        self.write_calendar()
        self.assertFalse(self.freshness(now=dt.datetime(2026, 9, 23, 12, tzinfo=CST))["stale"])

    def test_missing_or_malformed_calendar_falls_back_without_waiver(self):
        for field, value in (("nextTradingDate", None), ("nextTradingDate", "2026-02-30"),
                             ("nextTradingDate", "2026-09-18"), ("nextTradingDate", "2099-01-01"),
                             ("status", "unknown"), ("targetDate", "2026-09-17"),
                             ("sourceIds", []), ("dataAsOf", "2026-09-18T08:00:00"),
                             ("dataAsOf", "2026-09-17T08:00:00+08:00")):
            original = dict(self.calendar)
            with self.subTest(field=field, value=value):
                self.calendar[field] = value
                self.write_calendar()
                result = self.freshness()
                self.assertTrue(result["stale"])
                self.assertIsNone(result["freshnessPolicy"])
            self.calendar = original

    def test_hash_mismatch_never_waives_staleness(self):
        for filename in ("input.json", "published.json"):
            self.write_calendar()
            path = self.archive / filename
            path.write_bytes(path.read_bytes() + (b" " if filename == "input.json" else b"broken"))
            self.assertTrue(self.freshness()["stale"])
            self.assertIsNone(self.freshness()["freshnessPolicy"])
        self.write_calendar()
        self.report_path.write_text("changed report")
        self.assertIsNone(self.freshness()["freshnessPolicy"])

    def test_date_or_source_time_missing_wrong_naive_or_future_never_waived(self):
        for stamp in (None, self.source.replace(tzinfo=None), self.source-dt.timedelta(days=1)):
            result = self.freshness(source=stamp)
            self.assertTrue(result["stale"])
            self.assertIsNone(result["freshnessPolicy"])
        self.assertTrue(self.freshness(now=self.source-dt.timedelta(hours=1))["stale"])
        self.write_calendar(target="2026-09-17")
        self.assertIsNone(self.freshness()["freshnessPolicy"])

    def test_timezone_is_business_cst_not_host_timezone(self):
        utc_now = self.now.astimezone(dt.timezone.utc)
        utc_source = self.source.astimezone(dt.timezone.utc)
        self.assertEqual(self.freshness(), self.freshness(now=utc_now, source=utc_source))

    def test_health_and_services_do_not_gain_a_weekend_waiver(self):
        for name in ("progress", "projects"):
            self.assertTrue(exporter.freshness(self.now, self.source, name)["stale"])
            result = self.freshness(page=name)
            self.assertTrue(result["stale"])
            self.assertIsNone(result["freshnessPolicy"])

    def test_export_paths_use_policy_but_receipt_missing_stays_insufficient(self):
        before = {path: sha(path.read_bytes()) for path in (self.report_path,
                  self.archive / "input.json", self.archive / "published.json")}
        out = self.root / "out"
        manifest = exporter.export_all(self.root, out, self.now, self.root / "no-health.json",
                                       _publisher=_make_stub_publisher())
        for name in ("today.json", "delivery.json", f"reports/{self.date}.json"):
            panel = json.loads((out / name).read_bytes())
            self.assertFalse(panel["stale"], name)
            self.assertEqual(panel["freshnessPolicy"]["reportDate"], self.date)
        delivery = json.loads((out / "delivery.json").read_bytes())
        self.assertEqual(delivery["layers"]["telegram"]["status"], "insufficient")
        self.assertEqual(delivery["layers"]["relay"]["status"], "insufficient")
        self.assertTrue(manifest["files"]["progress.json"]["stale"])
        self.assertFalse(manifest["files"]["today.json"]["stale"])
        self.assertEqual(before, {path: sha(path.read_bytes()) for path in before})


if __name__ == "__main__":
    unittest.main(verbosity=2)
