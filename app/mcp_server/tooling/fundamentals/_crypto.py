"""Handlers for get_kimchi_premium and get_funding_rate tools."""

from __future__ import annotations

import datetime
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
        # We always fetch 500 trades to calculate all windows derived from a single fetch.
        trades = await fetch_recent_trades(market=market, count=500)

        parsed_ticks = []
        for tick in trades:
            raw_vol = tick.get("trade_volume")
            try:
                vol = float(raw_vol) if raw_vol is not None else None
            except (TypeError, ValueError):
                vol = None

            side = tick.get("ask_bid")

            # Extract timestamp (in milliseconds)
            ts = tick.get("timestamp")
            if ts is not None:
                try:
                    ts_val = float(ts)
                except (TypeError, ValueError):
                    ts_val = None
            else:
                ts_val = None
                for field in ("trade_timestamp", "trade_date_utc"):
                    val = tick.get(field)
                    if val:
                        try:
                            text = str(val)
                            if text.endswith("Z"):
                                text = text[:-1] + "+00:00"
                            dt = datetime.datetime.fromisoformat(text)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=datetime.UTC)
                            ts_val = dt.timestamp() * 1000.0
                            break
                        except (ValueError, TypeError):
                            pass

            if vol is not None and side in ("BID", "ASK"):
                parsed_ticks.append({"volume": vol, "side": side, "timestamp": ts_val})

        # Defensive sorting: ensure newest-first for window calculations (ROB-589).
        parsed_ticks.sort(key=lambda t: t["timestamp"] or 0, reverse=True)

        def _calculate_window_stats(
            ticks_list: list[dict[str, Any]], W: int
        ) -> dict[str, Any]:
            ticks_slice = ticks_list[:W]
            buy_vol = 0.0
            sell_vol = 0.0
            max_vol = 0.0
            used = 0
            for t in ticks_slice:
                vol = t["volume"]
                side = t["side"]
                if side == "BID":
                    buy_vol += vol
                    used += 1
                    if vol > max_vol:
                        max_vol = vol
                elif side == "ASK":
                    sell_vol += vol
                    used += 1
                    if vol > max_vol:
                        max_vol = vol

            total_vol = buy_vol + sell_vol
            if total_vol > 0:
                buy_ratio = round(buy_vol / total_vol, 4)
                sell_ratio = round(sell_vol / total_vol, 4)
                net = round((buy_vol - sell_vol) / total_vol, 4)
                largest_trade_share = round(max_vol / total_vol, 4)
            else:
                buy_ratio = sell_ratio = net = largest_trade_share = None

            timestamps = [
                t["timestamp"] for t in ticks_slice if t["timestamp"] is not None
            ]
            if len(timestamps) >= 2:
                span_seconds = round((timestamps[0] - timestamps[-1]) / 1000.0, 4)
            else:
                span_seconds = 0.0

            return {
                "net": net,
                "buy_ratio": buy_ratio,
                "sell_ratio": sell_ratio,
                "trade_count": used,
                "span_seconds": span_seconds,
                "largest_trade_share": largest_trade_share,
            }

        # Calculate standard windows
        stats_50 = _calculate_window_stats(parsed_ticks, 50)
        stats_200 = _calculate_window_stats(parsed_ticks, 200)
        stats_500 = _calculate_window_stats(parsed_ticks, 500)

        windows_dict = {
            "50": {
                "net": stats_50["net"],
                "buy_ratio": stats_50["buy_ratio"],
                "trade_count": stats_50["trade_count"],
                "span_seconds": stats_50["span_seconds"],
                "largest_trade_share": stats_50["largest_trade_share"],
            },
            "200": {
                "net": stats_200["net"],
                "buy_ratio": stats_200["buy_ratio"],
                "trade_count": stats_200["trade_count"],
                "span_seconds": stats_200["span_seconds"],
                "largest_trade_share": stats_200["largest_trade_share"],
            },
            "500": {
                "net": stats_500["net"],
                "buy_ratio": stats_500["buy_ratio"],
                "trade_count": stats_500["trade_count"],
                "span_seconds": stats_500["span_seconds"],
                "largest_trade_share": stats_500["largest_trade_share"],
            },
        }

        # Capped default window matching user input `count`
        capped_default = min(max(count, 1), 500)
        stats_default = _calculate_window_stats(parsed_ticks, capped_default)

        # Disjoint Segments Analysis
        recent_stats = stats_50
        older_stats = _calculate_window_stats(parsed_ticks[50:], 450)

        recent_net = recent_stats["net"]
        older_net = older_stats["net"]

        epsilon = 0.10
        if recent_net is None:
            trend = "neutral"
        elif older_net is None:
            if abs(recent_net) < epsilon:
                trend = "neutral"
            elif recent_net >= epsilon:
                trend = "stable_up"
            else:
                trend = "stable_down"
        else:
            recent_neutral = abs(recent_net) < epsilon
            older_neutral = abs(older_net) < epsilon

            if recent_neutral and older_neutral:
                trend = "neutral"
            elif recent_neutral:
                if older_net >= epsilon:
                    trend = "weakening_up"
                else:
                    trend = "weakening_down"
            elif recent_net >= epsilon:
                if older_neutral:
                    trend = "strengthening_up"
                elif older_net <= -epsilon:
                    trend = "reversing_up"
                else:
                    if recent_net > older_net:
                        trend = "strengthening_up"
                    elif recent_net < older_net:
                        trend = "weakening_up"
                    else:
                        trend = "stable_up"
            else:
                if older_neutral:
                    trend = "strengthening_down"
                elif older_net >= epsilon:
                    trend = "reversing_down"
                else:
                    if recent_net < older_net:
                        trend = "strengthening_down"
                    elif recent_net > older_net:
                        trend = "weakening_down"
                    else:
                        trend = "stable_down"

        # Confidence
        confidence = "normal"
        note_parts = []

        recent_largest_share = stats_50["largest_trade_share"]
        if recent_largest_share is not None and recent_largest_share > 0.35:
            confidence = "low"
            note_parts.append(
                f"Whale trade dominance detected (largest trade share {recent_largest_share:.1%} > 35%)"
            )

        recent_trade_count = stats_50["trade_count"]
        if recent_trade_count < 15:
            confidence = "low"
            note_parts.append(f"Low trade count ({recent_trade_count} < 15)")

        # Consensus direction & agreement
        active_nets = []
        for w_name in ("50", "200", "500"):
            w_net = windows_dict[w_name]["net"]
            if w_net is not None:
                active_nets.append(w_net)

        if len(active_nets) == 0:
            direction = "neutral"
            agreement = True
            base_note = "No trades found in any analysis window."
        else:
            all_buy = all(net >= epsilon for net in active_nets)
            all_sell = all(net <= -epsilon for net in active_nets)
            all_neutral = all(abs(net) < epsilon for net in active_nets)

            if all_buy:
                direction = "buy"
                agreement = True
                base_note = f"Consensus buying pressure across active windows (nets: {', '.join(str(n) for n in active_nets)})."
            elif all_sell:
                direction = "sell"
                agreement = True
                base_note = f"Consensus selling pressure across active windows (nets: {', '.join(str(n) for n in active_nets)})."
            elif all_neutral:
                direction = "neutral"
                agreement = True
                base_note = "Low net flow (neutral) across all active windows."
            else:
                direction = "mixed"
                agreement = False
                base_note = f"Divergent flow signal (nets: 50={stats_50['net']}, 200={stats_200['net']}, 500={stats_500['net']})."

        if note_parts:
            note = f"{base_note} Caution: {'; '.join(note_parts)}."
        else:
            note = base_note

        return {
            "symbol": market,
            "as_of": datetime.datetime.now(datetime.UTC).isoformat(),
            "source": "upbit",
            "default_window": capped_default,
            "net": stats_default["net"],
            "buy_ratio": stats_default["buy_ratio"],
            "taker_buy_ratio": stats_default["buy_ratio"],
            "taker_sell_ratio": stats_default["sell_ratio"],
            "trade_count": stats_default["trade_count"],
            "instrument_type": "crypto",
            "windows": windows_dict,
            "consensus": {
                "direction": direction,
                "agreement": agreement,
                "trend": trend,
                "confidence": confidence,
                "note": note,
            },
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
