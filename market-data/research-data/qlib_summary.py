"""Read-only Qlib summary. No collectors, requests, environment mutation or storage."""
import math
import os
from decimal import Decimal, ROUND_HALF_UP
from qlib_io import load_qlib_features
from snapshot_runtime import QlibDateError, SnapshotValidationError
QLIB = os.path.expanduser("~/qlib/data/cn_data")

def limit_pct(sym):
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith("30") or code.startswith("68"):
        return 0.20
    return 0.10

LIMIT_BASIS = "raw_price_board_rules_v1"
# Registration-system dates after which new listings have 5 no-limit days.
_ALL_BOARDS_REGISTRATION = "2023-04-10"
_CHINEXT_REGISTRATION = "2020-08-24"
_TICK_TOLERANCE_CENTS = 0.2


def _cents(value):
    """Raw exchange price in integer cents, or None when not on the 0.01 grid."""
    x = float(value) * 100.0
    if not math.isfinite(x) or x <= 0:
        return None
    nearest = round(x)
    if abs(x - nearest) > _TICK_TOLERANCE_CENTS:
        return None
    return int(nearest)


def _half_up_cents(value_cents):
    return int(Decimal(repr(float(value_cents))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _limit_cents(ref_cents, pct, up):
    ref = Decimal(ref_cents) / 100
    mult = Decimal(1) + (Decimal(str(pct)) if up else -Decimal(str(pct)))
    return int((ref * mult).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def limit_rule_candidates(sym, date, listing_age, st_symbols=None):
    """Possible daily limit percentages for ``sym`` on ``date``.

    Returns ``(pcts, None)`` or ``(None, reason)``. With unknown ST status a
    main-board stock has two candidates (10% and 5%); a limit is only proven
    when every candidate agrees.
    """
    code = sym[2:]
    new = listing_age is not None and listing_age < 5
    if sym.startswith("bj"):
        if listing_age is not None and listing_age < 1:
            return None, "new_listing_no_limit"
        return (0.30,), None
    if code.startswith("68"):
        return (None, "new_listing_no_limit") if new else ((0.20,), None)
    if code.startswith("30") and date >= _CHINEXT_REGISTRATION:
        return (None, "new_listing_no_limit") if new else ((0.20,), None)
    if new and date >= _ALL_BOARDS_REGISTRATION:
        return None, "new_listing_no_limit"
    if listing_age == 0:
        return None, "rule_not_modeled"
    if st_symbols is None:
        return (0.10, 0.05), None
    return ((0.05,) if sym in st_symbols else (0.10,)), None


def _ex_rights_suspected(factor_prev, factor_cur, adj_prev, adj_cur):
    ratio = abs(float(factor_cur) / float(factor_prev) - 1.0)
    try:
        bound = 1.5 * (0.005 / float(adj_prev) + 0.005 / float(adj_cur))
    except (TypeError, ValueError, ZeroDivisionError):
        bound = None
    if bound is None or not math.isfinite(bound) or bound <= 0:
        return ratio > 1e-6
    # adjclose is published in cents; smaller factor moves are rounding jitter.
    return ratio > bound


def classify_limit(close, high, low, factor, adjclose, i, pcts):
    """Return ``("up"|"down"|None, unverified_reason|None)`` for bar ``i``."""
    if factor is None:
        return None, "factor_unavailable"
    f_prev, f_cur = float(factor[i - 1]), float(factor[i])
    if not (math.isfinite(f_prev) and math.isfinite(f_cur) and f_prev > 0 and f_cur > 0):
        return None, "factor_unavailable"
    cur = _cents(close[i] / f_cur)
    hi = _cents(high[i] / f_cur)
    lo = _cents(low[i] / f_cur)
    ref_raw = _cents(close[i - 1] / f_prev)
    if None in (cur, hi, lo, ref_raw):
        return None, "raw_price_off_tick"
    refs = [ref_raw]
    adj_prev = adjclose[i - 1] if adjclose is not None else None
    adj_cur = adjclose[i] if adjclose is not None else None
    if _ex_rights_suspected(f_prev, f_cur, adj_prev, adj_cur):
        refs.append(_half_up_cents(float(close[i - 1]) / f_cur * 100.0))
    # A (reference, pct) rule is only feasible if the observed bar fits inside
    # its band; e.g. a +10% close rules out the 5% ST rule.
    combos = []
    for ref in refs:
        for pct in pcts:
            upper, lower = _limit_cents(ref, pct, True), _limit_cents(ref, pct, False)
            if lower <= lo and hi <= upper:
                combos.append((ref, pct, upper, lower))
    if not combos:
        return None, "price_outside_all_bands"
    up_votes = [cur == hi == upper for _, _, upper, _ in combos]
    down_votes = [cur == lo == lower for _, _, _, lower in combos]
    if all(up_votes) and all(down_votes):
        return None, "limit_prices_coincide"
    if all(up_votes):
        return "up", None
    if all(down_votes):
        return "down", None
    if any(up_votes) or any(down_votes):
        votes = up_votes if any(up_votes) else down_votes
        ref_split = len({ref for (ref, _, _, _), vote in zip(combos, votes)}) > 1 and any(
            len({vote for (r, p, _, _), vote in zip(combos, votes) if p == pct}) > 1
            for pct in {p for _, p, _, _ in combos})
        return None, ("ex_rights_reference_unproven" if ref_split else "st_status_unknown")
    return None, None


def qlib_generation(qlib_root=QLIB):
    tag_path = os.path.join(os.path.dirname(qlib_root), ".tag")
    try:
        with open(tag_path, encoding="utf-8") as tag_file:
            tag = tag_file.read().strip()
        inode = os.stat(qlib_root).st_ino
    except (FileNotFoundError, OSError) as exc:
        raise SnapshotValidationError(f"Qlib generation marker unavailable: {exc}") from exc
    if not tag:
        raise SnapshotValidationError("Qlib generation marker is empty")
    return {"tag": tag, "inode": inode}

def reference_complete_date(calendar, symbol, qlib_root=QLIB, loader=load_qlib_features):
    try:
        start, features = loader(
            qlib_root, symbol, ("close", "high", "low", "amount")
        )
    except (FileNotFoundError, ValueError) as exc:
        raise SnapshotValidationError(
            f"Qlib reference features unavailable for {symbol}: {exc}"
        ) from exc
    usable = min(len(values) for values in features.values())
    close = features["close"]
    for index in range(usable - 1, 0, -1):
        values = (
            close[index - 1],
            close[index],
            features["high"][index],
            features["low"][index],
            features["amount"][index],
        )
        calendar_index = start + index
        if (
            0 <= calendar_index < len(calendar)
            and all(math.isfinite(float(value)) for value in values)
        ):
            return calendar[calendar_index]
    raise SnapshotValidationError(
        f"Qlib reference has no complete finite date: {symbol}"
    )

def qlib_market(date=None, *, qlib_root=QLIB, min_eligible=5000, min_coverage=0.995,
                feature_loader=load_qlib_features, generation_reader=None, reference_reader=None,
                st_symbols=None):
    generation_reader = generation_reader or (lambda: qlib_generation(qlib_root))
    reference_reader = reference_reader or (lambda cal, sym: reference_complete_date(cal, sym, qlib_root, feature_loader))
    generation_before = generation_reader()
    with open(os.path.join(qlib_root, "calendars/day.txt"), encoding="utf-8") as calendar_file:
        cal = [line.strip() for line in calendar_file if line.strip()]
    if date is None:
        # 日历可能领先特征数据；四只参考股的四字段必须共同对齐。
        ref_syms = ["sh600000", "sz000001", "sh600519", "sz300750"]
        reference_dates = {
            symbol: reference_reader(cal, symbol) for symbol in ref_syms
        }
        unique_dates = sorted(set(reference_dates.values()))
        if len(unique_dates) != 1:
            raise SnapshotValidationError(
                f"Qlib reference dates disagree: {reference_dates}"
            )
        date = unique_dates[0]
    elif date not in cal:
        raise QlibDateError(f"requested date is not in Qlib calendar: {date}")
    di = cal.index(date)
    insts = []
    with open(os.path.join(qlib_root, "instruments/all.txt"), encoding="utf-8") as instruments_file:
        for line in instruments_file:
            p = line.split()
            if len(p) >= 3:
                insts.append((p[0], p[1], p[2]))
    up = down = flat = invalid = eligible = 0
    limit_up, limit_down, rows = [], [], []
    unverified_reasons = {}
    excluded_no_limit = 0
    cal_index = {day: index for index, day in enumerate(cal)}
    for sym, sd, ed in insts:
        # A prior close is required to calculate the daily change, so first-day
        # listings are intentionally excluded from the eligible population.
        if not (sd < date <= ed):
            continue
        eligible += 1
        sym = sym.lower()  # all.txt 大写, feature 目录小写
        try:
            s, features = feature_loader(qlib_root, sym, ("close", "high", "low", "amount"))
        except (FileNotFoundError, ValueError) as exc:
            raise SnapshotValidationError(
                f"Qlib feature structure unavailable for {sym}: {exc}"
            ) from exc
        close = features["close"]
        high = features["high"]
        low = features["low"]
        amount = features["amount"]
        li = di - s
        if li < 1 or any(li >= len(values) for values in features.values()):
            raise SnapshotValidationError(
                f"Qlib feature index is out of range for {sym} on {date}"
            )
        required_values = (close[li - 1], close[li], high[li], low[li], amount[li])
        if not all(math.isfinite(float(value)) for value in required_values):
            invalid += 1
            continue
        chg = close[li] / close[li - 1] - 1
        if chg > 1e-4:
            up += 1
        elif chg < -1e-4:
            down += 1
        else:
            flat += 1
        try:
            _, extra = feature_loader(qlib_root, sym, ("factor",))
            factor = extra["factor"]
        except (FileNotFoundError, ValueError, KeyError):
            factor = None
        try:
            _, extra = feature_loader(qlib_root, sym, ("adjclose",))
            adjclose = extra["adjclose"]
        except (FileNotFoundError, ValueError, KeyError):
            adjclose = None
        if factor is not None and len(factor) <= li:
            factor = None
        if adjclose is not None and len(adjclose) <= li:
            adjclose = None
        start_index = cal_index.get(sd)
        # A start at the calendar origin means the listing predates the data.
        listing_age = None if start_index in (None, 0) else di - start_index
        pcts, rule_reason = limit_rule_candidates(sym, date, listing_age, st_symbols)
        if pcts is None:
            if rule_reason == "new_listing_no_limit":
                excluded_no_limit += 1
            else:
                unverified_reasons[rule_reason] = unverified_reasons.get(rule_reason, 0) + 1
            rows.append((sym, float(chg), float(amount[li]), float(close[li])))
            continue
        state, reason = classify_limit(close, high, low, factor, adjclose, li, pcts)
        if reason is not None:
            unverified_reasons[reason] = unverified_reasons.get(reason, 0) + 1
        if state == "up":
            streak, j = 0, li
            while j >= 1:
                age_j = None if listing_age is None else listing_age - (li - j)
                pcts_j, _ = limit_rule_candidates(sym, cal[s + j], age_j, st_symbols)
                if pcts_j is None or classify_limit(close, high, low, factor, adjclose, j, pcts_j)[0] != "up":
                    break
                streak += 1
                j -= 1
            limit_up.append({
                "sym": sym,
                "chg": round(float(chg) * 100, 2),
                "streak": streak,
                "close": round(float(close[li] / factor[li]), 2),
            })
        elif state == "down":
            limit_down.append({
                "sym": sym,
                "chg": round(float(chg) * 100, 2),
                "close": round(float(close[li] / factor[li]), 2),
            })
        rows.append((sym, float(chg), float(amount[li]), float(close[li])))
    coverage = (len(rows) / eligible) if eligible else 0.0
    if eligible < min_eligible or coverage < min_coverage:
        raise SnapshotValidationError(
            "Qlib market coverage is incomplete: "
            f"date={date}, valid={len(rows)}, eligible={eligible}, "
            f"invalid={invalid}, coverage={coverage:.6f}"
        )
    generation_after = generation_reader()
    if generation_before != generation_after:
        raise SnapshotValidationError(
            "Qlib generation changed during snapshot scan: "
            f"before={generation_before}, after={generation_after}"
        )
    rows.sort(key=lambda r: -r[1])
    return {
        "date": date,
        "generation": generation_after,
        "coverage": {
            "valid": len(rows),
            "eligible": eligible,
            "invalid": invalid,
            "ratio": round(coverage, 6),
        },
        "breadth": {"up": up, "down": down, "flat": flat, "total": up + down + flat},
        "limitUpCount": len(limit_up),
        "limitDownCount": len(limit_down),
        "limitStatus": {
            "basis": LIMIT_BASIS,
            "complete": not unverified_reasons,
            "unverified": sum(unverified_reasons.values()),
            "unverifiedReasons": dict(sorted(unverified_reasons.items())),
            "excludedNoLimit": excluded_no_limit,
            "stStatusKnown": st_symbols is not None,
        },
        "limitUp": sorted(limit_up, key=lambda x: -x["streak"])[:30],
        "limitDown": limit_down[:10],
        "topGainers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "amount": round(r[2], 0)} for r in sorted(rows, key=lambda r: -r[2])[:10]],
    }
