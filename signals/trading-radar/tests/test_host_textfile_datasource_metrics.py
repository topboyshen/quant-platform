"""Offline JSON fixtures only. Never invoke the exporter main() or a real journal."""
import ast
import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/export_host_textfile_metrics.py"
SPEC = importlib.util.spec_from_file_location("datasource_exporter_under_test", SOURCE)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)
I1, I2 = "a" * 32, "b" * 32
T = 1788766200000000  # 2026-09-07 15:30:00 CST


def row(inv=I1, offset=0, message="example", manager=False, cursor=None, **extra):
    value = {"__CURSOR": cursor or inv + ":" + str(offset), "__REALTIME_TIMESTAMP": str(T + offset),
             "_PID": "1" if manager else "12345", "MESSAGE": message}
    if manager:
        value.update({"UNIT": m.EOD_UNIT, "INVOCATION_ID": inv})
    else:
        value.update({"_SYSTEMD_UNIT": m.EOD_UNIT, "_SYSTEMD_INVOCATION_ID": inv})
    value.update(extra)
    return value


def start(inv=I1, offset=0):
    return row(inv, offset, manager=True, MESSAGE_ID=m.STARTING_ID, JOB_TYPE="start")


def finish(inv=I1, offset=30_000_000):
    return row(inv, offset, manager=True, MESSAGE_ID=m.FINISHED_ID, JOB_TYPE="start", JOB_RESULT="done")


def failed(inv=I1, offset=30_000_000, unit_result=False):
    if unit_result:
        return row(inv, offset, manager=True, MESSAGE_ID=m.FAILED_UNIT_ID, UNIT_RESULT="exit-code")
    return row(inv, offset, manager=True, MESSAGE_ID=m.FAILED_JOB_ID, JOB_TYPE="start", JOB_RESULT="failed")


def kline(inv=I1, offset=1_000_000, ok=False):
    text = "[fetch_tencent_kline] 腾讯 K线获取成功: 510300 rows=100" if ok else "[fetch_tencent_kline] 腾讯 K线获取失败 510300: HTTP Error 501"
    return row(inv, offset, text)


def body(rows):
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in rows)


def analyze(rows, now=None):
    return m.analyze_eod_journal(body(rows), now=T / 1_000_000 + 60 if now is None else now)


