#!/usr/bin/env python3
"""Fixed stdio client for the read-only stock research MCP server.

The client accepts one small control object on stdin, validates every symbol
against ``config/stocks.json``, starts only ``stock_research_server.py``, and
emits one JSON object on stdout.  It is intentionally not a generic MCP
launcher: executable, server path, environment, tools, and tool arguments are
all fixed by this module.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sys
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = PROJECT_ROOT / "mcp" / "stock_research_server.py"
STOCKS_PATH = PROJECT_ROOT / "config" / "stocks.json"

ALLOWED_TOOLS = (
    "get_stock_price",
    "get_financial_report",
    "get_news",
    "get_sector_rank",
    "get_capital_flow",
    "run_backtest",
)
MAX_INPUT_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
TOTAL_TIMEOUT_SECONDS = 200
TOOL_TIMEOUT_SECONDS = 60
MAX_PARALLEL_CALLS = 4
SYMBOL_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ClientContractError(ValueError):
    """A concise, safe error caused by invalid local control input."""


def _parse_iso_date(value: Any) -> date:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ClientContractError("targetDate must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ClientContractError("targetDate is not a valid date") from exc


def _controlled_symbols() -> set[str]:
    if not STOCKS_PATH.is_file() or STOCKS_PATH.stat().st_size > MAX_CONFIG_BYTES:
        raise RuntimeError("controlled stock configuration is unavailable")
    try:
        config = json.loads(STOCKS_PATH.read_text(encoding="utf-8"))
        pool = config["corePool"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("controlled stock configuration is invalid") from exc
    if not isinstance(pool, list):
        raise RuntimeError("controlled stock configuration is invalid")
    symbols = {
        item.get("symbol")
        for item in pool
        if isinstance(item, dict) and isinstance(item.get("symbol"), str)
    }
    if not symbols or any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise RuntimeError("controlled stock configuration is invalid")
    return symbols


def read_request() -> dict[str, Any]:
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise ClientContractError("request exceeds 256 KiB")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientContractError("stdin must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ClientContractError("request must be a JSON object")
    unexpected = set(value) - {"targetDate", "symbols"}
    if unexpected:
        raise ClientContractError("request contains unsupported fields")

    target = _parse_iso_date(value.get("targetDate"))
    symbols = value.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 10:
        raise ClientContractError("symbols must contain 1-10 controlled symbols")
    if any(not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise ClientContractError("symbols contains an invalid symbol")
    if len(set(symbols)) != len(symbols):
        raise ClientContractError("symbols must not contain duplicates")
    if not set(symbols).issubset(_controlled_symbols()):
        raise ClientContractError("symbols contains a symbol outside the controlled pool")
    return {"targetDate": target.isoformat(), "symbols": symbols}


def build_call_plan(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Build deterministic calls; no tool or parameter name comes from stdin."""
    target = _parse_iso_date(request["targetDate"])
    symbols = request["symbols"]
    price_start = (target - timedelta(days=240)).isoformat()
    calls: list[dict[str, Any]] = []
    for symbol in symbols:
        calls.append(
            {
                "tool": "get_stock_price",
                "arguments": {
                    "symbol": symbol,
                    "start_date": price_start,
                    "end_date": target.isoformat(),
                    "limit": 120,
                    "adjust": "qfq",
                },
            }
        )
    for symbol in symbols:
        calls.append(
            {
                "tool": "get_financial_report",
                "arguments": {"symbol": symbol, "as_of": target.isoformat()},
            }
        )
    for symbol in symbols:
        calls.append(
            {
                "tool": "get_news",
                "arguments": {"symbol": symbol, "limit": 20, "as_of": target.isoformat()},
            }
        )
    calls.append(
        {
            "tool": "get_sector_rank",
            "arguments": {"sector_type": "industry", "limit": 10},
        }
    )
    for symbol in symbols:
        calls.append(
            {
                "tool": "get_capital_flow",
                "arguments": {"symbol": symbol, "indicator": "5日", "limit": 10},
            }
        )
    calls.append(
        {
            "tool": "run_backtest",
            "arguments": {"target_date": target.isoformat(), "symbols": symbols},
        }
    )
    return calls


