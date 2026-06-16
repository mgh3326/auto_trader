from __future__ import annotations

import asyncio
import datetime as dt
import functools
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.market_quote_snapshots.builder import redact_sensitive_payload
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotUpsert,
)

logger = logging.getLogger(__name__)
ValuationFetcher = Callable[[str, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class MarketValuationBuildResult:
    payloads: tuple[MarketValuationSnapshotUpsert, ...]
    warnings: tuple[str, ...] = ()


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _to_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


async def default_valuation_fetcher(
    symbol: str, market: str, *, include_high_date: bool = False
) -> dict[str, Any]:
    if market == "kr":
        from app.services.naver_finance.valuation import fetch_valuation

        return await fetch_valuation(symbol)
    if market == "us":
        from app.services.brokers.yahoo.client import (
            fetch_52w_high_date,
            fetch_fast_info,
            fetch_fundamental_info,
        )
        from app.services.market_valuation_snapshots.finnhub_fallback import (
            apply_valuation_fallback,
        )

        raw: dict[str, Any] = {}
        yahoo_failed = False
        yahoo_exc: Exception | None = None
        try:
            # ROB-440 PR4: the 52w-high DATE needs a heavy OHLC fetch (1y daily) — a 3rd
            # yahoo call/symbol that over-loads yfinance at universe scale (FD/401). Opt-in
            # so the bulk valuation backfill is 2 calls/symbol; only undervalued_breakout
            # date-recency needs it (targeted run with include_high_date=True).
            if not include_high_date:
                fast_info, fundamentals = await asyncio.gather(
                    fetch_fast_info(symbol), fetch_fundamental_info(symbol)
                )
                raw = {**fast_info, **fundamentals}
            else:
                fast_info, fundamentals, high_52w_date = await asyncio.gather(
                    fetch_fast_info(symbol),
                    fetch_fundamental_info(symbol),
                    fetch_52w_high_date(
                        symbol
                    ),  # ROB-440 PR3: 52w-high date (date-recency)
                )
                # isoformat string keeps raw_payload (JSONB) serializable; parsed back in
                # _payload_from_raw → the high_52w_date column.
                raw = {
                    **fast_info,
                    **fundamentals,
                    "high_52w_date": high_52w_date.isoformat()
                    if high_52w_date
                    else None,
                }
        except Exception as exc:  # noqa: BLE001 — try Finnhub before giving up
            raw, yahoo_failed, yahoo_exc = {}, True, exc

        # ROB-434: backfill yahoo's null/missing valuation fields from Finnhub when
        # gated on. No-op when disabled / no key / no gap. source stays 'yahoo'.
        # include_high_date is threaded so high_52w_date counts as a gap/fill target
        # ONLY on the opt-in run that fetched it — otherwise every bulk-path symbol
        # (which never has the date) would falsely look "gapped" and call Finnhub.
        raw = await apply_valuation_fallback(
            symbol, raw, yahoo_failed=yahoo_failed, include_high_date=include_high_date
        )

        # Nothing recovered from a total yahoo failure → preserve today's skip+warn.
        if yahoo_failed and not raw and yahoo_exc is not None:
            raise yahoo_exc
        return raw
    raise ValueError(f"unsupported market: {market}")


def _source_for_market(market: str) -> str:
    return "naver_finance" if market == "kr" else "yahoo"


# ROB-434: single source of truth for per-column raw-key priority. Used by
# _payload_from_raw AND finnhub_fallback's gap detection so they never drift.
# Mirrors _payload_from_raw's original or-chains exactly.
_FIELD_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "per": ("per", "PER", "trailingPE"),
    "pbr": ("pbr", "PBR", "priceToBook"),
    "roe": ("roe", "ROE"),
    "dividend_yield": (
        "dividend_yield",
        "Dividend Yield",
        "trailingAnnualDividendYield",
    ),
    "market_cap": ("market_cap", "marketCap"),
    "high_52w": ("high_52w", "yearHigh"),
    "low_52w": ("low_52w", "yearLow"),
    "high_52w_date": ("high_52w_date",),
}


def _resolve_raw_value(raw: dict[str, Any], field: str) -> Any:
    """First truthy value among the field's priority keys (matches the original
    or-chain: 0/None are treated as absent)."""
    for key in _FIELD_SOURCE_KEYS[field]:
        value = raw.get(key)
        if value:
            return value
    return None


def _payload_from_raw(
    *, market: str, symbol: str, snapshot_date: dt.date, raw: dict[str, Any]
) -> MarketValuationSnapshotUpsert:
    return MarketValuationSnapshotUpsert(
        market=market,
        symbol=symbol,
        snapshot_date=snapshot_date,
        source=_source_for_market(market),
        per=_to_decimal(_resolve_raw_value(raw, "per")),
        pbr=_to_decimal(_resolve_raw_value(raw, "pbr")),
        roe=_to_decimal(_resolve_raw_value(raw, "roe")),
        dividend_yield=_to_decimal(_resolve_raw_value(raw, "dividend_yield")),
        market_cap=_to_decimal(_resolve_raw_value(raw, "market_cap")),
        high_52w=_to_decimal(_resolve_raw_value(raw, "high_52w")),
        low_52w=_to_decimal(_resolve_raw_value(raw, "low_52w")),
        high_52w_date=_to_date(_resolve_raw_value(raw, "high_52w_date")),
        raw_payload=redact_sensitive_payload(dict(raw)),
    )


async def build_valuation_snapshots_bulk_for_us(
    *, snapshot_date: dt.date, limit: int | None = None
) -> MarketValuationBuildResult:
    from app.services.market_valuation_snapshots.us_provider import (
        TvScreenerUsValuationProvider,
    )

    provider = TvScreenerUsValuationProvider()
    rows = await provider.fetch_rows(limit=limit)
    payloads = []
    for row in rows:
        symbol = row.get("symbol", "").split(":")[-1]  # Strip exchange prefix
        if not symbol:
            continue
        payloads.append(
            MarketValuationSnapshotUpsert(
                market="us",
                symbol=symbol,
                snapshot_date=snapshot_date,
                source="tvscreener",
                per=_to_decimal(row.get("price_earnings_ttm")),
                pbr=_to_decimal(row.get("price_book_ratio")),
                roe=_to_decimal(row.get("return_on_equity")),
                dividend_yield=_to_decimal(row.get("dividends_yield")),
                market_cap=_to_decimal(row.get("market_cap_basic")),
                high_52w=_to_decimal(row.get("price_52_week_high")),
                low_52w=_to_decimal(row.get("price_52_week_low")),
                high_52w_date=_to_date(row.get("price_52_week_high_date")),
                raw_payload=row,
            )
        )
    return MarketValuationBuildResult(payloads=tuple(payloads))


async def build_valuation_snapshots_for_market(
    *,
    market: str,
    symbols: Iterable[str],
    snapshot_date: dt.date,
    concurrency: int = 4,
    fetcher: ValuationFetcher | None = None,
    include_high_date: bool = False,
    use_bulk: bool = False,
) -> MarketValuationBuildResult:
    market_norm = market.strip().lower()
    if market_norm not in {"kr", "us"}:
        raise ValueError(f"unsupported market: {market}")

    if market_norm == "us" and use_bulk:
        bulk_result = await build_valuation_snapshots_bulk_for_us(
            snapshot_date=snapshot_date,
        )
        requested_symbols = {s.strip().upper() for s in symbols if s.strip()}
        if requested_symbols:
            filtered_payloads = tuple(
                p for p in bulk_result.payloads if p.symbol.upper() in requested_symbols
            )
            return MarketValuationBuildResult(payloads=filtered_payloads)
        return bulk_result

    # ROB-440 PR4: thread include_high_date into the default fetcher (opt-in heavy
    # OHLC). Custom fetchers manage their own behavior.
    fetch = fetcher or functools.partial(
        default_valuation_fetcher, include_high_date=include_high_date
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    symbols_list = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
    payloads: list[MarketValuationSnapshotUpsert | None] = [None] * len(symbols_list)
    warnings: list[str] = []

    async def _one(idx: int, symbol: str) -> None:
        async with sem:
            try:
                raw = await fetch(symbol, market_norm)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "valuation snapshot fetch failed market=%s symbol=%s: %s",
                    market_norm,
                    symbol,
                    exc,
                )
                warnings.append(f"{symbol}: fetch failed ({exc})")
                return
            payload = _payload_from_raw(
                market=market_norm, symbol=symbol, snapshot_date=snapshot_date, raw=raw
            )
            if not any(
                getattr(payload, field) is not None
                for field in (
                    "per",
                    "pbr",
                    "roe",
                    "dividend_yield",
                    "market_cap",
                    "high_52w",
                    "low_52w",
                )
            ):
                warnings.append(
                    f"{symbol}: skipped because valuation metrics are unavailable"
                )
                return
            payloads[idx] = payload

    await asyncio.gather(
        *(_one(idx, symbol) for idx, symbol in enumerate(symbols_list))
    )
    return MarketValuationBuildResult(
        payloads=tuple(payload for payload in payloads if payload is not None),
        warnings=tuple(warnings),
    )
