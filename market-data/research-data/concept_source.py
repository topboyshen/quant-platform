"""Bounded read-only Baidu concept source; no storage, proxy or fallback source."""
import json
import re
import time

import requests


CONCEPT_URL = "https://finance.pae.baidu.com/api/getrelatedblock"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CATEGORIES = 32
MAX_ITEMS_PER_CATEGORY = 1000
ERROR_TYPES = {
    "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError", "SSLError",
    "RequestException", "HTTPStatus", "InvalidJSON", "InvalidSchema",
    "UpstreamFailure", "SecurityMismatch", "ResponseTooLarge", "InvalidInput",
}
STAGES = {"input", "related_blocks"}


def safe_concept_diagnostic(value):
    """Only known fields can enter status records; never copy upstream prose."""
    if not isinstance(value, dict) or value.get("source") != "concepts":
        return None
    stage, error_type = value.get("stage"), value.get("errorType")
    if (not isinstance(stage, str) or stage not in STAGES
            or not isinstance(error_type, str) or error_type not in ERROR_TYPES):
        return None
    result = {key: value[key] for key in ("source", "stage", "errorType")}
    code = value.get("code")
    if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code):
        result["code"] = code
    for key, lower, upper in (("attempt", 1, 3), ("httpStatus", 100, 599)):
        if type(value.get(key)) is int and lower <= value[key] <= upper:
            result[key] = value[key]
    return result


class ConceptError(RuntimeError):
    """Contains sanitized source diagnostics, not exception text or payloads."""

    def __init__(self, stage, error_type, code=None, attempt=1, http_status=None):
        diagnostic = {"source": "concepts", "stage": stage,
                      "errorType": error_type, "code": code,
                      "attempt": attempt, "httpStatus": http_status}
        self.diagnostic = safe_concept_diagnostic(diagnostic)
        if self.diagnostic is None:
            self.diagnostic = {"source": "concepts", "stage": "related_blocks",
                               "errorType": "RequestException"}
        super().__init__(json.dumps(self.diagnostic, sort_keys=True))


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON constant")


def _valid_name(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


class ConceptClient:
    """Reuse one normal HTTPS session without changing source or market scope."""

    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gushitong.baidu.com/",
            "Accept": "application/json",
        })

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def concepts(self, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ConceptError("input", "InvalidInput")
        params = {"stock": json.dumps(
            [{"code": code, "market": "ab", "type": "stock"}], separators=(",", ":")),
            "finClientType": "pc"}
        for attempt in range(1, 4):
            retry = False
            try:
                with self.session.get(CONCEPT_URL, params=params, timeout=(5, 15),
                                      allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if status != 200:
                        error = ConceptError("related_blocks", "HTTPStatus", code, attempt, status)
                        retry = status == 429 or 500 <= status <= 599
                    else:
                        document = self._read_document(response, code, attempt)
                        return self._parse(document, code, attempt)
            except requests.RequestException as exc:
                error_type = type(exc).__name__
                if error_type not in ERROR_TYPES:
                    error_type = "RequestException"
                error = ConceptError("related_blocks", error_type, code, attempt)
                retry = (isinstance(exc, (requests.Timeout, requests.ConnectionError))
                         and not isinstance(exc, requests.exceptions.SSLError))
            if not retry or attempt == 3:
                raise error from None
            time.sleep(attempt)

    @staticmethod
    def _read_document(response, code, attempt):
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                size = int(declared)
            except (TypeError, ValueError):
                raise ConceptError("related_blocks", "InvalidSchema", code, attempt, 200) from None
            if size < 0:
                raise ConceptError("related_blocks", "InvalidSchema", code, attempt, 200)
            if size > MAX_RESPONSE_BYTES:
                raise ConceptError("related_blocks", "ResponseTooLarge", code, attempt, 200)
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ConceptError("related_blocks", "ResponseTooLarge", code, attempt, 200)
                raw.extend(chunk)
        try:
            return json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_strict_object,
                              parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ConceptError("related_blocks", "InvalidJSON", code, attempt, 200) from None

    @staticmethod
    def _parse(document, code, attempt):
        def reject(error_type="InvalidSchema"):
            raise ConceptError("related_blocks", error_type, code, attempt, 200)

        if not isinstance(document, dict) or "ResultCode" not in document:
            reject()
        result_code = document["ResultCode"]
        if type(result_code) not in (str, int):
            reject()
        if result_code not in (0, "0"):
            reject("UpstreamFailure")
        result = document.get("Result")
        if not isinstance(result, dict):
            reject()
        if set(result) != {code}:
            reject("SecurityMismatch")
        categories = result[code]
        if not isinstance(categories, list) or not 1 <= len(categories) <= MAX_CATEGORIES:
            reject()
        parsed = {}
        for category in categories:
            if not isinstance(category, dict):
                reject()
            name, items = category.get("name"), category.get("list")
            if (not _valid_name(name) or name in parsed or not isinstance(items, list)
                    or len(items) > MAX_ITEMS_PER_CATEGORY):
                reject()
            names = []
            for item in items:
                if not isinstance(item, dict) or not _valid_name(item.get("name")):
                    reject()
                names.append(item["name"])
            parsed[name] = names
        # Known successful snapshots contain an explicit "概念" category. Missing
        # this category is a partial response, not proof of no concept membership.
        # A present empty list is preserved, provided other metadata is nonempty.
        if "概念" not in parsed or not any(parsed.values()):
            reject()
        return parsed
