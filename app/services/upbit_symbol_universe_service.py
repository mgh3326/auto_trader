from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.services.symbol_universe_common import has_any_rows, normalize_name, sync_hint
from app.services.upbit_market_event import (
    STATE_STALE,
    STATE_UNKNOWN,
    STATE_WARNING,
    MarketEventState,
    load_market_event_states,
    parse_market_event,
)

logger = logging.getLogger(__name__)

_UPBIT_MARKET_ALL_URL = "https://api.upbit.com/v1/market/all"
_UPBIT_UNIVERSE_SYNC_COMMAND = "uv run python scripts/sync_upbit_symbol_universe.py"

#: Stored ``market_warning`` provenance vocabulary (hk 5945). ``NONE``/``CAUTION``
#: are explicit Upbit statements. ``UNKNOWN`` means the source did not say:
#: the ``market_warning`` key was absent, null, or blank at sync time. Never
#: store ``NONE`` for an absent source — readers treat ``NONE`` as "no warning".
#: hk 6026: this column is now only a legacy projection of
#: ``market_event.warning`` (유의); new readers use the closed state from
#: ``app.services.upbit_market_event``.
UNKNOWN_MARKET_WARNING = "UNKNOWN"


class UpbitSymbolUniverseLookupError(ValueError):
    pass


class UpbitSymbolUniverseEmptyError(UpbitSymbolUniverseLookupError):
    pass


class UpbitSymbolNotRegisteredError(UpbitSymbolUniverseLookupError):
    pass


class UpbitSymbolInactiveError(UpbitSymbolUniverseLookupError):
    pass


class UpbitSymbolNameAmbiguousError(UpbitSymbolUniverseLookupError):
    pass


@dataclass(frozen=True)
class _UniverseRow:
    market: str
    quote_currency: str
    base_currency: str
    korean_name: str
    english_name: str
    market_event: dict[str, Any] | None
    market_event_observed_at: datetime


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _legacy_market_warning(market_event: dict[str, Any] | None) -> str:
    """Legacy ``market_warning`` projection: 유의 only (hk 6026).

    Old Upbit ``market_warning`` meant 유의 종목 여부, which ``market_event.warning``
    now carries; 주의(caution) never lived in that column. ``UNKNOWN`` when
    Upbit did not state the field.
    """
    parsed = parse_market_event(market_event)
    if parsed is None:
        return UNKNOWN_MARKET_WARNING
    return "CAUTION" if parsed["warning"] else "NONE"


def _split_market(market: str) -> tuple[str, str] | None:
    if "-" not in market:
        return None
    quote, base = market.split("-", 1)
    quote = quote.strip().upper()
    base = base.strip().upper()
    if not quote or not base:
        return None
    return quote, base


async def _fetch_upbit_market_all() -> list[dict[str, Any]]:
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(_UPBIT_MARKET_ALL_URL, params={"isDetails": "true"})
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("unexpected Upbit market/all response shape")
    return payload


