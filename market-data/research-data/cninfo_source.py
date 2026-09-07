"""Bounded, read-only CNINFO collection; no proxy, storage, or fallback source."""
import datetime as dt
import json
import re
import time

import requests


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
}


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
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cninfo.com.cn/new/disclosure",
            "Origin": "https://www.cninfo.com.cn",
        })
        self.org_ids = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def request_json(self, method, url, stage, code=None, **kwargs):
        for attempt in range(1, 4):
            retry = False
            try:
                with self.session.request(method, url, timeout=(5, 15),
                                          allow_redirects=False, **kwargs) as response:
                    status = response.status_code
                    if status != 200:
                        error = CninfoError(stage, "HTTPStatus", code, attempt, status)
                        retry = status == 429 or 500 <= status <= 599
                    else:
                        try:
                            return response.json()
                        except ValueError:
                            raise CninfoError(stage, "InvalidJSON", code, attempt, status) from None
            except requests.RequestException as exc:
                error_type = type(exc).__name__
                if error_type not in ERROR_TYPES:
                    error_type = "RequestException"
                error = CninfoError(stage, error_type, code, attempt)
                retry = isinstance(exc, (requests.Timeout, requests.ConnectionError))
            if not retry or attempt == 3:
                raise error from None
            time.sleep(attempt)

    def load_org_ids(self, code):
        if self.org_ids is not None:
            return
        document = self.request_json("GET", STOCK_MAP_URL, "stock_map", code)
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

    def announcements(self, code, page_size=10):
        if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                or type(page_size) is not int or not 1 <= page_size <= 100):
            raise CninfoError("input", "InvalidInput")
        self.load_org_ids(code)
        if code not in self.org_ids:
            raise CninfoError("stock_map", "UnknownSecurity", code)
        # Keep the existing fulltext query, unfiltered dates and page size.
        payload = {"stock": f"{code},{self.org_ids[code]}", "tabName": "fulltext",
                   "pageSize": str(page_size), "pageNum": "1", "column": "",
                   "category": "", "plate": "", "seDate": "", "searchkey": "",
                   "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}
        document = self.request_json("POST", ANNOUNCEMENTS_URL, "announcements", code,
                                     data=payload)
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
        if (not isinstance(rows, list) or len(rows) != min(page_size, total)):
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
            item = {"title": title, "date": date.isoformat(),
                    "dateKind": "source_disclosure_date",
                    "observedAt": observed_at.isoformat(),
                    "nextDayDisclosure": next_day}
            if next_day:
                item["disclosureEvidence"] = {
                    key: row[key] for key in ("orgId", "announcementId", "adjunctUrl")
                }
            result.append(item)
        return result