class JournalIdentityTests(unittest.TestCase):
    def test_start_application_and_manager_finished_form_complete_event(self):
        result = analyze([start(), kline(), finish()])
        self.assertEqual((result["parsed"], result["complete"], result["has_samples"]), (1, 1, 1))
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["event"]["result"], "success")
        self.assertEqual(result["event"]["event_date"], "2026-09-07")
        self.assertEqual(result["event"]["event_time"], "2026-09-07T15:30:30+08:00")

    def test_later_retry_unit_success_is_selected_over_earlier_eod_failure(self):
        retry = m.EOD_RETRY_UNIT
        later = [
            start(), kline(), failed(),
            start(I2, 40_000_000), kline(I2, 41_000_000, ok=True), finish(I2, 50_000_000),
        ]
        later[3]["UNIT"] = retry
        later[4]["_SYSTEMD_UNIT"] = retry
        later[5]["UNIT"] = retry
        result = analyze(later)
        self.assertEqual(result["event"]["invocation_id"], I2)
        self.assertEqual(result["event"]["result"], "success")
        self.assertEqual(result["counts"]["kline_fail"], 0)
        self.assertEqual(result["counts"]["kline_ok"], 1)

    def test_latest_complete_invocation_not_window_sum(self):
        result = analyze([start(), kline(), finish(), start(I2, 40_000_000),
                          kline(I2, 41_000_000, ok=True), finish(I2, 50_000_000)])
        self.assertEqual(result["event"]["invocation_id"], I2)
        self.assertEqual(result["counts"]["kline_fail"], 0)
        self.assertEqual(result["counts"]["kline_ok"], 1)

    def test_latest_running_preserves_old_complete_and_marks_progress(self):
        result = analyze([start(), kline(), finish(), start(I2, 40_000_000), kline(I2, 41_000_000, ok=True)])
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["in_progress"], 1)
        self.assertEqual(result["latest_incomplete"], 1)
        self.assertEqual(result["counts"]["kline_fail"], 1)
        self.assertEqual(result["counts"]["kline_ok"], 0)

    def test_latest_terminal_without_start_is_not_latest_normal(self):
        result = analyze([start(), kline(), finish(), kline(I2, 41_000_000, ok=True), finish(I2, 50_000_000)])
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["in_progress"], 0)
        self.assertEqual(result["latest_incomplete"], 1)
        self.assertEqual(result["counts"]["kline_ok"], 0)

    def test_old_healthy_plus_over_budget_unfinished_is_unknown_not_forever_running(self):
        rows = [start(), kline(ok=True), finish(), start(I2, 40_000_000), kline(I2, 41_000_000)]
        result = analyze(rows, now=T / 1_000_000 + 40 + 55 * 60)
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["counts"]["kline_fail"], 0)
        self.assertEqual(result["complete"], 1)
        self.assertEqual(result["latest_incomplete"], 1)
        self.assertEqual(result["in_progress"], 0)

    def test_app_only_without_start_is_unknown_not_running(self):
        result = analyze([start(), kline(ok=True), finish(), kline(I2, 41_000_000)])
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["latest_incomplete"], 1)
        self.assertEqual(result["in_progress"], 0)

    def test_future_start_is_unknown_not_running(self):
        result = analyze([start(), kline(ok=True), finish(), start(I2, 90_000_000)],
                         now=T / 1_000_000 + 60)
        self.assertEqual(result["latest_incomplete"], 1)
        self.assertEqual(result["in_progress"], 0)

    def test_running_budget_boundary_does_not_reset_from_latest_log_time(self):
        rows = [start(), kline(offset=2_999_000_000)]
        within = analyze(rows, now=T / 1_000_000 + 2999)
        exhausted = analyze(rows, now=T / 1_000_000 + 3000)
        self.assertEqual(within["in_progress"], 1)
        self.assertEqual(exhausted["in_progress"], 0)
        self.assertEqual(exhausted["latest_incomplete"], 1)

    def test_only_running_is_not_complete_or_zero_success(self):
        result = analyze([start(), kline(ok=True)])
        self.assertEqual(result["parsed"], 1)
        self.assertEqual(result["complete"], 0)
        self.assertEqual(result["in_progress"], 1)
        text = m.render_eod_metrics(result, sampled_at=123)
        self.assertIn("radar_datasource_primary_kline_failure_ratio NaN", text)
        self.assertNotIn("radar_datasource_eod_info{", text)

    def test_no_start_never_claims_complete(self):
        result = analyze([kline(), finish()])
        self.assertEqual(result["complete"], 0)
        self.assertEqual(result["latest_incomplete"], 1)

    def test_empty_journal_is_parsed_but_no_evidence(self):
        result = m.analyze_eod_journal("")
        self.assertEqual(result["parsed"], 1)
        self.assertEqual((result["complete"], result["has_samples"]), (0, 0))
        self.assertIsNone(result["event"])

    def test_malformed_json_fails_closed_without_partial_counts(self):
        result = m.analyze_eod_journal(body([start(), kline(), finish()]) + "\n{bad json")
        self.assertEqual(result["parsed"], 0)
        self.assertEqual(result["complete"], 0)
        self.assertIsNone(result["counts"])

    def test_nonobject_json_fails_closed(self):
        for value in ([], None, "not an object", 1):
            with self.subTest(value=value):
                self.assertEqual(m.analyze_eod_journal(json.dumps(value))["parsed"], 0)

    def test_deep_json_is_explicit_parse_unknown_not_uncaught_recursion(self):
        result = m.analyze_eod_journal("[" * 2000 + "0" + "]" * 2000)
        self.assertEqual(result["parsed"], 0)
        self.assertEqual(result["complete"], 0)
        self.assertIsNone(result["counts"])

    def test_duplicate_cursor_is_not_duplicate_log_entry(self):
        item = kline()
        result = analyze([start(), item, copy.deepcopy(item), finish()])
        self.assertEqual(result["counts"]["kline_fail"], 1)

    def test_conflicting_duplicate_cursor_fails_closed(self):
        item = kline()
        other = copy.deepcopy(item)
        other["MESSAGE"] += "changed"
        self.assertEqual(analyze([start(), item, other, finish()])["parsed"], 0)

    def test_missing_or_ambiguous_journal_identity_fails_closed(self):
        for key, value in (("__CURSOR", None), ("_SYSTEMD_INVOCATION_ID", ""),
                           ("_SYSTEMD_INVOCATION_ID", [I1, I2]), ("MESSAGE", ["one", "two"]),
                           ("__REALTIME_TIMESTAMP", "not a time")):
            with self.subTest(key=key):
                item = kline()
                item[key] = value
                self.assertEqual(analyze([start(), item, finish()])["parsed"], 0)

    def test_pid1_manager_invocation_field_not_application_field_is_used(self):
        beginning, ending = start(), finish()
        beginning["_SYSTEMD_INVOCATION_ID"] = I2
        ending["_SYSTEMD_INVOCATION_ID"] = I2
        self.assertEqual(analyze([beginning, kline(), ending])["event"]["invocation_id"], I1)

    def test_unrelated_outcomes_cannot_dilute_eod_failures(self):
        unrelated = kline(I2, 2_000_000, ok=True)
        unrelated["_SYSTEMD_UNIT"] = "stock-radar-swing-outcomes.service"
        result = analyze([start(), kline(), unrelated, finish()])
        self.assertEqual(result["counts"]["kline_samples"], 1)
        self.assertEqual(result["counts"]["kline_fail"], 1)

    def test_no_localized_message_dependency_for_finished(self):
        ending = finish()
        ending["MESSAGE"] = "服务已完成，无任何英文 Finished"
        self.assertEqual(analyze([start(), kline(), ending])["complete"], 1)

    def test_application_finished_text_is_not_manager_terminal(self):
        ending = row(offset=5_000_000, message="Finished stock-radar-swing-eod.service", MESSAGE_ID=m.FINISHED_ID,
                     JOB_TYPE="start", JOB_RESULT="done", INVOCATION_ID=I1)
        self.assertEqual(analyze([start(), kline(), ending])["complete"], 0)

    def test_deactivated_successfully_is_not_job_finished(self):
        ending = row(offset=5_000_000, manager=True, MESSAGE_ID="7ad2d189f7e94e70a38c781354912448",
                     message="Deactivated successfully")
        self.assertEqual(analyze([start(), kline(), ending])["complete"], 0)

    def test_manager_failed_job_and_unit_fields_are_terminal_failure(self):
        for ending in (failed(), failed(unit_result=True)):
            with self.subTest(ending=ending):
                result = analyze([start(), kline(), ending])
                self.assertEqual(result["complete"], 1)
                self.assertEqual(result["event"]["result"], "failed")
                self.assertIn("radar_datasource_eod_terminal_success 0", m.render_eod_metrics(result))

    def test_conflicting_terminal_results_are_not_silently_success(self):
        result = analyze([start(), kline(), failed(offset=20_000_000), finish()])
        self.assertEqual(result["parsed"], 0)

    def test_out_of_order_rows_are_grouped_by_identity_and_timestamp(self):
        result = analyze([finish(), kline(), start()])
        self.assertEqual(result["complete"], 1)

    def test_application_log_outside_manager_lifecycle_is_incomplete(self):
        result = analyze([start(), kline(offset=40_000_000), finish()])
        self.assertEqual(result["complete"], 0)
        self.assertEqual(result["latest_incomplete"], 1)


