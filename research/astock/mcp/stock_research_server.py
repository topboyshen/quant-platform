r"""Read-only MCP stdio server for bounded A-share research tools.

This server is an MCP bridge contract; it does not place trades, execute
commands, accept filesystem paths, or alter the production morning-report
bridge.  Start from the project root with the project's Python environment::

    .venv/bin/python mcp/stock_research_server.py
    .venv\Scripts\python.exe mcp\stock_research_server.py
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from qlib_bridge import run_bridge as read_qlib_backtest
from stock_research_provider import StockResearchProvider


server = MCPServer(
    "astock-stock-research",
    title="AStock Read-only Stock Research",
    description="Validated, bounded A-share evidence via AkShare plus fixed read-only Qlib artifacts.",
    version="1.1.0",
)

_provider: StockResearchProvider | None = None


def configure_provider(provider: StockResearchProvider | None) -> None:
    """Inject a provider for tests/embedding; ``None`` restores lazy default."""

    global _provider
    _provider = provider


def _get_provider() -> StockResearchProvider:
    global _provider
    if _provider is None:
        _provider = StockResearchProvider()
    return _provider


READ_ONLY_OPEN_WORLD = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

READ_ONLY_LOCAL_ARTIFACT = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


@server.tool(
    name="get_stock_price",
    description="Return bounded daily A-share K-lines plus derived SMA, RSI and MACD indicators. Read-only; dates use YYYY-MM-DD.",
    annotations=READ_ONLY_OPEN_WORLD,
    structured_output=True,
)
def get_stock_price(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 120,
    adjust: str = "qfq",
) -> dict[str, Any]:
    return _get_provider().get_stock_price(symbol, start_date, end_date, limit, adjust)


@server.tool(
    name="get_financial_report",
    description="Return disclosed financial reports with bounded institution and major-shareholder context. Read-only; as_of uses YYYY-MM-DD and quarter uses YYYYQ1..YYYYQ4.",
    annotations=READ_ONLY_OPEN_WORLD,
    structured_output=True,
)
def get_financial_report(
    symbol: str,
    as_of: str | None = None,
    quarter: str | None = None,
) -> dict[str, Any]:
    return _get_provider().get_financial_report(symbol, as_of, quarter)


@server.tool(
    name="get_news",
    description="Return bounded timestamped media news for one A-share code. This is not an official-announcement feed. Read-only.",
    annotations=READ_ONLY_OPEN_WORLD,
    structured_output=True,
)
def get_news(symbol: str, limit: int = 10, as_of: str | None = None) -> dict[str, Any]:
    return _get_provider().get_news(symbol, limit, as_of)


@server.tool(
    name="get_sector_rank",
    description="Return a bounded current industry or concept sector ranking. sector_type is industry or concept. Read-only.",
    annotations=READ_ONLY_OPEN_WORLD,
    structured_output=True,
)
def get_sector_rank(sector_type: str = "industry", limit: int = 10) -> dict[str, Any]:
    return _get_provider().get_sector_rank(sector_type, limit)


@server.tool(
    name="get_capital_flow",
    description="Return bounded stock fund-flow history when symbol is supplied, otherwise a market fund-flow ranking for 今日/3日/5日/10日. Read-only and not a trading signal.",
    annotations=READ_ONLY_OPEN_WORLD,
    structured_output=True,
)
def get_capital_flow(
    symbol: str | None = None,
    indicator: str = "5日",
    limit: int = 10,
) -> dict[str, Any]:
    return _get_provider().get_capital_flow(symbol, indicator, limit)


@server.tool(
    name="run_backtest",
    description=(
        "Return the latest validated local Qlib CSI800/Alpha158 historical "
        "backtest summary and requested-symbol cross-sectional rankings. "
        "Compatibility name only: this read-only tool does not start training, "
        "run a dynamic backtest, accept paths, write files, publish, or trade."
    ),
    annotations=READ_ONLY_LOCAL_ARTIFACT,
    structured_output=True,
)
def run_backtest(
    target_date: str | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    # Explicit snake_case MCP → camelCase fixed-bridge mapping. No caller path,
    # experiment id, command, config or model name crosses this boundary.
    return read_qlib_backtest(
        {"targetDate": target_date, "symbols": list(symbols or [])}
    )


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()


__all__ = [
    "configure_provider",
    "get_capital_flow",
    "get_financial_report",
    "get_news",
    "get_sector_rank",
    "get_stock_price",
    "main",
    "run_backtest",
    "server",
]
