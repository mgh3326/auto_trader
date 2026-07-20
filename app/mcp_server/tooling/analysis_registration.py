"""Analysis tool registration and MCP wire-up."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.mcp_server.tooling.analysis_tool_handlers import (
    analyze_portfolio_impl,
    analyze_stock_batch_impl,
    analyze_stock_impl,
    get_correlation_impl,
    get_crypto_top_movers_impl,
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
from app.mcp_server.tooling.screener_snapshot_tool import screen_stocks_snapshot_impl
from app.mcp_server.tooling.theme_events import get_theme_events_impl

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Full analysis tool namespace. get_crypto_fear_greed registers on every
# profile (ROB-503).
ANALYSIS_TOOL_NAMES: set[str] = {
    "analyze_stock",
    "analyze_portfolio",
    "analyze_stock_batch",
    "screen_stocks",
    "screen_stocks_snapshot",
    # ROB-359: "recommend_stocks" is intentionally registry-hidden (parked).
    # screen_stocks is the single candidate-discovery entrypoint; the
    # recommend_stocks_impl implementation is retained in
    # analysis_tool_handlers for a future narrow build_buy_plan tool.
    "get_top_stocks",
    "get_crypto_top_movers",
    "get_disclosures",
    "get_correlation",
    "get_dividends",
    "get_crypto_fear_greed",
    "get_momentum_candidates",
    "get_theme_events",
    "research_session_get",
    "research_session_list_recent",
    "stage_analysis_get",
    "research_summary_get",
}


def register_analysis_tools(
    mcp: FastMCP,
) -> None:
    """Register MCP tools for analysis, screening, and ranking utilities."""

    @mcp.tool(
        name="get_momentum_candidates",
        description=(
            "Use for KR-only intraday 급등 early-catch scoring; for general filtered "
            "discovery use screen_stocks or screen_stocks_snapshot, and for simple "
            "market rankings use get_top_stocks. Read-only early-catch candidates "
            "for 급등 Korean stocks from persisted "
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
        name="get_theme_events",
        description=(
            "Read-only 테마/업종 클러스터 snapshots from persisted Naver Stock theme "
            "events (10-minute intraday collector). Returns rank/name/change_rate/"
            "trade_value/stock_count/leader_symbols per item, with an intraday "
            "data_state=stale tag when the latest snapshot is >20min old during a "
            "live KRX session. Does not fetch Naver or mutate broker/order state."
        ),
    )
    async def get_theme_events(
        market: str = "kr",
        event_kind: str = "all",
        top_n: int = 20,
        trading_date: str | None = None,
        at: str | None = None,
        include_stocks: bool = False,
    ) -> dict[str, Any]:
        return await get_theme_events_impl(
            market=market,
            event_kind=event_kind,
            top_n=top_n,
            trading_date=trading_date,
            at=at,
            include_stocks=include_stocks,
        )

    @mcp.tool(
        name="get_top_stocks",
        description=(
            "Use for a simple ranking-type sort with no filter parameters; for "
            "filtered candidate discovery use screen_stocks or the persisted-preset "
            "screen_stocks_snapshot, and for KR intraday 급등 scoring use "
            "get_momentum_candidates. Get top stocks by ranking type across different "
            "markets (KR/US/Crypto). "
            "KR: volume, market_cap, gainers, losers, foreign_net_buy, "
            "foreign_net_sell (foreigners = back-compat alias for foreign_net_buy). "
            "Foreign rankings expose named foreign_net_qty / foreign_net_amount "
            "fields (no longer stuffed into volume/trade_amount), backfill "
            "market_cap from fundamentals snapshots with a market_cap_source "
            "provenance tag, and apply a default-ON liquidity filter "
            "(|foreign_net_amount| >= FOREIGNERS_MIN_NET_AMOUNT_KRW, default 1억 "
            "KRW; pass include_illiquid=true to bypass). "
            "min_market_cap (KR only, raw KRW) drops rows with a KNOWN market_cap "
            "below the floor — never excludes a row just because KIS omitted "
            "market_cap for that ranking type (honest, never fabricated); the "
            "excluded count is echoed under market_cap_filter. Useful on "
            "ranking_type=losers to cut illiquid junk-cap noise before a "
            "지지선 매수 후보 scan; for a ranked-by-support-distance view use "
            "screen_stocks_snapshot(preset='support_proximity') instead. "
            "US: volume, market_cap, gainers, losers "
            "Crypto: volume, gainers, losers, relative_strength (vs BTC 24h)."
        ),
    )
    async def get_top_stocks(
        market: str = "kr",
        ranking_type: str = "volume",
        limit: int = 20,
        include_illiquid: bool = False,
        min_market_cap: float | None = None,
    ) -> dict[str, Any]:
        return await get_top_stocks_impl(
            market=market,
            ranking_type=ranking_type,
            limit=limit,
            include_illiquid=include_illiquid,
            min_market_cap=min_market_cap,
        )

    @mcp.tool(
        name="get_crypto_top_movers",
        description=(
            "Read-only Upbit KRW crypto candidate discovery. "
            "ranking_type supports relative_strength (default, vs BTC 24h), "
            "volume, gainers, and losers. Returns the same ranking row shape as "
            "get_top_stocks(market='crypto') with relative-strength fields when "
            "ranking_type='relative_strength'."
        ),
    )
    async def get_crypto_top_movers(
        ranking_type: str = "relative_strength",
        limit: int = 20,
    ) -> dict[str, Any]:
        return await get_crypto_top_movers_impl(
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
            "Analyze multiple stocks in parallel with compact summaries. For KR/crypto "
            "and US regular-session analysis, a planned batch already returns a fresh "
            "price plus context, so a separate get_quote is normally unnecessary. "
            "Exception: use get_quote(include_extended_hours=True) for US premarket/"
            "afterhours; use get_quote for previous_close, and get_ohlcv when OHLC "
            "candles are required. "
            "Returns per-symbol compact summary (symbol, price, RSI, consensus, supports/resistances) "
            "by default, or full analysis when quick=False. "
            "When include_position=True (default), each compact summary carries a "
            "'position' field: an array (one entry per holding account, since a symbol "
            "may be held across e.g. toss+samsung) of {account, account_mode, qty, "
            "avg_buy_price, pnl_pct, order_routable}, or null when not held. "
            "order_routable mirrors get_holdings: manual non-toss (samsung/수기) -> "
            "false, toss_api -> TOSS_LIVE_ORDER_MUTATIONS_ENABLED, kis/upbit -> true "
            "(ROB-562); account_mode is a provenance label, NOT a routing selector. "
            "Slowly-changing provider data (KR naver valuation/opinions, US yfinance "
            "valuation/opinions + finnhub profile) is served from an intraday "
            "fetch-layer cache; price/RSI/support-resistance/recommendation are "
            "recomputed fresh on every call. Each result carries cache_hit (whether "
            "cached provider data was served) and derived_as_of (ISO KST timestamp "
            "of when that provider data was fetched). refresh=True bypasses the "
            "cache read and re-fetches provider data fresh (ROB-638). When a "
            "non-stale analysis_artifact already covers a symbol, that compact "
            "summary also carries fresh_artifact_exists {artifact_uuid, as_of, "
            "kind} — a soft reuse hint (fetch via analysis_artifact_get); the "
            "analysis still runs (ROB-648). "
            "decision_history_account_mode='kis_mock' switches the advisory "
            "decision_history block to the explicit mock/counterfactual branch; "
            "the default keeps the live/default lesson corpus and excludes mirror "
            "counterfactual rows."
        ),
    )
    async def analyze_stock_batch(
        symbols: list[str | int],
        market: str | None = None,
        include_peers: bool = False,
        quick: bool = True,
        include_position: bool = True,
        refresh: bool = False,
        decision_history_account_mode: Literal["kis_mock"] | None = None,
    ) -> dict[str, Any]:
        return await analyze_stock_batch_impl(
            symbols=symbols,
            market=market,
            include_peers=include_peers,
            quick=quick,
            include_position=include_position,
            refresh=refresh,
            decision_history_account_mode=decision_history_account_mode,
        )

    @mcp.tool(
        name="screen_stocks",
        description=(
            "Use for live, generic filter/sort discovery across KR/US/Crypto; use "
            "screen_stocks_snapshot for curated persisted presets, get_top_stocks "
            "for simple rankings, and get_momentum_candidates for KR intraday 급등 "
            "scoring. If this returns reason='krx_session_expired', fall back to "
            "screen_stocks_snapshot. Screen stocks across markets (KR/US/Crypto) "
            "with filters. "
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

    @mcp.tool(
        name="screen_stocks_snapshot",
        description=(
            "Use for curated persisted-preset discovery; use screen_stocks for live "
            "generic filters, get_top_stocks for simple rankings, and "
            "get_momentum_candidates for KR intraday 급등 scoring. Snapshot-backed "
            "screener: run one or more /invest/screener presets over "
            "their base snapshots (Discovery Workflow, ROB-515). "
            "Unlike screen_stocks (generic tvscreener/KIS path), this serves persisted "
            "screener snapshot data. preset can be a single ID or a comma-separated "
            "list (e.g. 'consecutive_gainers,double_buy'); presets can also be a "
            "list for multi-preset sweeps with symbol deduplication and "
            "matchedPresets tagging. "
            "preset='support_proximity' (KR, ROB-976) ranks a quality-filtered "
            "blue-chip universe by distance to its nearest support level "
            "(reuses get_support_resistance's fib/volume-profile/Bollinger "
            "clustering, bounded to top candidates by market cap); symbols with "
            "no support below the current price are excluded, never fabricated. "
            "riskContext on each row carries the support kind/strength. "
            "filters=[{field, operator(gte|lte|eq), value}] tune the preset's "
            "thresholds (threaded for consecutive_gainers and crypto). "
            "exclude_held hides KIS-live portfolio symbols; exclude_watched is "
            "accepted for compatibility but currently emits an explicit unsupported "
            "warning in MCP because no user watchlist context is wired. "
            "exclude_symbols hides already-processed symbols. "
            "min_analyst_count (coverage), min_analyst_buy_count (buy-count), "
            "min_market_cap (raw marketCapValue), and min/max_market_cap_eok "
            "(float, unit 1억원) apply discovery-quality filters across the result set. "
            "sort='matched_presets_desc' ranks multi-preset intersections first. "
            "Read-only. Preset sweeps are capped at 5 presets; analyst filters require at most "
            "200 merged rows before enrichment. "
            "KR analyst consensus (buy/hold/sell counts + target prices) is cached "
            "daily per symbol (Redis, KST-date TTL); the displayed target-upside is "
            "recomputed each call from a fresh price so it stays intraday-current. "
            "min_analyst_* filters resolve consensus from the cache and only the "
            "returned page is enriched. "
            "priceLabel, changePctLabel, and metricValueLabel are values at the "
            "snapshot time and "
            "may be stale by up to one session; before confirming a candidate, "
            "revalidate price/change with get_quote and technical analysis with "
            "analyze_stock_batch. analysisContext.rsi14, when present, is the "
            "separately exposed RSI field. "
            "Results are capped (default 40) and paginated via limit/offset."
        ),
    )
    async def screen_stocks_snapshot(
        preset: str | None = None,
        presets: list[str] | None = None,
        market: Literal["kr", "us", "crypto"] = "kr",
        filters: list[dict[str, Any]] | None = None,
        exclude_watched: bool = False,
        exclude_held: bool = False,
        exclude_symbols: list[str] | None = None,
        min_analyst_count: int | None = None,
        min_analyst_buy_count: int | None = None,
        min_market_cap: float | None = None,
        min_market_cap_eok: float | None = None,
        max_market_cap_eok: float | None = None,
        sort: Literal["matched_presets_desc"] | None = None,
        limit: int = 40,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await screen_stocks_snapshot_impl(
            preset=preset,
            presets=presets,
            market=market,
            filters=filters,
            exclude_watched=exclude_watched,
            exclude_held=exclude_held,
            exclude_symbols=exclude_symbols,
            min_analyst_count=min_analyst_count,
            min_analyst_buy_count=min_analyst_buy_count,
            min_market_cap=min_market_cap,
            min_market_cap_eok=min_market_cap_eok,
            max_market_cap_eok=max_market_cap_eok,
            sort=sort,
            limit=limit,
            offset=offset,
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
        name="get_crypto_fear_greed",
        description=(
            "Get the Crypto Fear & Greed Index from Alternative.me with current "
            "and history."
        ),
    )
    async def get_crypto_fear_greed(days: int = 7) -> dict[str, Any]:
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
