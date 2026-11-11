"""Verify the shared frozen primary-source session certificate, never infer.

The backend and ETF producer consume identical, checksum-pinned 2024–2026
calendar bytes. No network fetch, silent repair, or unreviewed future-year
weekday extrapolation is allowed here.
"""
from datetime import date
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

AUTHORITY_VERSION = "research.market-session-evidence.v1"
AUTHORITY_URL = "https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml"
EXPECTED_CERT_SHA = "27c352d986d578b50317741b57a02db656e97de5be79cfb501d3ccd3de21d30f"
AUTHORITY_PATH = Path(__file__).resolve().parents[1] / "data/research_market_session_evidence.json"


def _certificate():
    path = Path(os.getenv("RADAR_CALENDAR_AUTHORITY_FILE", "").strip() or AUTHORITY_PATH)
    expected = os.getenv("RADAR_CALENDAR_AUTHORITY_SHA256", "").strip() or EXPECTED_CERT_SHA
    if path.is_symlink() or not path.is_file():
        raise ValueError("exchange_calendar_authority_unavailable")
    with path.open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("exchange_calendar_authority_checksum_mismatch")
    evidence = json.loads(raw)
    if not isinstance(evidence, dict):
        raise ValueError("exchange_calendar_authority_contract_invalid")
    calendar = evidence.get("calendar")
    if (evidence.get("contractVersion") != AUTHORITY_VERSION or not isinstance(calendar, dict)
            or not isinstance(calendar.get("source"), dict)
            or calendar["source"].get("verification") != "reviewed_primary_source"
            or urlsplit(calendar["source"].get("url") or "").hostname not in {"www.sse.com.cn", "www.szse.cn"}):
        raise ValueError("exchange_calendar_authority_contract_invalid")
    dates = [date.fromisoformat(value) for value in calendar.get("sessions", [])]
    if not dates or dates != sorted(set(dates)):
        raise ValueError("exchange_calendar_authority_sessions_invalid")
    return calendar, dates, expected


def expected_sessions_2026():
    """Read-only helper uses the same certificate as actual freshness."""
    _, dates, _ = _certificate()
    return [value for value in dates if value.year == 2026]


def verify_session_year(dates, year):
    """Compare the full year, not the self-reported count of a changed cache."""
    calendar, registered, digest = _certificate()
    if (date.fromisoformat(calendar["startDate"]) > date(year, 1, 1)
            or date.fromisoformat(calendar["endDate"]) < date(year, 12, 31)):
        raise ValueError("exchange_calendar_authority_unavailable_for_year")
    expected = [value for value in registered if value.year == year]
    actual = [value for value in dates if value.year == year]
    if actual != expected:
        raise ValueError("exchange_calendar_authority_mismatch")
    return {"version": AUTHORITY_VERSION, "source": calendar["source"]["url"],
            "year": year, "session_count": len(expected), "certificate_sha256": digest}
