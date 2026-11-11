"""Versioned occurrence cutoff compatibility; temporary fixtures, no sends."""
import unittest
from unittest.mock import Mock, patch

from tests import test_calendar_skip_relay as calendar_cases
from tests import test_research_relay as research_cases

relay = research_cases.relay
DAY = research_cases.DAY
CUTOFF = f"{DAY}T00:00:00.000Z"
STARTED = f"{DAY}T00:00:04.000Z"


class OccurrenceCutoffCompatibilityTests(unittest.TestCase):
    def make_case(self, kind, *, version="2.0", cutoff=CUTOFF):
        if kind == "skip":
            owner = calendar_cases.CalendarSkipTests()
            owner.setUp()
            case = owner.case
            write = owner.write
        else:
            owner = case = research_cases.RelayTests()
            case.setUp()

            def write():
                case.write_fixture()
                case.write_research_seal()

        self.addCleanup(owner.doCleanups)
        if version == "2.0":
            case.occurrence.update(schemaVersion=version, startedAt=STARTED,
                                   frozenCutoffInstant=cutoff)
            case.run["frozenCutoffInstant"] = cutoff
        write()
        return case, write

    def outcome(self, case):
        sender = Mock(side_effect=AssertionError("unexpected send"))
        with patch.object(relay, "read_post_config", side_effect=AssertionError("config read")):
            result = case.execute(dry_run=True, sender=sender)
        sender.assert_not_called()
        return result

    def test_v2_supports_separate_schedule_cutoff_for_research_and_signed_skip(self):
        for kind in ("research", "skip"):
            with self.subTest(kind=kind):
                case, _ = self.make_case(kind)
                self.assertNotEqual(case.occurrence["startedAt"], case.run["frozenCutoffInstant"])
                result = self.outcome(case)
                self.assertEqual(result["status"], "ready", result)
                self.assertEqual(result["payload_kind"], kind)

    def test_v1_without_new_field_retains_research_and_skip_semantics(self):
        for kind in ("research", "skip"):
            with self.subTest(kind=kind):
                case, _ = self.make_case(kind, version="1.0")
                self.assertNotIn("frozenCutoffInstant", case.occurrence)
                self.assertEqual(self.outcome(case)["status"], "ready")

    def test_v2_missing_invalid_or_mismatched_cutoff_cannot_fall_back(self):
        mutations = (
            ("missing", lambda o, r: o.pop("frozenCutoffInstant"), "occurrence_cutoff_invalid"),
            ("null", lambda o, r: o.update(frozenCutoffInstant=None), "occurrence_cutoff_invalid"),
            ("naive", lambda o, r: o.update(frozenCutoffInstant=f"{DAY}T00:00:00"), "occurrence_cutoff_invalid"),
            ("invalid date", lambda o, r: o.update(frozenCutoffInstant="2026-09-31T00:00:00Z"), "occurrence_cutoff_invalid"),
            ("prior day", lambda o, r: o.update(frozenCutoffInstant="2026-09-08T00:00:00Z"), "occurrence_cutoff_invalid"),
            ("after start", lambda o, r: o.update(frozenCutoffInstant=f"{DAY}T00:00:05Z"), "occurrence_cutoff_invalid"),
            ("start before schedule", lambda o, r: o.update(startedAt="2026-09-08T23:59:59Z"), "occurrence_cutoff_invalid"),
            ("changed claim", lambda o, r: o.update(frozenCutoffInstant=f"{DAY}T00:00:01Z"), "occurrence_cutoff_invalid"),
            ("changed run", lambda o, r: r.update(frozenCutoffInstant=f"{DAY}T00:00:01Z"), "occurrence_cutoff_mismatch"),
            ("missing run cutoff", lambda o, r: r.pop("frozenCutoffInstant"), "occurrence_cutoff_mismatch"),
            ("different encoding", lambda o, r: o.update(frozenCutoffInstant=f"{DAY}T08:00:00+08:00"), "occurrence_cutoff_invalid"),
            ("mixed v1", lambda o, r: o.update(schemaVersion="1.0"), "occurrence_version_invalid"),
            ("unknown version", lambda o, r: o.update(schemaVersion="3.0"), "today_run_missing"),
        )
        for kind in ("research", "skip"):
            for label, mutate, reason in mutations:
                with self.subTest(kind=kind, mutation=label):
                    case, write = self.make_case(kind)
                    mutate(case.occurrence, case.run)
                    if "frozenCutoffInstant" in case.run:
                        write()
                    else:
                        # Preserve the old seal; the missing claim must be
                        # rejected before any seal fallback could be attempted.
                        case.write("work/last-run.json", case.run)
                    result = self.outcome(case)
                    self.assertEqual((result["status"], result["reason"]), ("held", reason))

    def test_v2_self_consistent_wrong_schedule_is_rejected(self):
        for kind in ("research", "skip"):
            for cutoff in (f"{DAY}T00:30:00.000Z", f"{DAY}T08:00:00+08:00"):
                with self.subTest(kind=kind, cutoff=cutoff):
                    case, write = self.make_case(kind, cutoff=cutoff)
                    # Matching run+occurrence dates, hashes and time order
                    # cannot turn a noncanonical/wrong schedule into v2 proof.
                    case.occurrence.update(startedAt=f"{DAY}T00:30:01.000Z",
                                           completedAt=f"{DAY}T00:30:25.000Z")
                    case.run.update(startedAt=f"{DAY}T00:30:02.000Z",
                                    completedAt=f"{DAY}T00:30:24.000Z",
                                    cutoffLocal=DAY + (" 08:30" if "00:30" in cutoff else " 08:00"))
                    write()
                    now = research_cases.NOW.replace(minute=35)
                    result = relay.run_relay(case.root, case.bus, now=now, dry_run=True)
                    self.assertEqual((result["status"], result["reason"]),
                                     ("held", "occurrence_cutoff_invalid"))

    def test_legacy_skip_still_rejects_run_cutoff_different_from_claim_start(self):
        case, write = self.make_case("skip", version="1.0")
        case.run["frozenCutoffInstant"] = CUTOFF
        write()
        result = self.outcome(case)
        self.assertEqual((result["status"], result["reason"]), ("held", "calendar_skip_run_invalid"))


if __name__ == "__main__":
    unittest.main()
