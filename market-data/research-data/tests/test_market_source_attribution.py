"""Offline source attribution and same-run deferred status for market staging."""

import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import traceback
import types
import unittest
from unittest import mock

import daily_snapshot


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(enumerate(self.rows))


def sector_rows():
    return Rows([{
        "板块": f"行业{index}", "涨跌幅": 1, "净流入": 2,
        "上涨家数": 20, "下跌家数": 10, "领涨股": "样本",
    } for index in range(60)])


class MarketSourceAttributionTests(unittest.TestCase):
    def test_parser_failures_are_attributed_to_the_call_that_returned_rows(self):
        class BrokenRows:
            def iterrows(self):
                raise ValueError("parsed table is malformed")

        for failing_endpoint in (
            "stock_board_industry_summary_ths", "stock_market_activity_legu"
        ):
            source = types.SimpleNamespace(
                stock_board_industry_summary_ths=mock.Mock(return_value=sector_rows()),
                stock_market_activity_legu=mock.Mock(return_value=Rows([])),
            )
            getattr(source, failing_endpoint).return_value = BrokenRows()
            with self.subTest(endpoint=failing_endpoint), mock.patch.dict(
                sys.modules, {"akshare": source}
            ), mock.patch.object(daily_snapshot.time, "sleep"), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(daily_snapshot.SnapshotDeferredError) as caught:
                    daily_snapshot.capture_current_akshare_market(directory)
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(
                    caught.exception.source_failure["attempts"],
                    [{"attempt": attempt, "endpoint": failing_endpoint,
                      "errorType": "ValueError"} for attempt in (1, 2)],
                )
                self.assertEqual(
                    getattr(source, failing_endpoint).call_count, 2
                )

    def test_industry_failure_records_endpoint_and_attempts_without_stage(self):
        secret = "TOKEN-DO-NOT-LOG"
        source = types.SimpleNamespace(
            stock_board_industry_summary_ths=mock.Mock(
                side_effect=ValueError(f"No tables found {secret}")),
            stock_market_activity_legu=mock.Mock(),
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            sys.modules, {"akshare": source}
        ), mock.patch.object(daily_snapshot.time, "sleep"):
            with self.assertRaises(daily_snapshot.SnapshotDeferredError) as caught:
                daily_snapshot.capture_current_akshare_market(directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

        error = caught.exception
        self.assertEqual(source.stock_board_industry_summary_ths.call_count, 2)
        source.stock_market_activity_legu.assert_not_called()
        self.assertEqual(error.source_failure, {
            "provider": "akshare",
            "attemptCount": 2,
            "attempts": [
                {"attempt": 1, "endpoint": "stock_board_industry_summary_ths",
                 "errorType": "ValueError"},
                {"attempt": 2, "endpoint": "stock_board_industry_summary_ths",
                 "errorType": "ValueError"},
            ],
        })
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, "".join(traceback.format_exception(error)))

    def test_activity_failure_records_endpoint_and_attempts_without_stage(self):
        source = types.SimpleNamespace(
            stock_board_industry_summary_ths=mock.Mock(side_effect=sector_rows),
            stock_market_activity_legu=mock.Mock(
                side_effect=ValueError("No tables found")),
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            sys.modules, {"akshare": source}
        ), mock.patch.object(daily_snapshot.time, "sleep"):
            with self.assertRaises(daily_snapshot.SnapshotDeferredError) as caught:
                daily_snapshot.capture_current_akshare_market(directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

        self.assertEqual(source.stock_board_industry_summary_ths.call_count, 2)
        self.assertEqual(source.stock_market_activity_legu.call_count, 2)
        self.assertEqual(
            [item["endpoint"] for item in caught.exception.source_failure["attempts"]],
            ["stock_market_activity_legu", "stock_market_activity_legu"],
        )

    def test_mixed_failures_preserve_the_endpoint_of_each_attempt(self):
        source = types.SimpleNamespace(
            stock_board_industry_summary_ths=mock.Mock(
                side_effect=[ValueError("No tables found"), sector_rows()]),
            stock_market_activity_legu=mock.Mock(
                side_effect=ValueError("No tables found")),
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            sys.modules, {"akshare": source}
        ), mock.patch.object(daily_snapshot.time, "sleep"):
            with self.assertRaises(daily_snapshot.SnapshotDeferredError) as caught:
                daily_snapshot.capture_current_akshare_market(directory)
        self.assertEqual(
            [item["endpoint"] for item in caught.exception.source_failure["attempts"]],
            ["stock_board_industry_summary_ths", "stock_market_activity_legu"],
        )

    def test_known_qlib_target_and_run_id_are_persisted_with_failure(self):
        source = types.SimpleNamespace(
            stock_board_industry_summary_ths=mock.Mock(
                side_effect=ValueError("No tables found")),
            stock_market_activity_legu=mock.Mock(),
        )
        qlib = {"date": "2026-09-24", "generation": {"tag": "release-09-24"}}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            sys.modules, {"akshare": source}
        ), mock.patch.object(daily_snapshot, "DB", str(Path(directory) / "snapshot.db")), mock.patch.object(
            daily_snapshot, "STATE_DIR", str(Path(directory) / "state")
        ), mock.patch.object(daily_snapshot, "AKSHARE_STAGE_DIR", None), mock.patch.object(
            daily_snapshot, "qlib_market", return_value=qlib
        ), mock.patch.object(daily_snapshot.time, "sleep"), mock.patch.object(
            daily_snapshot.syslog, "openlog"
        ), mock.patch.object(daily_snapshot.syslog, "syslog"), mock.patch.object(
            sys, "argv", ["daily_snapshot.py"]
        ):
            started = time.monotonic()
            with self.assertRaises(daily_snapshot.SnapshotDeferredError) as caught:
                daily_snapshot.run_snapshot("run-24", started)
            daily_snapshot.emit_deferred_status(
                "run-24", started, caught.exception, stream=io.StringIO())
            record = json.loads((Path(directory) / "state" / "last-run.json").read_text())
            with sqlite3.connect(Path(directory) / "snapshot.db") as conn:
                rows = conn.execute("SELECT count(*) FROM snapshot").fetchone()[0]
            stage_dir = Path(directory) / "state" / "akshare-market"
            self.assertFalse(stage_dir.exists())

        self.assertEqual(rows, 0)
        self.assertEqual(record["runId"], "run-24")
        self.assertEqual(record["status"], "deferred")
        self.assertEqual(record["target"], "2026-09-24")
        self.assertEqual(record["qlibTag"], "release-09-24")
        self.assertEqual(record["sourceFailure"]["attemptCount"], 2)
        self.assertEqual(
            record["sourceFailure"]["attempts"][-1]["endpoint"],
            "stock_board_industry_summary_ths",
        )

    def test_existing_snapshot_does_not_hide_failed_current_stage(self):
        emitted = []
        source_failure = {
            "provider": "akshare", "attemptCount": 2,
            "attempts": [{"attempt": 2, "endpoint": "stock_market_activity_legu",
                          "errorType": "ValueError"}],
        }
        qlib = {
            "date": "2026-09-24", "generation": {"tag": "release-09-24"},
            "coverage": {"ratio": 1},
        }
        with mock.patch.object(daily_snapshot, "qlib_market", return_value=qlib), mock.patch.object(
            daily_snapshot, "existing_snapshot",
            return_value={"generatedAt": "earlier", "payloadSha256": "existing-sha"},
        ), mock.patch.object(
            daily_snapshot, "capture_current_akshare_market",
            side_effect=daily_snapshot.SnapshotDeferredError(
                "current collection failed", source_failure=source_failure),
        ), mock.patch.object(daily_snapshot, "emit_status", side_effect=emitted.append), mock.patch.object(
            sys, "argv", ["daily_snapshot.py"]
        ):
            with self.assertRaises(daily_snapshot.SnapshotDeferredError) as caught:
                daily_snapshot.run_snapshot("same-run", time.monotonic())
        self.assertEqual(caught.exception.target, "2026-09-24")
        self.assertEqual(caught.exception.source_failure, source_failure)
        self.assertEqual(emitted, [])

    def test_target_stays_unknown_until_qlib_establishes_it(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            daily_snapshot, "STATE_DIR", directory
        ), mock.patch.object(daily_snapshot.syslog, "openlog"), mock.patch.object(
            daily_snapshot.syslog, "syslog"
        ):
            daily_snapshot.emit_deferred_status(
                "pre-qlib", time.monotonic(),
                daily_snapshot.SnapshotDeferredError("source unavailable"),
                stream=io.StringIO(),
            )
            record = json.loads((Path(directory) / "last-run.json").read_text())
        self.assertNotIn("target", record)
        self.assertEqual(record["runId"], "pre-qlib")
        self.assertNotIn("qlibTag", record)


if __name__ == "__main__":
    unittest.main()
