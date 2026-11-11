"""Pure local calendar evidence reader shared by writers and the audit.
No collector imports, business execution, network requests or writes.
"""
import hashlib
import datetime as dt
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
def _sibling(filename):
    spec = importlib.util.spec_from_file_location("_calendar_" + filename[:-3], Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_contract = _sibling("core_contract.py")
digest, _instant, validate_calendar_evidence = _contract.digest, _contract.instant, _contract.validate_calendar_evidence
snapshot_contract = _sibling("snapshot_contract.py")


def strict_json(raw):
    def reject(_):
        raise ValueError("non_finite_json")
    def number(value):
        result = float(value)
        if not math.isfinite(result): raise ValueError("non_finite_json")
        return result
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=reject, parse_float=number, object_pairs_hook=unique)


def _assert_publication_absent(project_root, archive, today):
    """Recheck the exact fallback precondition without following symlinks."""
    if archive != project_root / "work/report-archives" / today:
        raise ValueError("unexpected unpublished archive path")
    if project_root.resolve() != project_root.absolute():
        raise ValueError("symlinked calendar project root")
    # The mandatory producer directories must still exist after file re-reads.
    # Their disappearance is not evidence that publication has never happened.
    for directory in (project_root, project_root / "work"):
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("invalid calendar project directory")
    # Only missing optional archive paths mean absent. lstat rejects even
    # dangling links; permission errors and non-directories must fail closed.
    for directory in (archive.parent, archive):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("invalid unpublished archive directory")
    try:
        (archive / "published.json").lstat()
    except FileNotFoundError:
        return
    raise ValueError("publication appeared during calendar read")


def _open_context_evidence(read, today, now, state_raw, last_raw, *, project_root, unpublished_archive):
    """Read already-collected calendar evidence, never run or approve publication."""
    _assert_publication_absent(project_root, unpublished_archive, today)
    state, last = strict_json(state_raw), strict_json(last_raw)
    version = state.get("schemaVersion")
    if last.get("schemaVersion") != "1.0":
        raise ValueError("unsupported terminal calendar run schema")
    if version == "2.0":
        canonical_cutoff = today + "T00:00:00.000Z"
        if (state.get("frozenCutoffInstant") != canonical_cutoff
                or last.get("frozenCutoffInstant") != canonical_cutoff):
            raise ValueError("invalid v2 terminal calendar frozen cutoff")
    elif version != "1.0" or "frozenCutoffInstant" in state:
        raise ValueError("unsupported terminal calendar state schema")
    if (state.get("task") != "morning-report" or state.get("targetDate") != today
            or state.get("timedOut") is not False or state.get("interrupted") is not False
            or last.get("targetDate") != today
            or last.get("phase") not in {"collect", "validate", "analysis", "generate", "review", "publish"}
            or last.get("publicationGate") != ("passed" if last.get("status") == "success" else "blocked")):
        raise ValueError("unbound terminal calendar run")
    # This is the one shared pre-publication file, not an arbitrary caller path.
    if last.get("contextFile") != "work/morning-context.json":
        raise ValueError("invalid terminal calendar context path")
    context_raw = read("work/morning-context.json", 16 * 1024 * 1024)
    context_hash = hashlib.sha256(context_raw).hexdigest()
    if context_hash != last.get("contextSha256"):
        raise ValueError("terminal calendar context hash mismatch")
    context = strict_json(context_raw)
    calendar, meta = context["data"]["tradingCalendar"], context["meta"]
    if (calendar["targetDate"] != today or calendar["status"] != "trading_day"
            or meta.get("targetDate") != today or meta.get("timezone") != "Asia/Shanghai"):
        raise ValueError("terminal calendar day/status mismatch")
    source_id = "akshare:sina:trade-calendar"
    sources = context.get("sources")
    if not isinstance(sources, list):
        raise ValueError("terminal calendar source registry missing")
    matching = [s for s in sources if isinstance(s, dict) and s.get("id") == source_id]
    if (len(matching) != 1 or calendar.get("sourceIds") != [source_id]
            or matching[0].get("dataAsOf") != calendar["dataAsOf"]
            or matching[0].get("url") != "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"):
        raise ValueError("terminal calendar source identity mismatch")
    completed_key = "failedAt" if last.get("status") == "failed" else "completedAt"
    binding = {"kind": "validated_terminal_context", "targetDate": today,
               "contextSha256": context_hash, "lastRunSha256": hashlib.sha256(last_raw).hexdigest(),
               "stateSha256": hashlib.sha256(state_raw).hexdigest(), "invocationId": state["invocationId"],
               "cutoffInstant": last["frozenCutoffInstant"], "occurrenceStartedAt": state["startedAt"],
               "occurrenceCompletedAt": state["completedAt"], "runStartedAt": last["startedAt"],
               "runCompletedAt": last[completed_key], "stateStatus": state["status"],
               "runStatus": last["status"], "childExitCode": state["childExitCode"]}
    if version == "2.0":
        binding.update(stateSchemaVersion=version, stateFrozenCutoffInstant=state["frozenCutoffInstant"])
    # The bridge declares second-resolution meta.asOf; do not interpret its
    # truncation as a subsecond occurrence mismatch.
    if not _instant(last["startedAt"]).replace(microsecond=0) <= _instant(meta["asOf"]) <= _instant(last[completed_key]):
        raise ValueError("terminal context creation outside run")
    document = {"schemaVersion": 1, "targetDate": today, "status": "open",
                "previousTradingDate": calendar["previousTradingDate"], "nextTradingDate": calendar["nextTradingDate"],
                "sourceIds": calendar["sourceIds"], "observedAt": calendar["dataAsOf"],
                "validUntil": today + "T23:59:59.999999+08:00"}
    evidence = {"document": document, "binding": binding}
    evidence["sha256"] = digest(evidence)
    validate_calendar_evidence(evidence, now)
    # A later run can atomically replace each shared file independently. Do not
    # accept a tuple that changed while this reader was assembling it.
    if (read("work/last-run.json", 4 * 1024 * 1024) != last_raw
            or read("work/orchestrator/states/morning-report-" + today + ".json", 4 * 1024 * 1024) != state_raw
            or read("work/morning-context.json", 16 * 1024 * 1024) != context_raw):
        raise ValueError("terminal calendar evidence changed during read")
    _assert_publication_absent(project_root, unpublished_archive, today)
    return evidence


