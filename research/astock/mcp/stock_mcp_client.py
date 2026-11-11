#!/usr/bin/env python3
"""Call the project's read-only AkShare MCP server over stdio.

The process accepts exactly one normalized AStock request on stdin and emits
exactly one JSON object on stdout.  Server and tool names are fixed so this
adapter cannot be used as a generic process or MCP launcher.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = PROJECT_ROOT / "mcp" / "akshare_server.py"
TOOL_NAME = "get_a_share_morning_context"
MAX_INPUT_BYTES = 256 * 1024


def read_request() -> dict[str, Any]:
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("request exceeds 256 KiB")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"stdin is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


async def call_stock_mcp(request: dict[str, Any]) -> dict[str, Any]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
        cwd=PROJECT_ROOT,
        env={
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
        },
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if TOOL_NAME not in names:
                raise RuntimeError(f"required MCP tool is missing: {TOOL_NAME}")
            result = await session.call_tool(
                TOOL_NAME,
                {"request": request},
                read_timeout_seconds=180,
            )
            if result.is_error:
                messages = [
                    item.text
                    for item in result.content
                    if getattr(item, "type", None) == "text"
                ]
                raise RuntimeError("; ".join(messages) or "MCP tool returned an error")
            context = result.structured_content
            if not isinstance(context, dict):
                raise RuntimeError("MCP tool did not return structured JSON")
            return {
                "transport": "stdio",
                "server": initialized.server_info.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                ),
                "tool": TOOL_NAME,
                "context": context,
            }


def main() -> int:
    try:
        request = read_request()
        response = asyncio.run(call_stock_mcp(request))
        json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except Exception as exc:  # CLI boundary: one concise error, never partial JSON.
        print(f"stock MCP client failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
