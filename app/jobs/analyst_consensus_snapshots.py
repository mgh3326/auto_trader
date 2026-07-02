"""Dry-run-first job runner for analyst_consensus_snapshots rows (ROB-641)."""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.models.analyst_consensus_snapshot import AnalystConsensusSnapshot
from app.services.analyst_consensus_snapshots.builder import build_consensus_snapshots
from app.services.analyst_consensus_snapshots.repository import (
    AnalystConsensusSnapshotsRepository,
    AnalystConsensusSnapshotUpsert,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalystConsensusSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 100
    concurrency: int = 4
    commit: bool = False
    now: dt.datetime | None = None


@dataclass(frozen=True)
class AnalystConsensusSnapshotSample:
    market: str
    symbol: str
    source: str
    snapshot_date: dt.date
    total_count: int | None
    target_mean: Decimal | None
    current_price: Decimal | None


@dataclass(frozen=True)
class AnalystConsensusSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    batches: int
    started_at: dt.datetime
    finished_at: dt.datetime
    snapshot_date_distribution: dict[str, int] = field(default_factory=dict)
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[AnalystConsensusSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()


def _validate_market(market: str) -> str:
    market_norm = market.strip().lower()
    if market_norm not in {"kr", "us"}:
        raise ValueError(f"Unsupported consensus snapshot market: {market}")
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
        else:
            from app.models.us_symbol_universe import USSymbolUniverse

            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
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
        else:
            from app.models.us_symbol_universe import USSymbolUniverse

            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(USSymbolUniverse.is_active.is_(True))
                .order_by(USSymbolUniverse.symbol)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


def _payload_key(
    payload: AnalystConsensusSnapshotUpsert,
) -> tuple[str, str, dt.date, str]:
    return (
        payload.market.strip().lower(),
        _normalize_symbol(payload.symbol),
        payload.snapshot_date,
        payload.source.strip().lower(),
    )


async def _classify_idempotency(
    payloads: list[AnalystConsensusSnapshotUpsert],
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
            AnalystConsensusSnapshot.market == market,
            AnalystConsensusSnapshot.symbol == symbol,
            AnalystConsensusSnapshot.snapshot_date == snapshot_date,
            AnalystConsensusSnapshot.source == source,
        )
        for market, symbol, snapshot_date, source in unique_keys
    ]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(
                AnalystConsensusSnapshot.market,
                AnalystConsensusSnapshot.symbol,
                AnalystConsensusSnapshot.snapshot_date,
                AnalystConsensusSnapshot.source,
            ).where(sa.or_(*conditions))
        )
        existing = set(result.all())
    return {
        "wouldInsert": len(unique_keys) - len(existing),
        "wouldUpdate": len(existing),
        "duplicatePayloadKeys": duplicate_payload_keys,
    }


async def _commit_payloads(payloads: list[AnalystConsensusSnapshotUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        await AnalystConsensusSnapshotsRepository(session).upsert(payloads)
        await session.commit()


def _sample(payload: AnalystConsensusSnapshotUpsert) -> AnalystConsensusSnapshotSample:
    return AnalystConsensusSnapshotSample(
        market=payload.market,
        symbol=payload.symbol,
        source=payload.source,
        snapshot_date=payload.snapshot_date,
        total_count=payload.total_count,
        target_mean=payload.target_mean,
        current_price=payload.current_price,
    )


async def run_analyst_consensus_snapshot_build(
    request: AnalystConsensusSnapshotBuildRequest,
) -> AnalystConsensusSnapshotBuildResult:
    market = _validate_market(request.market)
    started_at = request.now or dt.datetime.now(dt.UTC)
    symbols = await (
        resolve_active_universe(market)
        if request.all_symbols
        else resolve_symbols(market, list(request.symbols), request.limit or 20)
    )
    if not symbols:
        finished_at = dt.datetime.now(dt.UTC)
        return AnalystConsensusSnapshotBuildResult(
            market=market,
            symbols_resolved=0,
            snapshots_built=0,
            committed=request.commit,
            batches=0,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={
                "wouldInsert": 0,
                "wouldUpdate": 0,
                "duplicatePayloadKeys": 0,
            },
            warnings=("no symbols resolved",),
        )
    effective_batch_size = max(
        1, request.batch_size if request.all_symbols else len(symbols)
    )
    idempotency = Counter(
        {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0}
    )
    distribution: Counter[str] = Counter()
    samples: list[AnalystConsensusSnapshotSample] = []
    warnings: list[str] = []
    total_built = 0
    batches = 0
    for start in range(0, len(symbols), effective_batch_size):
        batches += 1
        result = await build_consensus_snapshots(
            market=market,
            symbols=symbols[start : start + effective_batch_size],
            now=started_at,
            concurrency=request.concurrency,
        )
        payloads = list(result.payloads)
        warnings.extend(f"batch {batches}: {warning}" for warning in result.warnings)
        total_built += len(payloads)
        distribution.update(p.snapshot_date.isoformat() for p in payloads)
        idempotency.update(await _classify_idempotency(payloads))
        samples.extend(_sample(p) for p in payloads[: max(0, 10 - len(samples))])
        if request.commit and payloads:
            await _commit_payloads(payloads)
    finished_at = dt.datetime.now(dt.UTC)
    return AnalystConsensusSnapshotBuildResult(
        market=market,
        symbols_resolved=len(symbols),
        snapshots_built=total_built,
        committed=request.commit,
        batches=batches,
        started_at=started_at,
        finished_at=finished_at,
        snapshot_date_distribution=dict(sorted(distribution.items())),
        idempotency=dict(idempotency),
        samples=tuple(samples),
        warnings=tuple(warnings),
    )
