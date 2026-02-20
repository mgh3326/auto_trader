from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.kr_symbol_universe import KRSymbolUniverse

logger = logging.getLogger(__name__)

_MASTER_BASE_URL = "https://new.real.download.dws.co.kr/common/master"
_KOSPI_ZIP = "kospi_code.mst.zip"
_KOSDAQ_ZIP = "kosdaq_code.mst.zip"
_NXT_KOSPI_ZIP = "nxt_kospi_code.mst.zip"
_NXT_KOSDAQ_ZIP = "nxt_kosdaq_code.mst.zip"
_KR_UNIVERSE_SYNC_COMMAND = "uv run python scripts/sync_kr_symbol_universe.py"

_KOSPI_SUFFIX_LENGTH = 228
_KOSDAQ_SUFFIX_LENGTH = 222


class KRSymbolUniverseLookupError(ValueError):
    pass


class KRSymbolUniverseEmptyError(KRSymbolUniverseLookupError):
    pass


class KRSymbolNotRegisteredError(KRSymbolUniverseLookupError):
    pass


class KRSymbolInactiveError(KRSymbolUniverseLookupError):
    pass


class KRSymbolNameAmbiguousError(KRSymbolUniverseLookupError):
    pass


@dataclass(frozen=True)
class _BaseSymbolRow:
    symbol: str
    name: str
    exchange: str


@dataclass(frozen=True)
class _UniverseRow:
    symbol: str
    name: str
    exchange: str
    nxt_eligible: bool


def _normalize_symbol_or_none(value: str) -> str | None:
    symbol = str(value or "").upper()
    if len(symbol) < 6:
        symbol = symbol.zfill(6)
    if len(symbol) == 6 and symbol.isalnum():
        return symbol
    return None


def _normalize_name(value: str) -> str:
    return str(value or "").strip()


def _sync_hint() -> str:
    return f"Sync required: {_KR_UNIVERSE_SYNC_COMMAND}"


async def _download_mst_lines(zip_name: str) -> list[str]:
    url = f"{_MASTER_BASE_URL}/{zip_name}"
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        member = zip_name.removesuffix(".zip")
        names = archive.namelist()
        if member not in names:
            candidates = [name for name in names if name.lower().endswith(".mst")]
            if len(candidates) != 1:
                raise ValueError(f"mst member not found in {zip_name}: {names}")
            member = candidates[0]
        payload = archive.read(member)
    content = payload.decode("cp949")
    return [line.rstrip("\r\n") for line in content.splitlines() if line.strip()]


def _parse_base_rows(
    lines: list[str],
    suffix_length: int,
    exchange: str,
) -> tuple[dict[str, _BaseSymbolRow], int]:
    parsed: dict[str, _BaseSymbolRow] = {}
    skipped = 0
    for line in lines:
        if len(line) <= suffix_length:
            raise ValueError(f"invalid mst row length for {exchange}: {line}")
        head = line[: len(line) - suffix_length]
        if len(head) < 21:
            raise ValueError(f"invalid mst header length for {exchange}: {line}")
        symbol = _normalize_symbol_or_none(head[0:9].rstrip().upper())
        if symbol is None:
            skipped += 1
            continue
        name = head[21:].strip()
        if not name:
            skipped += 1
            continue
        row = _BaseSymbolRow(symbol=symbol, name=name, exchange=exchange)
        existing = parsed.get(symbol)
        if existing is not None and (
            existing.exchange != row.exchange or existing.name != row.name
        ):
            raise ValueError(
                f"duplicate symbol conflict for {symbol}: "
                f"{existing.exchange}/{existing.name} vs {row.exchange}/{row.name}"
            )
        parsed[symbol] = row
    return parsed, skipped


def _parse_nxt_symbols(lines: list[str], suffix_length: int) -> tuple[set[str], int]:
    symbols: set[str] = set()
    skipped = 0
    for line in lines:
        if len(line) <= suffix_length:
            raise ValueError(f"invalid NXT mst row length: {line}")
        head = line[: len(line) - suffix_length]
        if len(head) < 9:
            raise ValueError(f"invalid NXT mst header length: {line}")
        symbol = _normalize_symbol_or_none(head[0:9].rstrip().upper())
        if symbol is None:
            skipped += 1
            continue
        symbols.add(symbol)
    return symbols, skipped


