from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.us_symbol_universe import USSymbolUniverse

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


def _sync_hint() -> str:
    return f"Sync required: {_US_UNIVERSE_SYNC_COMMAND}"


def _normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def _normalize_name(value: str) -> str:
    return str(value or "").strip()


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

        symbol = _normalize_symbol(columns[4])
        if not symbol:
            skipped += 1
            continue

        rows.append(
            _UniverseRow(
                symbol=symbol,
                exchange=exchange,
                name_kr=_normalize_name(columns[6]),
                name_en=_normalize_name(columns[7]),
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
        raise ValueError(f"us_symbol_universe source is empty. {_sync_hint()}")

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

    async with AsyncSessionLocal() as session:
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


async def _has_any_rows(db: AsyncSession) -> bool:
    result = await db.execute(select(USSymbolUniverse.symbol).limit(1))
    return result.scalar_one_or_none() is not None


async def _resolve_active_symbol_row(db: AsyncSession, symbol: str) -> USSymbolUniverse:
    normalized_symbol = _normalize_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("symbol is required")

    result = await db.execute(
        select(USSymbolUniverse).where(USSymbolUniverse.symbol == normalized_symbol)
    )
    row = result.scalar_one_or_none()
    if row is None:
        if not await _has_any_rows(db):
            raise USSymbolUniverseEmptyError(
                f"us_symbol_universe is empty. {_sync_hint()}"
            )
        raise USSymbolNotRegisteredError(
            f"US symbol '{normalized_symbol}' is not registered in us_symbol_universe. "
            f"{_sync_hint()}"
        )
    if not row.is_active:
        raise USSymbolInactiveError(
            f"US symbol '{normalized_symbol}' is inactive in us_symbol_universe. "
            f"{_sync_hint()}"
        )

    return row


async def get_us_exchange_by_symbol(
    symbol: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        row = await _resolve_active_symbol_row(db, symbol)
        return row.exchange

    async with AsyncSessionLocal() as session:
        row = await _resolve_active_symbol_row(session, symbol)
        return row.exchange


async def _resolve_active_symbol_by_name(db: AsyncSession, name: str) -> str:
    normalized_name = _normalize_name(name)
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
        if not await _has_any_rows(db):
            raise USSymbolUniverseEmptyError(
                f"us_symbol_universe is empty. {_sync_hint()}"
            )
        raise USSymbolNotRegisteredError(
            f"US name '{normalized_name}' is not registered in us_symbol_universe. "
            f"{_sync_hint()}"
        )

    active_rows = [row for row in rows if row.is_active]
    if not active_rows:
        symbols = sorted({row.symbol for row in rows})
        preview = ", ".join(symbols[:10])
        raise USSymbolInactiveError(
            f"US name '{normalized_name}' is inactive in us_symbol_universe. "
            f"matched_symbols=[{preview}]. {_sync_hint()}"
        )

    unique_symbols = sorted({row.symbol for row in active_rows})
    if len(unique_symbols) > 1:
        preview = ", ".join(unique_symbols[:10])
        raise USSymbolNameAmbiguousError(
            f"US name '{normalized_name}' is ambiguous in us_symbol_universe. "
            f"matched_symbols=[{preview}] count={len(unique_symbols)}. {_sync_hint()}"
        )

    return unique_symbols[0]


async def get_us_symbol_by_name(
    name: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        return await _resolve_active_symbol_by_name(db, name)

    async with AsyncSessionLocal() as session:
        return await _resolve_active_symbol_by_name(session, name)


async def _search_us_symbols_impl(
    db: AsyncSession,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_name(query)
    if not normalized_query:
        return []

    pattern = f"%{normalized_query}%"
    stmt = (
        select(USSymbolUniverse)
        .where(
            USSymbolUniverse.is_active.is_(True),
            or_(
                USSymbolUniverse.symbol.ilike(pattern),
                USSymbolUniverse.name_kr.ilike(pattern),
                USSymbolUniverse.name_en.ilike(pattern),
            ),
        )
        .order_by(USSymbolUniverse.symbol.asc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows and not await _has_any_rows(db):
        raise USSymbolUniverseEmptyError(f"us_symbol_universe is empty. {_sync_hint()}")

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

    async with AsyncSessionLocal() as session:
        return await _search_us_symbols_impl(session, query, capped_limit)


__all__ = [
    "USSymbolUniverseLookupError",
    "USSymbolUniverseEmptyError",
    "USSymbolNotRegisteredError",
    "USSymbolInactiveError",
    "USSymbolNameAmbiguousError",
    "build_us_symbol_universe_snapshot",
    "get_us_exchange_by_symbol",
    "get_us_symbol_by_name",
    "search_us_symbols",
    "sync_us_symbol_universe",
]
