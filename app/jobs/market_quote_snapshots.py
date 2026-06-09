"""Dry-run-first job runner for market_quote_snapshots rows."""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
)
from app.services.market_quote_snapshots.builder import build_quote_snapshots_for_market
from app.services.market_quote_snapshots.repository import (
    MarketQuoteSnapshotsRepository,
    MarketQuoteSnapshotUpsert,
)
from app.services.snapshot_commit_guard import assert_min_coverage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketQuoteSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 100
    concurrency: int = 4
    commit: bool = False
    now: dt.datetime | None = None


@dataclass(frozen=True)
class MarketQuoteSnapshotSample:
    market: str
    symbol: str
    source: str
    snapshot_at: dt.datetime
    price: Decimal
    previous_close: Decimal | None
    volume: int | None


@dataclass(frozen=True)
class MarketQuoteSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    batches: int
    started_at: dt.datetime
    finished_at: dt.datetime
    snapshot_at_distribution: dict[str, int] = field(default_factory=dict)
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[MarketQuoteSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()


def _validate_market(market: str) -> str:
    market_norm = market.strip().lower()
    if market_norm not in {"kr", "us", "crypto"}:
        raise ValueError(f"Unsupported quote snapshot market: {market}")
    return market_norm


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


async def resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    market_norm = _validate_market(market)
    if override:
        return [_normalize_symbol(symbol) for symbol in override if symbol.strip()]
    async with AsyncSessionLocal() as session:
        if market_norm == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse

            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
                .limit(limit)
            )
        elif market_norm == "us":
            from app.models.us_symbol_universe import USSymbolUniverse

            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
                .limit(limit)
            )
        else:
            from app.models.upbit_symbol_universe import UpbitSymbolUniverse

            stmt = (
                sa.select(UpbitSymbolUniverse.market)
                .where(UpbitSymbolUniverse.is_active.is_(True))
                .where(UpbitSymbolUniverse.quote_currency == "KRW")
                .order_by(UpbitSymbolUniverse.market)
                .limit(limit)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def resolve_active_universe(market: str) -> list[str]:
    market_norm = _validate_market(market)
    async with AsyncSessionLocal() as session:
        if market_norm == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse

            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
            )
        elif market_norm == "us":
            from app.models.us_symbol_universe import USSymbolUniverse

            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
            )
        else:
            from app.models.upbit_symbol_universe import UpbitSymbolUniverse

            stmt = (
                sa.select(UpbitSymbolUniverse.market)
                .where(UpbitSymbolUniverse.is_active.is_(True))
                .where(UpbitSymbolUniverse.quote_currency == "KRW")
                .order_by(UpbitSymbolUniverse.market)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


def _payload_key(
    payload: MarketQuoteSnapshotUpsert,
) -> tuple[str, str, str, dt.datetime]:
    return (
        payload.market.strip().lower(),
        _normalize_symbol(payload.symbol),
        payload.source.strip().lower(),
        payload.snapshot_at.replace(microsecond=0),
    )


async def _classify_idempotency(
    payloads: list[MarketQuoteSnapshotUpsert],
) -> dict[str, int]:
    keys = [_payload_key(payload) for payload in payloads]
    duplicate_payload_keys = sum(
        count - 1 for count in Counter(keys).values() if count > 1
    )
    unique_keys = set(keys)
    if not unique_keys:
        return {
            "wouldInsert": 0,
            "wouldUpdate": 0,
            "duplicatePayloadKeys": duplicate_payload_keys,
        }
    conditions = [
        sa.and_(
            MarketQuoteSnapshot.market == market,
            MarketQuoteSnapshot.symbol == symbol,
            MarketQuoteSnapshot.source == source,
            MarketQuoteSnapshot.snapshot_at == snapshot_at,
        )
        for market, symbol, source, snapshot_at in unique_keys
    ]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(
                MarketQuoteSnapshot.market,
                MarketQuoteSnapshot.symbol,
                MarketQuoteSnapshot.source,
                MarketQuoteSnapshot.snapshot_at,
            ).where(sa.or_(*conditions))
        )
        existing = set(result.all())
    return {
        "wouldInsert": len(unique_keys) - len(existing),
        "wouldUpdate": len(existing),
        "duplicatePayloadKeys": duplicate_payload_keys,
    }


