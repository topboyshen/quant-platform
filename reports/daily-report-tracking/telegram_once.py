"""One provider request and bounded, value-free transport diagnostics."""
import errno
import hashlib
import json
import math
import re
import time


DIAGNOSTIC_SCHEMA = "telegram-send-diagnostic.v1"
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 20
MAX_RETRIES = 0
MAX_DIAGNOSTIC_BYTES = 8192
EXPECTED_BOT_USERNAME = "CmdrQuant_bot"
EXPECTED_BOT_ID = 8658511979  # Public getMe identity, independently checked 2026-10-29.
_ERROR_NAMES = frozenset({
    "ReadTimeout", "ConnectTimeout", "Timeout", "ProxyError", "SSLError",
    "ConnectionError", "ChunkedEncodingError", "HTTPError", "TooManyRedirects",
    "InvalidURL", "InvalidProxyURL", "MissingSchema", "InvalidSchema",
    "InvalidHeader", "JSONDecodeError", "ValueError", "TypeError", "OSError",
    "TimeoutError", "ConnectionResetError", "ConnectionAbortedError",
    "BrokenPipeError", "gaierror", "SSLCertVerificationError", "MaxRetryError",
    "ReadTimeoutError", "ConnectTimeoutError", "NewConnectionError",
    "ProxySchemeUnknown", "ProxySchemeUnsupported", "HostChangedError",
    "ProtocolError", "LocationParseError", "LocationValueError",
    "NameResolutionError", "EmptyPoolError", "ClosedPoolError", "FullPoolError",
})
_FRAME_MODULES = {
    "/requests/adapters.py": ("requests.adapters", {"send"}),
    "/requests/sessions.py": ("requests.sessions", {"request", "send", "post"}),
    "/urllib3/connectionpool.py": ("urllib3.connectionpool", {
        "urlopen", "_prepare_proxy", "_make_request", "_validate_conn", "_raise_timeout"}),
    "/urllib3/connection.py": ("urllib3.connection", {
        "connect", "_new_conn", "_tunnel", "_connect_tls_proxy", "getresponse", "request"}),
    "/urllib3/util/connection.py": ("urllib3.util.connection", {"create_connection"}),
    "/urllib3/util/retry.py": ("urllib3.util.retry", {"increment"}),
    "/urllib3/response.py": ("urllib3.response", {
        "read", "_raw_read", "_error_catcher", "stream"}),
    "/ssl.py": ("ssl", {"do_handshake", "wrap_socket", "_create", "read", "write"}),
    "/http/client.py": ("http.client", {"_tunnel", "getresponse", "begin", "_read_status"}),
    "/socket.py": ("socket", {"create_connection", "readinto"}),
}
_PROXY_KEYS = frozenset({"http", "https", "all", "https://api.telegram.org",
                         "all://api.telegram.org"})


def fingerprint(chat_id):
    return hashlib.sha256(str(chat_id).encode()).hexdigest()


def _error_name(exc):
    name = type(exc).__name__
    return name if name in _ERROR_NAMES else "OtherException"


def _body_metadata(text):
    digest = hashlib.sha256()
    size = 0
    utf16_units = 0
    for offset in range(0, len(text), 4096):
        chunk = text[offset:offset + 4096]
        raw = chunk.encode("utf-8")
        digest.update(raw)
        size += len(raw)
        utf16_units += len(chunk.encode("utf-16-le")) // 2
    return {"sha256": digest.hexdigest(), "utf8_bytes": size,
            "code_points": len(text), "utf16_units": utf16_units}