class JournalMessageEncodingTests(unittest.TestCase):
    def test_string_message_remains_unchanged(self):
        text = "\r[fetch_tencent_kline] 腾讯 K线获取成功: 510300 rows=1"
        self.assertEqual(m.journal_message(text), text)

    def test_integer_byte_message_is_strictly_decoded_and_counted(self):
        item = kline()
        item["MESSAGE"] = list(("\r  0%|          |\r" + item["MESSAGE"]).encode("utf-8"))
        result = analyze([start(), item, finish()])
        self.assertEqual((result["parsed"], result["complete"]), (1, 1))
        self.assertEqual(result["counts"]["kline_fail"], 1)

    def test_duplicate_byte_message_preserves_cursor_deduplication(self):
        item = kline()
        item["MESSAGE"] = list(item["MESSAGE"].encode("utf-8"))
        result = analyze([start(), item, copy.deepcopy(item), finish()])
        self.assertEqual(result["counts"]["kline_fail"], 1)

    def test_empty_bytes_are_not_samples_or_recovery(self):
        result = analyze([start(), row(offset=1_000_000, message=[]), finish()])
        self.assertEqual((result["parsed"], result["complete"], result["has_samples"]), (1, 1, 0))
        self.assertIn("radar_datasource_primary_kline_failure_ratio NaN", m.render_eod_metrics(result))

    def test_byte_boundaries_require_valid_utf8_without_replacement(self):
        self.assertEqual(m.journal_message([0, 127, 194, 128]), "\x00\x7f\x80")
        for value in ([255], [128], [192, 175], [237, 160, 128], [226, 130]):
            with self.subTest(value=value):
                item = row(offset=2_000_000, message=value)
                result = analyze([start(), kline(), item, finish()])
                self.assertEqual(result["parsed"], 0)
                self.assertEqual(result["error"], "journal_message_invalid_utf8")
                self.assertIsNone(result["counts"])

    def test_nonbytes_arrays_and_other_types_fail_closed(self):
        for value in ([True], [False], [13.0], [-1], [256], ["13"], ["one", "two"],
                      [[13]], [13, [13]], [13, None], None, {}, 13):
            with self.subTest(value=value):
                result = analyze([start(), kline(), row(offset=2_000_000, message=value), finish()])
                self.assertEqual(result["parsed"], 0)
                self.assertIsNone(result["counts"])

    def test_bytes_are_never_used_to_normalize_identity_fields(self):
        for manager in (False, True):
            identity = "INVOCATION_ID" if manager else "_SYSTEMD_INVOCATION_ID"
            for key in (identity, "__CURSOR", "__REALTIME_TIMESTAMP"):
                with self.subTest(manager=manager, key=key):
                    item = start() if manager else kline()
                    item[key] = list(item[key].encode("utf-8"))
                    result = analyze([item, finish()] if manager else [start(), item, finish()])
                    self.assertEqual(result["parsed"], 0)

    def test_missing_application_or_lifecycle_identity_is_not_skipped(self):
        for item, key in ((kline(), "_SYSTEMD_INVOCATION_ID"), (start(), "INVOCATION_ID"),
                          (finish(), "INVOCATION_ID"), (failed(), "INVOCATION_ID")):
            with self.subTest(key=key, item=item):
                del item[key]
                self.assertEqual(analyze([start(), kline(), finish(), item])["parsed"], 0)

    def test_real_20260828_binary_message_fixture_decodes_without_dropping_row(self):
        item = json.loads((ROOT / "tests/fixtures/host_textfile_datasource/eod-message-bytes-20260828.redacted.json").read_text(encoding="utf-8"))
        self.assertEqual(len(item["MESSAGE"]), 258)
        self.assertTrue(all(type(byte) is int and 0 <= byte <= 255 for byte in item["MESSAGE"]))
        decoded = m.journal_message(item["MESSAGE"])
        self.assertEqual(decoded.count("\r"), 3)
        self.assertIn("[_collect_total_amount] AkShare 成交额采集失败", decoded)
        inv, offset = item["_SYSTEMD_INVOCATION_ID"], int(item["__REALTIME_TIMESTAMP"]) - T
        result = analyze([start(inv, offset - 1_000_000), item, finish(inv, offset + 1_000_000)])
        self.assertEqual((result["parsed"], result["complete"]), (1, 1))
        self.assertEqual(result["event"]["invocation_id"], inv)
        self.assertEqual(result["counts"]["total_amount_fail"], 0)  # Not the Tencent function.
        recent = (ROOT / "tests/fixtures/host_textfile_datasource/eod-journal-20260907.redacted.jsonl").read_text(encoding="utf-8")
        expected = m.analyze_eod_journal(recent)
        combined = m.analyze_eod_journal(body([item]) + "\n" + recent)
        self.assertEqual(combined, expected)  # Older binary row cannot poison/dilute latest complete run.


