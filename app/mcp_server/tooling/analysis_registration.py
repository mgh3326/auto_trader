"""Analysis tool registration and MCP wire-up."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.mcp_server.tooling.analysis_tool_handlers import (
    analyze_portfolio_impl,
    analyze_stock_batch_impl,
    analyze_stock_impl,
    get_correlation_impl,
    get_disclosures_impl,
    get_dividends_impl,
    get_fear_greed_index_impl,
    get_top_stocks_impl,
    screen_stocks_impl,
)
from app.mcp_server.tooling.momentum_candidates import get_momentum_candidates_impl
from app.mcp_server.tooling.research_pipeline_read import (
    research_session_get_impl,
    research_session_list_recent_impl,
    research_summary_get_impl,
    stage_analysis_get_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

ANALYSIS_TOOL_NAMES: set[str] = {
    "analyze_stock",
    "analyze_portfolio",
    "analyze_stock_batch",
    "screen_stocks",
    # ROB-359: "recommend_stocks" is intentionally registry-hidden (parked).
    # screen_stocks is the single candidate-discovery entrypoint; the
    # recommend_stocks_impl implementation is retained in
    # analysis_tool_handlers for a future narrow build_buy_plan tool.
    "get_top_stocks",
    "get_disclosures",
    "get_correlation",
    "get_dividends",
    "get_fear_greed_index",
    "get_momentum_candidates",
    "research_session_get",
    "research_session_list_recent",
    "stage_analysis_get",
    "research_summary_get",
}


def register_analysis_tools(mcp: FastMCP) -> None:
    """Register MCP tools for analysis, screening, and ranking utilities."""

    @mcp.tool(
        name="get_momentum_candidates",
        description=(
            "Read-only early-catch candidates for 급등 Korean stocks from persisted "
            "Naver Stock momentum snapshots. Scores cross-surface signals such as "
            "searchTop, quantTop, up, priceTop, KRX/NXT confirmation, rank deltas, "
            "and theme leadership. Does not fetch Naver or mutate broker/order state."
        ),
    )
    async def get_momentum_candidates(
        market: str = "kr",
        date: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await get_momentum_candidates_impl(
            market=market,
            date=date,
            limit=limit,
        )

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
    ) -> dict[str, Any]:
        return await get_top_stocks_impl(
            market=market,
            ranking_type=ranking_type,
            limit=limit,
        )

    @mcp.tool(
        name="get_disclosures",
        description=(
            "Get DART (OPENDART) disclosure filings for Korean corporations. "
            "Supports direct 6-digit stock-code inputs (e.g., '005930') and best-effort "
            "Korean company-name inputs (e.g., '삼성전자'). "
            "Returns filing date, report name, report number, and "
            "corporation name."
        ),
    )
    async def get_disclosures(
        symbol: str,
        days: int = 30,
        limit: int = 20,
        report_type: str | None = None,
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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
        include_rotation_plan: bool = False,
    ) -> dict[str, Any]:
        return await analyze_portfolio_impl(
            symbols=symbols,
            market=market,
            include_peers=include_peers,
            include_rotation_plan=include_rotation_plan,
        )

    @mcp.tool(
        name="analyze_stock_batch",
        description=(
            "Analyze multiple stocks in parallel with compact summaries. "
            "Returns per-symbol compact summary (symbol, price, RSI, consensus, supports/resistances) "
            "by default, or full analysis when quick=False."
        ),
    )
    async def analyze_stock_batch(
        symbols: list[str | int],
        market: str | None = None,
        include_peers: bool = False,
        quick: bool = True,
    ) -> dict[str, Any]:
        return await analyze_stock_batch_impl(
            symbols=symbols,
            market=market,
            include_peers=include_peers,
            quick=quick,
        )

    @mcp.tool(
        name="screen_stocks",
        description=(
            "Screen stocks across markets (KR/US/Crypto) with filters. "
            "KR supports kospi/kosdaq/konex/all, 30-day ADV via adv_krw_min "
            "(1B KRW conservative, 5B KRW aggressive), instrument_types, "
            "and exclude_sectors. "
            "sort_by='trade_amount' is supported for KR and crypto only; "
            "for US use 'volume', 'market_cap', or 'change_rate'."
        ),
    )
    async def screen_stocks(
        market: Literal["kr", "kospi", "kosdaq", "konex", "all", "us", "crypto"] = "kr",
        asset_type: Literal["stock", "etf", "etn"] | None = None,
        category: str | None = None,
        sector: str | None = None,
        exclude_sectors: list[str] | None = None,
        instrument_types: list[
            Literal["common", "preferred", "etf", "reit", "spac", "unknown"]
        ]
        | None = None,
        strategy: str | None = None,
        sort_by: Literal[
            "volume",
            "trade_amount",
            "market_cap",
            "change_rate",
            "dividend_yield",
            "rsi",
        ]
        | None = None,
        sort_order: Literal["asc", "desc"] = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        min_dividend: float | None = None,
        min_analyst_buy: float | None = None,
        max_rsi: float | None = None,
        adv_krw_min: int | None = None,
        market_cap_min_krw: int | None = None,
        market_cap_max_krw: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await screen_stocks_impl(
            market=market,
            asset_type=asset_type,
            category=category,
            sector=sector,
            exclude_sectors=exclude_sectors,
            instrument_types=instrument_types,
            strategy=strategy,
            sort_by=sort_by,
            sort_order=sort_order,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            min_dividend=min_dividend,
            min_analyst_buy=min_analyst_buy,
            max_rsi=max_rsi,
            adv_krw_min=adv_krw_min,
            market_cap_min_krw=market_cap_min_krw,
            market_cap_max_krw=market_cap_max_krw,
            limit=limit,
        )

    # ROB-359: recommend_stocks is intentionally NOT registered on the MCP tool
    # surface (registry-hidden / parked). Rationale: its role overlapped
    # ambiguously with screen_stocks and it could be invoked as a new-buy basis.
    # screen_stocks is now the single candidate-discovery entrypoint. The
    # read-only recommend_stocks_impl implementation is retained in
    # app.mcp_server.tooling.analysis_tool_handlers so it can be re-introduced
    # later as a narrow build_buy_plan tool. Do not call recommend_stocks from
    # active report/operator prompts.

    @mcp.tool(
        name="get_dividends",
        description="Get dividend information for US stocks (via yfinance).",
    )
    async def get_dividends(symbol: str) -> dict[str, Any]:
        return await get_dividends_impl(symbol=symbol)

    @mcp.tool(
        name="get_fear_greed_index",
        description=(
            "Get the Crypto Fear & Greed Index from Alternative.me with current and history."
        ),
    )
    async def get_fear_greed_index(days: int = 7) -> dict[str, Any]:
        return await get_fear_greed_index_impl(days=days)

    @mcp.tool(
        name="research_session_get",
        description="Returns 1 research session with its 4 latest stage rows and summary.",
    )
    async def research_session_get(session_id: int) -> dict[str, Any]:
        return await research_session_get_impl(session_id=session_id)

    @mcp.tool(
        name="research_session_list_recent",
        description="Returns recent N research sessions with status, decision, and confidence.",
    )
    async def research_session_list_recent(limit: int = 10) -> dict[str, Any]:
        return await research_session_list_recent_impl(limit=limit)

    @mcp.tool(
        name="stage_analysis_get",
        description="Returns one research stage analysis row by id.",
    )
    async def stage_analysis_get(stage_id: int) -> dict[str, Any]:
        return await stage_analysis_get_impl(stage_id=stage_id)

    @mcp.tool(
        name="research_summary_get",
        description="Returns one research summary with its linked stage rows by summary id.",
    )
    async def research_summary_get(summary_id: int) -> dict[str, Any]:
        return await research_summary_get_impl(summary_id=summary_id)


__all__ = ["ANALYSIS_TOOL_NAMES", "register_analysis_tools"]
