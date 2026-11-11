"""Bounded public THS F10 transport. No login, proxy, challenge or fallback."""
from contextlib import contextmanager
import datetime
import hashlib
import json
import re
import signal
import threading
import time

import requests
from bs4 import BeautifulSoup

MAX_RESPONSE_BYTES = 1024 * 1024
PAGE_DEADLINE_SECONDS = 45
ERROR_TYPES = {"InvalidInput", "HTTPStatus", "Timeout", "ConnectTimeout",
               "ReadTimeout", "ConnectionError", "SSLError", "RequestException",
               "InvalidSchema", "InvalidEncoding", "SecurityMismatch",
               "ResponseTooLarge", "FutureReportDate", "DeadlineExceeded",
               "DeadlineUnavailable"}
STAGES = {"input", "concept_page", "holder_page"}


def safe_source_diagnostic(value):
    if not isinstance(value, dict):
        return None
    if (value.get("source") not in ("concepts", "insiders")
            or not isinstance(value.get("stage"), str) or value["stage"] not in STAGES
            or not isinstance(value.get("errorType"), str) or value["errorType"] not in ERROR_TYPES):
        return None
    result = {k: value[k] for k in ("source", "stage", "errorType")}
    result["provider"] = "ths_f10"
    if isinstance(value.get("code"), str) and re.fullmatch(r"[0-9]{6}", value["code"]):
        result["code"] = value["code"]
    for key, lo, hi in (("attempt", 1, 3), ("httpStatus", 100, 599)):
        if type(value.get(key)) is int and lo <= value[key] <= hi:
            result[key] = value[key]
    return result


class SourceError(RuntimeError):
    def __init__(self, source, stage, error_type, code=None, attempt=1, http_status=None):
        self.diagnostic = safe_source_diagnostic({
            "source": source, "stage": stage, "errorType": error_type,
            "code": code, "attempt": attempt, "httpStatus": http_status})
        if self.diagnostic is None:
            raise ValueError("invalid diagnostic contract")
        super().__init__(json.dumps(self.diagnostic, sort_keys=True))


class _PageDeadlineExpired(BaseException):
    """Do not let a supplier/parser's broad Exception handler swallow expiry."""


@contextmanager
def _page_deadline(source, stage, code):
    """Own an otherwise unused main-thread POSIX timer for this entire page.

    A requests read timeout measures inactivity, not total streaming duration.
    This alarm also interrupts headers, a slow body, parsing and retry sleeps.
    An existing alarm or blocked/unavailable SIGALRM is never replaced.
    """
    def unavailable():
        return SourceError(source, stage, "DeadlineUnavailable", code, attempt=None)

    if threading.current_thread() is not threading.main_thread():
        raise unavailable()
    deadline = PAGE_DEADLINE_SECONDS
    if type(deadline) not in (int, float) or not 0 < deadline <= 45:
        raise unavailable()
    try:
        old_handler = signal.getsignal(signal.SIGALRM)
        old_timer = signal.getitimer(signal.ITIMER_REAL)
        if any(old_timer):
            raise unavailable()
        if (hasattr(signal, "pthread_sigmask")
                and signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, [])):
            raise unavailable()
    except (AttributeError, OSError, ValueError):
        raise unavailable() from None

    def expired(signum, frame):
        raise _PageDeadlineExpired()

    installed = False
    try:
        try:
            signal.signal(signal.SIGALRM, expired)
            installed = True
            signal.setitimer(signal.ITIMER_REAL, deadline)
        except (AttributeError, OSError, ValueError):
            raise unavailable() from None
        yield
    except _PageDeadlineExpired:
        # It may expire at any attempt or inside parsing; do not invent a
        # request-attempt number or leak a response/underlying exception.
        raise SourceError(source, stage, "DeadlineExceeded", code, attempt=None) from None
    finally:
        if installed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                signal.signal(signal.SIGALRM, old_handler)


def security_market(code):
    if not isinstance(code, str) or not re.fullmatch(r"(?:00|30|60|68)[0-9]{4}", code):
        raise ValueError("unsupported mainland A-share identity")
    return ("SH", "17") if code.startswith(("60", "68")) else ("SZ", "33")


