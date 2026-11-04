from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import zipfile
from collections.abc import AsyncIterator, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.symbol_universe_common import has_any_rows, normalize_name, sync_hint

logger = logging.getLogger(__name__)

_MASTER_BASE_URL = "https://new.real.download.dws.co.kr/common/master"
_US_SOURCE_FILES: tuple[tuple[str, str], ...] = (
    ("nasmst.cod.zip", "NASD"),
    ("nysmst.cod.zip", "NYSE"),
    ("amsmst.cod.zip", "AMEX"),
)
_US_UNIVERSE_SYNC_COMMAND = "uv run python scripts/sync_us_symbol_universe.py"


class USSymbolUniverseLookupError(ValueError):
    pass


class USSymbolUniverseEmptyError(USSymbolUniverseLookupError):
    pass


class USSymbolNotRegisteredError(USSymbolUniverseLookupError):
    pass


class USSymbolInactiveError(USSymbolUniverseLookupError):
    pass


class USSymbolNameAmbiguousError(USSymbolUniverseLookupError):
    pass


@dataclass(frozen=True)
class _UniverseRow:
    symbol: str
    exchange: str
    name_kr: str
    name_en: str


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _canonical_symbol(value: str) -> str:
    """Canonical ``us_symbol_universe.symbol`` key for one caller symbol."""
    return to_db_symbol(_normalize_symbol(value))


# ROB-1487: request-scoped exchange map.
#
# Batch callers (analyze_stock_batch, the get_holdings price fan-out, the
# screen_stocks streak enrichment) know their whole symbol set up front, so one
# ``us_exchange_prefetch`` scope resolves all of them with a single query.
# Without it every symbol issued its own single-row SELECT *and* its own
# ``AsyncSessionLocal()`` checkout inside a concurrency-4/5 fan-out, which
# pushed the MCP server past ``DB_POOL_SIZE`` and made the pool dial fresh
# connections (Sentry ``connect`` spans, p95 877 ms) on every burst.
_us_exchange_cache: ContextVar[dict[str, str] | None] = ContextVar(
    "us_exchange_cache", default=None
)


async def _download_cod_lines(zip_name: str) -> list[str]:
    url = f"{_MASTER_BASE_URL}/{zip_name}"
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        member = zip_name.removesuffix(".zip")
        names = archive.namelist()
        if member not in names:
            candidates = [name for name in names if name.lower().endswith(".cod")]
            if len(candidates) != 1:
                raise ValueError(f"cod member not found in {zip_name}: {names}")
            member = candidates[0]
        payload = archive.read(member)

    content = payload.decode("cp949")
    return [line.rstrip("\r\n") for line in content.splitlines() if line.strip()]


def _parse_cod_rows(lines: list[str], exchange: str) -> tuple[list[_UniverseRow], int]:
    rows: list[_UniverseRow] = []
    skipped = 0

    for columns in csv.reader(lines, delimiter="\t"):
        if len(columns) < 8:
            skipped += 1
            continue

        symbol = to_db_symbol(_normalize_symbol(columns[4]))
        if not symbol:
            skipped += 1
            continue

        rows.append(
            _UniverseRow(
                symbol=symbol,
                exchange=exchange,
                name_kr=normalize_name(columns[6]),
                name_en=normalize_name(columns[7]),
            )
        )

    return rows, skipped


async def build_us_symbol_universe_snapshot() -> dict[str, _UniverseRow]:
    source_names = [zip_name for zip_name, _ in _US_SOURCE_FILES]
    downloaded = await asyncio.gather(
        *[_download_cod_lines(name) for name in source_names]
    )

    snapshot: dict[str, _UniverseRow] = {}
    duplicate_overwrites = 0

    for (zip_name, exchange), lines in zip(_US_SOURCE_FILES, downloaded, strict=True):
        parsed_rows, skipped = _parse_cod_rows(lines, exchange)
        logger.info(
            "US symbol universe parse source=%s exchange=%s valid=%d skipped=%d",
            zip_name,
            exchange,
            len(parsed_rows),
            skipped,
        )
        for row in parsed_rows:
            existing = snapshot.get(row.symbol)
            if existing is not None and existing.exchange != row.exchange:
                duplicate_overwrites += 1
            snapshot[row.symbol] = row

    if not snapshot:
        raise ValueError(
            f"us_symbol_universe source is empty. {sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
        )

    if duplicate_overwrites > 0:
        logger.warning(
            "US symbol universe duplicate symbols overwritten count=%d",
            duplicate_overwrites,
        )

    return snapshot


