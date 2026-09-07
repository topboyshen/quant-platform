"""Bounded, read-only CNINFO collection; no proxy, storage, or fallback source."""
import datetime as dt
import json
import re
import time

import requests


STOCK_MAP_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
ANNOUNCEMENTS_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
ERROR_TYPES = {
    "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError", "SSLError",
    "RequestException", "HTTPStatus", "InvalidJSON", "InvalidSchema",
    "UnknownSecurity", "SecurityMismatch", "InvalidInput",
}


class CninfoError(RuntimeError):
    """Contains allowlisted diagnostics, never raw exceptions, bodies or headers."""

    def __init__(self, stage, error_type, code=None, attempt=1, http_status=None):
        self.diagnostic = {"source": "cninfo", "stage": stage,
                           "errorType": error_type, "attempt": attempt}
        if code is not None and re.fullmatch(r"[0-9]{6}", str(code)):
            self.diagnostic["code"] = code
        if http_status is not None:
            self.diagnostic["httpStatus"] = http_status
        super().__init__(json.dumps(self.diagnostic, sort_keys=True))


def safe_diagnostic(value):
    """Do not promote untrusted source text into operational logs."""
    if not isinstance(value, dict) or value.get("source") != "cninfo":
        return None
    if value.get("stage") not in {"stock_map", "announcements", "input"}:
        return None
    if value.get("errorType") not in ERROR_TYPES:
        return None
    result = {key: value[key] for key in ("source", "stage", "errorType")}
    if isinstance(value.get("code"), str) and re.fullmatch(r"[0-9]{6}", value["code"]):
        result["code"] = value["code"]
    for key, lower, upper in (("attempt", 1, 3), ("httpStatus", 100, 599)):
        if type(value.get(key)) is int and lower <= value[key] <= upper:
            result[key] = value[key]
    return result


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
        if not isinstance(document, dict) or "announcements" not in document:
            raise CninfoError("announcements", "InvalidSchema", code)
        rows, total = document["announcements"], document.get("totalAnnouncement")
        if type(total) is not int or total < 0:
            raise CninfoError("announcements", "InvalidSchema", code)
        if rows is None and total == 0:
            rows = []
        if (not isinstance(rows, list) or len(rows) != min(page_size, total)):
            raise CninfoError("announcements", "InvalidSchema", code)
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise CninfoError("announcements", "InvalidSchema", code)
            if row.get("secCode") != code:
                raise CninfoError("announcements", "SecurityMismatch", code)
            title, timestamp = row.get("announcementTitle"), row.get("announcementTime")
            if not isinstance(title, str) or not title.strip() or type(timestamp) not in (int, str):
                raise CninfoError("announcements", "InvalidSchema", code)
            try:
                timestamp = int(timestamp)
                if timestamp > 10**12:
                    timestamp = timestamp / 1000
                date = dt.datetime.fromtimestamp(timestamp, dt.timezone(dt.timedelta(hours=8))).date()
                if not dt.date(1990, 1, 1) <= date <= dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date():
                    raise ValueError("out of range")
            except (ValueError, OverflowError, OSError):
                raise CninfoError("announcements", "InvalidSchema", code) from None
            result.append({"title": title, "date": date.isoformat()})
        return result
