"""Pure, versioned core artifact validation. No collectors, I/O or environment changes."""
import datetime as dt
import hashlib
import json
import re

SCHEMA = "research.core-market.v1"
LEGACY_SCHEMA = "research.full-snapshot.v1"
CORE_SOURCES = ["qlib"]
APPENDIX_SOURCES = ["akshareMarket", "lhb", "spot", "fundFlow", "lhbOfficial", "mootdx", "tencent",
                    "baostock", "announcements", "news", "northbound", "concepts",
                    "insiders", "hotStocks", "globalMarkets"]


def encode(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def date(value):
    if not isinstance(value, str) or dt.date.fromisoformat(value).isoformat() != value:
        raise ValueError("noncanonical trade date")
    return value


def instant(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def validate_calendar_evidence(evidence, now):
    """Validate frozen local calendar identity/freshness, not its remote provider."""
    if not isinstance(evidence, dict) or set(evidence) != {"document", "binding", "sha256"}:
        raise ValueError("calendar evidence binding missing")
    doc = evidence["document"]
    required = {"schemaVersion", "targetDate", "status", "previousTradingDate", "nextTradingDate",
                "sourceIds", "observedAt", "validUntil"}
    if not isinstance(doc, dict) or set(doc) != required or type(doc["schemaVersion"]) is not int or doc["schemaVersion"] != 1:
        raise ValueError("invalid calendar evidence schema")
    binding = evidence["binding"]
    if not isinstance(binding, dict):
        raise ValueError("invalid calendar binding")
    if binding.get("kind") == "validated_closed_interval":
        return _validate_closed_interval(evidence, now)
    if set(binding) == {"archiveDate", "inputSha256", "cutoffInstant", "publishedStatus"}:
        if (binding["publishedStatus"] != "sealed" or doc["status"] != "open"
                or not isinstance(binding["inputSha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", binding["inputSha256"])):
            raise ValueError("invalid calendar archive binding")
        bound_date, cutoff = binding["archiveDate"], instant(binding["cutoffInstant"])
    elif set(binding) == {"kind", "targetDate", "contextSha256", "lastRunSha256", "stateSha256", "completedAt"}:
        if (binding["kind"] != "validated_non_trading_skip" or doc["status"] != "closed"
                or any(not isinstance(binding[k], str) or not re.fullmatch(r"[0-9a-f]{64}", binding[k])
                       for k in ("contextSha256", "lastRunSha256", "stateSha256"))):
            raise ValueError("invalid calendar skip binding")
        bound_date, cutoff = binding["targetDate"], instant(binding["completedAt"])
    elif set(binding) - {"stateSchemaVersion", "stateFrozenCutoffInstant"} == {"kind", "targetDate", "contextSha256", "lastRunSha256", "stateSha256",
                           "invocationId", "cutoffInstant", "occurrenceStartedAt", "occurrenceCompletedAt",
                           "runStartedAt", "runCompletedAt", "stateStatus", "runStatus", "childExitCode"}:
        if (binding["kind"] != "validated_terminal_context" or doc["status"] != "open"
                or any(not isinstance(binding[k], str) or not re.fullmatch(r"[0-9a-f]{64}", binding[k])
                       for k in ("contextSha256", "lastRunSha256", "stateSha256"))
                or not isinstance(binding["invocationId"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", binding["invocationId"])
                or type(binding["childExitCode"]) is not int
                or not ((binding["stateStatus"] == "success" and binding["runStatus"] == "success"
                         and binding["childExitCode"] == 0)
                        or (binding["stateStatus"] == "failed" and binding["runStatus"] in {"failed", "blocked"}
                            and binding["childExitCode"] > 0)
                        or (binding["stateStatus"] == "blocked" and binding["runStatus"] == "blocked"
                            and binding["childExitCode"] == 2))):
            raise ValueError("invalid terminal calendar context binding")
        bound_date, cutoff = binding["targetDate"], instant(binding["cutoffInstant"])
        started, completed, run_started, run_completed = (instant(binding[k]) for k in (
            "occurrenceStartedAt", "occurrenceCompletedAt", "runStartedAt", "runCompletedAt"))
        if "stateSchemaVersion" in binding or "stateFrozenCutoffInstant" in binding:
            canonical = bound_date + "T00:00:00.000Z"
            cutoff_bound = (binding.get("stateSchemaVersion") == "2.0"
                            and binding.get("stateFrozenCutoffInstant") == canonical
                            and binding["cutoffInstant"] == canonical and cutoff <= started)
        else:
            cutoff_bound = cutoff == started  # immutable legacy v1 evidence
        if (not cutoff_bound or not started <= run_started <= run_completed <= completed <= now
                or any(t.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat() != bound_date
                       for t in (started, completed, run_started, run_completed))):
            raise ValueError("terminal calendar context run time mismatch")
    else:
        raise ValueError("invalid calendar binding fields")
    if digest({"document": doc, "binding": binding}) != evidence["sha256"]:
        raise ValueError("calendar evidence hash mismatch")
    target, previous, following = (date(doc[k]) for k in ("targetDate", "previousTradingDate", "nextTradingDate"))
    if (doc["status"] not in {"open", "closed"} or not previous < target < following
            or not isinstance(doc["sourceIds"], list) or not doc["sourceIds"]
            or any(not isinstance(x, str) for x in doc["sourceIds"])
            or "akshare:sina:trade-calendar" not in doc["sourceIds"]):
        raise ValueError("invalid calendar day/source identity")
    observed, until = instant(doc["observedAt"]), instant(doc["validUntil"])
    if (bound_date != target or cutoff.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat() != target
            or now.tzinfo is None or not observed <= cutoff <= now <= until or now - observed > dt.timedelta(hours=24)):
        raise ValueError("calendar evidence stale or clock invalid")
    if now.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat() != target:
        raise ValueError("calendar evidence is not for current Shanghai day")
    return doc


def _validate_closed_interval(evidence, now):
    """Only derive closed days between two explicitly known trading days.

    Keep the original observation and its binding. Freshness is checked at the
    original validation instant; the closed interval expires at the next open
    day. It can never authorize an open-day write.
    """
    doc, binding = evidence["document"], evidence["binding"]
    if set(binding) != {"kind", "sourceEvidence", "derivedAt"}:
        raise ValueError("invalid closed interval binding")
    source = binding["sourceEvidence"]
    if (not isinstance(source, dict) or not isinstance(source.get("binding"), dict)
            or source["binding"].get("kind") == "validated_closed_interval"):
        raise ValueError("nested or missing interval source")
    original_binding = source["binding"]
    # A terminal context includes completion after its frozen cutoff. Validate
    # that tuple at its completion, not before the terminal evidence existed.
    source_at = original_binding.get("occurrenceCompletedAt",
                                    original_binding.get("cutoffInstant", original_binding.get("completedAt")))
    original = validate_calendar_evidence(source, instant(source_at))
    target = date(doc["targetDate"])
    source_day = date(original["targetDate"])
    previous = source_day if original["status"] == "open" else original["previousTradingDate"]
    following = original["nextTradingDate"]
    cst = dt.timezone(dt.timedelta(hours=8))
    until = dt.datetime.combine(dt.date.fromisoformat(following), dt.time(), cst) - dt.timedelta(microseconds=1)
    derived = instant(binding["derivedAt"])
    if (not source_day < target < following
            or not previous < target
            or target[:4] != source_day[:4] or following[:4] != source_day[:4]
            or (dt.date.fromisoformat(following) - dt.date.fromisoformat(previous)).days > 31
            or now.tzinfo is None or not instant(source_at) <= derived <= now <= until
            or derived.astimezone(cst).date().isoformat() != target
            or now.astimezone(cst).date().isoformat() != target):
        raise ValueError("closed interval expired or outside proven calendar range")
    expected = {"schemaVersion": 1, "targetDate": target, "status": "closed",
                "previousTradingDate": previous, "nextTradingDate": following,
                "sourceIds": original["sourceIds"], "observedAt": original["observedAt"],
                "validUntil": until.isoformat()}
    if doc != expected or digest({"document": doc, "binding": binding}) != evidence["sha256"]:
        raise ValueError("closed interval identity mismatch")
    return doc


def validate_core(record):
    if not isinstance(record, dict) or set(record) != {
        "schema", "scope", "fullSnapshot", "date", "generatedAt", "runId",
        "requiredSources", "appendix", "qlib", "artifactKind", "includesCompletePriceArchive", "calendarEvidence",
    }:
        raise ValueError("invalid core schema fields")
    if (record["schema"] != SCHEMA or record["scope"] != "core_market"
            or record["fullSnapshot"] is not False or record["requiredSources"] != CORE_SOURCES
            or record["artifactKind"] != "qlib_market_summary" or record["includesCompletePriceArchive"] is not False
            or record["appendix"] != {"status": "not_requested", "sources": APPENDIX_SOURCES}):
        raise ValueError("core scope declaration mismatch")
    day, generated = date(record["date"]), instant(record["generatedAt"])
    calendar = validate_calendar_evidence(record["calendarEvidence"], generated)
    if calendar["status"] != "open" or calendar["targetDate"] != day:
        raise ValueError("core target not authorized by open calendar evidence")
    if not isinstance(record["runId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", record["runId"]):
        raise ValueError("invalid core run id")
    q = record["qlib"]
    if not isinstance(q, dict) or q.get("date") != day:
        raise ValueError("core/Qlib date mismatch")
    generation = q.get("generation") or {}
    if not isinstance(generation.get("tag"), str) or not generation["tag"]:
        raise ValueError("Qlib generation missing")
    c, b = q.get("coverage") or {}, q.get("breadth") or {}
    if any(type(c.get(k)) is not int or c[k] < 0 for k in ("valid", "eligible", "invalid")):
        raise ValueError("invalid Qlib coverage counts")
    if c["eligible"] < 5000 or c["valid"] + c["invalid"] != c["eligible"]:
        raise ValueError("Qlib population/count mismatch")
    ratio = c["valid"] / c["eligible"]
    if ratio < 0.995 or c.get("ratio") != round(ratio, 6):
        raise ValueError("Qlib coverage gate failed")
    if any(type(b.get(k)) is not int or b[k] < 0 for k in ("up", "down", "flat", "total")):
        raise ValueError("invalid Qlib breadth")
    if b["up"] + b["down"] + b["flat"] != b["total"] or b["total"] != c["valid"]:
        raise ValueError("Qlib breadth/count mismatch")
    if generated.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat() < day:
        raise ValueError("Qlib summary generated before market day")
    encode(record)
    return record
