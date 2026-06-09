"""Handlers for get_kimchi_premium and get_funding_rate tools."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_binance import (
    _fetch_funding_rate,
    _fetch_funding_rate_batch,
    _fetch_long_short_ratio,
    _fetch_open_interest,
)
from app.mcp_server.tooling.fundamentals_sources_coingecko import (
    _fetch_coingecko_coin_social,
    _normalize_crypto_base_symbol,
    _resolve_batch_crypto_symbols,
    _resolve_coingecko_coin_id,
)
from app.mcp_server.tooling.fundamentals_sources_naver import _fetch_kimchi_premium
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.services.brokers.upbit.public_trades import fetch_recent_trades

_ALLOWED_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


async def handle_get_kimchi_premium(
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


async def handle_get_funding_rate(
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


async def handle_get_open_interest(
    symbol: str | None = None,
    period: str = "1h",
    limit: int = 30,
) -> dict[str, Any]:
    if symbol is None or not symbol.strip():
        raise ValueError("symbol is required")
    period = (period or "").strip().lower()
    if period not in _ALLOWED_PERIODS:
        raise ValueError(
            f"period must be one of: {', '.join(sorted(_ALLOWED_PERIODS))}"
        )
    normalized_symbol = _normalize_crypto_base_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("symbol is required")
    capped_limit = min(max(limit, 1), 500)
    try:
        return await _fetch_open_interest(normalized_symbol, period, capped_limit)
    except Exception as exc:
        return _error_payload(
            source="binance",
            message=str(exc),
            symbol=f"{normalized_symbol}USDT",
            instrument_type="crypto",
        )


async def handle_get_long_short_ratio(
    symbol: str | None = None,
    period: str = "1h",
    limit: int = 30,
) -> dict[str, Any]:
    if symbol is None or not symbol.strip():
        raise ValueError("symbol is required")
    period = (period or "").strip().lower()
    if period not in _ALLOWED_PERIODS:
        raise ValueError(
            f"period must be one of: {', '.join(sorted(_ALLOWED_PERIODS))}"
        )
    normalized_symbol = _normalize_crypto_base_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("symbol is required")
    capped_limit = min(max(limit, 1), 500)
    try:
        return await _fetch_long_short_ratio(normalized_symbol, period, capped_limit)
    except Exception as exc:
        return _error_payload(
            source="binance",
            message=str(exc),
            symbol=f"{normalized_symbol}USDT",
            instrument_type="crypto",
        )


async def handle_get_crypto_order_flow(
    symbol: str,
    count: int = 200,
) -> dict[str, Any]:
    """ROB-452 P2: Upbit recent-trade taker order-flow (retail buy/sell pressure proxy).

    Volume-weighted taker_buy_ratio / taker_sell_ratio / net from /v1/trades/ticks.
    Repo convention (upbit_websocket.py): ask_bid "BID" = taker buy, "ASK" = taker sell.
    Read-only public Upbit data — source is "upbit" (NOT binance).
    """
    if not symbol or not symbol.strip():
        raise ValueError("symbol is required")
    try:
        base = _normalize_crypto_base_symbol(symbol)
        if not base:
            raise ValueError("symbol is required")
        market = f"KRW-{base}"
        capped = min(max(count, 1), 500)
        trades = await fetch_recent_trades(market=market, count=capped)

        buy_vol = 0.0
        sell_vol = 0.0
        used = 0
        for tick in trades:
            raw = tick.get("trade_volume")
            try:
                vol = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                vol = None
            if vol is None:
                continue
            side = tick.get("ask_bid")
            if side == "BID":
                buy_vol += vol
                used += 1
            elif side == "ASK":
                sell_vol += vol
                used += 1

        total = buy_vol + sell_vol
        if total > 0:
            taker_buy_ratio: float | None = round(buy_vol / total, 4)
            taker_sell_ratio: float | None = round(sell_vol / total, 4)
            net: float | None = round((buy_vol - sell_vol) / total, 4)
        else:
            # missing != zero: no usable ticks → None, never a fabricated 0.0
            taker_buy_ratio = taker_sell_ratio = net = None

        return {
            "symbol": market,
            "taker_buy_ratio": taker_buy_ratio,
            "taker_sell_ratio": taker_sell_ratio,
            "net": net,
            "trade_count": used,
            "source": "upbit",
            "instrument_type": "crypto",
        }
    except Exception as exc:
        return _error_payload(
            source="upbit",
            message=str(exc),
            symbol=symbol,
            instrument_type="crypto",
        )


async def handle_get_crypto_social(symbol: str) -> dict[str, Any]:
    """ROB-452 P2: CoinGecko community/developer social signals for a crypto symbol."""
    if not symbol or not symbol.strip():
        raise ValueError("symbol is required")
    try:
        coin_id = await _resolve_coingecko_coin_id(symbol)
        data = await _fetch_coingecko_coin_social(coin_id)
        community = data.get("community_data") or {}
        developer = data.get("developer_data") or {}
        return {
            "symbol": _normalize_crypto_base_symbol(symbol) or symbol,
            "coin_id": coin_id,
            # sentiment_votes_up_percentage sits at the top level of the coin object.
            "sentiment_votes_up_pct": data.get("sentiment_votes_up_percentage"),
            "twitter_followers": community.get("twitter_followers"),
            "reddit_subscribers": community.get("reddit_subscribers"),
            "dev_commits_4w": developer.get("commit_count_4_weeks"),
            "source": "coingecko",
            "instrument_type": "crypto",
        }
    except Exception as exc:
        return _error_payload(
            source="coingecko",
            message=str(exc),
            symbol=symbol,
            instrument_type="crypto",
        )
