"""Batch 1 (2026-09-26): seal monitored, expired timers 已结束, holiday expected report."""
import datetime as dt
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import collect_services as collector  # noqa: E402
import export_dashboard as exporter  # noqa: E402
import os
exporter.DELIVERY_CONTRACT_PATH = Path(os.environ.get("DELIVERY_CONTRACT_TEST_MODULE",
    str(Path(__file__).resolve().parents[3] / "signal/scripts/delivery_evidence.py")))

CST = dt.timezone(dt.timedelta(hours=8))


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class CollectorTests(unittest.TestCase):
    def test_seal_is_in_the_monitored_set_and_exported(self):
        self.assertIn(("astock-research-seal.service", "user", "service"), collector.UNITS)
        self.assertIn("astock-research-seal.service", exporter.KNOWN_UNITS)
        self.assertEqual({u for u, _, _ in collector.UNITS}, set(exporter.KNOWN_UNITS))

    def test_expired_one_shot_timer_is_ended_not_running(self):
        props = {"LoadState": "loaded", "ActiveState": "active", "SubState": "elapsed",
                 "UnitFileState": "enabled", "NextElapseUSecRealtime": "",
                 "NextElapseUSecMonotonic": "infinity", "LastTriggerUSec": "Mon 2026-09-21 08:20:00 CST"}
        health, note = collector.classify("timer", props)
        self.assertEqual(health, "ended")
        self.assertIn("已结束", note)
        self.assertNotIn("定时器在运行", note)

    def test_waiting_timers_stay_running(self):
        for props in ({"SubState": "waiting", "NextElapseUSecRealtime": "Mon 2026-09-28 08:00:00 CST",
                       "NextElapseUSecMonotonic": "0"},
                      {"SubState": "waiting", "NextElapseUSecRealtime": "",
                       "NextElapseUSecMonotonic": "2w 4d 18h 29min 26.836840s"}):
            props.update(LoadState="loaded", ActiveState="active", UnitFileState="enabled")
            self.assertEqual(collector.classify("timer", props), ("ok", "定时器在运行"))

    def test_oneshot_inactive_is_not_failure_and_condition_skip_is_noted(self):
        base = {"LoadState": "loaded", "Type": "oneshot", "ActiveState": "inactive", "Result": "success",
                "ExecMainStatus": "0", "ExecMainStartTimestamp": "@1790294400",
                "ExecMainExitTimestamp": "@1790294411"}
        self.assertEqual(collector.classify("service", base), ("ok", "oneshot 正常结束"))
        skipped = dict(base, ConditionResult="no", ConditionTimestamp="@1790899200")
        health, note = collector.classify("service", skipped)
        self.assertEqual(health, "ok")
        self.assertIn("按启动条件跳过", note)
        never_ran = {"LoadState": "loaded", "Type": "oneshot", "ActiveState": "inactive",
                     "Result": "success", "ConditionResult": "no", "ConditionTimestamp": "@1790899200"}
        self.assertEqual(collector.classify("service", never_ran)[0], "ok")

    def test_failed_seal_stays_failed_even_after_a_condition_skip(self):
        props = {"LoadState": "loaded", "Type": "oneshot", "ActiveState": "failed", "Result": "exit-code",
                 "ExecMainStatus": "1", "ExecMainExitTimestamp": "@1790294411",
                 "ConditionResult": "no", "ConditionTimestamp": "@1790899200"}
        self.assertEqual(collector.classify("service", props)[0], "failed")

    def test_condition_properties_are_not_forbidden(self):
        parsed = collector.parse_show("ConditionResult=no\nConditionTimestamp=@1\nExecCondition=/bin/x\n")
        self.assertEqual(parsed, {"ConditionResult": "no", "ConditionTimestamp": "@1"})


REPORT = "# A股晨报 {date}\n\n> 数据截止：{date} 08:00（Asia/Shanghai）\n\n## 一句话结论\n\n测试。\n"


