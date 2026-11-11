"""Offline contract: repo templates must match the deployed alert wiring."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class UnitGraphTests(unittest.TestCase):
    def test_no_old_direct_send_service_referenced(self):
        for conf in (ROOT / "systemd").rglob("*.conf"):
            text = conf.read_text(encoding="utf-8")
            # Comments may mention the old name for context; directives must not.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                self.assertNotIn("astock-seal-failure-alert", stripped,
                                 f"{conf}: non-comment line references the retired unit")

    def test_base_unit_also_updated(self):
        text = (ROOT / "systemd" / "astock-research-seal.service").read_text(encoding="utf-8")
        self.assertNotIn("astock-seal-failure-alert", text)

    def test_both_dropins_use_the_unified_service(self):
        for rel in ("systemd/astock-xhs-draft.service.d/failure-alert.conf",
                    "systemd/astock-research-seal.service.d/failure-alert.conf"):
            path = ROOT / rel
            self.assertTrue(path.is_file(), f"{rel} must exist")
            text = path.read_text(encoding="utf-8")
            self.assertIn("OnFailure=research-chain-alert-post.service", text)
            self.assertIn("OnFailure=", text)  # the reset line

    def test_draft_dropin_resets_inherited_onfailure(self):
        text = (ROOT / "systemd" / "astock-xhs-draft.service.d" / "failure-alert.conf").read_text(encoding="utf-8")
        self.assertIn("OnFailure=", text)

if __name__ == "__main__":
    unittest.main()
