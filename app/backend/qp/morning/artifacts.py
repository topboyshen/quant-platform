"""Read-only readers for the legacy morning-chain artifacts.

The readers never write the legacy roots and deliberately drop alert evidence
that may contain chat identifiers.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def read_precollect(astock_root: str | Path, day: dt.date) -> dict[str, Any] | None:
    """CNINFO pre-collect staging file (schema v2) and PDF evidence count."""
    root = Path(astock_root)
    path = root / "work/cninfo-staging" / f"{day.isoformat()}.json"
    data = _load(path)
    if data is None:
        return None
    body = {k: v for k, v in data.items() if k not in ("schemaVersion", "payloadSha256")}
    expected = hashlib.sha256(_canonical(body)).hexdigest()
    documents = data.get("documents") if isinstance(data.get("documents"), dict) else {}
    codes = data.get("codes") if isinstance(data.get("codes"), dict) else {}
    pdf_dir = root / "data/inbox/cninfo-pdf"
    pdf_files = len(list(pdf_dir.glob("*.pdf"))) if pdf_dir.is_dir() else 0
    return {"file": str(path), "schema_version": data.get("schemaVersion"),
            "target_date": data.get("targetDate"),
            "batch_completed_at": data.get("batchCompletedAt"),
            "collector_source_sha256": data.get("collectorSourceSha256"),
            "codes": len(codes), "documents": len(documents),
            "payload_sha256": data.get("payloadSha256"),
            "payload_sha256_ok": data.get("payloadSha256") == expected,
            "pdf_files": pdf_files}


def read_generation(astock_root: str | Path, day: dt.date) -> dict[str, Any] | None:
    path = Path(astock_root) / "work/orchestrator/states" / f"morning-report-{day.isoformat()}.json"
    data = _load(path)
    if data is None:
        return None
    return {"file": str(path), "status": data.get("status"),
            "target_date": data.get("targetDate"),
            "child_exit_code": data.get("childExitCode"),
            "started_at": data.get("startedAt"), "completed_at": data.get("completedAt"),
            "task": data.get("task"), "invocation_id": data.get("invocationId")}


def read_seal(astock_root: str | Path, day: dt.date) -> dict[str, Any] | None:
    seal_dir = Path(astock_root) / "work/research-seal" / day.isoformat()
    if not seal_dir.is_dir():
        return None
    for path in sorted(seal_dir.glob("*.json")):
        if path.name.endswith(".digest.json"):
            continue
        data = _load(path)
        if data is None:
            continue
        digest_path = seal_dir / f"{path.stem}.digest.json"
        digest = _load(digest_path) or {}
        return {"file": str(path), "sha256": path.stem, "state": data.get("state"),
                "kind": data.get("kind"), "schema_version": data.get("schemaVersion"),
                "target_date": data.get("targetDate"), "generated_at": data.get("generatedAt"),
                "target_matches": data.get("targetDate") == day.isoformat(),
                "digest_present": digest_path.is_file(),
                "digest_file": str(digest_path) if digest else None,
                "digest_schema": digest.get("schemaVersion"),
                "digest_kind": digest.get("kind"),
                "report_sha256": digest.get("reportSha256"),
                "report_sha_matches": digest.get("reportSha256") == path.stem,
                "report_date": digest.get("reportDate"),
                "data_date": digest.get("dataDate"),
                "input_sha256": digest.get("inputSha256"),
                "selection_sha256": digest.get("selectionSha256"),
                "source_count": len(digest.get("sourceIndex") or []),
                "limitation_count": len(digest.get("limitations") or [])}
    return None


def read_relay(bus_root: str | Path, day: dt.date) -> dict[str, Any] | None:
    path = Path(bus_root) / "out/research-digest-latest.json"
    data = _load(path)
    if data is None:
        return None
    text = data.get("plain_text") if isinstance(data.get("plain_text"), str) else ""
    attempt = _load(Path(bus_root) / "out/relay-attempts" / f"{day.isoformat()}.json")
    delivery = _load(Path(bus_root) / "out/relay-delivery" / f"{day.isoformat()}.json")
    produced = data.get("produced_at")
    produced_date = None
    try:
        produced_date = dt.datetime.fromisoformat(str(produced)).date().isoformat()
    except ValueError:
        produced_date = None
    return {"file": str(path), "target_date": data.get("target_date"),
            "target_matches": data.get("target_date") == day.isoformat(),
            "produced_at": produced, "produced_date": produced_date,
            "digest_type": data.get("digest_type"),
            "health": data.get("health"), "freshness": data.get("freshness"),
            "plain_text_sha256": _sha(text), "plain_text_bytes": len(text.encode("utf-8")),
            "attempt": None if attempt is None else {
                "file": str(Path(bus_root) / "out/relay-attempts" / f"{day.isoformat()}.json"),
                "status": attempt.get("status"), "reason": attempt.get("reason"),
                "sent": attempt.get("sent"), "observed_at": attempt.get("observedAt")},
            "delivery": None if delivery is None else {
                "file": str(Path(bus_root) / "out/relay-delivery" / f"{day.isoformat()}.json"),
                "status": delivery.get("status"), "reason": delivery.get("reason"),
                "payload_sha256": delivery.get("payload_sha256"),
                "report_sha256": delivery.get("report_sha256"),
                "content_match": delivery.get("content_match"),
                "attempted_at": delivery.get("attempted_at")}}


def read_alert(state_dir: str | Path, day: dt.date) -> dict[str, Any] | None:
    path = Path(state_dir) / f"research-chain-alert-{day.isoformat()}.json"
    data = _load(path)
    if data is None:
        return None
    incident = data.get("incident") if isinstance(data.get("incident"), dict) else {}
    transition = data.get("transition") if isinstance(data.get("transition"), dict) else {}
    causes = [{"code": c.get("code"), "detail": c.get("detail"), "reason": c.get("reason")}
              for c in incident.get("causes", []) if isinstance(c, dict)]
    return {"file": str(path),
            "business_atoms": data.get("businessAtoms") if isinstance(data.get("businessAtoms"), list) else [],
            "transition": {"added": transition.get("added") or [],
                           "recovered": transition.get("recovered") or [],
                           "sequence": transition.get("sequence")},
            "causes": causes,
            "first_seen": incident.get("first_seen"), "last_seen": incident.get("last_seen"),
            "notified_at": incident.get("notified_at"),
            "delivery_wording": incident.get("delivery_wording"),
            "transition_count": data.get("transitionCount"),
            "last_observed_at": data.get("last_observed_at")}
