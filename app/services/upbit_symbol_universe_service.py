from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.upbit_symbol_universe import UpbitSymbolUniverse

logger = logging.getLogger(__name__)

_UPBIT_MARKET_ALL_URL = "https://api.upbit.com/v1/market/all"
_UPBIT_UNIVERSE_SYNC_COMMAND = "uv run python scripts/sync_upbit_symbol_universe.py"

_upbit_maps: dict[str, dict[str, str]] | None = None


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
    symbol: str
    korean_name: str
    english_name: str
    market: str
    market_warning: str


def _sync_hint() -> str:
    return f"Sync required: {_UPBIT_UNIVERSE_SYNC_COMMAND}"


def _normalize_name(value: str) -> str:
    return str(value or "").strip()


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _normalize_market_warning(value: str) -> str:
    warning = str(value or "").strip().upper()
    return warning or "NONE"


def _split_symbol(symbol: str) -> tuple[str, str] | None:
    if "-" not in symbol:
        return None
    quote, base = symbol.split("-", 1)
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

    snapshot: dict[str, _UniverseRow] = {}
    skipped = 0
    for item in rows:
        symbol = _normalize_symbol(item.get("market", ""))
        split = _split_symbol(symbol)
        if split is None:
            skipped += 1
            continue

        market, _ = split
        korean_name = _normalize_name(item.get("korean_name", ""))
        english_name = _normalize_name(item.get("english_name", ""))
        if not korean_name or not english_name:
            skipped += 1
            continue

        snapshot[symbol] = _UniverseRow(
            symbol=symbol,
            korean_name=korean_name,
            english_name=english_name,
            market=market,
            market_warning=_normalize_market_warning(
                item.get("market_warning", "NONE")
            ),
        )

    if not snapshot:
        raise ValueError(f"upbit_symbol_universe source is empty. {_sync_hint()}")

    if skipped > 0:
        logger.warning("Upbit symbol universe parse skipped invalid rows=%d", skipped)

    return snapshot


async def _apply_snapshot(
    db: AsyncSession,
    snapshot: dict[str, _UniverseRow],
) -> dict[str, int]:
    existing_result = await db.execute(select(UpbitSymbolUniverse))
    existing_rows = {row.symbol: row for row in list(existing_result.scalars().all())}

    inserted = 0
    updated = 0
    deactivated = 0

    for symbol, row in snapshot.items():
        existing = existing_rows.get(symbol)
        if existing is None:
            db.add(
                UpbitSymbolUniverse(
                    symbol=row.symbol,
                    korean_name=row.korean_name,
                    english_name=row.english_name,
                    market=row.market,
                    market_warning=row.market_warning,
                    is_active=True,
                )
            )
            inserted += 1
            continue

        changed = False
        if existing.korean_name != row.korean_name:
            existing.korean_name = row.korean_name
            changed = True
        if existing.english_name != row.english_name:
            existing.english_name = row.english_name
            changed = True
        if existing.market != row.market:
            existing.market = row.market
            changed = True
        if existing.market_warning != row.market_warning:
            existing.market_warning = row.market_warning
            changed = True
        if not existing.is_active:
            existing.is_active = True
            changed = True
        if changed:
            updated += 1

    snapshot_symbols = set(snapshot)
    for symbol, existing in existing_rows.items():
        if symbol in snapshot_symbols:
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

    async with AsyncSessionLocal() as session:
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


async def _has_any_rows(db: AsyncSession) -> bool:
    result = await db.execute(select(UpbitSymbolUniverse.symbol).limit(1))
    return result.scalar_one_or_none() is not None


def _build_maps(rows: list[UpbitSymbolUniverse]) -> dict[str, dict[str, str]]:
    pair_to_name_kr: dict[str, str] = {}
    name_to_pair_kr: dict[str, str] = {}
    coin_to_name_kr: dict[str, str] = {}
    coin_to_name_en: dict[str, str] = {}
    coin_to_pair: dict[str, str] = {}

    for row in rows:
        split = _split_symbol(row.symbol)
        if split is None:
            continue
        _, coin = split
        pair = row.symbol

        pair_to_name_kr[pair] = row.korean_name
        name_to_pair_kr[row.korean_name] = pair
        coin_to_name_kr[coin] = row.korean_name
        coin_to_name_en[coin] = row.english_name
        coin_to_pair[coin] = pair

    return {
        "NAME_TO_PAIR_KR": name_to_pair_kr,
        "PAIR_TO_NAME_KR": pair_to_name_kr,
        "COIN_TO_PAIR": coin_to_pair,
        "COIN_TO_NAME_KR": coin_to_name_kr,
        "COIN_TO_NAME_EN": coin_to_name_en,
    }


