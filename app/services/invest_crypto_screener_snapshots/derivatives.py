"""ROB-443 Phase 1: crypto-native derivative enrichment for screener snapshots.

Funding rate is a crypto-native sentiment signal with no stock analog. Binance's
premiumIndex returns *all* USD-M perps in a single call, so enriching the whole
Upbit universe costs one request (``_fetch_funding_rate_batch`` filters to the
requested base symbols). Upbit-only coins with no Binance perp simply do not
appear in the result → ``funding_rate`` stays ``None`` (fail-closed, never
fabricated).

open_interest / long_short_ratio are per-symbol (N requests) and rate-limit
sensitive, so they are deferred to a follow-up PR.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from app.mcp_server.tooling.fundamentals_sources_binance import (
    _fetch_funding_rate_batch,
)

FundingBatchFetcher = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]


def base_symbol_from_upbit(upbit_symbol: str) -> str | None:
    """``KRW-BTC`` -> ``BTC``; anything not a KRW market returns None."""
    s = str(upbit_symbol or "").strip().upper()
    if not s.startswith("KRW-"):
        return None
    base = s[4:]
    return base or None


async def fetch_funding_rates(
    upbit_symbols: list[str],
    *,
    fetcher: FundingBatchFetcher = _fetch_funding_rate_batch,
) -> dict[str, Decimal]:
    """Map ``KRW-XXX`` -> Binance perp funding rate for coins that have a perp.

    One batch call. Coins without a Binance USDT perp are absent from the result
    (caller treats missing as ``None``). Fail-open: any fetch error returns ``{}``
    so the snapshot build still succeeds (funding simply not enriched).
    """
    base_to_upbit: dict[str, str] = {}
    for sym in upbit_symbols:
        base = base_symbol_from_upbit(sym)
        if base and base not in base_to_upbit:
            base_to_upbit[base] = str(sym).strip().upper()
    if not base_to_upbit:
        return {}

    try:
        rows = await fetcher(sorted(base_to_upbit))
    except Exception:  # noqa: BLE001 — fail-open: build proceeds without funding
        return {}

    out: dict[str, Decimal] = {}
    for row in rows:
        base = str(row.get("symbol") or "").strip().upper()
        upbit = base_to_upbit.get(base)
        raw = row.get("funding_rate")
        if upbit is None or raw is None:
            continue
        try:
            out[upbit] = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
    return out
