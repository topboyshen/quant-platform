"""Offline fixture-only CSI tests; no upstream query or production file access."""

from pathlib import Path
import re
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

from indices import INDEX_MAP, IndicesError, interval_symbol, update_indices


REF = "80q7jtqjrga15h61uufm60h6uqirk6iq"
CALENDAR = ["2019-01-02", "2020-01-02", "2026-09-01", "2026-09-04", "2026-09-07", "2026-09-08"]
BASE, TARGET = "2026-09-04", "2026-09-08"
ORIGINAL = ("SH600999\t2019-01-02\t2020-01-02\n"
            "SH600000\t2026-09-01\t2026-09-04\n"
            "SZ000001\t2026-09-01\t2026-09-04\n").encode()


class FakeClient:
    def __init__(self):
        self.ref = REF
        self.groups = {code: {"2026-09-01": ["600000.SH", "000001.SZ"],
                              "2026-09-07": ["600000.SH", "920000.BJ"],
                              "2026-09-08": ["000001.SZ", "920000.BJ"]} for code in INDEX_MAP.values()}
        self.calls = []

    def latest_ref(self):
        return self.ref

    def table(self, name):
        return f"`{name}` AS OF '{self.ref}'"

    def query(self, sql):
        self.calls.append(sql)
        assert "AS OF '" + REF + "'" in sql
        code = re.search(r"index_code='([^']+)'", sql).group(1)
        return [{"trade_date": day, "member_count": str(len(members))} for day, members in sorted(self.groups[code].items())]

    def paginated_rows(self, table, columns, where, order_fields):
        self.calls.append(where)
        assert table == "ts_index_weight"
        assert order_fields == ("index_code", "trade_date", "stock_code")
        code = re.search(r"index_code='([^']+)'", where).group(1)
        dates = re.findall(r"'(2026-[0-9-]+)'", where)
        return [{"index_code": code, "stock_code": stock, "trade_date": day}
                for day in dates for stock in sorted(self.groups[code][day])]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qlib-index-test-")
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "old"
        self.stage = Path(self.temp.name) / "stage"
        for root in (self.old, self.stage):
            (root / "instruments").mkdir(parents=True)
            for name in INDEX_MAP:
                (root / "instruments" / (name + ".txt")).write_bytes(ORIGINAL)
        self.client = FakeClient()
        guard = patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        counts = patch("indices.NOMINAL_COUNTS", {name: 2 for name in INDEX_MAP if name != "csiall"})
        counts.start()
        self.addCleanup(counts.stop)

    def run_update(self, **kwargs):
        return update_indices(self.old, self.stage, kwargs.get("client", self.client),
                              kwargs.get("base", BASE), kwargs.get("target", TARGET), kwargs.get("calendar", CALENDAR))

    def unchanged(self, root):
        self.assertTrue(all((root / "instruments" / (name + ".txt")).read_bytes() == ORIGINAL for name in INDEX_MAP))

    def test_add_remove_readd_preserves_history_and_old_tree(self):
        summary = self.run_update()
        self.unchanged(self.old)
        text = (self.stage / "instruments/csi300.txt").read_text()
        self.assertIn("SH600999\t2019-01-02\t2020-01-02\n", text)
        self.assertIn("SH600000\t2026-09-01\t2026-09-07\n", text)
        self.assertIn("SZ000001\t2026-09-01\t2026-09-04\n", text)
        self.assertIn("BJ920000\t2026-09-07\t2026-09-08\n", text)
        self.assertIn("SZ000001\t2026-09-08\t2026-09-08\n", text)
        self.assertTrue(summary["historical_membership_unchanged"])
        self.assertEqual(summary["indices"]["csi300"]["target_snapshot"], TARGET)

    def test_no_new_snapshot_carries_observed_set_and_reports_old_date(self):
        for groups in self.client.groups.values():
            groups.pop("2026-09-07")
            groups.pop("2026-09-08")
        report = self.run_update()
        self.assertEqual(report["indices"]["csi300"]["target_snapshot"], "2026-09-01")
        self.assertIn("SH600000\t2026-09-01\t2026-09-08", (self.stage / "instruments/csi300.txt").read_text())

    def test_closed_legacy_ids_retained_without_mapping_or_membership(self):
        historical = (b"SHT00018\t2005-04-08\t2005-12-30\n"
                      b"SHT00018\t2006-01-04\t2007-01-03\n")
        for root in (self.old, self.stage):
            (root / "instruments/csi300.txt").write_bytes(historical + ORIGINAL)
        report = self.run_update()
        self.assertEqual((self.old / "instruments/csi300.txt").read_bytes(), historical + ORIGINAL)
        self.assertTrue((self.stage / "instruments/csi300.txt").read_bytes().startswith(historical))
        self.assertEqual(report["indices"]["csi300"]["base_members"], 2)
        self.assertEqual(report["indices"]["csi300"]["target_members"], 2)
        self.assertTrue(report["historical_membership_unchanged"])

    def test_legacy_id_cannot_be_active_at_base_or_future(self):
        for end in (BASE, TARGET):
            raw = f"SHT00018\t2005-04-08\t{end}\n".encode() + ORIGINAL
            for root in (self.old, self.stage):
                (root / "instruments/csi300.txt").write_bytes(raw)
            with self.subTest(end=end), self.assertRaisesRegex(IndicesError, "legacy_symbol_interval_not_closed"):
                self.run_update()
            self.assertEqual((self.stage / "instruments/csi300.txt").read_bytes(), raw)
        self.assertEqual(self.client.calls, [])

    def test_source_cannot_introduce_alphabetic_member_even_in_baseline(self):
        for day in ("2026-09-01", TARGET):
            for spelling in ("T00018.SH", "SHT00018"):
                self.client = FakeClient()
                self.client.groups[INDEX_MAP["csi300"]][day][0] = spelling
                with self.subTest(day=day, spelling=spelling), self.assertRaisesRegex(IndicesError, "invalid_symbol"):
                    self.run_update()
                self.unchanged(self.old)
                self.unchanged(self.stage)

    def test_closed_legacy_overlap_and_reversed_dates_still_rejected(self):
        for historical in (b"SHT00018\t2005-04-08\t2006-01-04\nSHT00018\t2006-01-04\t2007-01-03\n",
                           b"SHT00018\t2007-01-03\t2005-04-08\n"):
            raw = historical + ORIGINAL
            for root in (self.old, self.stage):
                (root / "instruments/csi300.txt").write_bytes(raw)
            with self.subTest(historical=historical), self.assertRaises(IndicesError):
                self.run_update()
            self.assertEqual((self.stage / "instruments/csi300.txt").read_bytes(), raw)

    def test_lowercase_closed_legacy_spelling_is_preserved(self):
        original = (b"SHT00018\t2005-04-08\t2007-01-03\n" + ORIGINAL).lower()
        for root in (self.old, self.stage):
            for name in INDEX_MAP:
                (root / "instruments" / (name + ".txt")).write_bytes(original)
        self.run_update()
        self.assertTrue((self.stage / "instruments/csi300.txt").read_bytes().startswith(
            b"sht00018\t2005-04-08\t2007-01-03\n"))

    def test_base_set_mismatch_stops_all_stage_writes(self):
        self.client.groups[INDEX_MAP["csiall"]]["2026-09-01"] = ["600001.SH", "000001.SZ"]
        with self.assertRaisesRegex(IndicesError, "base_membership_mismatch"):
            self.run_update()
        self.unchanged(self.old)
        self.unchanged(self.stage)

    def test_timeout_stops_before_stage_writes(self):
        with patch.object(self.client, "paginated_rows", side_effect=TimeoutError("mock timeout")):
            with self.assertRaises(TimeoutError):
                self.run_update()
        self.unchanged(self.old)
        self.unchanged(self.stage)

    def test_missing_member_duplicate_and_wrong_date_rejected(self):
        original = self.client.paginated_rows
        for mode in ("missing", "duplicate", "wrong_date", "wrong_index"):
            def broken(*args, mode=mode):
                rows = original(*args)
                if mode == "missing":
                    return rows[1:]
                if mode == "duplicate":
                    return rows + [rows[0]]
                rows[0] = dict(rows[0], **({"trade_date": "2026-09-09"} if mode == "wrong_date" else {"index_code": "000000.SH"}))
                return rows
            with self.subTest(mode=mode), patch.object(self.client, "paginated_rows", side_effect=broken), self.assertRaises(IndicesError):
                self.run_update()
            self.unchanged(self.stage)

    def test_fixed_index_cardinality_cannot_silently_drop_member(self):
        self.client.groups[INDEX_MAP["csi300"]]["2026-09-08"] = ["000001.SZ"]
        with self.assertRaisesRegex(IndicesError, "cardinality_changed"):
            self.run_update()
        self.unchanged(self.stage)

    def test_old_terminal_must_have_nominal_cardinality(self):
        with patch("indices.NOMINAL_COUNTS", {"csi300": 300, "csi500": 500, "csi800": 800, "csi1000": 1000}):
            with self.assertRaisesRegex(IndicesError, "csi300:expected=300:actual=2"):
                self.run_update()
        self.unchanged(self.stage)

    def test_csiall_can_add_member_from_complete_snapshot(self):
        self.client.groups[INDEX_MAP["csiall"]]["2026-09-08"].append("600001.SH")
        report = self.run_update()
        self.assertEqual(report["indices"]["csiall"]["target_members"], 3)

    def test_new_snapshot_must_be_trading_day(self):
        groups = self.client.groups[INDEX_MAP["csi300"]]
        groups["2026-09-06"] = groups.pop("2026-09-07")
        with self.assertRaisesRegex(IndicesError, "not_in_trading_calendar"):
            self.run_update()

    def test_source_baseline_required_and_range_checked(self):
        for day in ("2026-09-07", "2026-08-31"):
            self.client = FakeClient()
            groups = self.client.groups[INDEX_MAP["csi300"]]
            groups[day] = groups.pop("2026-09-01")
            with self.subTest(day=day), self.assertRaises(IndicesError):
                self.run_update()

    def test_same_root_and_symlink_rejected(self):
        with self.assertRaisesRegex(IndicesError, "overlaps"):
            update_indices(self.old, self.old, self.client, BASE, TARGET, CALENDAR)
        path = self.stage / "instruments/csi300.txt"
        path.unlink()
        path.symlink_to(self.old / "instruments/csi300.txt")
        with self.assertRaises(IndicesError):
            self.run_update()
        self.unchanged(self.old)

    def test_stage_ancestor_swap_cannot_redirect_writes_to_old(self):
        original_query = self.client.query
        swapped = False
        def swap_during_query(sql):
            nonlocal swapped
            if not swapped:
                self.stage.rename(self.stage.with_name("stage-retained"))
                self.stage.symlink_to(self.old, target_is_directory=True)
                swapped = True
            return original_query(sql)
        with patch.object(self.client, "query", side_effect=swap_during_query):
            with self.assertRaisesRegex(IndicesError, "stage_directory_changed"):
                self.run_update()
        self.unchanged(self.old)

    def test_old_future_or_overlapping_intervals_rejected(self):
        for raw in (ORIGINAL.replace(b"2026-09-04", b"2026-09-09"), ORIGINAL + b"SH600000\t2026-09-01\t2026-09-04\n"):
            for root in (self.old, self.stage):
                (root / "instruments/csi300.txt").write_bytes(raw)
            with self.assertRaises(IndicesError):
                self.run_update()

    def test_stage_must_match_old_before_any_updates(self):
        (self.stage / "instruments/csiall.txt").write_bytes(b"changed\n")
        with self.assertRaisesRegex(IndicesError, "not_original_clone"):
            self.run_update()
        self.assertEqual((self.stage / "instruments/csi300.txt").read_bytes(), ORIGINAL)

    def test_lowercase_files_keep_their_case(self):
        lower = ORIGINAL.lower()
        for root in (self.old, self.stage):
            for name in INDEX_MAP:
                (root / "instruments" / (name + ".txt")).write_bytes(lower)
        self.run_update()
        self.assertIn("bj920000", (self.stage / "instruments/csi300.txt").read_text())

    def test_hardlinked_stage_does_not_modify_old_inode_contents(self):
        import os
        for name in INDEX_MAP:
            target = self.stage / "instruments" / (name + ".txt")
            target.unlink()
            os.link(self.old / "instruments" / (name + ".txt"), target)
        self.run_update()
        self.unchanged(self.old)

    def test_calendar_invalid_and_pin_change_fail(self):
        with self.assertRaises(IndicesError):
            self.run_update(calendar=CALENDAR[::-1])
        with patch.object(self.client, "latest_ref", side_effect=[REF, "1" * 32]):
            with self.assertRaisesRegex(IndicesError, "source_commit_changed"):
                self.run_update()
        self.unchanged(self.stage)


class IntervalSymbolTests(unittest.TestCase):
    def test_only_bounded_legacy_shape_or_existing_numeric_shape(self):
        self.assertEqual(interval_symbol("SHT00018", "2007-01-03", BASE), "SHT00018")
        self.assertEqual(interval_symbol("sza00001", "2007-01-03", BASE), "SZA00001")
        self.assertEqual(interval_symbol("SH600000", BASE, BASE), "SH600000")
        for value in ("SHAA0018", "SHT0001", "SHT000180", "../SHT00018", "T00018.SH", "SHT00018\x00", None):
            with self.subTest(value=value), self.assertRaises(IndicesError):
                interval_symbol(value, "2007-01-03", BASE)

    def test_end_and_boundary_must_be_real_canonical_dates(self):
        for end, boundary in (("2007-02-30", BASE), ("2007-01-03", "2026-9-4"), (BASE, BASE)):
            with self.subTest(end=end, boundary=boundary), self.assertRaises(IndicesError):
                interval_symbol("SHT00018", end, boundary)


if __name__ == "__main__":
    unittest.main()
