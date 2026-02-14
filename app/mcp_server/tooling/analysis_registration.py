"""Analysis tool registration and MCP wire-up."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.analysis_tool_handlers import (
    analyze_portfolio_impl,
    analyze_stock_impl,
    get_correlation_impl,
    get_disclosures_impl,
    get_dividends_impl,
    get_fear_greed_index_impl,
    get_top_stocks_impl,
    recommend_stocks_impl,
    screen_stocks_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

ANALYSIS_TOOL_NAMES: set[str] = {
    "analyze_stock",
    "analyze_portfolio",
    "screen_stocks",
    "recommend_stocks",
    "get_top_stocks",
    "get_disclosures",
    "get_correlation",
    "get_dividends",
    "get_fear_greed_index",
}


def register_analysis_tools(mcp: FastMCP) -> None:
    """Register MCP tools for analysis, screening, and ranking utilities."""

    @mcp.tool(
        name="get_top_stocks",
        description=(
            "Get top stocks by ranking type across different markets (KR/US/Crypto). "
            "KR: volume, market_cap, gainers, losers, foreigners "
            "US: volume, market_cap, gainers, losers "
            "Crypto: volume, gainers, losers."
        ),
    )
    async def get_top_stocks(
        market: str = "kr",
        ranking_type: str = "volume",
        limit: int = 20,
    ) -> dict:
        return await get_top_stocks_impl(
            market=market,
            ranking_type=ranking_type,
            limit=limit,
        )

    @mcp.tool(
        name="get_disclosures",
        description=(
            "Get DART (OPENDART) disclosure filings for Korean corporations. "
            "Supports both 6-digit corp codes (e.g., '005930') and Korean company names "
            "(e.g., '삼성전자'). Returns filing date, report name, report number, and "
            "corporation name."
        ),
    )
    async def get_disclosures(
        symbol: str,
        days: int = 30,
        limit: int = 20,
        report_type: str | None = None,
    ) -> dict:
        return await get_disclosures_impl(
            symbol=symbol,
            days=days,
            limit=limit,
            report_type=report_type,
        )

    @mcp.tool(
        name="get_correlation",
        description=(
            "Calculate Pearson correlation matrix between multiple assets. "
            "Supports Korean stocks (KIS), US stocks (yfinance), and crypto (Upbit). "
            "Uses daily closing prices over specified period."
        ),
    )
    async def get_correlation(
        symbols: list[str],
        period: int = 60,
    ) -> dict:
        return await get_correlation_impl(symbols=symbols, period=period)

    @mcp.tool(
        name="analyze_stock",
        description=(
            "Comprehensive stock analysis with quotes, indicators, support/resistance, "
            "and market-specific fundamentals."
        ),
    )
    async def analyze_stock(
        symbol: str | int,
        market: str | None = None,
        include_peers: bool = False,
    ) -> dict:
        return await analyze_stock_impl(
            symbol=symbol,
            market=market,
            include_peers=include_peers,
        )

    @mcp.tool(
        name="analyze_portfolio",
        description=(
            "Analyze multiple stocks in parallel. Returns per-symbol analysis plus "
            "portfolio summary."
        ),
    )
    async def analyze_portfolio(
        symbols: list[str | int],
        market: str | None = None,
        include_peers: bool = False,
    ) -> dict:
        return await analyze_portfolio_impl(
            symbols=symbols,
            market=market,
            include_peers=include_peers,
        )

    @mcp.tool(
        name="screen_stocks",
        description=(
            "Screen stocks across markets (KR/US/Crypto) with various filters."
        ),
    )
    async def screen_stocks(
        market: str = "kr",
        asset_type: str | None = None,
        category: str | None = None,
        strategy: str | None = None,
        sort_by: str = "volume",
        sort_order: str = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        max_rsi: float | None = None,
        limit: int = 20,
    ) -> dict:
        return await screen_stocks_impl(
            market=market,  # type: ignore[arg-type]
            asset_type=asset_type,  # type: ignore[arg-type]
            category=category,
            strategy=strategy,
            sort_by=sort_by,  # type: ignore[arg-type]
            sort_order=sort_order,  # type: ignore[arg-type]
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            limit=limit,
        )

    @mcp.tool(
        name="recommend_stocks",
        description=(
            "Recommend stocks from available top ranks using strategy-specific filters."
        ),
    )
    async def recommend_stocks(
        budget: float,
        market: str = "kr",
        strategy: str = "balanced",
        exclude_symbols: list[str] | None = None,
        sectors: list[str] | None = None,
        max_positions: int = 5,
        exclude_held: bool = True,
    ) -> dict:
        return await recommend_stocks_impl(
            budget=budget,
            market=market,
            strategy=strategy,
            exclude_symbols=exclude_symbols,
            sectors=sectors,
            max_positions=max_positions,
            exclude_held=exclude_held,
        )

    @mcp.tool(
        name="get_dividends",
        description="Get dividend information for US stocks (via yfinance).",
    )
    async def get_dividends(symbol: str) -> dict:
        return await get_dividends_impl(symbol=symbol)

    @mcp.tool(
        name="get_fear_greed_index",
        description=(
            "Get the Crypto Fear & Greed Index from Alternative.me with current and history."
        ),
    )
    async def get_fear_greed_index(days: int = 7) -> dict:
        return await get_fear_greed_index_impl(days=days)


__all__ = ["ANALYSIS_TOOL_NAMES", "register_analysis_tools"]
