"""ROB-452 P1: get_crypto_catalysts — supply/event catalysts for crypto (read-only).

Aggregates three sources, each independently fail-open (one source failing never kills
the tool — partial results survive with per-block state):
  * token_unlocks  — Tokenomist unlock/vesting (PoC stub today → state "disabled",
                     honest: it emits no data without an enabled adapter).
  * upbit_notices  — NEW keyless Upbit notices feed (listings / 유의 / 점검).
  * market_warnings — existing Upbit market-warning read model (CAUTION designations).

symbol (e.g. "XRP", "KRW-XRP") scopes warnings + notices; None = market-wide.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.mcp_server.tooling.fundamentals_sources_coingecko import (
    _normalize_crypto_base_symbol,
)
from app.services.external.crypto_insights import (
    fetch_tokenomist_unlocks_poc,
    tokenomist_api_key_from_env,
)
from app.services.upbit_public_read_model.market_warnings import get_market_warnings
from app.services.upbit_public_read_model.notices import fetch_upbit_notices


async def _tokenomist_block() -> dict[str, Any]:
    try:
        result = await fetch_tokenomist_unlocks_poc(
            api_key=tokenomist_api_key_from_env()
        )
    except Exception as exc:  # noqa: BLE001 — fail-open per source
        return {
            "state": "unavailable",
            "source": "tokenomist",
            "items": [],
            "warnings": [str(exc)],
        }
    items = [
        {"metric": m.metric, "value": float(m.value) if m.value is not None else None}
        for m in result.metrics
    ]
    return {
        # PoC emits no metrics → honest "disabled" (do not claim live unlock data).
        "state": "fresh" if items else "disabled",
        "source": "tokenomist",
        "items": items,
        "warnings": list(result.warnings),
    }


async def _market_warnings_block(*, markets: list[str] | None) -> dict[str, Any]:
    try:
        block = await get_market_warnings(markets=markets, include_event_detail=False)
    except Exception as exc:  # noqa: BLE001 — fail-open per source
        return {
            "state": "unavailable",
            "source": "upbit_market_warnings",
            "entries": {},
            "errorReason": str(exc),
        }
    # Only CAUTION is a catalyst; NONE is noise.
    entries = {
        market: {"warning": entry.warning, "event": entry.event}
        for market, entry in block.entries.items()
        if entry.warning == "CAUTION"
    }
    return {
        "state": block.meta.state,
        "source": "upbit_market_warnings",
        "fetched_at": (
            block.meta.fetchedAt.isoformat() if block.meta.fetchedAt else None
        ),
        "entries": entries,
    }


def _filter_notices_by_base(block: dict[str, Any], base: str) -> dict[str, Any]:
    if block.get("state") != "fresh":
        return block
    base_up = base.upper()
    filtered = [
        item
        for item in block.get("items", [])
        if item.get("title") and base_up in str(item["title"]).upper()
    ]
    return {**block, "items": filtered}


async def handle_get_crypto_catalysts(
    symbol: str | None = None,
    days: int = 14,
) -> dict[str, Any]:
    """Aggregate crypto catalysts (token unlocks + Upbit notices + market warnings)."""
    now = dt.datetime.now(dt.UTC)

    base: str | None = None
    market: str | None = None
    if symbol:
        try:
            base = _normalize_crypto_base_symbol(symbol) or None
        except Exception:  # noqa: BLE001 — normalization best-effort
            base = None
        if base:
            market = f"KRW-{base}"

    unlocks_block = await _tokenomist_block()
    notices_block = await fetch_upbit_notices(days=days)
    if base:
        notices_block = _filter_notices_by_base(notices_block, base)
    warnings_block = await _market_warnings_block(markets=[market] if market else None)

    return {
        "symbol": market or symbol or None,
        "window_days": days,
        "as_of": now.isoformat(),
        "catalysts": {
            "token_unlocks": unlocks_block,
            "upbit_notices": notices_block,
            "market_warnings": warnings_block,
        },
    }