async def _apply_snapshot(
    db: AsyncSession,
    snapshot: dict[str, _UniverseRow],
) -> dict[str, int]:
    existing_result = await db.execute(select(USSymbolUniverse))
    existing_rows = {row.symbol: row for row in list(existing_result.scalars().all())}

    inserted = 0
    updated = 0
    deactivated = 0

    for symbol, row in snapshot.items():
        existing = existing_rows.get(symbol)
        if existing is None:
            db.add(
                USSymbolUniverse(
                    symbol=row.symbol,
                    exchange=row.exchange,
                    name_kr=row.name_kr,
                    name_en=row.name_en,
                    is_active=True,
                )
            )
            inserted += 1
            continue

        changed = False
        if existing.exchange != row.exchange:
            existing.exchange = row.exchange
            changed = True
        if existing.name_kr != row.name_kr:
            existing.name_kr = row.name_kr
            changed = True
        if existing.name_en != row.name_en:
            existing.name_en = row.name_en
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


async def sync_us_symbol_universe(db: AsyncSession | None = None) -> dict[str, int]:
    snapshot = await build_us_symbol_universe_snapshot()
    if db is not None:
        return await _apply_snapshot(db, snapshot)

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        async with session.begin():
            result = await _apply_snapshot(session, snapshot)

    logger.info(
        "US symbol universe synced total=%d inserted=%d updated=%d deactivated=%d",
        result["total"],
        result["inserted"],
        result["updated"],
        result["deactivated"],
    )
    return result


async def _resolve_active_symbol_row(db: AsyncSession, symbol: str) -> USSymbolUniverse:
    normalized_symbol = to_db_symbol(_normalize_symbol(symbol))
    if not normalized_symbol:
        raise ValueError("symbol is required")

    result = await db.execute(
        select(USSymbolUniverse).where(USSymbolUniverse.symbol == normalized_symbol)
    )
    row = result.scalar_one_or_none()
    if row is None:
        if not await has_any_rows(db, USSymbolUniverse.symbol):
            raise USSymbolUniverseEmptyError(
                f"us_symbol_universe is empty. {sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
            )
        raise USSymbolNotRegisteredError(
            f"US symbol '{normalized_symbol}' is not registered in us_symbol_universe. "
            f"{sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
        )
    if not row.is_active:
        raise USSymbolInactiveError(
            f"US symbol '{normalized_symbol}' is inactive in us_symbol_universe. "
            f"{sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
        )

    return row


async def get_us_exchange_by_symbol(
    symbol: str,
    db: AsyncSession | None = None,
) -> str:
    # ROB-1487: a warm ``us_exchange_prefetch`` scope answers without a query and
    # without opening a session. A symbol the scope did not resolve (unknown,
    # inactive, or never requested) falls through to the single-row resolver
    # below, so results and error types are unchanged.
    cached = _us_exchange_cache.get()
    if cached is not None:
        canonical = _canonical_symbol(symbol)
        if canonical in cached:
            return cached[canonical]

    if db is not None:
        row = await _resolve_active_symbol_row(db, symbol)
        return row.exchange

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        row = await _resolve_active_symbol_row(session, symbol)
        return row.exchange


async def get_us_exchanges_by_symbols(
    symbols: Iterable[str],
    db: AsyncSession | None = None,
) -> dict[str, str]:
    """Return ``{canonical_symbol: exchange}`` for active US symbols in ONE query.

    Canonical keys come from ``to_db_symbol`` so ``BRK/B``, ``BRK-B`` and
    ``BRK.B`` all resolve to ``BRK.B``. Symbols that are absent from the active
    universe are simply omitted — the batched lookup is a prefetch, not an
    authority: ``get_us_exchange_by_symbol`` still raises
    ``USSymbolNotRegisteredError`` / ``USSymbolInactiveError`` /
    ``USSymbolUniverseEmptyError`` for a caller that requires the row.
    """
    canonical: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        key = _canonical_symbol(raw)
        if key and key not in seen:
            seen.add(key)
            canonical.append(key)
    if not canonical:
        return {}

    async def _run(session: AsyncSession) -> dict[str, str]:
        result = await session.execute(
            select(USSymbolUniverse.symbol, USSymbolUniverse.exchange).where(
                USSymbolUniverse.symbol.in_(canonical),
                USSymbolUniverse.is_active.is_(True),
            )
        )
        return dict(result.all())

    if db is not None:
        return await _run(db)

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        return await _run(session)