def _merge_base_rows(
    kospi: dict[str, _BaseSymbolRow],
    kosdaq: dict[str, _BaseSymbolRow],
) -> dict[str, _BaseSymbolRow]:
    merged = dict(kospi)
    for symbol, row in kosdaq.items():
        existing = merged.get(symbol)
        if existing is not None and (
            existing.exchange != row.exchange or existing.name != row.name
        ):
            raise ValueError(
                f"duplicate symbol conflict for {symbol}: "
                f"{existing.exchange}/{existing.name} vs {row.exchange}/{row.name}"
            )
        merged[symbol] = row
    return merged


def _build_snapshot(
    base_rows: dict[str, _BaseSymbolRow],
    nxt_symbols: set[str],
) -> dict[str, _UniverseRow]:
    if not base_rows:
        raise ValueError("base universe has no valid symbols")
    missing = sorted(nxt_symbols - set(base_rows))
    if missing:
        preview = ", ".join(missing[:10])
        raise ValueError(
            "NXT symbols missing in base universe: "
            f"count={len(missing)} symbols=[{preview}]"
        )
    snapshot: dict[str, _UniverseRow] = {}
    for symbol, row in base_rows.items():
        snapshot[symbol] = _UniverseRow(
            symbol=symbol,
            name=row.name,
            exchange=row.exchange,
            nxt_eligible=symbol in nxt_symbols,
        )
    return snapshot


async def build_kr_symbol_universe_snapshot() -> dict[str, _UniverseRow]:
    kospi_lines = await _download_mst_lines(_KOSPI_ZIP)
    kosdaq_lines = await _download_mst_lines(_KOSDAQ_ZIP)
    nxt_kospi_lines = await _download_mst_lines(_NXT_KOSPI_ZIP)
    nxt_kosdaq_lines = await _download_mst_lines(_NXT_KOSDAQ_ZIP)

    kospi_rows, kospi_skipped = _parse_base_rows(
        kospi_lines,
        _KOSPI_SUFFIX_LENGTH,
        "KOSPI",
    )
    logger.info(
        "KR symbol universe parse source=%s valid=%d skipped=%d",
        _KOSPI_ZIP,
        len(kospi_rows),
        kospi_skipped,
    )
    kosdaq_rows, kosdaq_skipped = _parse_base_rows(
        kosdaq_lines,
        _KOSDAQ_SUFFIX_LENGTH,
        "KOSDAQ",
    )
    logger.info(
        "KR symbol universe parse source=%s valid=%d skipped=%d",
        _KOSDAQ_ZIP,
        len(kosdaq_rows),
        kosdaq_skipped,
    )
    base_rows = _merge_base_rows(kospi_rows, kosdaq_rows)

    nxt_symbols, nxt_kospi_skipped = _parse_nxt_symbols(
        nxt_kospi_lines,
        _KOSPI_SUFFIX_LENGTH,
    )
    logger.info(
        "KR symbol universe parse source=%s valid=%d skipped=%d",
        _NXT_KOSPI_ZIP,
        len(nxt_symbols),
        nxt_kospi_skipped,
    )
    nxt_kosdaq_symbols, nxt_kosdaq_skipped = _parse_nxt_symbols(
        nxt_kosdaq_lines,
        _KOSDAQ_SUFFIX_LENGTH,
    )
    logger.info(
        "KR symbol universe parse source=%s valid=%d skipped=%d",
        _NXT_KOSDAQ_ZIP,
        len(nxt_kosdaq_symbols),
        nxt_kosdaq_skipped,
    )
    nxt_symbols.update(nxt_kosdaq_symbols)
    return _build_snapshot(base_rows, nxt_symbols)


async def _apply_snapshot(
    db: AsyncSession,
    snapshot: dict[str, _UniverseRow],
) -> dict[str, int]:
    existing_result = await db.execute(select(KRSymbolUniverse))
    existing_rows = {row.symbol: row for row in list(existing_result.scalars().all())}

    inserted = 0
    updated = 0
    deactivated = 0

    for symbol, row in snapshot.items():
        existing = existing_rows.get(symbol)
        if existing is None:
            db.add(
                KRSymbolUniverse(
                    symbol=row.symbol,
                    name=row.name,
                    exchange=row.exchange,
                    nxt_eligible=row.nxt_eligible,
                    is_active=True,
                )
            )
            inserted += 1
            continue

        changed = False
        if existing.name != row.name:
            existing.name = row.name
            changed = True
        if existing.exchange != row.exchange:
            existing.exchange = row.exchange
            changed = True
        if existing.nxt_eligible != row.nxt_eligible:
            existing.nxt_eligible = row.nxt_eligible
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


