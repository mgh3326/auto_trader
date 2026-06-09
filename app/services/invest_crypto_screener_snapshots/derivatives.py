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

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from app.mcp_server.tooling.fundamentals_sources_binance import (
    _fetch_funding_rate_batch,
    _fetch_long_short_ratio,
    _fetch_open_interest,
)

FundingBatchFetcher = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
PerSymbolFetcher = Callable[[str, str, int], Awaitable[dict[str, Any]]]


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


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


async def fetch_oi_and_long_short(
    perp_upbit_symbols: list[str],
    *,
    oi_fetcher: PerSymbolFetcher = _fetch_open_interest,
    lsr_fetcher: PerSymbolFetcher = _fetch_long_short_ratio,
    period: str = "1h",
    limit: int = 24,
    concurrency: int = 8,
) -> dict[str, dict[str, Decimal | None]]:
    """Per-symbol open-interest + global long/short ratio for coins that have a perp.

    Unlike funding (one batch call), these endpoints are per-symbol, so the caller
    should pass ONLY the perp coins (e.g. the symbols funding enrichment matched),
    not the whole universe. Bounded concurrency; **fail-open per coin and per
    metric** — one coin's (or one endpoint's) error leaves that field None and
    never blocks the rest.

    Returns ``{KRW-XXX: {"open_interest_usd", "oi_change_24h",
    "long_short_account_ratio"}}`` for coins with at least one metric resolved.
    """
    base_to_upbit: dict[str, str] = {}
    for sym in perp_upbit_symbols:
        base = base_symbol_from_upbit(sym)
        if base and base not in base_to_upbit:
            base_to_upbit[base] = str(sym).strip().upper()
    if not base_to_upbit:
        return {}

    sem = asyncio.Semaphore(max(1, concurrency))
    out: dict[str, dict[str, Decimal | None]] = {}

    async def _one(base: str, upbit: str) -> None:
        async with sem:
            row: dict[str, Decimal | None] = {}
            try:
                oi = await oi_fetcher(base, period, limit)
                history = oi.get("open_interest_history") or []
                latest_usd = (
                    history[-1].get("sum_open_interest_value_usd") if history else None
                )
                row["open_interest_usd"] = _to_decimal(latest_usd)
                row["oi_change_24h"] = _to_decimal(oi.get("oi_change_pct"))
            except Exception:  # noqa: BLE001 — fail-open per metric
                pass
            try:
                lsr = await lsr_fetcher(base, period, limit)
                global_leg = lsr.get("global_account") or {}
                row["long_short_account_ratio"] = _to_decimal(global_leg.get("ratio"))
            except Exception:  # noqa: BLE001 — fail-open per metric
                pass
            if any(v is not None for v in row.values()):
                out[upbit] = row

    await asyncio.gather(*(_one(b, u) for b, u in base_to_upbit.items()))
    return out
