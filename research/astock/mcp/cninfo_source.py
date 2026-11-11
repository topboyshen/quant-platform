"""Bounded, read-only CNINFO collection; no proxy, storage, or fallback source."""
import datetime as dt
import json
import re
import time

import requests

try:
    from curl_cffi.requests.errors import RequestsError as CurlRequestsError
except ImportError:
    CurlRequestsError = ()  # type: ignore[misc, assignment]


STOCK_MAP_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
ANNOUNCEMENTS_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CST = dt.timezone(dt.timedelta(hours=8))
VALIDATION_REASONS = {
    "document_shape", "total_count", "page_incomplete", "row_shape",
    "title_or_time_type", "date_value", "date_out_of_range",
    "future_disclosure_evidence",
}
ERROR_TYPES = {
    "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError", "SSLError",
    "RequestException", "HTTPStatus", "InvalidJSON", "InvalidSchema",
    "UnknownSecurity", "SecurityMismatch", "InvalidInput",
    "CutoffReached", "BudgetReached",
}
TRANSPORT_ERRORS = (requests.RequestException, CurlRequestsError) if CurlRequestsError else (requests.RequestException,)
CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.cninfo.com.cn/new/disclosure",
    "Origin": "https://www.cninfo.com.cn",
}


def build_cninfo_session():
    """CNINFO CDN blocks default TLS fingerprints as bots; Chrome impersonation is required."""
    try:
        from curl_cffi import requests as impersonated
        session = impersonated.Session(impersonate="chrome120")
        session.trust_env = False
        # Do not override the impersonated User-Agent; a generic Mozilla UA is classified as a bot.
        session.headers.update({
            "Referer": CNINFO_HEADERS["Referer"],
            "Origin": CNINFO_HEADERS["Origin"],
        })
        return session
    except (ImportError, TypeError, ValueError):
        session = requests.Session()
        session.trust_env = False
        session.headers.update(CNINFO_HEADERS)
        return session


class CninfoError(RuntimeError):
    """Contains allowlisted diagnostics, never raw exceptions, bodies or headers."""

    def __init__(self, stage, error_type, code=None, attempt=1, http_status=None,
                 *, validation_reason=None):
        self.diagnostic = {"source": "cninfo", "stage": stage,
                           "errorType": error_type, "attempt": attempt}
        if code is not None and re.fullmatch(r"[0-9]{6}", str(code)):
            self.diagnostic["code"] = code
        if http_status is not None:
            self.diagnostic["httpStatus"] = http_status
        if isinstance(validation_reason, str) and validation_reason in VALIDATION_REASONS:
            self.diagnostic["validationReason"] = validation_reason
        super().__init__(json.dumps(self.diagnostic, sort_keys=True))


def safe_diagnostic(value):
    """Do not promote untrusted source text into operational logs."""
    if not isinstance(value, dict) or value.get("source") != "cninfo":
        return None
    if not isinstance(value.get("stage"), str) or value["stage"] not in {
            "stock_map", "announcements", "input"}:
        return None
    if not isinstance(value.get("errorType"), str) or value["errorType"] not in ERROR_TYPES:
        return None
    result = {key: value[key] for key in ("source", "stage", "errorType")}
    if isinstance(value.get("code"), str) and re.fullmatch(r"[0-9]{6}", value["code"]):
        result["code"] = value["code"]
    for key, lower, upper in (("attempt", 1, 3), ("httpStatus", 100, 599)):
        if type(value.get(key)) is int and lower <= value[key] <= upper:
            result[key] = value[key]
    if (isinstance(value.get("validationReason"), str)
            and value["validationReason"] in VALIDATION_REASONS):
        result["validationReason"] = value["validationReason"]
    return result


def _now_cst():
    return dt.datetime.now(CST)