async def _has_any_rows(db: AsyncSession) -> bool:
    result = await db.execute(select(KRSymbolUniverse.symbol).limit(1))
    return result.scalar_one_or_none() is not None


async def _resolve_active_symbol_by_name(db: AsyncSession, name: str) -> str:
    normalized_name = _normalize_name(name)
    if not normalized_name:
        raise ValueError("name is required")

    stmt = select(KRSymbolUniverse).where(
        func.btrim(KRSymbolUniverse.name) == normalized_name
    )
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows:
        if not await _has_any_rows(db):
            raise KRSymbolUniverseEmptyError(
                f"kr_symbol_universe is empty. {_sync_hint()}"
            )
        raise KRSymbolNotRegisteredError(
            f"KR name '{normalized_name}' is not registered in kr_symbol_universe. "
            f"{_sync_hint()}"
        )

    active_rows = [row for row in rows if row.is_active]
    if not active_rows:
        symbols = sorted({row.symbol for row in rows})
        preview = ", ".join(symbols[:10])
        raise KRSymbolInactiveError(
            f"KR name '{normalized_name}' is inactive in kr_symbol_universe. "
            f"matched_symbols=[{preview}]. {_sync_hint()}"
        )

    unique_symbols = sorted({row.symbol for row in active_rows})
    if len(unique_symbols) > 1:
        preview = ", ".join(unique_symbols[:10])
        raise KRSymbolNameAmbiguousError(
            f"KR name '{normalized_name}' is ambiguous in kr_symbol_universe. "
            f"matched_symbols=[{preview}] count={len(unique_symbols)}. {_sync_hint()}"
        )

    return unique_symbols[0]


async def get_kr_symbol_by_name(
    name: str,
    db: AsyncSession | None = None,
) -> str:
    if db is not None:
        return await _resolve_active_symbol_by_name(db, name)

    async with AsyncSessionLocal() as session:
        return await _resolve_active_symbol_by_name(session, name)


async def _search_kr_symbols_impl(
    db: AsyncSession,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    normalized_query = _normalize_name(query)
    if not normalized_query:
        return []

    pattern = f"%{normalized_query}%"
    exchange_priority = case(
        (KRSymbolUniverse.exchange == "KOSPI", 0),
        (KRSymbolUniverse.exchange == "KOSDAQ", 1),
        else_=2,
    )
    stmt = (
        select(KRSymbolUniverse)
        .where(
            KRSymbolUniverse.is_active.is_(True),
            or_(
                KRSymbolUniverse.symbol.ilike(pattern),
                KRSymbolUniverse.name.ilike(pattern),
            ),
        )
        .order_by(
            exchange_priority.asc(),
            KRSymbolUniverse.name.asc(),
            KRSymbolUniverse.symbol.asc(),
        )
        .limit(limit)
    )

    rows = list((await db.execute(stmt)).scalars().all())
    if not rows and not await _has_any_rows(db):
        raise KRSymbolUniverseEmptyError(f"kr_symbol_universe is empty. {_sync_hint()}")

    return [
        {
            "symbol": row.symbol,
            "name": row.name,
            "instrument_type": "equity_kr",
            "exchange": row.exchange,
            "is_active": row.is_active,
        }
        for row in rows
    ]


async def search_kr_symbols(
    query: str,
    limit: int,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    capped_limit = min(max(int(limit), 1), 100)
    if db is not None:
        return await _search_kr_symbols_impl(db, query, capped_limit)

    async with AsyncSessionLocal() as session:
        return await _search_kr_symbols_impl(session, query, capped_limit)


async def sync_kr_symbol_universe(db: AsyncSession | None = None) -> dict[str, int]:
    snapshot = await build_kr_symbol_universe_snapshot()
    if db is not None:
        return await _apply_snapshot(db, snapshot)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await _apply_snapshot(session, snapshot)
    logger.info(
        "KR symbol universe synced total=%d inserted=%d updated=%d deactivated=%d",
        result["total"],
        result["inserted"],
        result["updated"],
        result["deactivated"],
    )
    return result


__all__ = [
    "KRSymbolUniverseLookupError",
    "KRSymbolUniverseEmptyError",
    "KRSymbolNotRegisteredError",
    "KRSymbolInactiveError",
    "KRSymbolNameAmbiguousError",
    "build_kr_symbol_universe_snapshot",
    "get_kr_symbol_by_name",
    "search_kr_symbols",
    "sync_kr_symbol_universe",
]
