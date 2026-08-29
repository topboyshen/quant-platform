#!/usr/bin/env python3
"""统一每日数据快照层: qlib 量价 + akshare 板块/活跃度 → SQLite append-only。

用法: python3 daily_snapshot.py   (仅采集本地 Qlib 最新完整交易日；不支持历史回填)
输出: 打印摘要 JSON; 全量快照写入 ~/research-data/snapshot.db (表 snapshot, date 主键)
"""
import datetime
import fcntl
import hashlib
import io
import json
import math
import numbers
import os
import sqlite3
import sys
import syslog
import time
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout

from qlib_io import load_qlib_features, read_qlib_day_bin

# akshare 需直连中国源, 去掉代理环境变量
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

QLIB = os.path.expanduser("~/qlib/data/cn_data")
DB = os.environ.get(
    "RESEARCH_DATA_SNAPSHOT_DB",
    os.path.expanduser("~/research-data/snapshot.db"),
)
STATE_DIR = os.environ.get(
    "RESEARCH_DATA_STATE_DIR",
    os.path.expanduser("~/.local/state/research-data"),
)
QLIB_MIN_ELIGIBLE = 5000
QLIB_MIN_COVERAGE = 0.999
SQLITE_TIMEOUT = 30


class QlibDateError(ValueError):
    """The requested trade date is not present in the local Qlib calendar."""


class SnapshotValidationError(RuntimeError):
    """The candidate snapshot is unsafe to persist."""


class SnapshotUsageError(RuntimeError):
    """The command-line invocation is unsupported."""


class SnapshotTemporaryError(RuntimeError):
    """A transient lock or database-busy condition blocked the run."""


def _snapshot_payload(snapshot):
    try:
        return json.dumps(
            snapshot,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"payload is not strict JSON: {exc}") from exc


def _payload_sha256(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reject_json_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def write_last_run(record, state_dir=None):
    state_dir = state_dir or STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "last-run.json")
    temporary_path = f"{path}.{os.getpid()}.tmp"
    payload = _snapshot_payload(record) + "\n"
    with open(temporary_path, "w", encoding="utf-8") as state_file:
        state_file.write(payload)
        state_file.flush()
        os.fsync(state_file.fileno())
    os.replace(temporary_path, path)


def emit_status(record, stream=None):
    stream = stream or sys.stdout
    record = dict(record)
    record.setdefault("timestamp", datetime.datetime.now().astimezone().isoformat())
    line = _snapshot_payload(record)
    print(line, file=stream, flush=True)
    try:
        syslog.openlog("research-data-snapshot")
        syslog.syslog(syslog.LOG_INFO, line)
    except (OSError, RuntimeError):
        pass
    write_last_run(record)


