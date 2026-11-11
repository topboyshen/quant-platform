#!/usr/bin/env python3
"""Authenticated release smoke test for the loopback integration service."""

from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.request
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dashboard import service_auth


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MAX_RESPONSE_BYTES = 1024 * 1024
logger = logging.getLogger("integration_smoke")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)


def _request_json(url: str, headers: Optional[dict[str, str]] = None) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "quant-integration-release-smoke/1", **(headers or {})},
    )
    with HTTP_OPENER.open(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status: {response.status}")
        if response.geturl() != url:
            raise RuntimeError("unexpected response URL")
        content_type = response.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("application/json"):
            raise RuntimeError("response is not JSON")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                parsed_length = int(content_length)
                if parsed_length < 0 or parsed_length > MAX_RESPONSE_BYTES:
                    raise RuntimeError("response is too large")
            except ValueError:
                raise RuntimeError("response Content-Length is invalid") from None
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("response is too large")
        payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("response is not a JSON object")
    return payload


def _validate_loopback_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise argparse.ArgumentTypeError("base URL must be loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError(
            "base URL must not contain credentials or query data"
        )
    return value.rstrip("/")


def _iso_date(value) -> Optional[str]:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def _validate_contract(context: dict) -> dict:
    if context.get("schema_version") != "quant.market-context.v1":
        raise RuntimeError("unexpected market-context schema")
    if context.get("status") not in {"ok", "degraded"}:
        raise RuntimeError("invalid market-context status")
    generated_at = context.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise RuntimeError("generated_at is invalid") from None
    if generated.tzinfo is None:
        raise RuntimeError("generated_at must include a timezone")

    market = context.get("market")
    if not isinstance(market, dict):
        raise RuntimeError("market context is not an object")
    if not isinstance(market.get("available"), bool):
        raise RuntimeError("market availability flag is invalid")
    if not isinstance(market.get("is_stale"), bool):
        raise RuntimeError("market staleness flag is invalid")
    if "expected_as_of" not in market:
        raise RuntimeError("market expected_as_of is missing")
    market_as_of = _iso_date(market.get("as_of"))
    expected_as_of = (
        _iso_date(market.get("expected_as_of"))
        if market.get("expected_as_of") is not None
        else None
    )
    if market.get("available") and market_as_of is None:
        raise RuntimeError("available market as_of is invalid")
    if market.get("expected_as_of") is not None and expected_as_of is None:
        raise RuntimeError("market expected_as_of is invalid")
    if market.get("available") and expected_as_of is None and not market["is_stale"]:
        raise RuntimeError("missing expected_as_of must fail market freshness")
    if (
        market_as_of is not None
        and expected_as_of is not None
        and market_as_of < expected_as_of
        and market.get("is_stale") is not True
    ):
        raise RuntimeError("market staleness contradicts expected_as_of")

    signals = context.get("signals")
    if not isinstance(signals, dict):
        raise RuntimeError("signals context is not an object")
    current = signals.get("current")
    recent = signals.get("recent")
    if not isinstance(current, list) or not isinstance(recent, list):
        raise RuntimeError("signal collections are invalid")
    today = datetime.now(tz=SHANGHAI_TZ).date().isoformat()
    for collection_name, collection in (("current", current), ("recent", recent)):
        for item in collection:
            if not isinstance(item, dict):
                raise RuntimeError(f"{collection_name} signal is not an object")
            decision_date = _iso_date(item.get("decision_date"))
            data_date = _iso_date(item.get("data_date"))
            if (
                decision_date is None
                or data_date is None
                or data_date > decision_date
                or decision_date > today
            ):
                raise RuntimeError(f"{collection_name} signal dates are invalid")
            if collection_name == "current" and decision_date != market_as_of:
                raise RuntimeError("current signal decision_date does not match market")
    latest_decision_date = signals.get("latest_decision_date")
    normalized_latest = (
        _iso_date(latest_decision_date) if latest_decision_date is not None else None
    )
    if latest_decision_date is not None and (
        normalized_latest is None or normalized_latest > today
    ):
        raise RuntimeError("latest_decision_date is invalid")
    all_signals = recent + current
    if all_signals and normalized_latest != max(
        item["decision_date"] for item in all_signals
    ):
        raise RuntimeError("latest_decision_date does not match signal collections")

    risk = context.get("risk")
    if not isinstance(risk, dict):
        raise RuntimeError("risk context is not an object")
    for flag in ("available", "is_stale", "is_future"):
        if not isinstance(risk.get(flag), bool):
            raise RuntimeError(f"risk {flag} flag is invalid")
    quality = context.get("quality")
    reasons = quality.get("degraded_reasons") if isinstance(quality, dict) else None
    if not isinstance(reasons, list) or not all(
        isinstance(reason, str) for reason in reasons
    ):
        raise RuntimeError("quality degraded_reasons is invalid")
    if context["status"] == "degraded" and not reasons:
        raise RuntimeError("degraded context must explain its quality reason")
    return context


def run(
    base_url: str,
    token_file: str,
    *,
    require_fresh: bool = False,
    require_ok: bool = False,
) -> dict:
    base_url = _validate_loopback_base_url(base_url)
    os.environ["RADAR_INTEGRATION_TOKEN_FILE"] = token_file
    token = service_auth.read_token_file("RADAR_INTEGRATION_TOKEN_FILE")
    if not token:
        raise RuntimeError("integration token file is unavailable or unsafe")

    health = _request_json(f"{base_url}/health")
    if health.get("status") != "ok":
        raise RuntimeError("liveness check failed")
    ready = _request_json(f"{base_url}/ready")
    if ready.get("status") != "ready":
        raise RuntimeError("readiness check failed")

    context = _request_json(
        f"{base_url}/integration/v1/market-context",
        headers={"Authorization": f"Bearer {token}"},
    )
    _validate_contract(context)
    market = context["market"]
    if require_fresh and (not market["available"] or market["is_stale"]):
        raise RuntimeError("fresh market context is required")
    if require_ok and context["status"] != "ok":
        raise RuntimeError("market context is degraded")
    return context


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        type=_validate_loopback_base_url,
        default="http://127.0.0.1:8092",
    )
    parser.add_argument(
        "--token-file",
        default="secrets/radar_integration_token",
    )
    parser.add_argument(
        "--require-fresh",
        action="store_true",
        help="fail unless market.available=true and market.is_stale=false",
    )
    parser.add_argument(
        "--require-ok",
        action="store_true",
        help="fail unless the overall context status is ok",
    )
    args = parser.parse_args()
    try:
        context = run(
            args.base_url,
            args.token_file,
            require_fresh=args.require_fresh,
            require_ok=args.require_ok,
        )
    except Exception as exc:
        logger.error("integration smoke failed: %s", exc)
        return 1
    logger.info(
        "integration smoke passed: "
        f"status={context.get('status')} "
        f"market_as_of={context['market'].get('as_of')} "
        f"market_available={context['market'].get('available')} "
        f"market_stale={context['market'].get('is_stale')} "
        f"signals={len(context.get('signals', {}).get('current', []))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
