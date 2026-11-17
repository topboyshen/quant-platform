"""M8.1 morning-chain snapshot: validate stage artifacts and freeze a report."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from qp.models.reports import Report
from qp.morning.artifacts import (
    read_alert,
    read_generation,
    read_precollect,
    read_relay,
    read_seal,
)
from qp.morning.classify import classify_chain
from qp.reports.archive import ReportConflict, SourceRef, freeze_report

KIND = "morning_chain"


def build_payload(
    *, day: dt.date, astock_root: str | Path, bus_root: str | Path, state_dir: str | Path
) -> dict[str, Any]:
    payload = {
        "date": day.isoformat(),
        "precollect": read_precollect(astock_root, day),
        "generation": read_generation(astock_root, day),
        "seal": read_seal(astock_root, day),
        "relay": read_relay(bus_root, day),
        "alerts": read_alert(state_dir, day),
    }
    payload["classification"] = classify_chain(payload)
    return payload


def import_morning_chain(
    session: Session, *, day: dt.date, astock_root: str | Path, bus_root: str | Path, state_dir: str | Path
) -> tuple[Report, bool]:
    payload = build_payload(day=day, astock_root=astock_root, bus_root=bus_root, state_dir=state_dir)
    stages = [payload[key] for key in ("precollect", "generation", "seal", "relay", "alerts")]
    if all(stage is None for stage in stages):
        raise ValueError("no morning chain artifacts for the date")
    sources = [
        SourceRef(source=f"morning_{key}", locator=(stage or {}).get("file"), detail={"present": stage is not None})
        for key, stage in zip(("precollect", "generation", "seal", "relay", "alerts"), stages, strict=False)
        if stage is not None
    ]
    version = 1
    while True:
        try:
            return freeze_report(
                session, kind=KIND, business_date=day, payload=payload, sources=sources, version=version
            )
        except ReportConflict:
            version += 1
            if version > 10:
                raise