def _load_current_evidence(path, now):
    """Read a sealed archive, or same-day hash-bound terminal AStock context.

    This adapter never invokes AStock. An existing invalid archive cannot fall
    back to a context tuple. If publication has not produced a published.json,
    collected calendar evidence can survive report failure. Shared collector
    failure still defers; this adapter is publication-independent, not a second
    independent trading-calendar provider.
    """
    if path is None:
        raise ValueError("independent trading calendar evidence not configured")
    path = Path(path)
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise ValueError("independent trading calendar evidence unavailable")
    try:
        today = now.astimezone(snapshot_contract.CST).date().isoformat()
        root = None
        if path.name != today:
            root = path
            archive = root / "work/report-archives" / today
            if archive.exists() or archive.is_symlink():
                if archive.is_symlink() or not archive.is_dir() or archive.resolve() != archive.absolute():
                    raise ValueError("invalid current archive")
                published = archive / "published.json"
                path = archive if published.exists() or published.is_symlink() else root
            else:
                path = root
        def read(name, limit):
            # Do not follow directory symlinks into unrelated project data.
            if any((path / parent).is_symlink() for parent in Path(name).parents if str(parent) != "."):
                raise ValueError("symlinked calendar path")
            fd = os.open(path / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                    raise ValueError("invalid calendar archive file")
                value = stream.read(limit + 1)
                if len(value) > limit:
                    raise ValueError("calendar archive file too large")
                return value
        if root is not None and path == root:
            state_raw = read("work/orchestrator/states/morning-report-" + today + ".json", 4 * 1024 * 1024)
            last_raw = read("work/last-run.json", 4 * 1024 * 1024)
            state, last = strict_json(state_raw), strict_json(last_raw)
            if state.get("status") != "skipped":
                return _open_context_evidence(read, today, now, state_raw, last_raw,
                                             project_root=root, unpublished_archive=archive)
            _assert_publication_absent(root, archive, today)
            if (state.get("task") != "morning-report" or state.get("targetDate") != today
                    or state.get("status") != "skipped" or type(state.get("childExitCode")) is not int
                    or state["childExitCode"] != 0 or state.get("timedOut") is True or state.get("interrupted") is True
                    or last.get("targetDate") != today or last.get("status") != "skipped"
                    or last.get("phase") != "collect" or last.get("tradingStatus") != "non_trading_day"
                    or last.get("publicationGate") != "not_applicable"):
                raise ValueError("unproven closed branch")
            context_file = last.get("contextFile")
            if (not isinstance(context_file, str) or not re.fullmatch(r"work/[A-Za-z0-9._/-]+\.json", context_file)
                    or ".." in Path(context_file).parts):
                raise ValueError("invalid calendar context path")
            context_raw = read(context_file, 16 * 1024 * 1024)
            context_hash = hashlib.sha256(context_raw).hexdigest()
            if context_hash != last.get("contextSha256"):
                raise ValueError("calendar context hash mismatch")
            calendar = strict_json(context_raw)["data"]["tradingCalendar"]
            if calendar["targetDate"] != today or calendar["status"] != "non_trading_day":
                raise ValueError("calendar skip day/status mismatch")
            state_time, last_time = _instant(state["completedAt"]), _instant(last["completedAt"])
            if (not last_time <= state_time <= now or
                    any(t.astimezone(snapshot_contract.CST).date().isoformat() != today for t in (state_time, last_time))):
                raise ValueError("calendar skip time mismatch")
            document = {"schemaVersion": 1, "targetDate": today, "status": "closed",
                        "previousTradingDate": calendar["previousTradingDate"], "nextTradingDate": calendar["nextTradingDate"],
                        "sourceIds": calendar["sourceIds"], "observedAt": calendar["dataAsOf"],
                        "validUntil": today + "T23:59:59.999999+08:00"}
            evidence = {"document": document, "binding": {"kind": "validated_non_trading_skip", "targetDate": today,
                        "contextSha256": context_hash, "lastRunSha256": hashlib.sha256(last_raw).hexdigest(),
                        "stateSha256": hashlib.sha256(state_raw).hexdigest(), "completedAt": last["completedAt"]}}
            evidence["sha256"] = digest(evidence)
            validate_calendar_evidence(evidence, now)
            _assert_publication_absent(root, archive, today)
            if (read("work/orchestrator/states/morning-report-" + today + ".json", 4 * 1024 * 1024) != state_raw
                    or read("work/last-run.json", 4 * 1024 * 1024) != last_raw
                    or read(context_file, 16 * 1024 * 1024) != context_raw):
                raise ValueError("calendar skip evidence changed during read")
            return evidence
        raw = read("input.json", 16 * 1024 * 1024)
        frozen = strict_json(raw)
        actual = hashlib.sha256(raw).hexdigest()
        publication = path / "published.json"
        if publication.is_symlink():
            raise ValueError("invalid calendar publication record")
        publication_absent = not publication.exists()
        if publication_absent:
            if (read("input.sha256", 256).decode().strip() != actual
                    or frozen.get("targetDate") != path.name):
                raise ValueError("calendar archive identity mismatch")
        else:
            published = strict_json(read("published.json", 8192))
            if (read("input.sha256", 256).decode().strip() != actual
                    or published.get("status") != "sealed" or published.get("inputSha256") != actual
                    or frozen.get("targetDate") != path.name):
                raise ValueError("calendar archive identity mismatch")
        calendar = frozen["compilerContext"]["data"]["tradingCalendar"]
        if calendar != frozen["rawContext"]["data"]["tradingCalendar"] or calendar["targetDate"] != path.name:
            raise ValueError("frozen calendar contexts disagree")
        status = {"trading_day": "open", "non_trading_day": "closed"}.get(calendar["status"])
        document = {"schemaVersion": 1, "targetDate": path.name, "status": status,
                    "previousTradingDate": calendar["previousTradingDate"], "nextTradingDate": calendar["nextTradingDate"],
                    "sourceIds": calendar["sourceIds"], "observedAt": calendar["dataAsOf"],
                    "validUntil": path.name + "T23:59:59.999999+08:00"}
        binding = ({"kind": "archived_input_publication_absent", "archiveDate": path.name,
                    "inputSha256": actual, "cutoffInstant": frozen["cutoffInstant"]}
                   if publication_absent else
                   {"archiveDate": path.name, "inputSha256": actual,
                    "cutoffInstant": frozen["cutoffInstant"], "publishedStatus": "sealed"})
        evidence = {"document": document, "binding": binding}
        evidence["sha256"] = digest(evidence)
        validate_calendar_evidence(evidence, now)
        if (read("input.json", 16 * 1024 * 1024) != raw
                or read("input.sha256", 256).decode().strip() != actual
                or publication.is_symlink()
                or publication.exists() == publication_absent):
            raise ValueError("calendar archive changed during read")
        return evidence
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("independent trading calendar evidence invalid or stale") from None


def _optional_directory(root, relative):
    directory = root
    for part in Path(relative).parts:
        directory = directory / part
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(mode):
            raise ValueError("invalid calendar directory")
    return directory


def _optional_file(root, relative, limit):
    """Only ENOENT means absent; bad files or symlinked parents are errors."""
    path = root / relative
    if _optional_directory(root, str(Path(relative).parent)) is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        # O_NOFOLLOW may return ENOENT for a dangling link on some platforms.
        if path.is_symlink():
            raise ValueError("symlinked calendar file")
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError("invalid calendar file")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("calendar file too large")
        return raw


def _current_material(root, today):
    """A bad or unfinished current run must not disappear behind older proof."""
    for directory in (root, root / "work"):
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("invalid calendar project directory")
    archive = root / "work/report-archives" / today
    # Check optional parents even when the day's child is absent.
    _optional_file(root, "work/report-archives/" + today + "/published.json", 8192)
    try:
        archive.lstat()
    except FileNotFoundError:
        pass
    else:
        return True
    state = _optional_file(root, "work/orchestrator/states/morning-report-" + today + ".json", 4 * 1024 * 1024)
    if state is not None:
        return True
    last = _optional_file(root, "work/last-run.json", 4 * 1024 * 1024)
    if last is not None and strict_json(last).get("targetDate") == today:
        return True
    context = _optional_file(root, "work/morning-context.json", 16 * 1024 * 1024)
    if context is not None:
        value = strict_json(context)
        if (value.get("meta", {}).get("targetDate") == today
                or value.get("data", {}).get("tradingCalendar", {}).get("targetDate") == today):
            return True
    return False


def _latest_source_day(root, today):
    """Choose the newest recorded date, never skip a damaged newer record."""
    days = set()
    for relative, pattern in (("work/report-archives", r"(\d{4}-\d{2}-\d{2})"),
                              ("work/orchestrator/states", r"morning-report-(\d{4}-\d{2}-\d{2})\.json")):
        directory = _optional_directory(root, relative)
        if directory is None:
            continue
        entries = list(directory.iterdir())
        for entry in entries:
            match = re.fullmatch(pattern, entry.name)
            if match:
                day = _contract.date(match[1])
                if day > today:
                    raise ValueError("future calendar material")
                if day < today:
                    days.add(day)
    raw = _optional_file(root, "work/last-run.json", 4 * 1024 * 1024)
    if raw is not None:
        day = strict_json(raw).get("targetDate")
        if day is not None:
            day = _contract.date(day)
            if day > today:
                raise ValueError("future calendar material")
            if day < today:
                days.add(day)
    context = _optional_file(root, "work/morning-context.json", 16 * 1024 * 1024)
    if context is not None:
        value = strict_json(context)
        declared = {day for day in (value.get("meta", {}).get("targetDate"),
                    value.get("data", {}).get("tradingCalendar", {}).get("targetDate")) if day is not None}
        if len(declared) != 1:
            raise ValueError("calendar context day missing or contradictory")
        day = _contract.date(declared.pop())
        if day > today:
            raise ValueError("future calendar material")
        if day < today:
            days.add(day)
    if not days:
        raise ValueError("no validated calendar interval source")
    latest = max(days)
    if (dt.date.fromisoformat(today) - dt.date.fromisoformat(latest)).days > 31:
        raise ValueError("calendar interval source too old")
    return latest


def load_calendar_evidence(path, now):
    """Read today's proof, or derive only a closed day from a frozen interval.

    The original provider observation stays unchanged. This uses the same
    contract and readers for full/core/audit; no calendar service is invoked.
    """
    if path is None:
        raise ValueError("independent trading calendar evidence not configured")
    root = Path(path)
    today = now.astimezone(snapshot_contract.CST).date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", root.name):
        return _load_current_evidence(root, now)
    if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
        raise ValueError("independent trading calendar evidence unavailable")
    try:
        if _current_material(root, today):
            return _load_current_evidence(root, now)
        source_day = _latest_source_day(root, today)
        source_now = dt.datetime.combine(dt.date.fromisoformat(source_day), dt.time(23, 59, 59, 999999), snapshot_contract.CST)
        source = _load_current_evidence(root, source_now)
        original = source["document"]
        previous = source_day if original["status"] == "open" else original["previousTradingDate"]
        until = dt.datetime.combine(dt.date.fromisoformat(original["nextTradingDate"]), dt.time(), snapshot_contract.CST) - dt.timedelta(microseconds=1)
        evidence = {"document": {"schemaVersion": 1, "targetDate": today, "status": "closed",
                    "previousTradingDate": previous, "nextTradingDate": original["nextTradingDate"],
                    "sourceIds": original["sourceIds"], "observedAt": original["observedAt"],
                    "validUntil": until.isoformat()},
                    "binding": {"kind": "validated_closed_interval", "sourceEvidence": source,
                                "derivedAt": now.isoformat()}}
        evidence["sha256"] = digest(evidence)
        validate_calendar_evidence(evidence, now)
        if (_current_material(root, today) or _latest_source_day(root, today) != source_day
                or _load_current_evidence(root, source_now) != source):
            raise ValueError("calendar interval evidence changed during read")
        return evidence
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("independent trading calendar evidence invalid or stale") from None


def archived_open_days(project_root, since, before, *, required_previous, expected_next=None,
                       known_last_day=None):
    """Read prior sealed day proofs without deriving trading days from Qlib.

    Historical runs and weekday arithmetic are not calendar evidence. Each
    accepted day passes the same sealed-input binding checks as today's reader;
    invalid archives remain explicit evidence failures and never become open days.
    """
    root = Path(project_root)
    since, before = _contract.date(since), _contract.date(before)
    result = {"openDays": [], "proofs": {}, "invalidDates": []}
    day = _contract.date(required_previous)
    following = _contract.date(expected_next) if expected_next is not None else None
    known = _contract.date(known_last_day) if known_last_day is not None else None
    boundary_next = ((dt.date.fromisoformat(known) + dt.timedelta(days=1)).isoformat()
                     if known is not None else None)

    def finish():
        if known is not None and known < before and since <= boundary_next and day != known:
            raise ValueError("historical calendar chain conflicts with known calendar boundary")
        result["openDays"].sort()
        return result

    if day >= before:
        raise ValueError("historical calendar chain starts outside the prior-day window")
    if day < since:
        return finish()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
        raise ValueError("independent trading calendar evidence unavailable")
    while day >= since:
        archive = root / "work/report-archives" / day
        # Missing required archives must not erase already-known failures.
        # Walk only the predecessor chain proved by the current calendar,
        # requiring every link until Qlib's known calendar boundary is reached.
        when = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(23, 59, 59, 999999),
                                   snapshot_contract.CST)
        evidence = _load_current_evidence(archive, when)
        document = evidence["document"]
        if document["status"] != "open" or (following is not None and document["nextTradingDate"] != following):
            raise ValueError("historical calendar chain conflict")
        result["openDays"].append(day)
        result["proofs"][day] = {"calendarEvidenceSha256": evidence["sha256"],
                                "inputSha256": evidence["binding"]["inputSha256"]}
        following, day = day, document["previousTradingDate"]
    return finish()