@contextlib.asynccontextmanager
async def us_exchange_prefetch(
    symbols: Iterable[str],
    db: AsyncSession | None = None,
) -> AsyncIterator[dict[str, str]]:
    """Resolve a batch's US exchanges once for the enclosed block (ROB-1487).

    Inside the block ``get_us_exchange_by_symbol`` answers from the prefetched
    map, so a fan-out over N US symbols costs one query and zero extra sessions
    instead of N queries and N session checkouts. The map is scoped to the
    block (and to the tasks spawned inside it) and is always reset on exit.

    Fail-open by contract: a prefetch is an optimization, so a failed batch read
    yields an empty map and the enclosed code falls back to the per-symbol
    resolver — exactly the behavior that existed before this helper. A prefetch
    failure must never make a caller that would have degraded (e.g. to the
    Yahoo quote arm) fail instead.
    """
    try:
        resolved = await get_us_exchanges_by_symbols(list(symbols), db=db)
    except Exception as exc:  # noqa: BLE001 — optimization only, never authoritative
        logger.warning(
            "us_exchange_prefetch failed; using per-symbol lookups instead: %s", exc
        )
        resolved = {}
    token = _us_exchange_cache.set(resolved)
    try:
        yield resolved
    finally:
        _us_exchange_cache.reset(token)


async def get_us_common_stock_flags(
    symbols: list[str],
    db: AsyncSession | None = None,
) -> dict[str, bool | None]:
    """Return ``{input_symbol: is_common_stock}`` for active US symbols (ROB-365 bug 5).

    ``is_common_stock`` is the authoritative NASDAQ-Trader flag (ROB-204). The
    returned dict is keyed by the caller's original symbol string for easy
    lookup. Symbols absent from the active universe are omitted; a present but
    un-synced symbol maps to ``None`` (the flag column is nullable). Callers
    treat both missing and ``None`` as "use algorithmic classification".
    """
    canon_to_original: dict[str, str] = {}
    for raw in symbols:
        if not raw:
            continue
        canon = to_db_symbol(_normalize_symbol(raw))
        if canon:
            canon_to_original.setdefault(canon, raw)
    if not canon_to_original:
        return {}

    async def _run(session: AsyncSession) -> dict[str, bool | None]:
        stmt = select(USSymbolUniverse.symbol, USSymbolUniverse.is_common_stock).where(
            USSymbolUniverse.symbol.in_(list(canon_to_original)),
            USSymbolUniverse.is_active.is_(True),
        )
        result = await session.execute(stmt)
        flags: dict[str, bool | None] = {}
        for canon_symbol, is_common in result.all():
            original = canon_to_original.get(canon_symbol)
            if original is not None:
                flags[original] = is_common
        return flags

    if db is not None:
        return await _run(db)

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        return await _run(session)


async def get_us_names_by_symbols(
    symbols: list[str],
    db: AsyncSession | None = None,
) -> dict[str, str]:
    """Return ``{input_symbol: display_name}`` for active US symbols.

    Name precedence mirrors ``search_us_symbols``: ``name_kr`` then ``name_en``.
    The dict is keyed by the caller's original symbol string. Symbols absent from
    the active universe (or with no usable name) are omitted, so callers keep
    showing the raw ticker on a miss.
    """
    canon_to_original: dict[str, str] = {}
    for raw in symbols:
        if not raw:
            continue
        canon = to_db_symbol(_normalize_symbol(raw))
        if canon:
            canon_to_original.setdefault(canon, raw)
    if not canon_to_original:
        return {}

    async def _run(session: AsyncSession) -> dict[str, str]:
        stmt = select(
            USSymbolUniverse.symbol,
            USSymbolUniverse.name_kr,
            USSymbolUniverse.name_en,
        ).where(
            USSymbolUniverse.symbol.in_(list(canon_to_original)),
            USSymbolUniverse.is_active.is_(True),
        )
        result = await session.execute(stmt)
        names: dict[str, str] = {}
        for canon_symbol, name_kr, name_en in result.all():
            original = canon_to_original.get(canon_symbol)
            if original is None:
                continue
            display = (name_kr or "").strip() or (name_en or "").strip()
            if display:
                names[original] = display
        return names

    if db is not None:
        return await _run(db)

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        return await _run(session)


