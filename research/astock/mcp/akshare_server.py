"""Read-only MCP server exposing the shared AkShare morning core + SQLite history.

Uses the official MCP Python SDK (mcp.server.MCPServer) pinned in
requirements.txt (mcp==2.0.0).  Two read-only tools are exposed:

  * ``get_a_share_morning_context`` — fetches a fresh normalized morning context
    from the same AkShare core used by the fixed stdin bridge.
  * ``query_morning_history`` — returns recent runs / a specific date's context
    from the SQLite history written by ``akshare_bridge.py``.

Both tools are read-only.  Start with::

    .venv/bin/python mcp/akshare_server.py            # Linux
    .venv\\Scripts\\python.exe mcp\\akshare_server.py   # Windows
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from akshare_provider import get_a_share_morning_context as build_morning_context
from storage import recent_runs, get_context_by_date, db_info

server = MCPServer(
    "astock-akshare",
    title="AStock AkShare Read-only Data",
    description="Read-only normalized A-share morning context backed by AkShare.",
    version="1.0.0",
)


@server.tool(
    name="get_a_share_morning_context",
    description="Return the normalized v1 A-share morning context for one supplied request.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
def get_a_share_morning_context(request: dict[str, Any]) -> dict[str, Any]:
    """Fetch public market data without performing any write or trading action."""
    return build_morning_context(request)


@server.tool(
    name="query_morning_history",
    description="Query the SQLite morning-context history (read-only). "
                "If target_date (YYYY-MM-DD) is given, return the most recent stored "
                "context for that date; otherwise return the `limit` most recent runs.",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=True,
)
def query_morning_history(target_date: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Read historical morning contexts persisted by the data bridge."""
    if target_date:
        result = get_context_by_date(target_date)
        return {"targetDate": target_date, "found": result is not None, "entry": result}
    return {"db": db_info(), "recent": recent_runs(limit)}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()