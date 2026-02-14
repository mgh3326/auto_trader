"""Fundamentals tool handlers and registration implementation."""

from __future__ import annotations

import asyncio
import datetime
from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.fundamentals_sources_binance import (
    _fetch_funding_rate,
    _fetch_funding_rate_batch,
)
from app.mcp_server.tooling.fundamentals_sources_coingecko import (
    _fetch_coingecko_coin_profile,
    _normalize_crypto_base_symbol,
    _resolve_batch_crypto_symbols,
    _resolve_coingecko_coin_id,
)
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
    _fetch_earnings_calendar_finnhub,
    _fetch_financials_finnhub,
    _fetch_insider_transactions_finnhub,
    _fetch_news_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_indices import (
    _DEFAULT_INDICES,
    _INDEX_META,
    _fetch_index_kr_current,
    _fetch_index_kr_history,
    _fetch_index_us_current,
    _fetch_index_us_history,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_company_profile_naver,
    _fetch_financials_naver,
    _fetch_financials_yfinance,
    _fetch_investment_opinions_naver,
    _fetch_investment_opinions_yfinance,
    _fetch_investor_trends_naver,
    _fetch_kimchi_premium,
    _fetch_news_naver,
    _fetch_sector_peers_naver,
    _fetch_sector_peers_us,
    _fetch_valuation_naver,
    _fetch_valuation_yfinance,
    _map_coingecko_profile_to_output,
)
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_fibonacci,
    _calculate_volume_profile,
    _cluster_price_levels,
    _compute_indicators,
    _fetch_ohlcv_for_indicators,
    _fetch_ohlcv_for_volume_profile,
    _format_fibonacci_source,
    _split_support_resistance_levels,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
    is_crypto_market as _is_crypto_market,
    is_korean_equity_code as _is_korean_equity_code,
    is_us_equity_symbol as _is_us_equity_symbol,
    normalize_symbol_input as _normalize_symbol_input,
    resolve_market_type as _resolve_market_type,
    to_optional_float as _to_optional_float,
)
from app.services import naver_finance

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
    "get_market_index",
    "get_support_resistance",
    "get_sector_peers",
}


async def _get_support_resistance_impl(
    symbol: str,
    market: str | None = None,
) -> dict[str, Any]:
    """Get support/resistance zones from multi-indicator clustering."""

    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    market_type, normalized_symbol = _resolve_market_type(symbol, market)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    source = source_map[market_type]

    try:
        df = await _fetch_ohlcv_for_indicators(
            normalized_symbol, market_type, count=60
        )
        if df.empty:
            raise ValueError(f"No data available for symbol '{normalized_symbol}'")

        for col in ("high", "low", "close"):
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        current_price = round(float(df["close"].iloc[-1]), 2)
        fib_result = _calculate_fibonacci(df, current_price)
        fib_result["symbol"] = normalized_symbol

        volume_profile_df = await _fetch_ohlcv_for_volume_profile(
            normalized_symbol, market_type, 60
        )
        volume_result = _calculate_volume_profile(volume_profile_df, bins=20)
        volume_result["symbol"] = normalized_symbol
        volume_result["period_days"] = 60

        indicator_result = _compute_indicators(df, ["bollinger"])
        indicator_result["symbol"] = normalized_symbol
        indicator_result["price"] = current_price
        indicator_result["instrument_type"] = market_type
        indicator_result["source"] = source

        if not fib_result.get("levels"):
            raise ValueError("Failed to calculate Fibonacci levels")
        if current_price <= 0:
            raise ValueError("failed to resolve current price")

        price_levels: list[tuple[float, str]] = []

        fib_levels = fib_result.get("levels", {})
        if isinstance(fib_levels, dict):
            for level_key, price in fib_levels.items():
                level_price = _to_optional_float(price)
                if level_price is None or level_price <= 0:
                    continue
                price_levels.append(
                    (level_price, _format_fibonacci_source(str(level_key)))
                )

        poc_price = _to_optional_float((volume_result.get("poc") or {}).get("price"))
        if poc_price is not None and poc_price > 0:
            price_levels.append((poc_price, "volume_poc"))

        value_area = volume_result.get("value_area") or {}
        value_area_high = _to_optional_float(value_area.get("high"))
        value_area_low = _to_optional_float(value_area.get("low"))
        if value_area_high is not None and value_area_high > 0:
            price_levels.append((value_area_high, "volume_value_area_high"))
        if value_area_low is not None and value_area_low > 0:
            price_levels.append((value_area_low, "volume_value_area_low"))

        bollinger = indicator_result.get("bollinger")
        if not isinstance(bollinger, dict):
            bollinger = (indicator_result.get("indicators") or {}).get("bollinger") or {}
        bb_upper = _to_optional_float(bollinger.get("upper"))
        bb_middle = _to_optional_float(bollinger.get("middle"))
        bb_lower = _to_optional_float(bollinger.get("lower"))
        if bb_upper is not None and bb_upper > 0:
            price_levels.append((bb_upper, "bb_upper"))
        if bb_middle is not None and bb_middle > 0:
            price_levels.append((bb_middle, "bb_middle"))
        if bb_lower is not None and bb_lower > 0:
            price_levels.append((bb_lower, "bb_lower"))

        clustered_levels = _cluster_price_levels(price_levels, tolerance_pct=0.02)
        supports, resistances = _split_support_resistance_levels(
            clustered_levels,
            current_price,
        )

        return {
            "symbol": normalized_symbol,
            "current_price": round(current_price, 2),
            "supports": supports,
            "resistances": resistances,
        }
    except Exception as exc:
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=normalized_symbol,
            instrument_type=market_type,
        )