class CountsAndMetricsTests(unittest.TestCase):
    def test_symmetric_kline_failure_ratio_uses_only_recognized_pairs(self):
        result = analyze([start(), kline(), kline(offset=2_000_000, ok=True), finish()])
        text = m.render_eod_metrics(result)
        self.assertIn("radar_datasource_primary_kline_failure_ratio 0.5", text)
        self.assertIn('kline_samples="2"', text)

    def test_failure_only_ratio_is_one_with_explicit_sample_count(self):
        text = m.render_eod_metrics(analyze([start(), kline(), finish()]))
        self.assertIn("radar_datasource_primary_kline_failure_ratio 1.0", text)
        self.assertIn("radar_datasource_primary_kline_sample_log_entries 1", text)

    def test_success_only_ratio_is_zero_with_explicit_sample_count(self):
        text = m.render_eod_metrics(analyze([start(), kline(ok=True), finish()]))
        self.assertIn("radar_datasource_primary_kline_failure_ratio 0.0", text)

    def test_total_amount_failure_is_not_kline_denominator(self):
        amount = row(offset=2_000_000, message="[_tencent_total_amount] 腾讯沪深成交额采集失败: 501 error")
        result = analyze([start(), kline(ok=True), amount, finish()])
        self.assertEqual(result["counts"]["kline_samples"], 1)
        self.assertEqual(result["counts"]["total_amount_fail"], 1)
        self.assertIn("radar_datasource_primary_kline_failure_ratio 0.0", m.render_eod_metrics(result))

    def test_only_total_amount_has_event_samples_but_no_kline_ratio(self):
        amount = row(offset=2_000_000, message="[_tencent_total_amount] 腾讯沪深成交额采集失败: 501")
        result = analyze([start(), amount, finish()])
        self.assertEqual(result["has_samples"], 1)
        self.assertEqual(result["counts"]["kline_samples"], 0)
        self.assertIn("radar_datasource_primary_kline_failure_ratio NaN", m.render_eod_metrics(result))

    def test_complete_but_no_recognized_samples_is_not_recovery(self):
        result = analyze([start(), row(offset=1_000_000, message="core completed"), finish()])
        self.assertEqual(result["complete"], 1)
        self.assertEqual(result["has_samples"], 0)
        self.assertIn("radar_datasource_primary_failure_ratio NaN", m.render_eod_metrics(result))

    def test_unknown_success_words_do_not_fabricate_primary_samples(self):
        item = row(offset=1_000_000, message="[fetch_tencent_kline] retried previous error; success maybe later")
        result = analyze([start(), item, finish()])
        self.assertEqual(result["counts"]["kline_samples"], 0)

    def test_kline_fallback_and_macro_fallback_are_separate(self):
        sina = row(offset=2_000_000, message="[fetch_akshare_kline] akshare_fund_etf_hist_sina 获取历史数据成功: 510300 rows=100 来源:sina")
        macro = row(offset=3_000_000, message="[collect] 宏观切片 2026-09-07 (来源:sina+akshare)")
        result = analyze([start(), kline(), sina, macro, finish()])
        self.assertEqual(result["counts"]["fallback_kline"], 1)
        self.assertEqual(result["counts"]["fallback_macro"], 1)
        self.assertEqual(result["counts"]["fallback"], 2)

    def test_a_single_line_cannot_count_twice_as_fallback(self):
        mixed = row(offset=2_000_000, message="[fetch_akshare_kline] akshare_fund_etf_hist_sina 获取历史数据成功: 510300 宏观切片 来源:sina+akshare")
        result = analyze([start(), kline(), mixed, finish()])
        self.assertEqual(result["counts"]["fallback"], 1)

    def test_fallback_failure_is_not_fallback_success(self):
        sina = row(offset=2_000_000, message="[fetch_akshare_kline] akshare_fund_etf_hist_sina 获取历史数据失败: 510300 error")
        result = analyze([start(), kline(), sina, finish()])
        self.assertEqual(result["counts"]["fallback"], 0)
        self.assertEqual(result["counts"]["sina_fail"], 1)

    def test_event_timestamp_does_not_change_when_export_is_repeated(self):
        result = analyze([start(), kline(), finish()])
        first, second = (m.render_eod_metrics(result, sampled_at=timestamp) for timestamp in (1, 2))
        prefix = "radar_datasource_eod_event_unixtime "
        self.assertEqual(next(line for line in first.splitlines() if line.startswith(prefix)),
                         next(line for line in second.splitlines() if line.startswith(prefix)))
        self.assertIn("radar_datasource_metrics_unixtime 1", first)
        self.assertIn("radar_datasource_metrics_unixtime 2", second)

    def test_deprecated_compatibility_metrics_are_explicit_and_no_sample_nan(self):
        text = m.render_eod_metrics(m.analyze_eod_journal(""))
        self.assertIn("DEPRECATED", text)
        self.assertIn("radar_datasource_primary_failure_ratio NaN", text)
        self.assertIn('radar_datasource_requests_total{source="tencent",result="ok"} NaN', text)

    def test_real_redacted_eod_fixture_has_exact_separate_counts(self):
        fixture = (ROOT / "tests/fixtures/host_textfile_datasource/eod-journal-20260907.redacted.jsonl").read_text(encoding="utf-8")
        result = m.analyze_eod_journal(fixture)
        self.assertEqual((result["parsed"], result["complete"], result["has_samples"]), (1, 1, 1))
        self.assertEqual(result["event"]["invocation_id"], "484daa7578b0466f91178fdc61a96d28")
        self.assertEqual(result["counts"]["kline_fail"], 10)
        self.assertEqual(result["counts"]["kline_ok"], 0)
        self.assertEqual(result["counts"]["total_amount_fail"], 1)
        self.assertEqual(result["counts"]["fallback_kline"], 10)
        self.assertEqual(result["counts"]["fallback_macro"], 1)
        self.assertEqual(result["counts"]["fallback"], 11)