def _next_day_disclosure_evidence(row, declared_at, org_id):
    """Accept only an identified official next-day date label, not a future event."""
    announcement_id = row.get("announcementId")
    return (
        declared_at.time() == dt.time(0)
        and row.get("orgId") == org_id
        and isinstance(announcement_id, str)
        and re.fullmatch(r"[0-9]{1,32}", announcement_id) is not None
        and isinstance(row.get("adjunctUrl"), str)
        and row["adjunctUrl"] in {
            f"finalpage/{declared_at.date().isoformat()}/{announcement_id}.PDF",
            f"finalpage/{declared_at.date().isoformat()}/{announcement_id}.pdf",
        }
    )


class CninfoClient:
    def __init__(self):
        self.session = build_cninfo_session()
        self.session.trust_env = False
        self.org_ids = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def request_json(self, method, url, stage, code=None, *, not_after=None, deadline=None, **kwargs):
        for attempt in range(1, 4):
            remaining = None
            if not_after is not None:
                remaining = (not_after - _now_cst()).total_seconds()
                if remaining <= 0:
                    raise CninfoError(stage, "CutoffReached", code, attempt)
            if deadline is not None:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise CninfoError(stage, "BudgetReached", code, attempt)
                remaining = budget if remaining is None else min(remaining, budget)
            timeout = (5, 15) if remaining is None else (min(5, remaining / 2), min(15, remaining / 2))
            retry = False
            try:
                response = self.session.request(method, url, timeout=timeout,
                                                allow_redirects=False, **kwargs)
                closer = getattr(response, "close", None)
                try:
                    status = response.status_code
                    if status != 200:
                        error = CninfoError(stage, "HTTPStatus", code, attempt, status)
                        retry = status == 429 or 500 <= status <= 599
                    else:
                        try:
                            return response.json()
                        except ValueError:
                            raise CninfoError(stage, "InvalidJSON", code, attempt, status) from None
                finally:
                    if callable(closer):
                        closer()
            except TRANSPORT_ERRORS as exc:
                error_type = type(exc).__name__
                if error_type not in ERROR_TYPES:
                    error_type = "RequestException"
                error = CninfoError(stage, error_type, code, attempt)
                retry = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or error_type in {
                    "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError",
                }
            if not retry or attempt == 3:
                raise error from None
            time.sleep(attempt)

    def load_org_ids(self, code, *, not_after=None, deadline=None):
        if self.org_ids is not None:
            return
        document = self.request_json("GET", STOCK_MAP_URL, "stock_map", code,
                                     not_after=not_after, deadline=deadline)
        rows = document.get("stockList") if isinstance(document, dict) else None
        if not isinstance(rows, list) or not rows:
            raise CninfoError("stock_map", "InvalidSchema", code)
        mapping = {}
        for row in rows:
            if not isinstance(row, dict):
                raise CninfoError("stock_map", "InvalidSchema", code)
            symbol, org_id = row.get("code"), row.get("orgId")
            if (not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol)
                    or not isinstance(org_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", org_id)
                    or (symbol in mapping and mapping[symbol] != org_id)):
                raise CninfoError("stock_map", "InvalidSchema", code)
            mapping[symbol] = org_id
        self.org_ids = mapping

    def announcements(self, code, page_size=10, *, not_after=None, deadline=None):
        """Legacy first-page discovery for historical financial evidence.

        This does not establish current-window completeness. Keep its output
        compatible with already-frozen inputs and PDF discovery callers.
        """
        rows, _, _ = self._announcement_page(code, page_size, not_after=not_after,
                                            deadline=deadline)
        return rows

    def announcement_window(self, code, *, window_start, cutoff, page_size=30,
                            max_pages=4, deadline=None):
        """Query a bounded disclosure-date window without inventing visibility.

        ``complete`` proves all returned pages of the explicit date query were
        read consistently, not an atomic snapshot or filings visible up to
        completion/cutoff. ``queriedAt`` is the last page's completion;
        ``observedThroughAt`` is the earliest page observation, so later pages
        cannot extend earlier pages' visibility. Rows retain their own times.
        Missing/failed pages, changed totals, duplicates and late observations
        leave ``complete=False``; an empty failed query never proves absence.
        """
        if isinstance(window_start, dt.date) and not isinstance(window_start, dt.datetime):
            window_start = dt.datetime.combine(window_start, dt.time(0), CST)
        if (not isinstance(window_start, dt.datetime)
                or window_start.tzinfo is None or window_start.utcoffset() is None
                or not isinstance(cutoff, dt.datetime)
                or cutoff.tzinfo is None or cutoff.utcoffset() is None
                or window_start > cutoff
                or cutoff - window_start > dt.timedelta(days=366)
                or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                or type(page_size) is not int or not 1 <= page_size <= 100
                or type(max_pages) is not int or not 1 <= max_pages <= 8
                or page_size * max_pages > 120):
            raise CninfoError("input", "InvalidInput")
        start = window_start.astimezone(CST)
        end = cutoff.astimezone(CST)
        coverage = {"kind": "window", "windowStartAt": start.isoformat(),
                    "cutoffAt": end.isoformat(), "queriedAt": None,
                    "observedThroughAt": None, "pageSize": page_size,
                    "maxPages": max_pages, "pagesFetched": 0, "total": None,
                    "returned": 0, "complete": False, "reason": "query_not_started"}
        result, identities = [], set()
        for page_num in range(1, max_pages + 1):
            # Do not initiate another network request after the frozen cutoff.
            if _now_cst() > end:
                coverage["reason"] = "cutoff_reached"
                break
            try:
                self.load_org_ids(code, not_after=end, deadline=deadline)
                if _now_cst() > end:
                    coverage["reason"] = "cutoff_reached"
                    break
                rows, total, observed = self._announcement_page(
                    code, page_size, page_num=page_num,
                    date_range=f"{start.date().isoformat()}~{end.date().isoformat()}",
                    not_after=end, deadline=deadline)
            except CninfoError as error:
                coverage["reason"] = ("cutoff_reached" if error.diagnostic.get("errorType") == "CutoffReached"
                                      else "query_failed")
                break
            coverage["queriedAt"] = observed.isoformat()
            if (coverage["observedThroughAt"] is None
                    or observed < dt.datetime.fromisoformat(coverage["observedThroughAt"])):
                coverage["observedThroughAt"] = observed.isoformat()
            coverage["pagesFetched"] += 1
            if observed > end:
                # Do not expose late rows as cutoff-known facts.
                coverage["reason"] = "observed_after_cutoff"
                break
            if coverage["total"] is None:
                coverage["total"] = total
            elif total != coverage["total"]:
                coverage["reason"] = "pagination_changed"
                break
            invalid = False
            for row in rows:
                day = dt.date.fromisoformat(row["date"])
                identity = ((row["url"],) if row.get("url")
                            else (row["title"], row["date"]))
                if not start.date() <= day <= end.date():
                    coverage["reason"] = "window_filter_mismatch"
                    invalid = True
                    break
                if identity in identities:
                    coverage["reason"] = "pagination_duplicate"
                    invalid = True
                    break
                identities.add(identity)
            if invalid:
                break
            result.extend(rows)
            coverage["returned"] = len(result)
            if len(result) == total:
                coverage["complete"] = True
                coverage["reason"] = "all_query_pages_collected"
                break
            coverage["reason"] = "page_limit_reached"
        return {"rows": result, "queryCoverage": coverage}

    def _announcement_page(self, code, page_size, *, page_num=1, date_range="", not_after=None,
                           deadline=None):
        if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                or type(page_size) is not int or not 1 <= page_size <= 100
                or type(page_num) is not int or not 1 <= page_num <= 8):
            raise CninfoError("input", "InvalidInput")
        self.load_org_ids(code, not_after=not_after, deadline=deadline)
        if code not in self.org_ids:
            raise CninfoError("stock_map", "UnknownSecurity", code)
        # Historical discovery remains unfiltered; window queries use seDate.
        payload = {"stock": f"{code},{self.org_ids[code]}", "tabName": "fulltext",
                   "pageSize": str(page_size), "pageNum": str(page_num), "column": "",
                   "category": "", "plate": "", "seDate": date_range, "searchkey": "",
                   "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}
        document = self.request_json("POST", ANNOUNCEMENTS_URL, "announcements", code,
                                     not_after=not_after, deadline=deadline, data=payload)
        # This is the actual observation time, not the issuer's disclosure-date label.
        observed_at = _now_cst()
        if not isinstance(document, dict) or "announcements" not in document:
            raise CninfoError("announcements", "InvalidSchema", code,
                              validation_reason="document_shape")
        rows, total = document["announcements"], document.get("totalAnnouncement")
        if type(total) is not int or total < 0:
            raise CninfoError("announcements", "InvalidSchema", code,
                              validation_reason="total_count")
        if rows is None and total == 0:
            rows = []
        expected = min(page_size, max(0, total - (page_num - 1) * page_size))
        if (not isinstance(rows, list) or len(rows) != expected):
            raise CninfoError("announcements", "InvalidSchema", code,
                              validation_reason="page_incomplete")
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise CninfoError("announcements", "InvalidSchema", code,
                                  validation_reason="row_shape")
            if (row.get("secCode") != code or
                    ("orgId" in row and row["orgId"] != self.org_ids[code])):
                raise CninfoError("announcements", "SecurityMismatch", code)
            title, timestamp = row.get("announcementTitle"), row.get("announcementTime")
            if not isinstance(title, str) or not title.strip() or type(timestamp) not in (int, str):
                raise CninfoError("announcements", "InvalidSchema", code,
                                  validation_reason="title_or_time_type")
            try:
                timestamp = int(timestamp)
                if timestamp > 10**12:
                    timestamp = timestamp / 1000
                declared_at = dt.datetime.fromtimestamp(timestamp, CST)
            except (ValueError, OverflowError, OSError):
                raise CninfoError("announcements", "InvalidSchema", code,
                                  validation_reason="date_value") from None
            date = declared_at.date()
            if not dt.date(1990, 1, 1) <= date <= observed_at.date() + dt.timedelta(days=1):
                raise CninfoError("announcements", "InvalidSchema", code,
                                  validation_reason="date_out_of_range")
            next_day = date > observed_at.date()
            # CNINFO date labels may name tomorrow's disclosure while already publicly
            # observable tonight. Do not infer a next trading day across holidays or
            # backdate this observation; require corroborating identity/attachment data.
            if next_day and not _next_day_disclosure_evidence(
                    row, declared_at, self.org_ids[code]):
                raise CninfoError("announcements", "InvalidSchema", code,
                                  validation_reason="future_disclosure_evidence")
            announcement_id = row.get("announcementId")
            adjunct_url = row.get("adjunctUrl")
            org_id = row.get("orgId") or self.org_ids[code]
            if isinstance(adjunct_url, str) and adjunct_url.startswith("finalpage/"):
                document_url = "https://static.cninfo.com.cn/" + adjunct_url
            elif isinstance(announcement_id, str) and re.fullmatch(r"[0-9]{1,32}", str(announcement_id)):
                document_url = (
                    "https://www.cninfo.com.cn/new/disclosure/detail"
                    f"?orgId={org_id}&announcementId={announcement_id}&stockCode={code}"
                )
            else:
                document_url = None
            item = {"title": title, "date": date.isoformat(),
                    "dateKind": "source_disclosure_date",
                    "observedAt": observed_at.isoformat(),
                    "nextDayDisclosure": next_day}
            if isinstance(document_url, str):
                item["url"] = document_url
            if next_day:
                item["disclosureEvidence"] = {
                    key: row[key] for key in ("orgId", "announcementId", "adjunctUrl")
                }
            result.append(item)
        return result, total, observed_at