@contextmanager
def snapshot_run_lock(state_dir=None):
    state_dir = state_dir or STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    lock_path = os.path.join(state_dir, "snapshot.lock")
    lock_file = open(lock_path, "a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise SnapshotTemporaryError("another snapshot run holds the lock") from exc
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _ensure_snapshot_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS snapshot("
        "date TEXT PRIMARY KEY, generated_at TEXT, payload TEXT)"
    )


def existing_snapshot(date, db_path=None):
    db_path = db_path or DB
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    try:
        with sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT) as conn:
            _ensure_snapshot_table(conn)
            row = conn.execute(
                "SELECT generated_at, payload FROM snapshot WHERE date = ?", (date,)
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise SnapshotTemporaryError(f"SQLite preflight is busy: {exc}") from exc
        raise SnapshotValidationError(f"SQLite preflight failed: {exc}") from exc
    except sqlite3.Error as exc:
        raise SnapshotValidationError(f"SQLite preflight failed: {exc}") from exc
    if row is None:
        return None
    try:
        payload = json.loads(row[1], parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(
            f"existing snapshot {date} contains invalid JSON: {exc}"
        ) from exc
    if payload.get("date") != date or (payload.get("qlib") or {}).get("date") != date:
        raise SnapshotValidationError(
            f"existing snapshot {date} has inconsistent internal dates"
        )
    existing_activity_date = activity_trade_date(payload.get("akshare") or {})
    if existing_activity_date != date:
        raise SnapshotValidationError(
            f"existing snapshot {date} is mixed-date: "
            f"akshare_activity={existing_activity_date}"
        )
    errors = source_error_paths(payload)
    if errors:
        raise SnapshotValidationError(
            f"existing snapshot {date} contains source errors: {', '.join(errors[:5])}"
        )
    return {
        "generatedAt": row[0],
        "payloadSha256": _payload_sha256(row[1]),
    }


def store_snapshot(snapshot, db_path=None):
    """Persist one immutable snapshot, allowing only an exact-payload retry."""
    db_path = db_path or DB
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    payload = _snapshot_payload(snapshot)
    payload_sha256 = _payload_sha256(payload)
    try:
        with sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _ensure_snapshot_table(conn)
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise SnapshotValidationError(f"SQLite quick_check failed: {integrity}")
            row = conn.execute(
                "SELECT payload FROM snapshot WHERE date = ?", (snapshot["date"],)
            ).fetchone()
            if row is not None:
                if row[0] == payload:
                    return {"status": "idempotent", "payloadSha256": payload_sha256}
                raise SnapshotValidationError(
                    f"snapshot conflict for {snapshot['date']}: immutable row already exists"
                )
            conn.execute(
                "INSERT INTO snapshot(date, generated_at, payload) VALUES(?,?,?)",
                (snapshot["date"], snapshot["generatedAt"], payload),
            )
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise SnapshotValidationError(f"SQLite quick_check failed: {integrity}")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise SnapshotTemporaryError(f"SQLite transaction is busy: {exc}") from exc
        raise SnapshotValidationError(f"SQLite transaction failed: {exc}") from exc
    except sqlite3.Error as exc:
        raise SnapshotValidationError(f"SQLite transaction failed: {exc}") from exc
    return {
        "status": "inserted",
        "payloadSha256": payload_sha256,
        "quickCheck": integrity,
    }


def source_error_paths(value, path=""):
    errors = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if (key == "error" or key.endswith("_error")) and child:
                errors.append(child_path)
            else:
                errors.extend(source_error_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            errors.extend(source_error_paths(child, child_path))
    return errors


def json_scalar(value, allow_nan_as_null=False):
    """Convert numeric scalars to strict JSON, with an explicit NaN whitelist."""
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Real):
        numeric = float(value)
        if math.isnan(numeric):
            if allow_nan_as_null:
                return None
            raise SnapshotValidationError("non-finite numeric source value: NaN")
        if not math.isfinite(numeric):
            raise SnapshotValidationError(
                f"non-finite numeric source value: {numeric}"
            )
        if isinstance(value, numbers.Integral):
            return int(value)
        if type(value) is not float:
            return numeric
    return value


def collect_source(func, *args, attempts=2, **kwargs):
    """Retry read-only source collection on exceptions or explicit error payloads."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = func(*args, **kwargs)
            errors = source_error_paths(result)
            if errors:
                raise RuntimeError(f"error payloads: {', '.join(errors[:5])}")
            return result
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(1)
    raise last_error


def activity_trade_date(akshare_payload):
    value = (akshare_payload.get("activity") or {}).get("统计日期")
    if value is None:
        return None
    candidate = str(value).strip()[:10]
    try:
        return datetime.date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def validate_daily_source_dates(target_trade_date, akshare_payload):
    if akshare_payload.get("error"):
        raise SnapshotValidationError(
            f"AkShare market activity unavailable: {akshare_payload['error']}"
        )
    activity_date = activity_trade_date(akshare_payload)
    if activity_date is None:
        raise SnapshotValidationError("AkShare market activity has no valid 统计日期")
    if activity_date != target_trade_date:
        raise SnapshotValidationError(
            "trade-date mismatch: "
            f"qlib={target_trade_date}, akshare_activity={activity_date}"
        )
    return activity_date


def validate_source_completeness(sources, watch_codes, fund_flow_codes):
    errors = source_error_paths(sources)
    if errors:
        raise SnapshotValidationError(
            f"source collection contains errors: {', '.join(errors[:8])}"
        )

    akshare_payload = sources["akshare"]
    if not akshare_payload.get("sectors"):
        raise SnapshotValidationError("AkShare sector summary is empty")
    spot_total = ((sources["spot"].get("breadth") or {}).get("total") or 0)
    if spot_total < 1000:
        raise SnapshotValidationError(f"spot market coverage too low: {spot_total}")
    if not sources["news"]:
        raise SnapshotValidationError("news collection is empty")
    if not sources["hotStocks"]:
        raise SnapshotValidationError("hot-stock collection is empty")
    if len(sources["globalMarkets"]) < 3:
        raise SnapshotValidationError("global-market collection is incomplete")

    expected_by_source = {
        "mootdx": watch_codes,
        "tencent": watch_codes,
        "baostock": watch_codes,
        "announcements": watch_codes,
        "concepts": watch_codes,
        "insiders": watch_codes,
        "fundFlow": fund_flow_codes,
    }
    for source_name, expected_codes in expected_by_source.items():
        missing = sorted(set(expected_codes) - set(sources[source_name]))
        if missing:
            raise SnapshotValidationError(
                f"{source_name} is missing expected codes: {','.join(missing)}"
            )

    nonempty_required = {
        "fundFlow": fund_flow_codes,
        "mootdx": watch_codes,
        "tencent": watch_codes,
        "baostock": watch_codes,
    }
    for source_name, expected_codes in nonempty_required.items():
        empty = [code for code in expected_codes if not sources[source_name].get(code)]
        if empty:
            raise SnapshotValidationError(
                f"{source_name} has empty payloads for: {','.join(sorted(empty))}"
            )

    northbound_values = [
        sources["northbound"].get(key) for key in ("hgt", "sgt", "total")
    ]
    if not any(
        isinstance(value, numbers.Real) and math.isfinite(float(value))
        for value in northbound_values
    ):
        raise SnapshotValidationError("northbound flow has no finite market value")


def requested_trade_date(argv):
    if argv:
        raise SnapshotUsageError(
            "historical date arguments are disabled because live sources cannot be "
            "validated as historical observations"
        )
    return None


def read_bin(path):
    return read_qlib_day_bin(path)


def limit_pct(sym):
    code = sym[2:]
    if sym.startswith("bj"):
        return 0.30
    if code.startswith("30") or code.startswith("68"):
        return 0.20
    return 0.10


def qlib_generation():
    tag_path = os.path.join(os.path.dirname(QLIB), ".tag")
    try:
        with open(tag_path, encoding="utf-8") as tag_file:
            tag = tag_file.read().strip()
        inode = os.stat(QLIB).st_ino
    except (FileNotFoundError, OSError) as exc:
        raise SnapshotValidationError(f"Qlib generation marker unavailable: {exc}") from exc
    if not tag:
        raise SnapshotValidationError("Qlib generation marker is empty")
    return {"tag": tag, "inode": inode}


def reference_complete_date(calendar, symbol):
    try:
        start, features = load_qlib_features(
            QLIB, symbol, ("close", "high", "low", "amount")
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


def qlib_market(date):
    generation_before = qlib_generation()
    with open(os.path.join(QLIB, "calendars/day.txt"), encoding="utf-8") as calendar_file:
        cal = [line.strip() for line in calendar_file if line.strip()]
    if date is None:
        # 日历可能领先特征数据；四只参考股的四字段必须共同对齐。
        ref_syms = ["sh600000", "sz000001", "sh600519", "sz300750"]
        reference_dates = {
            symbol: reference_complete_date(cal, symbol) for symbol in ref_syms
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
    with open(os.path.join(QLIB, "instruments/all.txt"), encoding="utf-8") as instruments_file:
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
            s, features = load_qlib_features(QLIB, sym, ("close", "high", "low", "amount"))
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
    if eligible < QLIB_MIN_ELIGIBLE or coverage < QLIB_MIN_COVERAGE:
        raise SnapshotValidationError(
            "Qlib market coverage is incomplete: "
            f"date={date}, valid={len(rows)}, eligible={eligible}, "
            f"invalid={invalid}, coverage={coverage:.6f}"
        )
    generation_after = qlib_generation()
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


def akshare_market():
    import akshare as ak
    sectors = ak.stock_board_industry_summary_ths()
    activity = ak.stock_market_activity_legu()
    sector_list = []
    for _, r in sectors.iterrows():
        sector_list.append({
            "name": json_scalar(r.get("板块")),
            "chg": json_scalar(r.get("涨跌幅")),
            "netInflow": json_scalar(r.get("净流入")),
            "upCount": json_scalar(r.get("上涨家数")),
            "downCount": json_scalar(r.get("下跌家数")),
            "leader": json_scalar(r.get("领涨股")),
        })
    act = {}
    for _, r in activity.iterrows():
        act[str(r["item"])] = json_scalar(r["value"])
    return {"sectors": sector_list, "activity": act}


def akshare_lhb(date):
    import akshare as ak
    d = date.replace("-", "")
    df = ak.stock_lhb_detail_daily_sina(date=d)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "code": str(r.get("股票代码")),
            "name": json_scalar(r.get("股票名称")),
            "close": json_scalar(r.get("收盘价")),
            "chg": json_scalar(r.get("对应值")),
            "reason": json_scalar(r.get("指标"), allow_nan_as_null=True),
        })
    return {"date": date, "count": len(rows), "rows": rows}


def spot_market():
    import akshare as ak
    df = ak.stock_zh_a_spot()  # sina 全A现货(实时)
    up = down = flat = 0
    limit_up = []
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码"))
        name = r.get("名称")
        try:
            chg = json_scalar(float(r.get("涨跌幅")))
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"spot change is invalid for {code}: {r.get('涨跌幅')}"
            ) from exc
        if chg > 0.01:
            up += 1
        elif chg < -0.01:
            down += 1
        else:
            flat += 1
        lim = 30 if code.startswith("bj") else (20 if code[2:].startswith(("30", "68")) else 10)
        if chg >= lim - 0.5:
            limit_up.append({"sym": code, "name": name, "chg": chg})
        try:
            amt = json_scalar(float(r.get("成交额")))
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"spot amount is invalid for {code}: {r.get('成交额')}"
            ) from exc
        rows.append((code, name, chg, amt))
    rows.sort(key=lambda r: -r[2])
    return {
        "date": "current",
        "breadth": {"up": up, "down": down, "flat": flat, "total": up + down + flat},
        "limitUpCount": len(limit_up),
        "limitUp": limit_up[:30],
        "topGainers": [{"sym": r[0], "name": r[1], "chg": r[2]} for r in rows[:10]],
        "topLosers": [{"sym": r[0], "name": r[1], "chg": r[2]} for r in rows[-10:][::-1]],
        "topAmount": [{"sym": r[0], "name": r[1], "amount": r[3]} for r in sorted(rows, key=lambda r: -r[3])[:10]],
    }


def fund_flow_sina(codes, days=5):
    import urllib.request, json
    results = {}
    for code in codes:
        pre = ("bj" if code.startswith(("92", "8")) else "sh" if code.startswith(("6", "9")) else "sz") + code
        u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={pre}")
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                t = r.read().decode("utf-8", "ignore")
            arr = json.loads(t[t.index("["):t.rindex("]") + 1])
            results[code] = [{"date": x.get("opendate"), "close": x.get("trade"),
                              "net_amount": x.get("netamount"), "turnover": x.get("turnover")} for x in arr]
        except Exception as e:
            results[code] = {"error": str(e)}
    return results


def lhb_official(date):
    import urllib.request, json, ssl
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    out = {"date": date, "szse": [], "sse": ""}
    su = (f"https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
          f"&CATALOGID=1842_xxpl&TABKEY=tab1&txtStart={date}&txtEnd={date}&random=0.9")
    req = urllib.request.Request(su, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/disclosure/supervision/dealinfo/index.html"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            d = json.loads(r.read())
        for row in d[0].get("data", []):
            out["szse"].append({"code": row.get("zqdm"), "name": row.get("zqjc"), "amount": row.get("cjje"), "reason": row.get("plyy")})
    except Exception as e:
        out["szse_error"] = str(e)
    eu = (f"https://query.sse.com.cn/infodisplay/showTradePublicFile.do?"
          f"jsonCallBack=cb&isPagination=false&dateTx={date}")
    req = urllib.request.Request(eu, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.sse.com.cn/disclosure/diclosure/public/"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            t = r.read().decode("utf-8", "ignore")
        out["sse"] = "\n".join(json.loads(t[t.index("(") + 1:t.rindex(")")]).get("fileContents", []))
    except Exception as e:
        out["sse_error"] = str(e)
    return out


def mootdx_quotes(codes):
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    df = client.quotes(symbol=codes)
    result = {}
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            result[code] = {
                "price": json_scalar(r.get("price")),
                "bid1": json_scalar(r.get("bid1")),
                "ask1": json_scalar(r.get("ask1")),
                "bid_vol1": json_scalar(r.get("bid_vol1")),
                "ask_vol1": json_scalar(r.get("ask_vol1")),
            }
    return result


def tencent_quote(codes):
    import urllib.request
    prefixed = []
    for c in codes:
        if c.startswith(("92", "8")):
            p = "bj" + c
        elif c.startswith(("6", "9", "5")):
            p = "sh" + c
        else:
            p = "sz" + c
        prefixed.append(p)
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    result = {}
    for line in data.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        result[key[2:]] = {
            "name": vals[1], "price": vals[3], "pe_ttm": vals[39], "pb": vals[46],
            "mcap_yi": vals[45], "float_mcap_yi": vals[44], "turnover_pct": vals[38],
            "limit_up": vals[47], "limit_down": vals[48],
        }
    return result


def baostock_valuation(codes, days=30):
    import baostock as bs
    import datetime
    import contextlib, io
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - datetime.timedelta(days=days * 2)).strftime("%Y-%m-%d")
    result = {}
    with contextlib.redirect_stdout(io.StringIO()):  # 抑制 baostock login/logout 的 stdout 输出
        bs.login()
        for code in codes:
            pre = ("sh." if code.startswith(("6", "9", "5")) else "sz." if code.startswith(("0", "3")) else "bj.") + code
            try:
                rs = bs.query_history_k_data_plus(pre, "date,code,close,peTTM,pbMRQ,psTTM,turn",
                                                  start_date=start, end_date=end, frequency="d", adjustflag="3")
                rows = []
                while rs.next():
                    r = rs.get_row_data()
                    rows.append({"date": r[0], "close": r[2], "pe_ttm": r[3], "pb": r[4], "ps": r[5], "turn": r[6]})
                result[code] = rows
            except Exception as e:
                result[code] = {"error": str(e)}
        bs.logout()
    return result


_CNINFO_ORGID_MAP = None


def cninfo_announcements(code, page_size=10):
    global _CNINFO_ORGID_MAP
    import requests
    if _CNINFO_ORGID_MAP is None:
        r = requests.get("http://www.cninfo.com.cn/new/data/szse_stock.json", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        _CNINFO_ORGID_MAP = {s["code"]: s["orgId"] for s in r.json().get("stockList", [])}
    org_id = _CNINFO_ORGID_MAP.get(code, f"gssz0{code}")
    payload = {"stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": str(page_size), "pageNum": "1",
               "column": "", "category": "", "plate": "", "seDate": "", "searchkey": "", "secid": "",
               "sortName": "", "sortType": "", "isHLtitle": "true"}
    r = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query", data=payload,
                      headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded",
                               "Referer": "https://www.cninfo.com.cn/new/disclosure", "Origin": "https://www.cninfo.com.cn"}, timeout=15)
    anns = r.json().get("announcements") or []
    import datetime
    out = []
    for a in anns:
        ts = a.get("announcementTime")
        date = ""
        if ts:
            try:
                ts = int(ts)
                if ts > 1e12:
                    ts = ts // 1000
                date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:
                date = str(ts)[:10]
        out.append({"title": a.get("announcementTitle"), "date": date})
    return out


def cls_telegraph(page_size=20):
    import requests, hashlib
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5", "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    r = requests.get(f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}",
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}, timeout=10)
    rolls = (r.json().get("data") or {}).get("roll_data") or []
    return [{"title": n.get("title", ""), "time": n.get("ctime")} for n in rolls]


def northbound_flow():
    import urllib.request, json
    req = urllib.request.Request("https://data.hexin.cn/market/hsgtApi/method/dayChart/",
                                 headers={"User-Agent": "Mozilla/5.0", "Host": "data.hexin.cn",
                                          "Referer": "https://data.hexin.cn/"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    hgt = d.get("hgt", [])
    sgt = d.get("sgt", [])
    hgt_close = hgt[-1] if hgt else None
    sgt_close = sgt[-1] if sgt else None
    hgt_open = hgt[0] if hgt else None
    sgt_open = sgt[0] if sgt else None
    return {"hgt": hgt_close, "sgt": sgt_close,
            "total": (hgt_close + sgt_close) if hgt and sgt else None,
            "hgt_trend": (hgt_close - hgt_open) if hgt else None,
            "sgt_trend": (sgt_close - sgt_open) if sgt else None}


def concept_blocks(code):
    import urllib.request, json
    url = ('https://finance.pae.baidu.com/api/getrelatedblock'
           f'?stock=[{{"code":"{code}","market":"ab","type":"stock"}}]&finClientType=pc')
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Host": "finance.pae.baidu.com",
                                               "Referer": "https://gushitong.baidu.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    result = d.get("Result", {}).get(code, [])
    return {cat.get("name", ""): [i.get("name") for i in cat.get("list", [])] for cat in result}


def insider_transactions(code):
    from mootdx.quotes import Quotes
    client = Quotes.factory(market="std")
    text = client.F10(symbol=code, name="股东研究")
    return (text or "")[:3000]  # 截断, 供 LLM 分析用


def hot_stocks():
    import urllib.request, json, datetime
    date = datetime.datetime.now().strftime("%Y-%m-%d")
    url = f"http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    rows = d.get("data") or []
    return [{"code": r.get("code"), "name": r.get("name"), "reason": r.get("reason", "")} for r in rows]


def global_markets():
    import akshare as ak
    result = {}
    errors = []
    for sym, name in [(".DJI", "道指"), (".IXIC", "纳指"), (".INX", "标普")]:
        try:
            df = ak.index_us_stock_sina(symbol=sym)
            close = df["close"].tolist()
            if len(close) >= 2:
                chg = (close[-1] - close[-2]) / close[-2] * 100
                result[name] = {"close": round(close[-1], 2), "chg": round(chg, 2)}
            else:
                errors.append(f"{name}: insufficient rows")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def run_snapshot(run_id, started_monotonic):
    date = requested_trade_date(sys.argv[1:])
    q = qlib_market(date)
    prior = existing_snapshot(q["date"])
    if prior is not None:
        emit_status({
            "runId": run_id,
            "status": "skipped_existing",
            "target": q["date"],
            "qlibTag": q["generation"]["tag"],
            "coverage": q["coverage"],
            "generatedAt": prior["generatedAt"],
            "payloadSha256": prior["payloadSha256"],
            "durationSeconds": round(time.monotonic() - started_monotonic, 3),
        })
        return 0

    collection_started_at = datetime.datetime.now().astimezone().isoformat()
    try:
        a = collect_source(akshare_market)
    except Exception as e:
        a = {"sectors": [], "activity": {}, "error": str(e)}
    activity_date = validate_daily_source_dates(q["date"], a)
    try:
        lhb = collect_source(akshare_lhb, q["date"])
    except Exception as e:
        lhb = {"date": q["date"], "count": 0, "rows": [], "error": str(e)}
    try:
        spot = collect_source(spot_market)
    except Exception as e:
        spot = {"date": "current", "error": str(e)}
    # 资金流: watchlist + 涨停股
    try:
        with open(
            os.path.expanduser("~/research-data/config/stocks.json"), encoding="utf-8"
        ) as config_file:
            cfg = json.load(config_file)
        watch_codes = [s["code"] for s in cfg.get("corePool", [])]
    except Exception as exc:
        raise SnapshotValidationError(f"stock config unavailable: {exc}") from exc
    if not watch_codes:
        raise SnapshotValidationError("stock config corePool is empty")
    limit_codes = [x["sym"][2:] for x in spot.get("limitUp", [])[:10]]
    ff_codes = list(dict.fromkeys(watch_codes + limit_codes))
    try:
        ff = collect_source(fund_flow_sina, ff_codes, days=5)
    except Exception as e:
        ff = {"error": str(e)}
    try:
        lhb_off = collect_source(lhb_official, q["date"])
    except Exception as e:
        lhb_off = {"date": q["date"], "error": str(e)}
    try:
        mq = collect_source(mootdx_quotes, watch_codes)
    except Exception as e:
        mq = {"error": str(e)}
    try:
        tq = collect_source(tencent_quote, watch_codes)
    except Exception as e:
        tq = {"error": str(e)}
    try:
        bv = collect_source(baostock_valuation, watch_codes, days=30)
    except Exception as e:
        bv = {"error": str(e)}
    try:
        anns = collect_source(
            lambda: {code: cninfo_announcements(code) for code in watch_codes}
        )
    except Exception as e:
        anns = {"error": str(e)}
    try:
        news = collect_source(cls_telegraph, 20)
    except Exception as e:
        news = {"error": str(e)}
    try:
        nb = collect_source(northbound_flow)
    except Exception as e:
        nb = {"error": str(e)}
    try:
        concepts = collect_source(
            lambda: {code: concept_blocks(code) for code in watch_codes}
        )
    except Exception as e:
        concepts = {"error": str(e)}
    try:
        insiders = collect_source(
            lambda: {code: insider_transactions(code) for code in watch_codes}
        )
    except Exception as e:
        insiders = {"error": str(e)}
    try:
        hot = collect_source(hot_stocks)
    except Exception as e:
        hot = {"error": str(e)}
    try:
        global_mkts = collect_source(global_markets)
    except Exception as e:
        global_mkts = {"error": str(e)}
    sources = {
        "akshare": a,
        "lhb": lhb,
        "spot": spot,
        "fundFlow": ff,
        "lhbOfficial": lhb_off,
        "mootdx": mq,
        "tencent": tq,
        "baostock": bv,
        "announcements": anns,
        "news": news,
        "northbound": nb,
        "concepts": concepts,
        "insiders": insiders,
        "hotStocks": hot,
        "globalMarkets": global_mkts,
    }
    validate_source_completeness(sources, watch_codes, ff_codes)
    collection_completed_at = datetime.datetime.now().astimezone().isoformat()
    snapshot = {
        "date": q["date"],
        "generatedAt": collection_completed_at,
        "sourceAsOf": {
            "collectionWindow": {
                "startedAt": collection_started_at,
                "completedAt": collection_completed_at,
            },
            "qlib": {"status": "ok", "kind": "trade_date", "value": q["date"]},
            "akshareActivity": {
                "status": "ok", "kind": "trade_date", "value": activity_date,
            },
            "lhb": {"status": "ok", "kind": "trade_date", "value": q["date"]},
            "lhbOfficial": {
                "status": "ok", "kind": "trade_date", "value": q["date"],
            },
            "liveAndRollingSources": {
                "status": "ok",
                "kind": "observed_at",
                "window": {
                    "startedAt": collection_started_at,
                    "completedAt": collection_completed_at,
                },
                "sources": [
                    "akshareSectors", "spot", "fundFlow", "mootdx", "tencent",
                    "baostock", "announcements", "news", "northbound", "concepts",
                    "insiders", "hotStocks", "globalMarkets",
                ],
            },
        },
        "qlib": q,
        "akshare": a,
        "lhb": lhb,
        "spot": spot,
        "fundFlow": ff,
        "lhbOfficial": lhb_off,
        "mootdx": mq,
        "tencent": tq,
        "baostock": bv,
        "announcements": anns,
        "news": news,
        "northbound": nb,
        "concepts": concepts,
        "insiders": insiders,
        "hotStocks": hot,
        "globalMarkets": global_mkts,
    }
    stored = store_snapshot(snapshot)
    emit_status({
        "runId": run_id,
        "status": stored["status"],
        "target": q["date"],
        "qlibTag": q["generation"]["tag"],
        "generatedAt": snapshot["generatedAt"],
        "sourceDates": {
            "qlib": q["date"],
            "akshareActivity": activity_date,
        },
        "coverage": q["coverage"],
        "payloadSha256": stored["payloadSha256"],
        "quickCheck": stored.get("quickCheck"),
        "durationSeconds": round(time.monotonic() - started_monotonic, 3),
    })
    return 0


def main(run_id=None, started_monotonic=None):
    run_id = run_id or uuid.uuid4().hex
    started_monotonic = started_monotonic or time.monotonic()
    with snapshot_run_lock():
        return run_snapshot(run_id, started_monotonic)


if __name__ == "__main__":
    _run_id = uuid.uuid4().hex
    _started_monotonic = time.monotonic()
    try:
        raise SystemExit(main(_run_id, _started_monotonic))
    except SnapshotUsageError as exc:
        emit_status({
            "runId": _run_id,
            "status": "usage_error",
            "reason": str(exc),
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(64)
    except SnapshotTemporaryError as exc:
        emit_status({
            "runId": _run_id,
            "status": "temporary_failure",
            "reason": str(exc),
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(75)
    except (QlibDateError, SnapshotValidationError) as exc:
        emit_status({
            "runId": _run_id,
            "status": "rejected",
            "reason": str(exc),
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        emit_status({
            "runId": _run_id,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "durationSeconds": round(time.monotonic() - _started_monotonic, 3),
        }, stream=sys.stderr)
        raise SystemExit(1)