class IsolationAndLimitsTests(unittest.TestCase):
    def bounded_reader(self, child_code, **overrides):
        # Replace only journalctl with an offline child that writes stdout.
        # This exercises real pipe reads, timeout/kill/reap, without a journal.
        original_popen = subprocess.Popen
        children = []
        def offline_popen(command, **kwargs):
            self.assertEqual(command[0], "journalctl")
            self.assertIn("json", command)
            self.assertIn(str(m.JOURNAL_MAX_LINES), command)
            self.assertTrue(any(arg.startswith("--output-fields=") for arg in command))
            child = original_popen([sys.executable, "-B", "-c", child_code], **kwargs)
            children.append(child)
            return child
        with mock.patch.object(m.subprocess, "Popen", side_effect=offline_popen), mock.patch.multiple(m, **overrides):
            try:
                return m.read_eod_journal()
            finally:
                self.assertTrue(children)
                self.assertTrue(all(child.poll() is not None for child in children), "bounded reader must reap child")

    def test_reader_has_bounded_command_and_returns_full_stdout(self):
        self.assertEqual(self.bounded_reader("print('offline fixture')", JOURNAL_TIMEOUT=2), "offline fixture\n")

    def test_reader_byte_overflow_reaps_child_and_fails(self):
        with self.assertRaises(ValueError):
            self.bounded_reader("print('x' * 1000)", JOURNAL_MAX_BYTES=10, JOURNAL_TIMEOUT=2)

    def test_reader_timeout_kills_and_reaps_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.bounded_reader("import time; time.sleep(10)", JOURNAL_TIMEOUT=0.1)

    def test_line_limit_fails_instead_of_returning_partial_success(self):
        with mock.patch.object(m, "JOURNAL_MAX_LINES", 2):
            self.assertEqual(analyze([start(), kline(), finish()])["parsed"], 0)

    def test_byte_limit_fails_instead_of_truncating(self):
        with mock.patch.object(m, "JOURNAL_MAX_BYTES", 10):
            self.assertEqual(analyze([start(), finish()])["parsed"], 0)

    def test_timeout_and_command_failure_are_unknown_not_zero_ratio(self):
        for exception in (OSError("not available"), subprocess.TimeoutExpired("fixture", 12),
                          subprocess.CalledProcessError(1, "fixture")):
            with self.subTest(exception=exception), mock.patch.object(m, "read_eod_journal", side_effect=exception):
                text = m.parse_eod_journal()
                self.assertIn("radar_datasource_eod_journal_parsed 0", text)
                self.assertIn("radar_datasource_eod_complete 0", text)
                self.assertIn("radar_datasource_primary_kline_failure_ratio NaN", text)

    def test_disk_atomic_and_main_functions_remain_exact_ast(self):
        old = ast.parse((ROOT / "tests/fixtures/host_textfile_datasource/exporter_pre20260908.py").read_text(encoding="utf-8"))
        new = ast.parse(SOURCE.read_text(encoding="utf-8"))
        def functions(tree):
            return {node.name: ast.dump(node, include_attributes=False) for node in tree.body if isinstance(node, ast.FunctionDef)}
        a, b = functions(old), functions(new)
        for name in ("atomic_write", "disk_metrics", "main"):
            with self.subTest(name=name):
                self.assertEqual(a[name], b[name])

    def test_index_valid_input_keeps_original_metric_output(self):
        spec = importlib.util.spec_from_file_location("datasource_original_index_reference",
                                                     ROOT / "tests/fixtures/host_textfile_datasource/exporter_pre20260908.py")
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        for source in ("tencent", "sina", "sina+akshare", "unknown", "", None):
            with self.subTest(source=source):
                path = mock.Mock()
                path.read_text.return_value = json.dumps({"source": source})
                with mock.patch.object(m, "INDEX_CACHE", path), mock.patch.object(original, "INDEX_CACHE", path):
                    self.assertEqual(m.index_cache_metrics(), original.index_cache_metrics())

    def test_index_nonobject_or_deep_json_is_unreadable_not_exporter_crash(self):
        cases = [json.dumps(value) for value in ([], None, "not an object", 1)]
        cases.append("[" * 2000 + "0" + "]" * 2000)
        for payload in cases:
            with self.subTest(payload=payload[:40]):
                path = mock.Mock()
                path.read_text.return_value = payload
                with mock.patch.object(m, "INDEX_CACHE", path):
                    text = m.index_cache_metrics()
                self.assertIn('radar_datasource_index_cache_source{source="unreadable"} 1', text)
                self.assertIn("radar_datasource_index_cache_is_primary 0", text)

    def test_watchdog_metrics_delegates_to_strict_json_contract(self):
        # Validation/fault matrices live in test_eod_contracts.py. The host exporter
        # must use that shared path without changing disk/main or business imports.
        with mock.patch.object(m, "render_watchdog_metrics", return_value="sentinel\n") as render:
            self.assertEqual(m.watchdog_metrics(), "sentinel\n")
        render.assert_called_once_with(m.WATCHDOG_PATH, m.CALENDAR_PATH)

    def test_parser_has_no_database_or_business_or_notification_imports(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".")[0])
        self.assertFalse(set(names) & {"sqlite3", "psycopg", "psycopg2", "radar", "telegram", "requests"})


