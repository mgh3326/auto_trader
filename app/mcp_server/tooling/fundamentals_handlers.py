"""Fundamentals tool handlers and registration implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.fundamentals._cost_basis_distribution import (
    _DEFAULT_GET_COST_BASIS_DISTRIBUTION_IMPL,
)
from app.mcp_server.tooling.fundamentals._cost_basis_distribution import (
    get_cost_basis_distribution_impl as _get_cost_basis_distribution_impl,
)
from app.mcp_server.tooling.fundamentals._crypto import (
    handle_get_crypto_order_flow,
    handle_get_crypto_social,
    handle_get_funding_rate,
    handle_get_kimchi_premium,
    handle_get_long_short_ratio,
    handle_get_open_interest,
)
from app.mcp_server.tooling.fundamentals._crypto_catalysts import (
    handle_get_crypto_catalysts,
)
from app.mcp_server.tooling.fundamentals._crypto_regime import (
    handle_get_crypto_market_regime,
)
from app.mcp_server.tooling.fundamentals._financials import (
    handle_get_earnings_calendar,
    handle_get_financials,
    handle_get_insider_transactions,
)
from app.mcp_server.tooling.fundamentals._fx_rates import handle_get_fx_rate
from app.mcp_server.tooling.fundamentals._intraday_investor_flow import (
    handle_get_intraday_investor_flow,
)
from app.mcp_server.tooling.fundamentals._market_index import (
    handle_get_market_index,
)
from app.mcp_server.tooling.fundamentals._news import handle_get_news
from app.mcp_server.tooling.fundamentals._profiles import (
    handle_get_company_profile,
    handle_get_crypto_profile,
)
from app.mcp_server.tooling.fundamentals._retail_sentiment import (
    handle_get_retail_sentiment,
)
from app.mcp_server.tooling.fundamentals._sector_peers import (
    handle_get_sector_peers,
)
from app.mcp_server.tooling.fundamentals._support_resistance import (
    _DEFAULT_GET_SUPPORT_RESISTANCE_IMPL,
)
from app.mcp_server.tooling.fundamentals._support_resistance import (
    get_support_resistance_impl as _get_support_resistance_impl,
)
from app.mcp_server.tooling.fundamentals._upbit_index import (
    handle_get_upbit_altseason,
    handle_get_upbit_index,
)
from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
    handle_get_investor_trends,
    handle_get_short_interest,
    handle_get_valuation,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Full fundamentals tool namespace. Crypto research tools register on every
# profile (ROB-503 restored them from the ROB-488 crypto-profile gate).
FUNDAMENTALS_TOOL_NAMES: set[str] = {
    "get_news",
    "get_company_profile",
    "get_crypto_profile",
    "get_financials",
    "get_insider_transactions",
    "get_earnings_calendar",
    "get_investor_trends",
    "get_intraday_investor_flow",
    "get_investment_opinions",
    "get_valuation",
    "get_short_interest",
    "get_kimchi_premium",
    "get_crypto_funding_rate",
    "get_crypto_open_interest",
    "get_crypto_long_short_ratio",
    "get_crypto_market_regime",
    "get_crypto_catalysts",
    "get_crypto_order_flow",
    "get_crypto_social",
    "get_retail_sentiment",
    "get_fx_rate",
    "get_market_index",
    "get_upbit_index",
    "get_upbit_altseason",
    "get_support_resistance",
    "get_cost_basis_distribution",
    "get_sector_peers",
}

# Crypto research subset (registered on all profiles; kept as metadata for
# tests/surface audits).
CRYPTO_FUNDAMENTALS_TOOL_NAMES: set[str] = {
    "get_crypto_profile",
    "get_kimchi_premium",
    "get_crypto_funding_rate",
    "get_crypto_open_interest",
    "get_crypto_long_short_ratio",
    "get_crypto_market_regime",
    "get_crypto_catalysts",
    "get_crypto_order_flow",
    "get_crypto_social",
    "get_upbit_index",
    "get_upbit_altseason",
}


def _register_fundamentals_tools_impl(
    mcp: FastMCP,
) -> None:
    @mcp.tool(
        name="get_news",
        description=(
            "Get recent news for a stock or cryptocurrency. Supports US stocks "
            "(Finnhub), Korean stocks (Naver Finance), and crypto (Finnhub)."
        ),
    )
    async def get_news(
        symbol: str | int,
        market: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return await handle_get_news(symbol, market, limit)

    @mcp.tool(
        name="get_company_profile",
        description=(
            "Get company profile for a US or Korean stock. Crypto symbols like "
            "KRW-BTC are not supported; use get_crypto_profile for cryptocurrencies."
        ),
    )
    async def get_company_profile(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_company_profile(symbol, market)

    @mcp.tool(
        name="get_crypto_profile",
        description=(
            "Get cryptocurrency profile data from CoinGecko. Accepts Upbit market "
            "code (e.g. KRW-BTC) or plain symbol (e.g. BTC)."
        ),
    )
    async def get_crypto_profile(symbol: str) -> dict[str, Any]:
        return await handle_get_crypto_profile(symbol)

    @mcp.tool(
        name="get_financials",
        description=(
            "Get financial statements for a US or Korean stock. Supports income "
            "statement, balance sheet, and cash flow."
        ),
    )
    async def get_financials(
        symbol: str,
        statement: str = "income",
        freq: str = "annual",
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_financials(symbol, statement, freq, market)

    @mcp.tool(
        name="get_insider_transactions",
        description=(
            "Get insider transactions for a US stock. Returns name, transaction "
            "type, shares, price, date. US stocks only."
        ),
    )
    async def get_insider_transactions(
        symbol: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await handle_get_insider_transactions(symbol, limit)

    @mcp.tool(
        name="get_earnings_calendar",
        description=(
            "Get earnings calendar for a US or Korean stock/date range. "
            "US uses Finnhub and includes EPS/revenue estimates when available. "
            "Korean equities read existing market_events rows from WiseFn/DART; "
            "KR shareholder meetings, ex-dividend dates, IR, and conferences are "
            "not included yet."
        ),
    )
    async def get_earnings_calendar(
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_earnings_calendar(symbol, from_date, to_date, market)

    @mcp.tool(
        name="get_investor_trends",
        description=(
            "Get foreign, institutional, and individual investor trading trends "
            "for a Korean stock. Returns net buy/sell data by investor type. "
            "Supports daily/weekly/monthly aggregation. Korean stocks only."
        ),
    )
    async def get_investor_trends(
        symbol: str,
        days: int = 20,
        period: str = "day",
    ) -> dict[str, Any]:
        return await handle_get_investor_trends(symbol, days, period)

    @mcp.tool(
        name="get_intraday_investor_flow",
        description=(
            "Get same-day intraday provisional foreign/institution net-buy "
            "quantity estimates for a Korean stock. Returns KIS "
            "investor-trend-estimate rows with provisional/as_of metadata. "
            "Korean stocks only. The KIS payload carries no date, so session "
            "attribution is machine-readable via these ADDITIVE fields: "
            "`confidence` ('observed' = KRX session live; 'inferred' = "
            "after-close same session day, today's date is correct but unstamped "
            "by the payload; 'carry_over' = future slot or non-session day, rows "
            "belong to a prior session), `as_of_date` (ISO DATE; for carry_over "
            "this is the previous XKRX trading session DATE only — never a "
            "fabricated prior-day time), `is_prior_session` (bool), and `warning` "
            "(structured {code, message} when carry_over, else null). `as_of` is "
            "a full ISO datetime only for observed/inferred and is null for "
            "carry_over — it is never silently upgraded from null to a stamped "
            "value. The existing `as_of`/`note` keys are unchanged for back-compat."
        ),
    )
    async def get_intraday_investor_flow(
        symbol: str,
    ) -> dict[str, Any]:
        return await handle_get_intraday_investor_flow(symbol)

    @mcp.tool(
        name="get_investment_opinions",
        description=(
            "Get securities firm investment opinions and target prices for a US or "
            "Korean stock. Returns analyst ratings, price targets, and upside "
            "potential. KR consensus (buy/hold/sell counts and avg/median/min/max "
            "target, upside_pct) is aggregated ONLY over opinions dated within "
            "opinion_window_months (default 12, clamped 1-60); rows dated past the "
            "window are excluded and reported via rows_excluded_stale, while "
            "undated rows are KEPT (fail-open) and counted in rows_undated. Target "
            "prices that are extreme outliers vs the current price (above +300% or "
            "below -75% upside, e.g. pre-split garbage) are excluded from target "
            "stats only — see target_price_outlier_count and target_price_honest. "
            "If no opinion survives the window, target stats and upside_pct are "
            "null and counts are 0 — there is NO fallback to stale averages (check "
            "rows_total, rows_used, newest_opinion_date, window_months metadata). "
            "The opinions list still includes older rows for reference. US "
            "consensus comes from the vendor (yfinance) and ignores "
            "opinion_window_months."
        ),
    )
    async def get_investment_opinions(
        symbol: str | int,
        limit: int = 10,
        market: str | None = None,
        opinion_window_months: int = 12,
    ) -> dict[str, Any]:
        return await handle_get_investment_opinions(
            symbol, limit, market, opinion_window_months
        )

    @mcp.tool(
        name="get_valuation",
        description=(
            "Get valuation metrics for a US or Korean stock. Crypto symbols are not "
            "supported. Returns PER, PBR, ROE, dividend yield, 52-week high/low, "
            "current price, and position within 52-week range."
        ),
    )
    async def get_valuation(
        symbol: str | int,
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_valuation(symbol, market)

    @mcp.tool(
        name="get_short_interest",
        description=(
            "Get short selling data for a Korean stock. Accepts only 6-digit "
            "Korean equity codes like '005930'. US tickers and crypto symbols "
            "are not supported."
        ),
    )
    async def get_short_interest(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:
        return await handle_get_short_interest(symbol, days)

    @mcp.tool(
        name="get_kimchi_premium",
        description=(
            "Get kimchi premium (김치 프리미엄) for cryptocurrencies. Compares Upbit "
            "KRW prices with Binance USDT prices to calculate premium percentage."
        ),
    )
    async def get_kimchi_premium(
        symbol: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return await handle_get_kimchi_premium(symbol)

    @mcp.tool(
        name="get_crypto_funding_rate",
        description=(
            "Get futures funding rate for a cryptocurrency from Binance. Positive = "
            "longs pay shorts, negative = shorts pay longs."
        ),
    )
    async def get_crypto_funding_rate(
        symbol: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return await handle_get_funding_rate(symbol, limit)

    @mcp.tool(
        name="get_crypto_open_interest",
        description=(
            "Get Binance USD-M futures open interest for a crypto symbol: current "
            "open interest plus recent history (sum OI and notional USD value) and "
            "the OI change over the window. Read-only public Binance data. "
            "period in {5m,15m,30m,1h,2h,4h,6h,12h,1d}."
        ),
    )
    async def get_crypto_open_interest(
        symbol: str,
        period: str = "1h",
        limit: int = 30,
    ) -> dict[str, Any]:
        return await handle_get_open_interest(symbol, period, limit)

    @mcp.tool(
        name="get_crypto_long_short_ratio",
        description=(
            "Get Binance USD-M long/short ratio for a crypto symbol: global account "
            "ratio (retail sentiment) and top-trader position ratio (smart money), "
            "each with current value, recent history, and a retail-vs-smart-money "
            "divergence note. Read-only public Binance data. "
            "period in {5m,15m,30m,1h,2h,4h,6h,12h,1d}."
        ),
    )
    async def get_crypto_long_short_ratio(
        symbol: str,
        period: str = "1h",
        limit: int = 30,
    ) -> dict[str, Any]:
        return await handle_get_long_short_ratio(symbol, period, limit)

    @mcp.tool(
        name="get_crypto_market_regime",
        description=(
            "Get crypto market-regime signals from the crypto_insight_snapshots "
            "store (read-only): Fear&Greed (fng), DeFi TVL by protocol, "
            "stablecoin supply, TradingView breadth, aggregate open interest. "
            "Each field is independently fresh/stale/missing/disabled — only "
            "fng is populated by default; tvl/stablecoin/breadth need "
            "operator-enabled providers and aggregate_oi (coinglass) is a "
            "disabled PoC. No arguments."
        ),
    )
    async def get_crypto_market_regime() -> dict[str, Any]:
        return await handle_get_crypto_market_regime()

    @mcp.tool(
        name="get_crypto_catalysts",
        description=(
            "Get crypto supply/event catalysts (read-only): token unlocks "
            "(Tokenomist, disabled PoC today), Upbit notices (listings / 유의 / "
            "점검), and Upbit market warnings (CAUTION). Each source is "
            "independently fresh/disabled/unavailable. Pass symbol (e.g. 'XRP') "
            "to scope to one coin, or omit for market-wide. days windows the "
            "notices feed."
        ),
    )
    async def get_crypto_catalysts(
        symbol: str | None = None,
        days: int = 14,
    ) -> dict[str, Any]:
        return await handle_get_crypto_catalysts(symbol, days)

    @mcp.tool(
        name="get_crypto_order_flow",
        description=(
            "Get Upbit recent-trade taker order-flow for a KRW crypto market "
            "(retail buy/sell pressure proxy). Returns multi-window (50/200/500) "
            "ratios and a 'consensus' verdict (direction, trend, confidence, note). "
            "Prefer 'consensus' over bare 'net' to filter transient noise. "
            "Read-only public Upbit data; count in [1,500] controls 'default_window'."
        ),
    )
    async def get_crypto_order_flow(symbol: str, count: int = 200) -> dict[str, Any]:
        return await handle_get_crypto_order_flow(symbol, count)

    @mcp.tool(
        name="get_crypto_social",
        description=(
            "Get CoinGecko community/developer social signals for a crypto symbol: "
            "sentiment_votes_up_pct, twitter_followers, reddit_subscribers, "
            "dev_commits_4w. Read-only; degrades (null fields) when CoinGecko "
            "lacks social data for the coin."
        ),
    )
    async def get_crypto_social(symbol: str) -> dict[str, Any]:
        return await handle_get_crypto_social(symbol)

    @mcp.tool(
        name="get_retail_sentiment",
        description=(
            "Get aggregate retail-discussion activity for a KR stock from Naver 종목토론 "
            "(rank + post/comment/reaction counts + overheat_flag). Aggregate-only — no "
            "raw post text. Live fetch is operator-gated (status='disabled' until "
            "enabled); a symbol outside the hot-discussion top-N returns status='not_ranked' "
            "(not zero). KR 6-digit codes only."
        ),
    )
    async def get_retail_sentiment(
        symbol: str,
        market: str = "kr",
        window: str = "1d",
    ) -> dict[str, Any]:
        return await handle_get_retail_sentiment(symbol, market, window)

    @mcp.tool(
        name="get_fx_rate",
        description=(
            "Get the current USD/KRW FX spot quote for exchange-timing and "
            "US-market cash conversion decisions. P1 supports only USDKRW "
            "spot lookup through the existing exchange-rate service; use "
            "ROB-565/follow-ups for account-routing total cost, trend, bank, "
            "or preferential effective-rate modeling."
        ),
    )
    async def get_fx_rate(
        pair: str = "USDKRW",
    ) -> dict[str, Any]:
        return await handle_get_fx_rate(pair)

    @mcp.tool(
        name="get_market_index",
        description=(
            "Get market index data. Supports KOSPI/KOSDAQ, major US indices "
            "(SPX/NASDAQ/DJI/VIX), and crypto market regime "
            "(CRYPTO=total market cap, BTC.D=BTC dominance via CoinGecko). "
            "Without symbol returns current major equity indices, with symbol "
            "adds OHLCV history (crypto has no history)."
        ),
    )
    async def get_market_index(
        symbol: str | None = None,
        period: str = "day",
        count: int = 20,
    ) -> dict[str, Any]:
        return await handle_get_market_index(symbol, period, count)

    @mcp.tool(
        name="get_upbit_index",
        description=(
            "Get Upbit digital-asset indices (디지털 자산 지수): market indices "
            "(UBMI=Upbit Market Index, UBAI=Upbit Altcoin Index, top-10/30) plus "
            "sector/strategy/theme indices, each with current value, 24h change, "
            "and yield/risk stats (daily~yearly yield, beta, sharpe, winRate). "
            "Read-only public data from datalab-static. Optional category in "
            "{market,sector,strategy,theme} filters the result."
        ),
    )
    async def get_upbit_index(
        category: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_upbit_index(category)

    @mcp.tool(
        name="get_upbit_altseason",
        description=(
            "Get an Upbit altseason snapshot: the UBAI/UBMI ratio (altcoin index "
            "vs market index) and 24h breadth (fraction of KRW-quoted alts beating "
            "BTC over 24h, derived from the official Upbit ticker). Higher ratio + "
            "higher breadth lean altseason. Read-only public data. Note: breadth is "
            "24h only (multi-period breadth is a separate follow-up). With constituents "
            "enabled, breadth.constituents lists KRW alts beating BTC with 24h change, "
            "vs-BTC relative strength, volume, and traded value."
        ),
    )
    async def get_upbit_altseason(
        include_constituents: bool = False,
        constituents_limit: int = 50,
    ) -> dict[str, Any]:
        return await handle_get_upbit_altseason(
            include_constituents=include_constituents,
            constituents_limit=constituents_limit,
        )

    @mcp.tool(
        name="get_support_resistance",
        description=(
            "Extract key support/resistance zones by combining Fibonacci levels, "
            "volume profile (POC/value area), and Bollinger Bands."
        ),
    )
    async def get_support_resistance(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:
        impl = _get_support_resistance_impl
        if not callable(impl):
            impl = _DEFAULT_GET_SUPPORT_RESISTANCE_IMPL
        return await impl(symbol, market)

    @mcp.tool(
        name="get_cost_basis_distribution",
        description=(
            "ESTIMATE holder cost-basis distribution (volume-by-price/VPVR) from the "
            "symbol's own trailing OHLCV — buckets with holder_share_pct, "
            "pct_holders_underwater/in_profit vs current price, vwap_estimate, "
            "heaviest_bucket. A proxy (estimate=true), NOT an exact holder file. "
            "kr/us/crypto. buckets in [2,100]."
        ),
    )
    async def get_cost_basis_distribution(
        symbol: str,
        market: str | None = None,
        buckets: int = 10,
    ) -> dict[str, Any]:
        impl = _get_cost_basis_distribution_impl
        if not callable(impl):
            impl = _DEFAULT_GET_COST_BASIS_DISTRIBUTION_IMPL
        return await impl(symbol, market, buckets)

    @mcp.tool(
        name="get_sector_peers",
        description=(
            "Get sector peer stocks for comparison. Supports Korean and US stocks. "
            "Not available for cryptocurrencies."
        ),
    )
    async def get_sector_peers(
        symbol: str,
        market: str = "",
        limit: int = 5,
        manual_peers: list[str] | None = None,
    ) -> dict[str, Any]:
        return await handle_get_sector_peers(symbol, market, limit, manual_peers)


__all__ = [
    "CRYPTO_FUNDAMENTALS_TOOL_NAMES",
    "FUNDAMENTALS_TOOL_NAMES",
    "_register_fundamentals_tools_impl",
    "_get_support_resistance_impl",
    "_get_cost_basis_distribution_impl",
]