async def _commit_payloads(payloads: list[MarketQuoteSnapshotUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        await MarketQuoteSnapshotsRepository(session).upsert(payloads)
        await session.commit()


def _sample(payload: MarketQuoteSnapshotUpsert) -> MarketQuoteSnapshotSample:
    return MarketQuoteSnapshotSample(
        market=payload.market,
        symbol=payload.symbol,
        source=payload.source,
        snapshot_at=payload.snapshot_at,
        price=payload.price,
        previous_close=payload.previous_close,
        volume=payload.volume,
    )


async def run_market_quote_snapshot_build(
    request: MarketQuoteSnapshotBuildRequest,
) -> MarketQuoteSnapshotBuildResult:
    market = _validate_market(request.market)
    started_at = request.now or dt.datetime.now(dt.UTC)
    symbols = await (
        resolve_active_universe(market)
        if request.all_symbols
        else resolve_symbols(market, list(request.symbols), request.limit or 20)
    )
    if not symbols:
        finished_at = dt.datetime.now(dt.UTC)
        return MarketQuoteSnapshotBuildResult(
            market=market,
            symbols_resolved=0,
            snapshots_built=0,
            committed=request.commit,
            batches=0,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=("no symbols resolved",),
        )
    effective_batch_size = max(
        1, request.batch_size if request.all_symbols else len(symbols)
    )
    idempotency = Counter(
        {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0}
    )
    distribution: Counter[str] = Counter()
    samples: list[MarketQuoteSnapshotSample] = []
    warnings: list[str] = []
    total_built = 0
    batches = 0
    for start in range(0, len(symbols), effective_batch_size):
        batches += 1
        result = await build_quote_snapshots_for_market(
            market=market,
            symbols=symbols[start : start + effective_batch_size],
            now=started_at,
            concurrency=request.concurrency,
        )
        payloads = list(result.payloads)
        warnings.extend(f"batch {batches}: {warning}" for warning in result.warnings)
        total_built += len(payloads)
        distribution.update(p.snapshot_at.isoformat() for p in payloads)
        idempotency.update(await _classify_idempotency(payloads))
        samples.extend(_sample(p) for p in payloads[: max(0, 10 - len(samples))])
        if request.commit and payloads:
            await _commit_payloads(payloads)
    finished_at = dt.datetime.now(dt.UTC)
    return MarketQuoteSnapshotBuildResult(
        market=market,
        symbols_resolved=len(symbols),
        snapshots_built=total_built,
        committed=request.commit,
        batches=batches,
        started_at=started_at,
        finished_at=finished_at,
        snapshot_at_distribution=dict(sorted(distribution.items())),
        idempotency=dict(idempotency),
        samples=tuple(samples),
        warnings=tuple(warnings),
    )


async def run_market_quote_snapshot_build_guarded(
    request: MarketQuoteSnapshotBuildRequest,
) -> MarketQuoteSnapshotBuildResult:
    """Two-pass coverage-guarded commit (ROB-426 PR2b).

    Runs a no-commit pass to count rows, asserts the build covers >= 60% of the
    active KR/US universe, then runs the committing pass. Crypto is skipped (no
    KR/US universe denominator). Raises PartialCommitBlocked on a thin build —
    the committing pass never runs. Callers wanting to bypass the guard call
    run_market_quote_snapshot_build directly (the --allow-partial path).
    """
    dry = await run_market_quote_snapshot_build(replace(request, commit=False))
    if dry.market in ("kr", "us"):
        async with AsyncSessionLocal() as session:
            universe_count = await active_universe_count(session, market=dry.market)
        if universe_count <= 0:
            logger.warning(
                "%s quote commit guard disabled: active universe count is 0 "
                "(coverage could not be verified)",
                dry.market,
            )
        assert_min_coverage(dry.snapshots_built, universe_count, market=dry.market)
    return await run_market_quote_snapshot_build(request)
