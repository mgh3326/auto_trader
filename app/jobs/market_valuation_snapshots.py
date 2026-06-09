"""Dry-run-first job runner for market_valuation_snapshots rows."""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
)
from app.services.market_valuation_snapshots.builder import (
    build_valuation_snapshots_for_market,
)
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
    MarketValuationSnapshotUpsert,
)
from app.services.snapshot_commit_guard import assert_min_coverage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketValuationSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 100
    concurrency: int = 4
    commit: bool = False
    today: dt.date | None = None
    # ROB-440 PR4: US screener wants common stocks only (is_common_stock) — the full
    # active universe (~12k incl ETFs/warrants) over-loads yfinance (FD/401). KR has
    # no is_common_stock column, so this is US-effective only.
    common_stocks_only: bool = False
    # ROB-440 PR4: the 52w-high DATE needs a heavy OHLC fetch (3rd yahoo call/symbol);
    # opt-in so the bulk valuation backfill stays light. Only undervalued_breakout
    # date-recency consumes it.
    include_high_date: bool = False


@dataclass(frozen=True)
class MarketValuationSnapshotSample:
    market: str
    symbol: str
    source: str
    snapshot_date: dt.date
    per: Decimal | None
    pbr: Decimal | None
    roe: Decimal | None
    dividend_yield: Decimal | None


@dataclass(frozen=True)
class MarketValuationSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    batches: int
    started_at: dt.datetime
    finished_at: dt.datetime
    snapshot_date_distribution: dict[str, int] = field(default_factory=dict)
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[MarketValuationSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()
    finnhub_backfill: dict[str, int] = field(default_factory=dict)
    field_nonnull_coverage: dict[str, int] = field(default_factory=dict)


def _validate_market(market: str) -> str:
    market_norm = market.strip().lower()
    if market_norm not in {"kr", "us"}:
        raise ValueError(f"Unsupported valuation snapshot market: {market}")
    return market_norm


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


async def resolve_symbols(
    market: str,
    override: list[str],
    limit: int,
    *,
    common_stocks_only: bool = False,
) -> list[str]:
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

            stmt = sa.select(USSymbolUniverse.symbol).where(
                USSymbolUniverse.is_active.is_(True)
            )
            if common_stocks_only:  # ROB-440 PR4: US common-stock filter
                stmt = stmt.where(USSymbolUniverse.is_common_stock.is_(True))
            stmt = stmt.order_by(USSymbolUniverse.symbol).limit(limit)
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def resolve_active_universe(
    market: str, *, common_stocks_only: bool = False
) -> list[str]:
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

            stmt = sa.select(USSymbolUniverse.symbol).where(
                USSymbolUniverse.is_active.is_(True)
            )
            if common_stocks_only:  # ROB-440 PR4: US common-stock filter
                stmt = stmt.where(USSymbolUniverse.is_common_stock.is_(True))
            stmt = stmt.order_by(USSymbolUniverse.symbol)
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


def _payload_key(
    payload: MarketValuationSnapshotUpsert,
) -> tuple[str, str, dt.date, str]:
    return (
        payload.market.strip().lower(),
        _normalize_symbol(payload.symbol),
        payload.snapshot_date,
        payload.source.strip().lower(),
    )


async def _classify_idempotency(
    payloads: list[MarketValuationSnapshotUpsert],
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
            MarketValuationSnapshot.market == market,
            MarketValuationSnapshot.symbol == symbol,
            MarketValuationSnapshot.snapshot_date == snapshot_date,
            MarketValuationSnapshot.source == source,
        )
        for market, symbol, snapshot_date, source in unique_keys
    ]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(
                MarketValuationSnapshot.market,
                MarketValuationSnapshot.symbol,
                MarketValuationSnapshot.snapshot_date,
                MarketValuationSnapshot.source,
            ).where(sa.or_(*conditions))
        )
        existing = set(result.all())
    return {
        "wouldInsert": len(unique_keys) - len(existing),
        "wouldUpdate": len(existing),
        "duplicatePayloadKeys": duplicate_payload_keys,
    }


