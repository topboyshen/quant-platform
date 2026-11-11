"""Research digest must not dirty tracked Grafana JSON."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/research_digest.py"
SPEC = importlib.util.spec_from_file_location("research_digest_under_test", SOURCE)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_digest_module_does_not_point_at_tracked_grafana_dashboard():
    assert not hasattr(module, "GRAFANA_DASH")
    assert not hasattr(module, "_sync_grafana_text_panel")
    assert "quantdinger-radar-runtime.json" not in SOURCE.read_text(encoding="utf-8")
