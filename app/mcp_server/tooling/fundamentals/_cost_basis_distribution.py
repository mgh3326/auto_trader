"""ROB-450: get_cost_basis_distribution — holder cost-basis ESTIMATE via self-OHLCV VPVR.

A volume-by-price (VPVR) APPROXIMATION of where holders are positioned, built from the
symbol's own trailing OHLCV — NOT a Naver/vendor holder-cost widget (license-clean).
Reuses the existing VPVR engine (_fetch_ohlcv_for_volume_profile + _calculate_volume_profile);
this handler only adds the holder-cost derivations (vwap estimate, underwater/in_profit,
heaviest bucket). Clearly labelled ``estimate=True`` — it is a proxy, not exact avg cost.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.mcp_server.tooling.market_data_indicators import (
    _calculate_volume_profile,
    _fetch_ohlcv_for_volume_profile,
)
from app.mcp_server.tooling.market_data_quotes import fetch_us_live_last_price
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)

# Holder cost-basis horizon (trailing daily candles). Internal — not a public arg.
_PERIOD_DAYS = 120


async def get_cost_basis_distribution_impl(
    symbol: str,
    market: str | None = None,
    buckets: int = 10,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """VPVR-based holder cost-basis ESTIMATE for a kr/us/crypto symbol."""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    market_type, normalized_symbol = _resolve_market_type(symbol, market)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    source = source_map[market_type]
    bucket_count = max(2, min(int(buckets), 100))

    try:
        if preloaded_df is not None and not preloaded_df.empty:
            df = preloaded_df
        else:
            df = await _fetch_ohlcv_for_volume_profile(
                normalized_symbol, market_type, _PERIOD_DAYS
            )
        if df.empty:
            raise ValueError(f"No data available for symbol '{normalized_symbol}'")
        for col in ("high", "low", "close", "volume"):
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        current_price = round(float(df["close"].iloc[-1]), 6)
        current_price_source = "ohlcv_close"
        current_price_stale = market_type == "equity_us"
        if market_type == "equity_us":
            live = await fetch_us_live_last_price(normalized_symbol)
            if live is not None:
                current_price = round(live, 6)
                current_price_source = "yahoo_live"
                current_price_stale = False

        vp = _calculate_volume_profile(df, bins=bucket_count)
        profile = vp.get("profile", [])

        buckets_out: list[dict[str, Any]] = []
        underwater = 0.0
        in_profit = 0.0
        for entry in profile:
            low = entry.get("price_low")
            high = entry.get("price_high")
            share = entry.get("volume_pct") or 0.0
            buckets_out.append(
                {
                    "price_low": low,
                    "price_high": high,
                    "holder_share_pct": share,
                    "est_volume": entry.get("volume"),
                }
            )
            if low is not None and high is not None:
                mid = (float(low) + float(high)) / 2
                # mid above current price → those holders bought higher = underwater
                if mid > current_price:
                    underwater += float(share)
                else:
                    in_profit += float(share)

        heaviest = (
            max(buckets_out, key=lambda b: b["holder_share_pct"] or 0.0)
            if buckets_out
            else None
        )

        # typical-price VWAP over the window = estimate of the average holder cost
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        vol = df["volume"]
        vol_sum = float(vol.sum())
        vwap_estimate = (
            round(float((typical * vol).sum() / vol_sum), 6) if vol_sum > 0 else None
        )

        as_of = str(df["date"].iloc[-1]) if "date" in df.columns else None

        return {
            "symbol": normalized_symbol,
            "instrument_type": market_type,
            "source": source,
            "estimate": True,  # honesty: VPVR proxy, not an exact holder-cost file
            "method": "vpvr_self_ohlcv",
            "as_of": as_of,
            "period_days": _PERIOD_DAYS,
            "candles_used": int(len(df)),
            "current_price": current_price,
            "current_price_source": current_price_source,
            "current_price_stale": current_price_stale,
            "vwap_estimate": vwap_estimate,
            "buckets": buckets_out,
            "pct_holders_underwater": round(underwater, 2),
            "pct_holders_in_profit": round(in_profit, 2),
            "heaviest_bucket": heaviest,
        }
    except Exception as exc:  # noqa: BLE001 — read-only; fail-open structured error
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=normalized_symbol,
            instrument_type=market_type,
        )


_DEFAULT_GET_COST_BASIS_DISTRIBUTION_IMPL = get_cost_basis_distribution_impl