def _safe_unavailable(call: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        "arguments": call["arguments"],
        "status": "unavailable",
        "evidenceStatus": "unavailable",
        "error": {"code": code, "message": "该项研究数据不可用。"},
    }


def _evidence_status(structured: dict[str, Any]) -> str:
    status = structured.get("status")
    return status if status in {"live", "partial", "unavailable"} else "available"


async def _invoke_call(
    session: ClientSession,
    call: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    try:
        async with semaphore:
            result = await session.call_tool(
                call["tool"],
                call["arguments"],
                read_timeout_seconds=TOOL_TIMEOUT_SECONDS,
            )
    except Exception:
        return _safe_unavailable(call, "tool_call_failed")
    if result.is_error:
        return _safe_unavailable(call, "tool_returned_error")
    structured = result.structured_content
    if not isinstance(structured, dict):
        return _safe_unavailable(call, "invalid_structured_output")
    return {
        "arguments": call["arguments"],
        "status": "ok",
        "evidenceStatus": _evidence_status(structured),
        "result": structured,
    }


async def call_stock_research_mcp(request: dict[str, Any]) -> dict[str, Any]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        cwd=str(PROJECT_ROOT),
        env={
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
        },
    )
    plan = build_call_plan(request)
    async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                listed_names = [tool.name for tool in listed.tools]
                if len(listed_names) != len(ALLOWED_TOOLS) or set(listed_names) != set(ALLOWED_TOOLS):
                    raise RuntimeError("MCP tool allowlist mismatch")

                semaphore = asyncio.Semaphore(MAX_PARALLEL_CALLS)
                outcomes = await asyncio.gather(
                    *(_invoke_call(session, call, semaphore) for call in plan)
                )

    grouped: dict[str, dict[str, Any]] = {
        tool: {"calls": []} for tool in ALLOWED_TOOLS
    }
    unavailable = 0
    partial = 0
    for call, outcome in zip(plan, outcomes, strict=True):
        grouped[call["tool"]]["calls"].append(outcome)
        if outcome["status"] != "ok" or outcome["evidenceStatus"] == "unavailable":
            unavailable += 1
        elif outcome["evidenceStatus"] == "partial":
            partial += 1

    if unavailable == len(outcomes):
        overall_status = "unavailable"
    elif unavailable or partial:
        overall_status = "partial"
    else:
        overall_status = "complete"

    warnings: list[str] = []
    if unavailable:
        warnings.append(f"{unavailable} 个细粒度研究调用无可用证据；对应结果已显式降级。")
    if partial:
        warnings.append(f"{partial} 个细粒度研究调用仅返回部分证据；备忘录必须披露缺口。")

    server_info = initialized.server_info
    return {
        "schemaVersion": "1.0",
        "transport": "stdio",
        "server": {
            "name": server_info.name,
            "version": server_info.version,
        },
        "status": overall_status,
        "allowedTools": list(ALLOWED_TOOLS),
        "tools": grouped,
        "warnings": warnings,
    }


def _encode_response(value: dict[str, Any]) -> bytes:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > MAX_OUTPUT_BYTES:
        raise RuntimeError("research bundle exceeds 16 MiB")
    return payload + b"\n"


def main() -> int:
    try:
        request = read_request()
        response = asyncio.run(call_stock_research_mcp(request))
        sys.stdout.buffer.write(_encode_response(response))
        return 0
    except ClientContractError as exc:
        print(f"stock research request rejected: {exc}", file=sys.stderr)
        return 2
    except TimeoutError:
        print("stock research client failed: timeout", file=sys.stderr)
        return 1
    except Exception:
        # Never relay server/tool stderr or exception text across this boundary.
        print("stock research client failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