def _route_policy(session):
    """Observe keys only: never read a proxy value, endpoint, environment or URL."""
    proxies = getattr(session, "proxies", None)
    trust = getattr(session, "trust_env", None)
    trust = trust if type(trust) is bool else None
    if type(proxies) is not dict:
        return {"category": "unknown", "policy_fingerprint": None,
                "meaning": "configuration_policy_not_endpoint_or_wire_route"}
    keys = sorted(key for key in proxies if type(key) is str and key in _PROXY_KEYS)
    other = len(proxies) != len(keys)
    if "https://api.telegram.org" in keys or "https" in keys:
        category = "explicit_https_proxy_policy"
    elif "all://api.telegram.org" in keys or "all" in keys:
        category = "explicit_all_proxy_policy"
    elif trust is True:
        category = "environment_proxy_possible"
    elif other or trust is None:
        category = "unknown"
    else:
        category = "no_explicit_https_proxy_policy"
    profile = {"category": category, "proxy_keys": keys, "trust_env": trust,
               "unrecognized_keys_present": other}
    raw = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    return {**profile, "policy_fingerprint": hashlib.sha256(raw).hexdigest(),
            "meaning": "configuration_policy_not_endpoint_or_wire_route"}


def _exception_metadata(exc):
    """Only allowlisted class/errno/code symbols; no args, str, repr or locals."""
    chain, frames, seen = [], [], set()
    relation = "raised"
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        item = {"class": _error_name(current), "relation": relation}
        number = getattr(current, "errno", None)
        if type(number) is int and number in errno.errorcode:
            item["errno"] = number
            item["errno_name"] = errno.errorcode[number]
        chain.append(item)
        tb = current.__traceback__
        visited = 0
        while tb is not None and visited < 32:
            code = tb.tb_frame.f_code
            filename = code.co_filename.replace("\\", "/")
            for suffix, (module, functions) in _FRAME_MODULES.items():
                if filename.endswith(suffix) and code.co_name in functions:
                    location = {"module": module, "function": code.co_name,
                                "line": tb.tb_lineno}
                    if location not in frames:
                        frames.append(location)
                    break
            tb = tb.tb_next
            visited += 1
        if current.__cause__ is not None:
            current, relation = current.__cause__, "cause"
        elif not current.__suppress_context__:
            current, relation = current.__context__, "context"
        else:
            current = None
    return {"chain": chain, "safe_code_locations": frames[-8:],
            "chain_limit": 4, "frame_limit": 8}


def _diagnostics(text, target, session):
    result = {"schema": DIAGNOSTIC_SCHEMA, "phase": "unknown",
              "intended_target_fingerprint": target,
              "configured": {"connect_seconds": CONNECT_TIMEOUT,
                             "read_seconds": READ_TIMEOUT,
                             "max_retries": MAX_RETRIES, "allow_redirects": False},
              "response_observed": False}
    for field, factory in (("body", lambda: _body_metadata(text)),
                           ("route_policy", lambda: _route_policy(session))):
        try:
            result[field] = factory()
        except Exception:
            result[field] = {"status": "unavailable"}
    return result


def _clock():
    try:
        value = time.monotonic()
        return value if type(value) in (float, int) and math.isfinite(value) else None
    except Exception:
        return None


