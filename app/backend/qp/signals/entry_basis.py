"""Compare the recorded entry evidence across filled horizons of one decision."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from qp.models.signals import DecisionOutcome


def _price(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    return price if price.is_finite() and price > 0 else None


def compare_entry_basis(rows: Iterable[DecisionOutcome]) -> dict[str, Any]:
    filled = sorted((row for row in rows if row.fill_status == "filled"), key=lambda row: row.horizon_sessions)
    horizons = [row.horizon_sessions for row in filled]
    fields: dict[str, dict[int, str | Decimal | None]] = {"entry_date": {}, "entry_open": {}, "entry_fill": {}}
    for row in filled:
        horizon = row.horizon_sessions
        details = row.details if isinstance(row.details, dict) else {}
        fields["entry_date"][horizon] = row.entry_date.isoformat() if row.entry_date is not None else None
        # entry_price alone does not identify both the raw open and the fill.
        # Historical records without the named evidence stay incomplete.
        for field in ("entry_open", "entry_fill"):
            fields[field][horizon] = _price(details.get(field))

    conflicts = []
    complete = len(horizons) >= 2
    for field, values in fields.items():
        known = set(values.values())
        if None in known:
            complete = False
            known.remove(None)
        if len(known) > 1:
            conflicts.append(
                {
                    "field": field,
                    "values_by_horizon": {
                        str(h): str(value) if value is not None else None for h, value in values.items()
                    },
                }
            )
    status = "conflict" if conflicts else "consistent" if complete else "insufficient"
    return {"status": status, "horizons": horizons, "conflicts": conflicts}