class F10Client:
    def __init__(self, source):
        if source not in ("concepts", "insiders"):
            raise ValueError("unsupported source")
        self.source = source
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "Mozilla/5.0",
                                     "Accept": "text/html"})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def page(self, code, kind):
        if kind not in ("concept", "holder"):
            raise SourceError(self.source, "input", "InvalidInput")
        try:
            market, market_id = security_market(code)
        except ValueError:
            raise SourceError(self.source, "input", "InvalidInput") from None
        stage = kind + "_page"
        with _page_deadline(self.source, stage, code):
            return self._page(code, kind, market, market_id, stage)

    def _page(self, code, kind, market, market_id, stage):
        url = f"https://basic.10jqka.com.cn/{code}/{kind}.html"
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for attempt in range(1, 4):
            retry = False
            try:
                with self.session.get(url, timeout=(5, 12), stream=True,
                                      allow_redirects=False) as response:
                    status = response.status_code
                    if status != 200:
                        error = SourceError(self.source, stage, "HTTPStatus", code, attempt, status)
                        retry = status == 429 or 500 <= status <= 599
                    else:
                        raw = self._read(response, stage, code, attempt)
                        soup, title = self._identity(raw, kind, code, market_id, attempt)
                        return soup, {
                            "provider": "ths_f10", "sourceUrl": url, "code": code,
                            "market": market, "observedAt": datetime.datetime.now(
                                datetime.timezone.utc).isoformat(),
                            "collectionStartedAt": started, "responseSha256": hashlib.sha256(raw).hexdigest(),
                            "parserVersion": 1, "title": title,
                            "timeKind": "observed_at", "sourcePublishedAt": None,
                            "transportSecurity": "https_verified", "status": "validated",
                        }
            except requests.RequestException as exc:
                error_type = type(exc).__name__
                if error_type not in ERROR_TYPES:
                    error_type = "RequestException"
                error = SourceError(self.source, stage, error_type, code, attempt)
                retry = (isinstance(exc, (requests.Timeout, requests.ConnectionError))
                         and not isinstance(exc, requests.exceptions.SSLError))
            if not retry or attempt == 3:
                raise error from None
            time.sleep(attempt)

    def _read(self, response, stage, code, attempt):
        def reject(kind):
            raise SourceError(self.source, stage, kind, code, attempt, 200)
        content_type = response.headers.get("Content-Type", "").lower()
        if not content_type.startswith("text/html"):
            reject("InvalidSchema")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            if not isinstance(declared, str) or not re.fullmatch(r"[0-9]+", declared):
                reject("InvalidSchema")
            if int(declared) > MAX_RESPONSE_BYTES:
                reject("ResponseTooLarge")
        raw = bytearray()
        for chunk in response.iter_content(8192):
            if chunk:
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    reject("ResponseTooLarge")
                raw.extend(chunk)
        return bytes(raw)

    def _identity(self, raw, kind, code, market_id, attempt):
        stage = kind + "_page"
        def reject(error="SecurityMismatch"):
            raise SourceError(self.source, stage, error, code, attempt, 200)
        # These public pages declare GBK. Reject unknown encodings and malformed
        # bytes instead of guessing/replacing identity characters.
        encoding = re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)', raw[:4096], re.I)
        if not encoding or encoding[1].lower() not in (b"gbk", b"gb2312", b"gb18030", b"utf-8"):
            reject("InvalidEncoding")
        codec = "utf-8" if encoding[1].lower() == b"utf-8" else "gb18030"
        try:
            body = raw.decode(codec)
        except UnicodeError:
            reject("InvalidEncoding")
        if "\x00" in body:
            reject("InvalidSchema")
        soup = BeautifulSoup(body, "html.parser")
        if len(soup.find_all(True)) > 20000:
            reject("ResponseTooLarge")
        for tag in soup.select("script,style,noscript,template"):
            tag.decompose()
        titles = soup.find_all("title")
        if len(titles) != 1:
            reject()
        title = titles[0].get_text(" ", strip=True)
        expected = "概念题材" if kind == "concept" else "股东研究"
        if not re.fullmatch(r"[^<>]{1,80}\(" + re.escape(code) + r"\) " + expected + r"_F10_同花顺金融服务网", title):
            reject()
        for ident, expected_value in (("stockCode", code), ("marketId", market_id)):
            nodes = soup.find_all(id=ident)
            if (len(nodes) != 1 or nodes[0].name != "input"
                    or nodes[0].get("value") != expected_value):
                reject()
        return soup, title