async def _load_active_krw_rows(db: AsyncSession) -> list[UpbitSymbolUniverse]:
    stmt = (
        select(UpbitSymbolUniverse)
        .where(
            UpbitSymbolUniverse.is_active.is_(True),
            UpbitSymbolUniverse.market == "KRW",
        )
        .order_by(UpbitSymbolUniverse.symbol.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def _load_maps_from_db(db: AsyncSession) -> dict[str, dict[str, str]]:
    rows = await _load_active_krw_rows(db)
    if not rows:
        if not await _has_any_rows(db):
            raise UpbitSymbolUniverseEmptyError(
                f"upbit_symbol_universe is empty. {_sync_hint()}"
            )
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe has no active KRW symbols. {_sync_hint()}"
        )

    maps = _build_maps(rows)
    if not maps["COIN_TO_PAIR"]:
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe has no active KRW symbols. {_sync_hint()}"
        )
    return maps


async def get_upbit_maps(db: AsyncSession | None = None) -> dict[str, dict[str, str]]:
    global _upbit_maps

    if _upbit_maps is not None and db is None:
        return _upbit_maps

    if db is not None:
        maps = await _load_maps_from_db(db)
        if _upbit_maps is None:
            _upbit_maps = maps
        return maps

    async with AsyncSessionLocal() as session:
        maps = await _load_maps_from_db(session)
    _upbit_maps = maps
    return maps


async def get_or_refresh_maps(
    force: bool = False,
    db: AsyncSession | None = None,
) -> dict[str, dict[str, str]]:
    global _upbit_maps
    if force:
        if db is not None:
            maps = await _load_maps_from_db(db)
            _upbit_maps = maps
            return maps
        async with AsyncSessionLocal() as session:
            maps = await _load_maps_from_db(session)
        _upbit_maps = maps
        return maps

    return await get_upbit_maps(db=db)


async def prime_upbit_constants() -> None:
    await get_upbit_maps()


async def _resolve_active_symbol_by_name(db: AsyncSession, name: str) -> str:
    normalized_name = _normalize_name(name)
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
        if not await _has_any_rows(db):
            raise UpbitSymbolUniverseEmptyError(
                f"upbit_symbol_universe is empty. {_sync_hint()}"
            )
        raise UpbitSymbolNotRegisteredError(
            f"Upbit name '{normalized_name}' is not registered in upbit_symbol_universe. "
            f"{_sync_hint()}"
        )

    active_rows = [row for row in rows if row.is_active]
    if not active_rows:
        symbols = sorted({row.symbol for row in rows})
        preview = ", ".join(symbols[:10])
        raise UpbitSymbolInactiveError(
            f"Upbit name '{normalized_name}' is inactive in upbit_symbol_universe. "
            f"matched_symbols=[{preview}]. {_sync_hint()}"
        )

    unique_symbols = sorted({row.symbol for row in active_rows})
    if len(unique_symbols) > 1:
        preview = ", ".join(unique_symbols[:10])
        raise UpbitSymbolNameAmbiguousError(
            f"Upbit name '{normalized_name}' is ambiguous in upbit_symbol_universe. "
            f"matched_symbols=[{preview}] count={len(unique_symbols)}. {_sync_hint()}"
        )

    return unique_symbols[0]


