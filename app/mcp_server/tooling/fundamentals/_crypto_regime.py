"""ROB-452 P1: get_crypto_market_regime — read-only view over crypto_insight_snapshots.

The crypto_insight_snapshots table (DefiLlama / Coinglass / TradingView / Fear&Greed)
was a dead-end — only the build job wrote it, no MCP tool read it. This exposes it.

HONEST SCAFFOLD: each field is independently fresh/stale/missing/disabled. Only `fng`
(Fear&Greed, alternative_me) is populated by the DEFAULT job. `tvl`/`stablecoin_supply`
(defillama) and `breadth` (tradingview, reference-only) require operator-enabled
providers; `aggregate_oi` (coinglass) is a disabled PoC that emits no rows. An empty
table yields per-field "missing" — never an error.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.services.crypto_insight_snapshots.repository import (
    get_latest_crypto_insight,
    list_latest_crypto_insights,
)

# Regime metrics are slow-moving (daily-ish); treat older than this as stale unless the
# row itself carries a tighter freshness_seconds.
_REGIME_STALE_AFTER_SECONDS = 24 * 3600


def _to_jsonable(value: Any) -> float | None:
    return float(value) if value is not None else None


def _state_for(snapshot: Any, *, now: dt.datetime) -> str:
    if snapshot is None:
        return "missing"
    age = (now - snapshot.snapshot_at).total_seconds()
    threshold = snapshot.freshness_seconds or _REGIME_STALE_AFTER_SECONDS
    return "fresh" if age <= threshold else "stale"


def _field_block(snapshot: Any, *, now: dt.datetime, provider: str) -> dict[str, Any]:
    if snapshot is None:
        return {"state": "missing", "provider": provider, "value": None}
    return {
        "state": _state_for(snapshot, now=now),
        "provider": snapshot.provider,
        "value": _to_jsonable(snapshot.value),
        "unit": snapshot.unit,
        "label": snapshot.label,
        "as_of": snapshot.snapshot_at.astimezone(dt.UTC).isoformat(),
    }


def _tvl_block(rows: list[Any], *, now: dt.datetime) -> dict[str, Any]:
    # DefiLlama writes one tvl row per protocol (e.g. bitcoin/ethereum) — there is no
    # single global aggregate. Surface the per-protocol values honestly rather than
    # fabricating a total.
    if not rows:
        return {"state": "missing", "provider": "defillama", "by_protocol": []}
    freshest = max(rows, key=lambda r: r.snapshot_at)
    return {
        "state": _state_for(freshest, now=now),
        "provider": "defillama",
        "by_protocol": [
            {
                "symbol": r.symbol,
                "value": _to_jsonable(r.value),
                "unit": r.unit,
                "as_of": r.snapshot_at.astimezone(dt.UTC).isoformat(),
            }
            for r in rows
        ],
    }


async def handle_get_crypto_market_regime() -> dict[str, Any]:
    """Latest crypto market-regime signals from crypto_insight_snapshots (read-only)."""
    now = dt.datetime.now(dt.UTC)
    try:
        async with AsyncSessionLocal() as session:
            fng = await get_latest_crypto_insight(
                session, "fear_greed", provider="alternative_me", symbol=None
            )
            stablecoin = await get_latest_crypto_insight(
                session, "stablecoin_supply", provider="defillama", symbol=None
            )
            breadth = await get_latest_crypto_insight(
                session, "tv_crypto_breadth", provider="tradingview", symbol=None
            )
            tvl_rows = await list_latest_crypto_insights(
                session, metrics=["tvl"], providers=["defillama"]
            )
    except Exception as exc:  # noqa: BLE001 — read-only; surface a structured error
        return _error_payload(
            source="crypto_insight_snapshots",
            message=str(exc),
            instrument_type="crypto",
        )

    return {
        "as_of": now.isoformat(),
        "source": "crypto_insight_snapshots",
        "regime": {
            "fng": _field_block(fng, now=now, provider="alternative_me"),
            "tvl": _tvl_block(list(tvl_rows), now=now),
            "stablecoin_supply": _field_block(
                stablecoin, now=now, provider="defillama"
            ),
            "breadth": _field_block(breadth, now=now, provider="tradingview"),
            "aggregate_oi": {
                "state": "disabled",
                "provider": "coinglass",
                "value": None,
                "note": (
                    "coinglass PoC adapter is disabled (no API key / not in default "
                    "providers) — emits no rows"
                ),
            },
        },
        # Honesty: tell the caller why fields may be empty.
        "note": (
            "fng is populated by the default snapshot job; tvl/stablecoin_supply "
            "(defillama) + breadth (tradingview, reference-only) require operator-enabled "
            "providers; aggregate_oi (coinglass) is a disabled PoC."
        ),
    }
