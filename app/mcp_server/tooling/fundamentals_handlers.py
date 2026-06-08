"""Fundamentals tool handlers and registration implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.fundamentals._crypto import (
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
from app.mcp_server.tooling.fundamentals._market_index import (
    handle_get_market_index,
)
from app.mcp_server.tooling.fundamentals._news import handle_get_news
from app.mcp_server.tooling.fundamentals._profiles import (
    handle_get_company_profile,
    handle_get_crypto_profile,
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

FUNDAMENTALS_TOOL_NAMES: set[str] = {
    "get_news",
    "get_company_profile",
    "get_crypto_profile",
    "get_financials",
    "get_insider_transactions",
    "get_earnings_calendar",
    "get_investor_trends",
    "get_investment_opinions",
    "get_valuation",
    "get_short_interest",
    "get_kimchi_premium",
    "get_funding_rate",
    "get_open_interest",
    "get_long_short_ratio",
    "get_crypto_market_regime",
    "get_crypto_catalysts",
    "get_market_index",
    "get_upbit_index",
    "get_upbit_altseason",
    "get_support_resistance",
    "get_sector_peers",
}


def _register_fundamentals_tools_impl(mcp: FastMCP) -> None:
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
            "Get earnings calendar for a US stock or date range. Returns earnings "
            "dates, EPS estimates and actuals. US stocks only."
        ),
    )
    async def get_earnings_calendar(
        symbol: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_earnings_calendar(symbol, from_date, to_date)

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
        name="get_investment_opinions",
        description=(
            "Get securities firm investment opinions and target prices for a US or "
            "Korean stock. Returns analyst ratings, price targets, and upside potential."
        ),
    )
    async def get_investment_opinions(
        symbol: str | int,
        limit: int = 10,
        market: str | None = None,
    ) -> dict[str, Any]:
        return await handle_get_investment_opinions(symbol, limit, market)

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
        name="get_funding_rate",
        description=(
            "Get futures funding rate for a cryptocurrency from Binance. Positive = "
            "longs pay shorts, negative = shorts pay longs."
        ),
    )
    async def get_funding_rate(
        symbol: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return await handle_get_funding_rate(symbol, limit)

    @mcp.tool(
        name="get_open_interest",
        description=(
            "Get Binance USD-M futures open interest for a crypto symbol: current "
            "open interest plus recent history (sum OI and notional USD value) and "
            "the OI change over the window. Read-only public Binance data. "
            "period in {5m,15m,30m,1h,2h,4h,6h,12h,1d}."
        ),
    )
    async def get_open_interest(
        symbol: str,
        period: str = "1h",
        limit: int = 30,
    ) -> dict[str, Any]:
        return await handle_get_open_interest(symbol, period, limit)

    @mcp.tool(
        name="get_long_short_ratio",
        description=(
            "Get Binance USD-M long/short ratio for a crypto symbol: global account "
            "ratio (retail sentiment) and top-trader position ratio (smart money), "
            "each with current value, recent history, and a retail-vs-smart-money "
            "divergence note. Read-only public Binance data. "
            "period in {5m,15m,30m,1h,2h,4h,6h,12h,1d}."
        ),
    )
    async def get_long_short_ratio(
        symbol: str,
        period: str = "1h",
        limit: int = 30,
    ) -> dict[str, Any]:
        return await handle_get_long_short_ratio(symbol, period, limit)

    @mcp.tool(
        name="get_crypto_market_regime",
        description=(
            "Get crypto market-regime signals from the crypto_insight_snapshots store "
            "(read-only): Fear&Greed (fng), DeFi TVL by protocol, stablecoin supply, "
            "TradingView breadth, aggregate open interest. Each field is independently "
            "fresh/stale/missing/disabled — only fng is populated by default; "
            "tvl/stablecoin/breadth need operator-enabled providers and aggregate_oi "
            "(coinglass) is a disabled PoC. No arguments."
        ),
    )
    async def get_crypto_market_regime() -> dict[str, Any]:
        return await handle_get_crypto_market_regime()

    @mcp.tool(
        name="get_crypto_catalysts",
        description=(
            "Get crypto supply/event catalysts (read-only): token unlocks (Tokenomist, "
            "disabled PoC today), Upbit notices (listings / 유의 / 점검), and Upbit "
            "market warnings (CAUTION). Each source is independently "
            "fresh/disabled/unavailable. Pass symbol (e.g. 'XRP') to scope to one coin, "
            "or omit for market-wide. days windows the notices feed."
        ),
    )
    async def get_crypto_catalysts(
        symbol: str | None = None,
        days: int = 14,
    ) -> dict[str, Any]:
        return await handle_get_crypto_catalysts(symbol, days)

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
            "24h only (multi-period breadth is a separate follow-up)."
        ),
    )
    async def get_upbit_altseason() -> dict[str, Any]:
        return await handle_get_upbit_altseason()

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
    "FUNDAMENTALS_TOOL_NAMES",
    "_register_fundamentals_tools_impl",
    "_get_support_resistance_impl",
]
