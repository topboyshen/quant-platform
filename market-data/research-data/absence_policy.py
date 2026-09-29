"""Pure source-absence scopes and continuous verified-trading-day evidence."""

def duration(calendar, target, symbols, absences, last_quoted):
    """Consecutive missing quotes per symbol, not this increment's row count.

    A prior-close-only gap with a real current quote has a zero current streak.
    Insufficient calendar/absence history cannot establish a bounded duration.
    """
    days = sorted(set(calendar))
    proven = target in days
    streaks = {}
    for symbol in sorted(symbols):
        quoted = last_quoted.get(symbol)
        missing = set(absences.get(symbol) or [])
        if quoted == target and target not in missing:
            streaks[symbol] = []
            continue
        if not quoted or quoted not in days or quoted > target:
            proven = False
            streaks[symbol] = []
            continue
        expected = [day for day in days if quoted < day <= target]
        if not expected or not set(expected) <= missing:
            proven = False
        streaks[symbol] = expected
    return {"proven": proven, "consecutiveMissingTradingDates": streaks,
            "maxConsecutiveMissingTradingDays": max(map(len, streaks.values()), default=0)}


def scopes(calendar, target, coverage, index, absences, last_quoted, proven):
    full_symbols = set(coverage.get("invalid_symbols") or [])
    csi_symbols = set(index.get("missingQuoteMembers") or []) | set(index.get("missingPreviousCloseMembers") or [])
    result = {}
    for name, symbols, count, eligible in (
        ("full_market", full_symbols, coverage.get("invalid"), coverage.get("eligible")),
        ("csi_members", csi_symbols, len(csi_symbols), index.get("uniqueMembers")),
    ):
        evidence = duration(calendar, target, symbols, absences, last_quoted)
        valid = (proven and evidence["proven"] and type(count) is int and count == len(symbols)
                 and type(eligible) is int and 0 < eligible and 0 <= count <= eligible)
        tolerance = max(20, .005 * eligible) if type(eligible) is int and eligible > 0 else None
        reason = None
        if not valid:
            reason = "proof_incomplete"
        elif count > tolerance:
            reason = f"count_over_tolerance:{count}>{tolerance:.1f}"
        elif evidence["maxConsecutiveMissingTradingDays"] > 3:
            reason = f"duration_over_bound:{evidence['maxConsecutiveMissingTradingDays']}>3"
        result[name] = {"scope": name, "invalidCount": count, "eligibleCount": eligible,
                        "absenceTolerance": tolerance, "symbols": sorted(symbols),
                        "proven": bool(valid), "severity": "warning" if reason else "information",
                        "escalation": reason, **{k: v for k, v in evidence.items() if k != "proven"}}
    return result