_DEFAULT_GET_SUPPORT_RESISTANCE_IMPL = _get_support_resistance_impl


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
        symbol = _normalize_symbol_input(symbol, market)
        if not symbol:
            raise ValueError("symbol is required")

        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            elif _is_crypto_market(symbol):
                market = "crypto"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in ("crypto", "upbit", "krw", "usdt"):
            normalized_market = "crypto"
        elif normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us', 'kr', or 'crypto'")

        capped_limit = min(max(limit, 1), 50)

        try:
            if normalized_market == "kr":
                return await _fetch_news_naver(symbol, capped_limit)
            return await _fetch_news_finnhub(symbol, normalized_market, capped_limit)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = {
                "kr": "equity_kr",
                "us": "equity_us",
                "crypto": "crypto",
            }.get(normalized_market, "equity_us")
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_company_profile",
        description=(
            "Get company profile for a US or Korean stock. Returns name, sector, "
            "industry, market cap, and financial ratios."
        ),
    )
    async def get_company_profile(
        symbol: str,
        market: str | None = None,
    ) -> dict[str, Any]:

        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError("Company profile is not available for cryptocurrencies")

        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_company_profile_naver(symbol)
            return await _fetch_company_profile_finnhub(symbol)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "finnhub"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_crypto_profile",
        description=(
            "Get cryptocurrency profile data from CoinGecko. Accepts Upbit market "
            "code (e.g. KRW-BTC) or plain symbol (e.g. BTC)."
        ),
    )
    async def get_crypto_profile(symbol: str) -> dict[str, Any]:

        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        normalized_symbol = _normalize_crypto_base_symbol(symbol)
        if not normalized_symbol:
            raise ValueError("symbol is required")

        try:
            coin_id = await _resolve_coingecko_coin_id(normalized_symbol)
            profile = await _fetch_coingecko_coin_profile(coin_id)
            result = _map_coingecko_profile_to_output(profile)
            if result.get("symbol") is None:
                result["symbol"] = normalized_symbol
            if result.get("name") is None:
                result["name"] = normalized_symbol
            return result
        except Exception as exc:
            return _error_payload(
                source="coingecko",
                message=str(exc),
                symbol=normalized_symbol,
                instrument_type="crypto",
            )

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

        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        statement = (statement or "income").strip().lower()
        if statement not in ("income", "balance", "cashflow"):
            raise ValueError("statement must be 'income', 'balance', or 'cashflow'")

        freq = (freq or "annual").strip().lower()
        if freq not in ("annual", "quarterly"):
            raise ValueError("freq must be 'annual' or 'quarterly'")

        if _is_crypto_market(symbol):
            raise ValueError(
                "Financial statements are not available for cryptocurrencies"
            )

        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_financials_naver(symbol, statement, freq)
            try:
                return await _fetch_financials_finnhub(symbol, statement, freq)
            except (ValueError, Exception):
                return await _fetch_financials_yfinance(symbol, statement, freq)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "yfinance"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

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

        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        capped_limit = min(max(limit, 1), 100)

        if _is_crypto_market(symbol):
            raise ValueError("Insider transactions are only available for US stocks")
        if _is_korean_equity_code(symbol):
            raise ValueError("Insider transactions are only available for US stocks")

        try:
            return await _fetch_insider_transactions_finnhub(symbol, capped_limit)
        except Exception as exc:
            return _error_payload(
                source="finnhub",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_us",
            )

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

        symbol = (symbol or "").strip() if symbol else None

        if symbol:
            if _is_crypto_market(symbol):
                raise ValueError("Earnings calendar is only available for US stocks")
            if _is_korean_equity_code(symbol):
                raise ValueError("Earnings calendar is only available for US stocks")

        if from_date:
            try:
                datetime.date.fromisoformat(from_date)
            except ValueError:
                raise ValueError("from_date must be ISO format (e.g., '2024-01-15')")

        if to_date:
            try:
                datetime.date.fromisoformat(to_date)
            except ValueError:
                raise ValueError("to_date must be ISO format (e.g., '2024-01-15')")

        try:
            return await _fetch_earnings_calendar_finnhub(symbol, from_date, to_date)
        except Exception as exc:
            return _error_payload(
                source="finnhub",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_us",
            )

    @mcp.tool(
        name="get_investor_trends",
        description=(
            "Get foreign and institutional investor trading trends for a Korean "
            "stock. Returns daily net buy/sell data. Korean stocks only."
        ),
    )
    async def get_investor_trends(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:

        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not _is_korean_equity_code(symbol):
            raise ValueError(
                "Investor trends are only available for Korean stocks "
                "(6-digit codes like '005930')"
            )

        capped_days = min(max(days, 1), 60)

        try:
            return await _fetch_investor_trends_naver(symbol, capped_days)
        except Exception as exc:
            return _error_payload(
                source="naver",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_kr",
            )

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

        symbol = _normalize_symbol_input(symbol, market)
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError(
                "Investment opinions are not available for cryptocurrencies"
            )

        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        if not market:
            raise ValueError("market is required")

        normalized_market = str(market).strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        capped_limit = min(max(limit, 1), 30)

        try:
            if normalized_market == "kr":
                return await _fetch_investment_opinions_naver(symbol, capped_limit)
            return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "yfinance"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_valuation",
        description=(
            "Get valuation metrics for a US or Korean stock. Returns PER, PBR, ROE, "
            "dividend yield, 52-week high/low, current price, and position within "
            "52-week range."
        ),
    )
    async def get_valuation(
        symbol: str | int,
        market: str | None = None,
    ) -> dict[str, Any]:

        symbol = _normalize_symbol_input(symbol, market)
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError("Valuation metrics are not available for cryptocurrencies")

        if market is None:
            if _is_korean_equity_code(symbol):
                market = "kr"
            else:
                market = "us"

        normalized_market = market.strip().lower()
        if normalized_market in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "equity_kr",
            "naver",
        ):
            normalized_market = "kr"
        elif normalized_market in ("us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"):
            normalized_market = "us"
        else:
            raise ValueError("market must be 'us' or 'kr'")

        try:
            if normalized_market == "kr":
                return await _fetch_valuation_naver(symbol)
            return await _fetch_valuation_yfinance(symbol)
        except Exception as exc:
            source = "naver" if normalized_market == "kr" else "yfinance"
            instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )

    @mcp.tool(
        name="get_short_interest",
        description=(
            "Get short selling data for a Korean stock. Returns daily short selling "
            "volume, amount, ratio, and balance. Korean stocks only."
        ),
    )
    async def get_short_interest(
        symbol: str,
        days: int = 20,
    ) -> dict[str, Any]:

        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if not _is_korean_equity_code(symbol):
            raise ValueError(
                "Short selling data is only available for Korean stocks "
                "(6-digit codes like '005930')"
            )

        capped_days = min(max(days, 1), 60)

        try:
            return await naver_finance.fetch_short_interest(symbol, capped_days)
        except Exception as exc:
            return _error_payload(
                source="krx",
                message=str(exc),
                symbol=symbol,
                instrument_type="equity_kr",
            )

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

        try:
            if symbol:
                sym = _normalize_crypto_base_symbol(symbol)
                if not sym:
                    raise ValueError("symbol is required")
                symbols = [sym]
                return await _fetch_kimchi_premium(symbols)

            symbols = await _resolve_batch_crypto_symbols()
            payload = await _fetch_kimchi_premium(symbols)
            rows: list[dict[str, Any]] = []
            for item in payload.get("data", []):
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        "symbol": item.get("symbol"),
                        "upbit_price": item.get("upbit_krw"),
                        "binance_price": item.get("binance_usdt"),
                        "premium_pct": item.get("premium_pct"),
                    }
                )
            return rows
        except Exception as exc:
            return _error_payload(
                source="upbit+binance",
                message=str(exc),
                instrument_type="crypto",
            )

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

        if symbol is not None and not symbol.strip():
            raise ValueError("symbol is required")

        try:
            if symbol is None:
                symbols = await _resolve_batch_crypto_symbols()
                return await _fetch_funding_rate_batch(symbols)

            normalized_symbol = _normalize_crypto_base_symbol(symbol)
            if not normalized_symbol:
                raise ValueError("symbol is required")

            capped_limit = min(max(limit, 1), 100)
            return await _fetch_funding_rate(normalized_symbol, capped_limit)
        except Exception as exc:
            normalized_symbol = _normalize_crypto_base_symbol(symbol or "")
            return _error_payload(
                source="binance",
                message=str(exc),
                symbol=f"{normalized_symbol}USDT" if normalized_symbol else None,
                instrument_type="crypto",
            )

    @mcp.tool(
        name="get_market_index",
        description=(
            "Get market index data. Supports KOSPI/KOSDAQ and major US indices. "
            "Without symbol returns current major indices, with symbol adds OHLCV history."
        ),
    )
    async def get_market_index(
        symbol: str | None = None,
        period: str = "day",
        count: int = 20,
    ) -> dict[str, Any]:
        period = (period or "day").strip().lower()
        if period not in ("day", "week", "month"):
            raise ValueError("period must be 'day', 'week', or 'month'")

        capped_count = min(max(count, 1), 100)

        if symbol:
            sym = symbol.strip().upper()
            meta = _INDEX_META.get(sym)
            if meta is None:
                raise ValueError(
                    f"Unknown index symbol '{sym}'. Supported: {', '.join(sorted(_INDEX_META))}"
                )

            try:
                if meta["source"] == "naver":
                    current_data, history = await asyncio.gather(
                        _fetch_index_kr_current(meta["naver_code"], meta["name"]),
                        _fetch_index_kr_history(
                            meta["naver_code"], capped_count, period
                        ),
                    )
                else:
                    current_data, history = await asyncio.gather(
                        _fetch_index_us_current(
                            meta["yf_ticker"], meta["name"], sym
                        ),
                        _fetch_index_us_history(
                            meta["yf_ticker"], capped_count, period
                        ),
                    )
                return {"indices": [current_data], "history": history}
            except Exception as exc:
                return _error_payload(source=meta["source"], message=str(exc), symbol=sym)

        tasks = []
        for idx_sym in _DEFAULT_INDICES:
            meta = _INDEX_META[idx_sym]
            if meta["source"] == "naver":
                tasks.append(_fetch_index_kr_current(meta["naver_code"], meta["name"]))
            else:
                tasks.append(
                    _fetch_index_us_current(
                        meta["yf_ticker"], meta["name"], idx_sym
                    )
                )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        indices: list[dict[str, Any]] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                indices.append({"symbol": _DEFAULT_INDICES[i], "error": str(r)})
            else:
                indices.append(r)

        return {"indices": indices}

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
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol is required")

        if _is_crypto_market(symbol):
            raise ValueError("Sector peers are not available for cryptocurrencies")

        capped_limit = min(max(limit, 1), 20)

        market_str = (market or "").strip().lower()
        if market_str in (
            "kr",
            "krx",
            "korea",
            "kospi",
            "kosdaq",
            "kis",
            "naver",
        ):
            resolved_market = "kr"
        elif market_str in ("us", "usa", "nyse", "nasdaq", "yahoo"):
            resolved_market = "us"
        elif market_str == "":
            if _is_korean_equity_code(symbol):
                resolved_market = "kr"
            elif _is_us_equity_symbol(symbol):
                resolved_market = "us"
            else:
                raise ValueError(
                    f"Cannot auto-detect market for symbol '{symbol}'. "
                    "Please specify market='kr' or market='us'."
                )
        else:
            raise ValueError("market must be 'kr' or 'us'")

        try:
            if resolved_market == "kr":
                return await _fetch_sector_peers_naver(
                    symbol, capped_limit, manual_peers
                )
            return await _fetch_sector_peers_us(symbol, capped_limit, manual_peers)
        except Exception as exc:
            source = "naver" if resolved_market == "kr" else "finnhub+yfinance"
            instrument_type = "equity_kr" if resolved_market == "kr" else "equity_us"
            return _error_payload(
                source=source,
                message=str(exc),
                symbol=symbol,
                instrument_type=instrument_type,
            )


__all__ = ["FUNDAMENTALS_TOOL_NAMES", "_register_fundamentals_tools_impl", "_get_support_resistance_impl"]