class HolidayExpectedReportTests(unittest.TestCase):
    """Synthetic 09-24 sealed report + validated 09-25 holiday skip (Mid-Autumn shape)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "AStock_AI_System"
        self.report_date, self.holiday = "2026-09-24", "2026-09-25"
        (self.project / "reports/daily").mkdir(parents=True)
        self.report = self.project / "reports/daily" / f"{self.report_date}.md"
        self.report.write_text(REPORT.format(date=self.report_date), encoding="utf-8")
        archive = self.project / "work/report-archives" / self.report_date
        (archive / "result").mkdir(parents=True)
        calendar = {"targetDate": self.report_date, "status": "trading_day", "nextTradingDate": "2026-09-28",
                    "dataAsOf": "2026-09-24T08:00:03+08:00", "sourceIds": ["akshare:sina:trade-calendar"]}
        frozen = json.dumps({"targetDate": self.report_date,
                             "compilerContext": {"data": {"tradingCalendar": calendar}}}).encode()
        (archive / "input.json").write_bytes(frozen)
        (archive / "result/result.json").write_text('{"status": "validated"}\n')
        (archive / "published.json").write_text(json.dumps({
            "status": "sealed", "inputSha256": sha(frozen), "reportSha256": sha(self.report.read_bytes()),
            "resultSha256": sha(b'{"status": "validated"}')}))
        self.write_skip()

    def write_skip(self, *, tamper=False, status="non_trading_day"):
        context = json.dumps({"data": {"tradingCalendar": {
            "targetDate": self.holiday, "status": status, "previousTradingDate": self.report_date,
            "nextTradingDate": "2026-09-28", "sourceIds": ["akshare:sina:trade-calendar"]}}}).encode()
        (self.project / "work/morning-context.json").write_bytes(context + (b" " if tamper else b""))
        (self.project / "work/last-run.json").write_text(json.dumps({
            "targetDate": self.holiday, "status": "skipped", "phase": "collect",
            "tradingStatus": "non_trading_day", "publicationGate": "not_applicable",
            "contextFile": "work/morning-context.json", "contextSha256": sha(context),
            "completedAt": "2026-09-25T00:00:11.085Z"}))

    def pages(self, now):
        today = exporter.build_today(self.root, now, 7)
        return today, exporter.build_delivery(self.root, now, today)

    def test_holiday_weekend_shows_not_required_and_judges_latest_trading_day(self):
        now = dt.datetime(2026, 9, 26, 15, 0, tzinfo=CST)
        today, delivery = self.pages(now)
        self.assertEqual(delivery["targetDate"], self.report_date)
        self.assertEqual(delivery["expectedReport"]["text"], "无需新稿，最近有效交易日为 2026-09-24（下一交易日 2026-09-28）")
        self.assertEqual(today["expectedReport"], delivery["expectedReport"])
        self.assertEqual(delivery["layers"]["seal"]["status"], "pass")
        self.assertFalse(delivery["stale"])
        self.assertEqual(delivery["freshnessPolicy"]["nextTradingDate"], "2026-09-28")
        self.assertNotIn("published_json_missing", delivery["readErrors"])

    def test_next_trading_day_is_awaited_then_overdue_never_the_holiday(self):
        """R04: on 09-28 the panel must await/overdue today's run, not the holiday."""
        awaiting = dt.datetime(2026, 9, 28, 8, 20, tzinfo=CST)
        today, delivery = self.pages(awaiting)
        self.assertEqual(delivery["expectedReport"]["status"], "awaiting")
        self.assertEqual(delivery["expectedReport"]["expectedDate"], "2026-09-28")
        self.assertIn("等待 2026-09-28 本次运行", delivery["expectedReport"]["text"])
        self.assertEqual(today["expectedReport"], delivery["expectedReport"])
        # 图层仍判定最近可用报告，不指向休市日
        self.assertEqual(delivery["targetDate"], self.report_date)

        overdue = dt.datetime(2026, 9, 28, 9, 2, tzinfo=CST)
        today, delivery = self.pages(overdue)
        self.assertEqual(delivery["expectedReport"]["status"], "overdue")
        self.assertEqual(delivery["expectedReport"]["expectedDate"], "2026-09-28")
        self.assertEqual(delivery["targetDate"], self.report_date)
        self.assertNotEqual(delivery["targetDate"], self.holiday)

    def test_published_status_failed_never_passes_even_with_matching_hashes(self):
        """R02: hash equality alone is not a sealed report."""
        path = self.project / "work/report-archives" / self.report_date / "published.json"
        published = json.loads(path.read_text())
        published["status"] = "failed"
        path.write_text(json.dumps(published))
        now = dt.datetime(2026, 9, 26, 15, 0, tzinfo=CST)
        _, delivery = self.pages(now)
        seal = delivery["layers"]["seal"]
        self.assertEqual(seal["status"], "fail")
        self.assertIn("不是已封存", seal["limitations"][0])
        self.assertEqual(seal["evidence"]["publishedStatus"], "failed")

    def test_published_status_missing_is_insufficient_not_pass(self):
        path = self.project / "work/report-archives" / self.report_date / "published.json"
        published = json.loads(path.read_text())
        published.pop("status")
        path.write_text(json.dumps(published))
        now = dt.datetime(2026, 9, 26, 15, 0, tzinfo=CST)
        _, delivery = self.pages(now)
        seal = delivery["layers"]["seal"]
        self.assertEqual(seal["status"], "insufficient")
        self.assertIn("缺少 status", seal["limitations"][0])

    def test_unbound_or_non_holiday_skip_keeps_previous_behaviour(self):
        now = dt.datetime(2026, 9, 26, 15, 0, tzinfo=CST)
        for kwargs in ({"tamper": True}, {"status": "trading_day"}):
            with self.subTest(**kwargs):
                self.write_skip(**kwargs)
                today, delivery = self.pages(now)
                self.assertIsNone(delivery["expectedReport"])
                self.assertEqual(delivery["targetDate"], self.holiday)

    def test_holiday_does_not_hide_a_missing_previous_trading_report(self):
        self.report.unlink()
        now = dt.datetime(2026, 9, 26, 15, 0, tzinfo=CST)
        today, delivery = self.pages(now)
        self.assertIsNone(today["expectedReport"])
        self.assertEqual(delivery["targetDate"], self.report_date)
        self.assertNotEqual(delivery["layers"]["seal"]["status"], "pass")
        self.assertTrue(delivery["stale"])


if __name__ == "__main__":
    unittest.main()