async def _commit_payloads(payloads: list[MarketValuationSnapshotUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        await MarketValuationSnapshotsRepository(session).upsert(payloads)
        await session.commit()


def _sample(payload: MarketValuationSnapshotUpsert) -> MarketValuationSnapshotSample:
    return MarketValuationSnapshotSample(
        market=payload.market,
        symbol=payload.symbol,
        source=payload.source,
        snapshot_date=payload.snapshot_date,
        per=payload.per,
        pbr=payload.pbr,
        roe=payload.roe,
        dividend_yield=payload.dividend_yield,
    )


_COVERAGE_FIELDS: tuple[str, ...] = (
    "per",
    "pbr",
    "roe",
    "dividend_yield",
    "market_cap",
    "high_52w",
    "low_52w",
    "high_52w_date",
)


def _aggregate_report(
    payloads: list[MarketValuationSnapshotUpsert],
) -> tuple[dict[str, int], dict[str, int]]:
    """ROB-434: per-field Finnhub-backfill counts + per-field non-null coverage,
    derived from each payload's raw_payload['_field_provenance'] and column values.
    Operator smoke (acceptance #5): provider attribution + coverage, works in dry-run."""
    backfill: Counter[str] = Counter()
    coverage: Counter[str] = Counter(dict.fromkeys(_COVERAGE_FIELDS, 0))
    for payload in payloads:
        provenance = (payload.raw_payload or {}).get("_field_provenance", {})
        for field_name, src in provenance.items():
            if src == "finnhub":
                backfill[field_name] += 1
        for field_name in _COVERAGE_FIELDS:
            if getattr(payload, field_name) is not None:
                coverage[field_name] += 1
    return dict(backfill), dict(coverage)


async def run_market_valuation_snapshot_build(
    request: MarketValuationSnapshotBuildRequest,
) -> MarketValuationSnapshotBuildResult:
    market = _validate_market(request.market)
    started_at = dt.datetime.now(dt.UTC)
    today = request.today or started_at.date()
    symbols = await (
        resolve_active_universe(market, common_stocks_only=request.common_stocks_only)
        if request.all_symbols
        else resolve_symbols(
            market,
            list(request.symbols),
            request.limit or 20,
            common_stocks_only=request.common_stocks_only,
        )
    )
    if not symbols:
        finished_at = dt.datetime.now(dt.UTC)
        return MarketValuationSnapshotBuildResult(
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
    samples: list[MarketValuationSnapshotSample] = []
    warnings: list[str] = []
    total_built = 0
    batches = 0
    finnhub_backfill: Counter[str] = Counter()
    coverage: Counter[str] = Counter(dict.fromkeys(_COVERAGE_FIELDS, 0))
    for start in range(0, len(symbols), effective_batch_size):
        batches += 1
        result = await build_valuation_snapshots_for_market(
            market=market,
            symbols=symbols[start : start + effective_batch_size],
            snapshot_date=today,
            concurrency=request.concurrency,
            include_high_date=request.include_high_date,
        )
        payloads = list(result.payloads)
        warnings.extend(f"batch {batches}: {warning}" for warning in result.warnings)
        total_built += len(payloads)
        distribution.update(p.snapshot_date.isoformat() for p in payloads)
        idempotency.update(await _classify_idempotency(payloads))
        samples.extend(_sample(p) for p in payloads[: max(0, 10 - len(samples))])
        batch_backfill, batch_coverage = _aggregate_report(payloads)
        finnhub_backfill.update(batch_backfill)
        coverage.update(batch_coverage)
        if request.commit and payloads:
            await _commit_payloads(payloads)
    finished_at = dt.datetime.now(dt.UTC)
    return MarketValuationSnapshotBuildResult(
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
        finnhub_backfill=dict(finnhub_backfill),
        field_nonnull_coverage=dict(coverage),
    )


async def run_market_valuation_snapshot_build_guarded(
    request: MarketValuationSnapshotBuildRequest,
) -> MarketValuationSnapshotBuildResult:
    """Two-pass coverage-guarded commit (ROB-426 PR2b). Mirrors the quote wrapper:
    dry no-commit pass → assert >= 60% of active KR/US universe → commit pass.
    Crypto/other markets are skipped. Raises PartialCommitBlocked on a thin build.
    """
    dry = await run_market_valuation_snapshot_build(replace(request, commit=False))
    if dry.market in ("kr", "us"):
        async with AsyncSessionLocal() as session:
            universe_count = await active_universe_count(session, market=dry.market)
        if universe_count <= 0:
            logger.warning(
                "%s valuation commit guard disabled: active universe count is 0 "
                "(coverage could not be verified)",
                dry.market,
            )
        assert_min_coverage(dry.snapshots_built, universe_count, market=dry.market)
    return await run_market_valuation_snapshot_build(request)