def _attach(receipt, diagnostics, started, finished):
    """Diagnostic trouble must not replace accepted proof or cause another send."""
    try:
        if started is not None and finished is not None and finished >= started:
            diagnostics["provider_call_elapsed_ms"] = min(
                round((finished - started) * 1000, 3), 604800000)
            diagnostics["elapsed_capped"] = (finished - started) > 604800
        else:
            diagnostics["provider_call_elapsed_ms"] = None
        raw = json.dumps(diagnostics, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_DIAGNOSTIC_BYTES:
            raise ValueError("diagnostic_budget")
        diagnostic = json.loads(raw)
    except Exception:
        diagnostic = {"schema": DIAGNOSTIC_SCHEMA, "phase": "unknown",
                      "collection_status": "unavailable"}
    return {**receipt, "diagnostics": diagnostic}


def make_sender(token, chat_id, expected_fingerprint, session, *,
                expected_bot_username=None, expected_bot_id=None):
    chat_id = str(chat_id)
    if (not re.fullmatch(r"-?[0-9]+", chat_id)
            or fingerprint(chat_id) != expected_fingerprint
            or not isinstance(token, str) or not token.strip()):
        raise ValueError("notification_target_not_verified")
    identity_bound = expected_bot_username is not None or expected_bot_id is not None
    if identity_bound and (not isinstance(expected_bot_username, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", expected_bot_username)
            or type(expected_bot_id) is not int or expected_bot_id <= 0
            or token.partition(":")[0] != str(expected_bot_id)):
        raise ValueError("notification_sender_not_verified")

    def send(text):
        diagnostics = _diagnostics(text, expected_fingerprint, session)
        response = None
        started = finished = None
        try:
            started = _clock()
            response = session.post(
                "https://api.telegram.org/bot" + token + "/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=False,
            )
            finished = _clock()
            diagnostics["response_observed"] = True
            code = response.status_code
            if 300 <= code < 400 or code >= 500:
                receipt = {"ok": False, "http_status": code,
                           "reason": "unconfirmed_http_response"}
            else:
                payload = response.json()
                if not isinstance(payload, dict):
                    receipt = {"ok": False, "reason": "invalid_provider_receipt"}
                else:
                    result = payload.get("result") or {}
                    if not isinstance(result, dict):
                        receipt = {"ok": False, "reason": "invalid_provider_receipt"}
                    else:
                        target = result.get("chat") or {}
                        if not isinstance(target, dict):
                            receipt = {"ok": False, "reason": "invalid_provider_receipt"}
                        elif (200 <= code < 300 and payload.get("ok") is True
                              and isinstance(result.get("message_id"), int)
                              and not isinstance(result["message_id"], bool)
                              and result["message_id"] > 0
                              and fingerprint(target.get("id")) == expected_fingerprint):
                            receipt = {"ok": True, "message_id": result["message_id"],
                                       "targetFingerprint": expected_fingerprint,
                                       "http_status": code}
                            if identity_bound:
                                sender = result.get("from")
                                returned_text = result.get("text")
                                platform_time = result.get("date")
                                matches = (isinstance(sender, dict)
                                    and type(sender.get("id")) is int
                                    and sender["id"] == expected_bot_id
                                    and sender.get("is_bot") is True
                                    and sender.get("username") == expected_bot_username
                                    and isinstance(returned_text, str) and returned_text == text
                                    and type(platform_time) is int and platform_time > 0)
                                if matches:
                                    receipt.update(providerProofSchema="telegram-public-identity.v1",
                                        botUsername=sender["username"], botId=sender["id"],
                                        bodySha256=hashlib.sha256(returned_text.encode()).hexdigest(),
                                        platformMessageDate=platform_time,
                                        returnedSenderMatches=True, returnedTextMatches=True)
                                else:
                                    receipt.update(ok=False, reason="provider_sender_or_body_unconfirmed")
                        elif (400 <= code < 500 and payload.get("ok") is False
                              and payload.get("error_code") == code and not result):
                            receipt = {"ok": False, "definitely_not_sent": True,
                                       "http_status": code,
                                       "targetFingerprint": expected_fingerprint}
                        else:
                            receipt = {"ok": False, "http_status": code,
                                       "reason": "receipt_identity_or_status_unconfirmed"}
        except Exception as exc:
            if finished is None:
                finished = _clock()
            try:
                diagnostics["exception"] = _exception_metadata(exc)
            except Exception:
                diagnostics["exception"] = {"status": "unavailable"}
            # Arbitrary custom class names are not a safe diagnostic alphabet.
            receipt = {"ok": False, "reason": _error_name(exc)}
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass  # A close failure must not erase an accepted receipt.
        return _attach(receipt, diagnostics, started, finished)

    return send


def configured_sender(expected_fingerprint):
    from radar.secrets import _read_setting
    from radar.data.session_pool import create_pooled_session
    token = _read_setting("TELEGRAM_BOT_TOKEN")
    target = _read_setting("TELEGRAM_CHAT_ID")
    return make_sender(token, target, expected_fingerprint,
                       create_pooled_session(max_retries=MAX_RETRIES, trust_env=False),
                       expected_bot_username=EXPECTED_BOT_USERNAME, expected_bot_id=EXPECTED_BOT_ID)