class ExecConditionSkipTests(unittest.TestCase):
    """A structural skip is audit, never a substitute for an executed run."""

    @staticmethod
    def skipped_retry(inv=I2, offset=40_000_000):
        skipped = row(inv, offset + 1_000_000, manager=True,
                      MESSAGE_ID="0e4284a0caca4bfc81c0bb6786972673",
                      UNIT_RESULT="exec-condition", message="本地化的条件跳过")
        rows = [start(inv, offset), skipped, finish(inv, offset + 2_000_000)]
        for item in rows:
            item["UNIT"] = m.EOD_RETRY_UNIT
        return rows

    def test_success_then_skipped_retry_preserves_executed_evidence_and_audit(self):
        result = analyze([start(), kline(ok=True), finish()] + self.skipped_retry())
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual((result["complete"], result["has_samples"], result["latest_incomplete"],
                          result["in_progress"]), (1, 1, 0, 0))
        self.assertEqual(result["counts"]["kline_ok"], 1)
        self.assertEqual(result["skipped_invocations"], [{
            "invocation_id": I2, "unit": m.EOD_RETRY_UNIT,
            "start_unixtime": T / 1_000_000 + 40,
            "unixtime": T / 1_000_000 + 42,
            "result": "skipped", "reason": "exec-condition",
        }])

    def test_failed_execution_is_not_recovered_by_skipped_retry(self):
        result = analyze([start(), kline(), failed()] + self.skipped_retry())
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["event"]["result"], "failed")
        self.assertEqual(result["counts"]["kline_fail"], 1)
        self.assertIn("radar_datasource_eod_terminal_success 0", m.render_eod_metrics(result))

    def test_failed_eod_then_successful_retry_remains_selected(self):
        later = [start(I2, 40_000_000), kline(I2, 41_000_000, ok=True), finish(I2, 42_000_000)]
        for item in later:
            item["UNIT" if item["_PID"] == "1" else "_SYSTEMD_UNIT"] = m.EOD_RETRY_UNIT
        result = analyze([start(), kline(), failed()] + later)
        self.assertEqual(result["event"]["invocation_id"], I2)
        self.assertEqual(result["event"]["result"], "success")
        self.assertEqual(result["skipped_invocations"], [])

    def test_only_skipped_is_unknown_not_success(self):
        result = analyze(self.skipped_retry())
        self.assertEqual((result["parsed"], result["complete"], result["has_samples"],
                          result["latest_incomplete"], result["in_progress"]), (1, 0, 0, 0, 0))
        self.assertIsNone(result["event"])
        self.assertIsNone(result["counts"])
        self.assertEqual(len(result["skipped_invocations"]), 1)
        self.assertNotIn("radar_datasource_eod_info{", m.render_eod_metrics(result))

    def test_genuine_zero_sample_execution_still_selected(self):
        result = analyze([start(), kline(ok=True), finish(),
                          start(I2, 40_000_000), finish(I2, 42_000_000)])
        self.assertEqual(result["event"]["invocation_id"], I2)
        self.assertEqual((result["complete"], result["has_samples"]), (1, 0))
        self.assertEqual(result["skipped_invocations"], [])

    def test_genuine_zero_sample_before_skip_stays_unknown(self):
        result = analyze([start(), finish()] + self.skipped_retry())
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual((result["complete"], result["has_samples"]), (1, 0))

    def test_skip_with_condition_log_is_allowed_without_business_samples(self):
        rows = self.skipped_retry()
        condition = row(I2, 40_500_000, message="same-day audit successful; retry not needed",
                        _SYSTEMD_UNIT=m.EOD_RETRY_UNIT)
        result = analyze([start(), kline(ok=True), finish()] + rows + [condition])
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(len(result["skipped_invocations"]), 1)

    def test_macro_only_original_evidence_is_preserved_without_claiming_kline(self):
        rows = [start(),
                row(offset=1_000_000, message="[_tencent_total_amount] 采集失败: HTTP Error 501"),
                row(offset=2_000_000, message="宏观切片 来源:sina+akshare"),
                finish()] + self.skipped_retry()
        result = analyze(rows)
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["has_samples"], 1)
        self.assertEqual(result["counts"]["total_amount_fail"], 1)
        self.assertEqual(result["counts"]["fallback_macro"], 1)
        self.assertEqual(result["counts"]["kline_samples"], 0)
        self.assertIn("radar_datasource_primary_kline_failure_ratio NaN", m.render_eod_metrics(result))

    def test_incomplete_skip_lifecycle_never_disappears(self):
        for remove in (0, 2):
            with self.subTest(missing=("start" if remove == 0 else "done")):
                retry = self.skipped_retry()
                del retry[remove]
                result = analyze([start(), kline(ok=True), finish()] + retry)
                self.assertEqual(result["event"]["invocation_id"], I1)
                self.assertEqual(result["latest_incomplete"], 1)
                self.assertEqual(result["in_progress"], 0)
                self.assertEqual(result["skipped_invocations"], [])

    def test_skip_outside_lifecycle_is_unknown(self):
        retry = self.skipped_retry()
        retry[1]["__REALTIME_TIMESTAMP"] = str(T + 43_000_000)
        result = analyze([start(), kline(ok=True), finish()] + retry)
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual(result["latest_incomplete"], 1)
        self.assertEqual(result["skipped_invocations"], [])

    def test_skip_with_failed_terminal_is_conflict(self):
        retry = self.skipped_retry()
        retry[2] = failed(I2, 42_000_000)
        retry[2]["UNIT"] = m.EOD_RETRY_UNIT
        result = analyze(retry)
        self.assertEqual(result["parsed"], 0)
        self.assertEqual(result["error"], "journal_skip_terminal_conflict")

    def test_skip_with_datasource_evidence_is_conflict_not_silently_discarded(self):
        retry = self.skipped_retry()
        sample = kline(I2, 40_500_000, ok=True)
        sample["_SYSTEMD_UNIT"] = m.EOD_RETRY_UNIT
        result = analyze([start(), kline(ok=True), finish()] + retry + [sample])
        self.assertEqual(result["parsed"], 0)
        self.assertEqual(result["error"], "journal_skip_datasource_conflict")

    def test_skip_cannot_use_manager_verdict_from_different_unit(self):
        retry = self.skipped_retry()
        retry[1]["UNIT"] = m.EOD_UNIT
        result = analyze(retry)
        self.assertEqual(result["parsed"], 0)
        self.assertEqual(result["error"], "journal_invocation_unit_conflict")

    def test_skip_marker_requires_exact_structured_manager_fields(self):
        for change in ({"MESSAGE_ID": "other"}, {"UNIT_RESULT": "success"},
                       {"_PID": "234", "_SYSTEMD_UNIT": m.EOD_RETRY_UNIT,
                        "_SYSTEMD_INVOCATION_ID": I2}):
            with self.subTest(change=change):
                retry = self.skipped_retry()
                retry[1].update(change)
                retry[1]["MESSAGE"] = "Skipped due to 'exec-condition'."
                result = analyze([start(), kline(ok=True), finish()] + retry)
                self.assertEqual(result["event"]["invocation_id"], I2)
                self.assertEqual(result["has_samples"], 0)
                self.assertEqual(result["skipped_invocations"], [])

    def test_duplicate_and_out_of_order_skip_rows_preserve_same_audit(self):
        rows = [start(), kline(ok=True), finish()] + self.skipped_retry()
        expected = analyze(rows)
        self.assertEqual(analyze(list(reversed(rows)) + [copy.deepcopy(rows[4])]), expected)

    def test_verified_skip_does_not_hide_an_unfinished_execution(self):
        third = "c" * 32
        result = analyze([start(), kline(ok=True), finish(), start(third, 35_000_000)]
                         + self.skipped_retry())
        self.assertEqual(result["event"]["invocation_id"], I1)
        self.assertEqual((result["latest_incomplete"], result["in_progress"]), (1, 1))

    def test_later_real_failure_stays_selected_after_an_earlier_skip(self):
        third = "c" * 32
        result = analyze([start(), kline(ok=True), finish()] + self.skipped_retry()
                         + [start(third, 44_000_000), kline(third, 45_000_000),
                            failed(third, 49_000_000)])
        self.assertEqual(result["event"]["invocation_id"], third)
        self.assertEqual(result["event"]["result"], "failed")

    def test_skipped_main_is_not_mislabeled_executed(self):
        rows = self.skipped_retry()
        for item in rows:
            item["UNIT"] = m.EOD_UNIT
        result = analyze(rows)
        self.assertEqual(result["complete"], 0)
        self.assertEqual(result["skipped_invocations"][0]["unit"], m.EOD_UNIT)

    def test_byte_truncated_skip_is_parse_unknown(self):
        payload = body([start(), kline(ok=True), finish()] + self.skipped_retry())
        result = m.analyze_eod_journal(payload[:-2], now=T / 1_000_000 + 60)
        self.assertEqual(result["parsed"], 0)
        self.assertIsNone(result["event"])

    def test_line_cap_not_cleared_by_valid_skip(self):
        rows = [start(), kline(ok=True), finish()] + self.skipped_retry()
        with mock.patch.object(m, "JOURNAL_MAX_LINES", len(rows)):
            result = analyze(rows)
        self.assertEqual(result["window_capped"], 1)
        self.assertEqual(result["event"]["invocation_id"], I1)


