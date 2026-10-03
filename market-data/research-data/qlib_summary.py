"""Read-only Qlib summary. No collectors, requests, environment mutation or storage."""
import math
import os
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
                feature_loader=load_qlib_features, generation_reader=None, reference_reader=None):
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
        lim = limit_pct(sym)
        lp = round(close[li - 1] * (1 + lim), 2)
        if abs(close[li] - lp) < 0.005 and abs(high[li] - close[li]) < 0.005:
            streak, j = 0, li
            while j >= 1:
                lpj = round(close[j - 1] * (1 + lim), 2)
                if abs(close[j] - lpj) < 0.005 and abs(high[j] - close[j]) < 0.005:
                    streak += 1
                    j -= 1
                else:
                    break
            limit_up.append({
                "sym": sym,
                "chg": round(float(chg) * 100, 2),
                "streak": streak,
                "close": round(float(close[li]), 2),
            })
        lpd = round(close[li - 1] * (1 - lim), 2)
        if abs(close[li] - lpd) < 0.005 and abs(low[li] - close[li]) < 0.005:
            limit_down.append({
                "sym": sym,
                "chg": round(float(chg) * 100, 2),
                "close": round(float(close[li]), 2),
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
        "limitUp": sorted(limit_up, key=lambda x: -x["streak"])[:30],
        "limitDown": limit_down[:10],
        "topGainers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "chg": round(r[1] * 100, 2)} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "amount": round(r[2], 0)} for r in sorted(rows, key=lambda r: -r[2])[:10]],
    }