async def get_upbit_symbol_by_name(
    name: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        return await _resolve_active_symbol_by_name(db, name)

    async with AsyncSessionLocal() as session:
        return await _resolve_active_symbol_by_name(session, name)


async def _search_upbit_symbols_impl(
    db: AsyncSession,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_name(query)
    if not normalized_query:
        return []

    pattern = f"%{normalized_query}%"
    stmt = (
        select(UpbitSymbolUniverse)
        .where(
            UpbitSymbolUniverse.is_active.is_(True),
            or_(
                UpbitSymbolUniverse.symbol.ilike(pattern),
                UpbitSymbolUniverse.korean_name.ilike(pattern),
                UpbitSymbolUniverse.english_name.ilike(pattern),
            ),
        )
        .order_by(UpbitSymbolUniverse.symbol.asc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows and not await _has_any_rows(db):
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe is empty. {_sync_hint()}"
        )

    return [
        {
            "symbol": row.symbol,
            "name": row.korean_name or row.english_name or row.symbol,
            "instrument_type": "crypto",
            "exchange": row.market,
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

    async with AsyncSessionLocal() as session:
        return await _search_upbit_symbols_impl(session, query, capped_limit)


async def _get_active_upbit_markets_impl(
    db: AsyncSession,
    fiat: str | None,
) -> list[str]:
    normalized_fiat = str(fiat or "").strip().upper()
    stmt = select(UpbitSymbolUniverse.symbol).where(
        UpbitSymbolUniverse.is_active.is_(True)
    )
    if normalized_fiat:
        stmt = stmt.where(UpbitSymbolUniverse.market == normalized_fiat)
    stmt = stmt.order_by(UpbitSymbolUniverse.symbol.asc())
    markets = list((await db.execute(stmt)).scalars().all())

    if not markets and not await _has_any_rows(db):
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe is empty. {_sync_hint()}"
        )
    return markets


async def _get_upbit_warning_markets_impl(
    db: AsyncSession,
    fiat: str | None,
) -> set[str]:
    normalized_fiat = str(fiat or "").strip().upper()
    stmt = select(UpbitSymbolUniverse.symbol).where(
        UpbitSymbolUniverse.is_active.is_(True),
        UpbitSymbolUniverse.market_warning == "CAUTION",
    )
    if normalized_fiat:
        stmt = stmt.where(UpbitSymbolUniverse.market == normalized_fiat)
    result = await db.execute(stmt)
    warning_markets = {
        str(symbol).strip().upper()
        for symbol in result.scalars().all()
        if str(symbol).strip()
    }

    if not warning_markets and not await _has_any_rows(db):
        raise UpbitSymbolUniverseEmptyError(
            f"upbit_symbol_universe is empty. {_sync_hint()}"
        )
    return warning_markets


async def get_active_upbit_markets(
    fiat: str | None = None,
    db: AsyncSession | None = None,
) -> list[str]:
    if db is not None:
        return await _get_active_upbit_markets_impl(db, fiat)

    async with AsyncSessionLocal() as session:
        return await _get_active_upbit_markets_impl(session, fiat)


async def get_upbit_warning_markets(
    fiat: str | None = None,
    db: AsyncSession | None = None,
) -> set[str]:
    if db is not None:
        return await _get_upbit_warning_markets_impl(db, fiat)

    async with AsyncSessionLocal() as session:
        return await _get_upbit_warning_markets_impl(session, fiat)


class _LazyUpbitDict:
    def __init__(self, key: str):
        self._key = key

    def _get_data(self) -> dict[str, str]:
        global _upbit_maps
        if _upbit_maps is None:
            raise RuntimeError(
                "Upbit symbol universe maps are not initialized. "
                "Call 'await prime_upbit_constants()' first."
            )
        return _upbit_maps[self._key]

    def __getitem__(self, key: str) -> str:
        return self._get_data()[key]

    def __contains__(self, key: object) -> bool:
        return key in self._get_data()

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._get_data().get(key, default)

    def keys(self):
        return self._get_data().keys()

    def values(self):
        return self._get_data().values()

    def items(self):
        return self._get_data().items()

    def __iter__(self):
        return iter(self._get_data())

    def __len__(self) -> int:
        return len(self._get_data())

    def __repr__(self) -> str:
        return repr(self._get_data())


class _LazyUpbitSet:
    def _get_data(self) -> set[str]:
        global _upbit_maps
        if _upbit_maps is None:
            raise RuntimeError(
                "Upbit symbol universe maps are not initialized. "
                "Call 'await prime_upbit_constants()' first."
            )
        return set(_upbit_maps["COIN_TO_NAME_KR"].keys())

    def __contains__(self, item: object) -> bool:
        return item in self._get_data()

    def __iter__(self):
        return iter(self._get_data())

    def __len__(self) -> int:
        return len(self._get_data())

    def __repr__(self) -> str:
        return repr(self._get_data())


NAME_TO_PAIR_KR = _LazyUpbitDict("NAME_TO_PAIR_KR")
PAIR_TO_NAME_KR = _LazyUpbitDict("PAIR_TO_NAME_KR")
COIN_TO_PAIR = _LazyUpbitDict("COIN_TO_PAIR")
COIN_TO_NAME_KR = _LazyUpbitDict("COIN_TO_NAME_KR")
COIN_TO_NAME_EN = _LazyUpbitDict("COIN_TO_NAME_EN")
KRW_TRADABLE_COINS = _LazyUpbitSet()


__all__ = [
    "UpbitSymbolUniverseLookupError",
    "UpbitSymbolUniverseEmptyError",
    "UpbitSymbolNotRegisteredError",
    "UpbitSymbolInactiveError",
    "UpbitSymbolNameAmbiguousError",
    "NAME_TO_PAIR_KR",
    "PAIR_TO_NAME_KR",
    "COIN_TO_PAIR",
    "COIN_TO_NAME_KR",
    "COIN_TO_NAME_EN",
    "KRW_TRADABLE_COINS",
    "build_upbit_symbol_universe_snapshot",
    "get_active_upbit_markets",
    "get_or_refresh_maps",
    "get_upbit_warning_markets",
    "get_upbit_maps",
    "get_upbit_symbol_by_name",
    "prime_upbit_constants",
    "search_upbit_symbols",
    "sync_upbit_symbol_universe",
]