def observation(inv=I1, offset=1_000_000, source="tencent_kline", route="adapter", state="ready"):
    payload = {"error_type": "", "latest_date": "2026-09-15", "required_date": "2026-09-15",
               "route": route, "rows": 80, "source": source, "state": state}
    return row(inv, offset, "kline_observation " + json.dumps(payload, sort_keys=True))


class KlineObservationMetricTests(unittest.TestCase):
    def test_observation_logs_are_preferred_over_legacy_tencent_lines(self):
        result = analyze([start(), kline(), observation(offset=1_100_000), finish()])
        self.assertEqual(result["counts"]["kline_fail"], 0)
        self.assertEqual(result["counts"]["kline_ok"], 1)
        self.assertEqual(result["counts"]["observation_count"], 1)
        text = m.render_eod_metrics(result, sampled_at=123)
        self.assertIn('radar_datasource_kline_observation_entries{source="tencent_kline",route="adapter",state="ready"} 1', text)

    def test_stale_tencent_observation_is_not_a_kline_failure(self):
        result = analyze([start(), observation(state="stale"), finish()])
        self.assertEqual(result["counts"]["kline_fail"], 0)
        self.assertEqual(result["counts"]["kline_ok"], 0)
        self.assertEqual(result["has_samples"], 1)
        self.assertIn("radar_datasource_primary_kline_failure_ratio NaN", m.render_eod_metrics(result))

    def test_parquet_cache_observation_does_not_masquerade_as_tencent(self):
        result = analyze([start(), observation(source="parquet", route="cache"), finish()])
        self.assertEqual(result["counts"]["kline_ok"], 0)
        self.assertEqual(result["counts"]["kline_fail"], 0)
        self.assertEqual(result["has_samples"], 1)


if __name__ == "__main__":
    unittest.main()