async def _resolve_active_symbol_by_name(db: AsyncSession, name: str) -> str:
    normalized_name = normalize_name(name)
    if not normalized_name:
        raise ValueError("name is required")

    stmt = select(USSymbolUniverse).where(
        or_(
            func.btrim(USSymbolUniverse.name_kr) == normalized_name,
            func.btrim(USSymbolUniverse.name_en) == normalized_name,
        )
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows:
        if not await has_any_rows(db, USSymbolUniverse.symbol):
            raise USSymbolUniverseEmptyError(
                f"us_symbol_universe is empty. {sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
            )
        raise USSymbolNotRegisteredError(
            f"US name '{normalized_name}' is not registered in us_symbol_universe. "
            f"{sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
        )

    active_rows = [row for row in rows if row.is_active]
    if not active_rows:
        symbols = sorted({row.symbol for row in rows})
        preview = ", ".join(symbols[:10])
        raise USSymbolInactiveError(
            f"US name '{normalized_name}' is inactive in us_symbol_universe. "
            f"matched_symbols=[{preview}]. {sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
        )

    unique_symbols = sorted({row.symbol for row in active_rows})
    if len(unique_symbols) > 1:
        preview = ", ".join(unique_symbols[:10])
        raise USSymbolNameAmbiguousError(
            f"US name '{normalized_name}' is ambiguous in us_symbol_universe. "
            f"matched_symbols=[{preview}] count={len(unique_symbols)}. {sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
        )

    return unique_symbols[0]


async def get_us_symbol_by_name(
    name: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        return await _resolve_active_symbol_by_name(db, name)

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        return await _resolve_active_symbol_by_name(session, name)


async def _search_us_symbols_impl(
    db: AsyncSession,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    normalized_query = normalize_name(query)
    if not normalized_query:
        return []

    raw_symbol_query = _normalize_symbol(query)
    canonical_symbol_query = to_db_symbol(raw_symbol_query)
    symbol_predicates: list[ColumnElement[bool]] = []
    seen_symbol_queries: set[str] = set()

    for symbol_query in (raw_symbol_query, canonical_symbol_query):
        if symbol_query in seen_symbol_queries:
            continue
        seen_symbol_queries.add(symbol_query)
        symbol_predicates.append(USSymbolUniverse.symbol.ilike(f"%{symbol_query}%"))

    name_pattern = f"%{normalized_query}%"
    stmt = (
        select(USSymbolUniverse)
        .where(
            USSymbolUniverse.is_active.is_(True),
            or_(
                *symbol_predicates,
                USSymbolUniverse.name_kr.ilike(name_pattern),
                USSymbolUniverse.name_en.ilike(name_pattern),
            ),
        )
        .order_by(USSymbolUniverse.symbol.asc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows and not await has_any_rows(db, USSymbolUniverse.symbol):
        raise USSymbolUniverseEmptyError(
            f"us_symbol_universe is empty. {sync_hint(_US_UNIVERSE_SYNC_COMMAND)}"
        )

    return [
        {
            "symbol": row.symbol,
            "name": row.name_kr or row.name_en or row.symbol,
            "instrument_type": "equity_us",
            "exchange": row.exchange,
            "is_active": row.is_active,
        }
        for row in rows
    ]


async def search_us_symbols(
    query: str,
    limit: int,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    capped_limit = min(max(int(limit), 1), 100)
    if db is not None:
        return await _search_us_symbols_impl(db, query, capped_limit)

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        return await _search_us_symbols_impl(session, query, capped_limit)


__all__ = [
    "USSymbolUniverseLookupError",
    "USSymbolUniverseEmptyError",
    "USSymbolNotRegisteredError",
    "USSymbolInactiveError",
    "USSymbolNameAmbiguousError",
    "build_us_symbol_universe_snapshot",
    "get_us_exchange_by_symbol",
    "get_us_exchanges_by_symbols",
    "get_us_symbol_by_name",
    "search_us_symbols",
    "sync_us_symbol_universe",
    "us_exchange_prefetch",
]