async def build_upbit_symbol_universe_snapshot() -> dict[str, _UniverseRow]:
    rows = await _fetch_upbit_market_all()
    observed_at = datetime.now(UTC)

    snapshot: dict[str, _UniverseRow] = {}
    skipped = 0
    unreadable_market_events = 0
    for item in rows:
        market = _normalize_symbol(item.get("market", ""))
        split = _split_market(market)
        if split is None:
            skipped += 1
            continue

        quote_currency, base_currency = split
        korean_name = normalize_name(item.get("korean_name", ""))
        english_name = normalize_name(item.get("english_name", ""))
        if not korean_name or not english_name:
            skipped += 1
            continue

        # Upbit replaced ``market_warning`` with ``market_event`` (2024-11-20);
        # the old key is gone from the response, so only the new one is read.
        market_event = parse_market_event(item.get("market_event"))
        if market_event is None:
            unreadable_market_events += 1

        snapshot[market] = _UniverseRow(
            market=market,
            quote_currency=quote_currency,
            base_currency=base_currency,
            korean_name=korean_name,
            english_name=english_name,
            market_event=market_event,
            market_event_observed_at=observed_at,
        )

    if not snapshot:
        raise ValueError(
            f"upbit_symbol_universe source is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    if skipped > 0:
        logger.warning("Upbit symbol universe parse skipped invalid rows=%d", skipped)
    if unreadable_market_events > 0:
        logger.warning(
            "Upbit market_event unreadable for %d of %d markets (stored as unknown)",
            unreadable_market_events,
            len(snapshot),
        )

    return snapshot


async def _apply_snapshot(
    db: AsyncSession,
    snapshot: dict[str, _UniverseRow],
) -> dict[str, int]:
    existing_result = await db.execute(select(UpbitSymbolUniverse))
    existing_rows = {row.market: row for row in list(existing_result.scalars().all())}

    inserted = 0
    updated = 0
    deactivated = 0

    for market, row in snapshot.items():
        existing = existing_rows.get(market)
        if existing is None:
            db.add(
                UpbitSymbolUniverse(
                    market=row.market,
                    quote_currency=row.quote_currency,
                    base_currency=row.base_currency,
                    korean_name=row.korean_name,
                    english_name=row.english_name,
                    market_warning=_legacy_market_warning(row.market_event),
                    market_event=row.market_event,
                    market_event_observed_at=row.market_event_observed_at,
                    is_active=True,
                )
            )
            inserted += 1
            continue

        changed = False
        if existing.quote_currency != row.quote_currency:
            existing.quote_currency = row.quote_currency
            changed = True
        if existing.base_currency != row.base_currency:
            existing.base_currency = row.base_currency
            changed = True
        if existing.korean_name != row.korean_name:
            existing.korean_name = row.korean_name
            changed = True
        if existing.english_name != row.english_name:
            existing.english_name = row.english_name
            changed = True
        legacy_warning = _legacy_market_warning(row.market_event)
        if existing.market_warning != legacy_warning:
            existing.market_warning = legacy_warning
            changed = True
        if existing.market_event != row.market_event:
            existing.market_event = row.market_event
            changed = True
        if existing.market_event_observed_at != row.market_event_observed_at:
            existing.market_event_observed_at = row.market_event_observed_at
            changed = True
        if not existing.is_active:
            existing.is_active = True
            changed = True
        if changed:
            updated += 1

    snapshot_markets = set(snapshot)
    for market, existing in existing_rows.items():
        if market in snapshot_markets:
            continue
        if existing.is_active:
            existing.is_active = False
            deactivated += 1

    await db.flush()
    return {
        "total": len(snapshot),
        "inserted": inserted,
        "updated": updated,
        "deactivated": deactivated,
    }


async def sync_upbit_symbol_universe(db: AsyncSession | None = None) -> dict[str, int]:
    snapshot = await build_upbit_symbol_universe_snapshot()
    if db is not None:
        return await _apply_snapshot(db, snapshot)

    async with _internal_session() as session:
        async with session.begin():
            result = await _apply_snapshot(session, snapshot)

    logger.info(
        "Upbit symbol universe synced total=%d inserted=%d updated=%d deactivated=%d",
        result["total"],
        result["inserted"],
        result["updated"],
        result["deactivated"],
    )
    return result


@asynccontextmanager
async def _internal_session() -> AsyncIterator[AsyncSession]:
    session = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        yield session
    finally:
        await session.close()


def _normalize_quote_currency(value: str | None, *, default: str = "KRW") -> str:
    quote_currency = _normalize_symbol(value or default)
    if not quote_currency:
        raise ValueError("quote_currency is required")
    return quote_currency


async def _resolve_active_row_by_market(
    db: AsyncSession,
    market: str,
) -> UpbitSymbolUniverse:
    normalized_market = _normalize_symbol(market)
    if not normalized_market:
        raise ValueError("market is required")

    stmt = select(UpbitSymbolUniverse).where(
        UpbitSymbolUniverse.market == normalized_market
    )
    row = (await db.execute(stmt)).scalar_one_or_none()

    if row is None:
        if not await has_any_rows(db, UpbitSymbolUniverse.market):
            raise UpbitSymbolUniverseEmptyError(
                f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
            )
        raise UpbitSymbolNotRegisteredError(
            f"Upbit market '{normalized_market}' is not registered in upbit_symbol_universe. "
            f"{sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    if not row.is_active:
        raise UpbitSymbolInactiveError(
            f"Upbit market '{normalized_market}' is inactive in upbit_symbol_universe. "
            f"{sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    return row


async def _resolve_active_row_by_coin(
    db: AsyncSession,
    currency: str,
    quote_currency: str,
) -> UpbitSymbolUniverse:
    normalized_currency = _normalize_symbol(currency)
    if not normalized_currency:
        raise ValueError("currency is required")

    normalized_quote_currency = _normalize_quote_currency(quote_currency)
    stmt = (
        select(UpbitSymbolUniverse)
        .where(
            UpbitSymbolUniverse.base_currency == normalized_currency,
            UpbitSymbolUniverse.quote_currency == normalized_quote_currency,
        )
        .order_by(UpbitSymbolUniverse.market.asc())
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows:
        if not await has_any_rows(db, UpbitSymbolUniverse.market):
            raise UpbitSymbolUniverseEmptyError(
                f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
            )
        raise UpbitSymbolNotRegisteredError(
            f"Upbit coin '{normalized_currency}' is not registered for quote '{normalized_quote_currency}' "
            f"in upbit_symbol_universe. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    active_rows = [row for row in rows if row.is_active]
    if not active_rows:
        markets = sorted({row.market for row in rows})
        preview = ", ".join(markets[:10])
        raise UpbitSymbolInactiveError(
            f"Upbit coin '{normalized_currency}' for quote '{normalized_quote_currency}' is inactive "
            f"in upbit_symbol_universe. matched_symbols=[{preview}]. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    return active_rows[0]


async def _resolve_active_symbol_by_name(db: AsyncSession, name: str) -> str:
    normalized_name = normalize_name(name)
    if not normalized_name:
        raise ValueError("name is required")

    stmt = select(UpbitSymbolUniverse).where(
        or_(
            func.btrim(UpbitSymbolUniverse.korean_name) == normalized_name,
            func.btrim(UpbitSymbolUniverse.english_name) == normalized_name,
        )
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows:
        if not await has_any_rows(db, UpbitSymbolUniverse.market):
            raise UpbitSymbolUniverseEmptyError(
                f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
            )
        raise UpbitSymbolNotRegisteredError(
            f"Upbit name '{normalized_name}' is not registered in upbit_symbol_universe. "
            f"{sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    active_rows = [row for row in rows if row.is_active]
    if not active_rows:
        markets = sorted({row.market for row in rows})
        preview = ", ".join(markets[:10])
        raise UpbitSymbolInactiveError(
            f"Upbit name '{normalized_name}' is inactive in upbit_symbol_universe. "
            f"matched_symbols=[{preview}]. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    unique_symbols = sorted({row.market for row in active_rows})
    if len(unique_symbols) > 1:
        preview = ", ".join(unique_symbols[:10])
        raise UpbitSymbolNameAmbiguousError(
            f"Upbit name '{normalized_name}' is ambiguous in upbit_symbol_universe. "
            f"matched_symbols=[{preview}] count={len(unique_symbols)}. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    return unique_symbols[0]


async def get_upbit_symbol_by_name(
    name: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        return await _resolve_active_symbol_by_name(db, name)

    async with _internal_session() as session:
        return await _resolve_active_symbol_by_name(session, name)


async def get_upbit_market_by_coin(
    currency: str,
    quote_currency: str = "KRW",
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        row = await _resolve_active_row_by_coin(db, currency, quote_currency)
        return row.market

    async with _internal_session() as session:
        row = await _resolve_active_row_by_coin(session, currency, quote_currency)
    return row.market


async def get_upbit_korean_name_by_coin(
    currency: str,
    quote_currency: str = "KRW",
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        row = await _resolve_active_row_by_coin(db, currency, quote_currency)
        return row.korean_name

    async with _internal_session() as session:
        row = await _resolve_active_row_by_coin(session, currency, quote_currency)
    return row.korean_name


async def get_upbit_korean_name_by_market(
    market: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        row = await _resolve_active_row_by_market(db, market)
        return row.korean_name

    async with _internal_session() as session:
        row = await _resolve_active_row_by_market(session, market)
    return row.korean_name


async def _get_upbit_market_display_names_impl(
    db: AsyncSession,
    markets: list[str],
) -> dict[str, dict[str, str | None]]:
    normalized_markets = [
        normalized
        for normalized in {_normalize_symbol(market) for market in markets}
        if normalized
    ]
    if not normalized_markets:
        return {}

    stmt = select(UpbitSymbolUniverse).where(
        UpbitSymbolUniverse.market.in_(normalized_markets),
        UpbitSymbolUniverse.is_active.is_(True),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows and not await has_any_rows(db, UpbitSymbolUniverse.market):
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    return {
        row.market: {
            "korean_name": row.korean_name or None,
            "english_name": row.english_name or None,
        }
        for row in rows
    }


async def get_upbit_market_display_names(
    markets: list[str],
    db: AsyncSession | None = None,
) -> dict[str, dict[str, str | None]]:
    if db is not None:
        return await _get_upbit_market_display_names_impl(db, markets)

    async with _internal_session() as session:
        return await _get_upbit_market_display_names_impl(session, markets)


async def get_upbit_coin_by_market(
    market: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        row = await _resolve_active_row_by_market(db, market)
        return row.base_currency

    async with _internal_session() as session:
        row = await _resolve_active_row_by_market(session, market)
    return row.base_currency


async def _search_upbit_symbols_impl(
    db: AsyncSession,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []

    pattern = f"%{normalized_query}%"
    stmt = (
        select(UpbitSymbolUniverse)
        .where(
            UpbitSymbolUniverse.is_active.is_(True),
            or_(
                UpbitSymbolUniverse.market.ilike(pattern),
                UpbitSymbolUniverse.korean_name.ilike(pattern),
                UpbitSymbolUniverse.english_name.ilike(pattern),
            ),
        )
        .order_by(UpbitSymbolUniverse.market.asc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows and not await has_any_rows(db, UpbitSymbolUniverse.market):
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )

    return [
        {
            "symbol": row.market,
            "name": row.korean_name or row.english_name or row.market,
            "instrument_type": "crypto",
            "exchange": row.quote_currency,
            "is_active": row.is_active,
            "market_warning": row.market_warning,
        }
        for row in rows
    ]


async def search_upbit_symbols(
    query: str,
    limit: int,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    capped_limit = min(max(int(limit), 1), 100)
    if db is not None:
        return await _search_upbit_symbols_impl(db, query, capped_limit)

    async with _internal_session() as session:
        return await _search_upbit_symbols_impl(session, query, capped_limit)


async def _get_active_upbit_markets_impl(
    db: AsyncSession,
    quote_currency: str | None,
) -> set[str]:
    normalized_quote_currency = str(quote_currency or "").strip().upper()
    stmt = select(UpbitSymbolUniverse.market).where(
        UpbitSymbolUniverse.is_active.is_(True)
    )
    if normalized_quote_currency:
        stmt = stmt.where(
            UpbitSymbolUniverse.quote_currency == normalized_quote_currency
        )
    stmt = stmt.order_by(UpbitSymbolUniverse.market.asc())
    markets = {
        str(market).strip().upper()
        for market in (await db.execute(stmt)).scalars().all()
        if str(market).strip()
    }

    if not markets and not await has_any_rows(db, UpbitSymbolUniverse.market):
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )
    return markets


async def _load_upbit_market_event_states(
    db: AsyncSession,
    quote_currency: str | None,
    now: datetime | None = None,
) -> dict[str, MarketEventState]:
    states = await load_market_event_states(db, quote_currency=quote_currency, now=now)
    if not states and not await has_any_rows(db, UpbitSymbolUniverse.market):
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
        )
    return states


async def _get_upbit_warning_markets_impl(
    db: AsyncSession,
    quote_currency: str | None,
) -> set[str]:
    """Active markets Upbit currently designates 유의 (hk 6026).

    A legacy projection of the closed state: only ``warning`` qualifies.
    주의(caution) markets are not in this set — readers show them with their
    types instead of excluding them.
    """
    states = await _load_upbit_market_event_states(db, quote_currency)
    return {market for market, state in states.items() if state.state == STATE_WARNING}


async def _get_upbit_unknown_warning_markets_impl(
    db: AsyncSession,
    quote_currency: str | None,
) -> set[str]:
    """Active markets whose alert state cannot be trusted (hk 5945 / hk 6026).

    Matches ``unknown`` (Upbit did not state ``market_event``) and ``stale``
    (the last observation is older than
    :data:`app.services.upbit_market_event.MARKET_EVENT_STALE_AFTER`). Readers
    that treat a market as safe must treat this set as not-safe (fail closed).
    """
    states = await _load_upbit_market_event_states(db, quote_currency)
    return {
        market
        for market, state in states.items()
        if state.state in {STATE_UNKNOWN, STATE_STALE}
    }


async def get_active_upbit_markets(
    db: AsyncSession | None = None,
    quote_currency: str | None = None,
    fiat: str | None = None,
) -> set[str]:
    effective_quote_currency = quote_currency if quote_currency is not None else fiat
    if db is not None:
        return await _get_active_upbit_markets_impl(db, effective_quote_currency)

    async with _internal_session() as session:
        return await _get_active_upbit_markets_impl(
            session,
            effective_quote_currency,
        )


async def get_active_upbit_base_currencies(
    quote_currency: str = "KRW",
    db: AsyncSession | None = None,
) -> set[str]:
    normalized_quote_currency = _normalize_quote_currency(quote_currency)

    stmt = (
        select(UpbitSymbolUniverse.base_currency)
        .where(
            UpbitSymbolUniverse.is_active.is_(True),
            UpbitSymbolUniverse.quote_currency == normalized_quote_currency,
        )
        .order_by(UpbitSymbolUniverse.base_currency.asc())
    )

    async def _load(session: AsyncSession) -> set[str]:
        base_currencies = {
            str(base_currency).strip().upper()
            for base_currency in (await session.execute(stmt)).scalars().all()
            if str(base_currency).strip()
        }
        if not base_currencies and not await has_any_rows(
            session, UpbitSymbolUniverse.market
        ):
            raise UpbitSymbolUniverseEmptyError(
                f"upbit_symbol_universe is empty. {sync_hint(_UPBIT_UNIVERSE_SYNC_COMMAND)}"
            )
        return base_currencies

    if db is not None:
        return await _load(db)

    async with _internal_session() as session:
        return await _load(session)


async def get_upbit_market_event_states(
    db: AsyncSession | None = None,
    quote_currency: str | None = None,
    fiat: str | None = None,
    now: datetime | None = None,
) -> dict[str, MarketEventState]:
    """One closed alert state per active market (hk 6026).

    ``now`` exists for freshness tests; production callers pass nothing.
    """
    effective_quote_currency = quote_currency if quote_currency is not None else fiat
    if db is not None:
        return await _load_upbit_market_event_states(db, effective_quote_currency, now)

    async with _internal_session() as session:
        return await _load_upbit_market_event_states(
            session,
            effective_quote_currency,
            now,
        )


async def get_upbit_warning_markets(
    db: AsyncSession | None = None,
    quote_currency: str | None = None,
    fiat: str | None = None,
) -> set[str]:
    effective_quote_currency = quote_currency if quote_currency is not None else fiat
    if db is not None:
        return await _get_upbit_warning_markets_impl(db, effective_quote_currency)

    async with _internal_session() as session:
        return await _get_upbit_warning_markets_impl(
            session,
            effective_quote_currency,
        )


async def get_upbit_unknown_warning_markets(
    db: AsyncSession | None = None,
    quote_currency: str | None = None,
    fiat: str | None = None,
) -> set[str]:
    effective_quote_currency = quote_currency if quote_currency is not None else fiat
    if db is not None:
        return await _get_upbit_unknown_warning_markets_impl(
            db, effective_quote_currency
        )

    async with _internal_session() as session:
        return await _get_upbit_unknown_warning_markets_impl(
            session,
            effective_quote_currency,
        )


__all__ = [
    "UpbitSymbolUniverseLookupError",
    "UpbitSymbolUniverseEmptyError",
    "UpbitSymbolNotRegisteredError",
    "UpbitSymbolInactiveError",
    "UpbitSymbolNameAmbiguousError",
    "build_upbit_symbol_universe_snapshot",
    "get_active_upbit_markets",
    "get_active_upbit_base_currencies",
    "get_upbit_coin_by_market",
    "get_upbit_korean_name_by_coin",
    "get_upbit_korean_name_by_market",
    "get_upbit_market_display_names",
    "get_upbit_market_by_coin",
    "get_upbit_market_event_states",
    "get_upbit_unknown_warning_markets",
    "get_upbit_warning_markets",
    "get_upbit_symbol_by_name",
    "search_upbit_symbols",
    "sync_upbit_symbol_universe",
]
