"""Job runner for building invest_screener_snapshots rows.

This module is the reusable boundary shared by the operator CLI and TaskIQ tasks.
It keeps production writes behind an explicit commit flag; dry-run is the default
for callers that construct SnapshotBuildRequest without overriding commit.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field, replace

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.services.invest_screener_snapshots.builder import build_snapshots_for_market
from app.services.invest_screener_snapshots.guards import (
    InsufficientRowsError,
    SuspiciousDistributionError,
    assert_dominant_partition,
    assert_min_row_count,
)
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotBuildRequest:
    market: str
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 200
    concurrency: int = 4
    commit: bool = False
    common_stocks_only: bool = False
    today: dt.date | None = None


@dataclass(frozen=True)
class SnapshotSample:
    market: str
    symbol: str
    snapshot_date: dt.date
    latest_close: str
    consecutive_up_days: int | None
    week_change_rate: str | None


@dataclass(frozen=True)
class SnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    skipped: int
    committed: bool
    batches: int
    started_at: dt.datetime
    finished_at: dt.datetime
    snapshot_date_distribution: dict[str, int] = field(default_factory=dict)
    samples: tuple[SnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()


def _validate_market(market: str) -> None:
    if market not in {"kr", "us"}:
        raise ValueError(f"Unsupported market: {market}")


async def _ensure_common_stock_flags_populated(session) -> None:
    from app.services.us_common_stock_classifier import has_populated_common_stock_flags

    if not await has_populated_common_stock_flags(session):
        raise ValueError(
            "US common-stock filter requested, but us_symbol_universe.is_common_stock "
            "has not been populated. Run scripts.sync_us_common_stock_flags first."
        )


async def resolve_symbols(
    market: str, override: list[str], limit: int, *, common_stocks_only: bool = False
) -> list[str]:
    _validate_market(market)
    if common_stocks_only and market != "us":
        raise ValueError("common_stocks_only is only supported for market='us'")
    async with AsyncSessionLocal() as session:
        if override:
            if not common_stocks_only:
                return override
            from app.models.us_symbol_universe import USSymbolUniverse

            await _ensure_common_stock_flags_populated(session)
            normalized_override = [
                symbol.strip().upper() for symbol in override if symbol.strip()
            ]
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(
                    USSymbolUniverse.symbol.in_(normalized_override),
                    USSymbolUniverse.is_active.is_(True),
                    USSymbolUniverse.is_common_stock.is_(True),
                )
                .order_by(USSymbolUniverse.symbol)
            )
            result = await session.execute(stmt)
            return [r[0] for r in result.all()]
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse

            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
                .limit(limit)
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse

            conditions = [USSymbolUniverse.is_active.is_(True)]
            if common_stocks_only:
                await _ensure_common_stock_flags_populated(session)
                conditions.append(USSymbolUniverse.is_common_stock.is_(True))
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(*conditions)
                .order_by(USSymbolUniverse.symbol)
                .limit(limit)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def resolve_active_universe(
    market: str, *, common_stocks_only: bool = False
) -> list[str]:
    _validate_market(market)
    if common_stocks_only and market != "us":
        raise ValueError("common_stocks_only is only supported for market='us'")
    async with AsyncSessionLocal() as session:
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse

            stmt = (
                sa.select(KRSymbolUniverse.symbol)
                .where(KRSymbolUniverse.is_active.is_(True))
                .order_by(KRSymbolUniverse.symbol)
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse

            conditions = [USSymbolUniverse.is_active.is_(True)]
            if common_stocks_only:
                await _ensure_common_stock_flags_populated(session)
                conditions.append(USSymbolUniverse.is_common_stock.is_(True))
            stmt = (
                sa.select(USSymbolUniverse.symbol)
                .where(*conditions)
                .order_by(USSymbolUniverse.symbol)
            )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def _commit_payloads(payloads: list[SnapshotUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        repo = InvestScreenerSnapshotsRepository(session)
        for payload in payloads:
            await repo.upsert(payload)
        await session.commit()


def _sample(payload: SnapshotUpsert) -> SnapshotSample:
    return SnapshotSample(
        market=payload.market,
        symbol=payload.symbol,
        snapshot_date=payload.snapshot_date,
        latest_close=str(payload.latest_close),
        consecutive_up_days=payload.consecutive_up_days,
        week_change_rate=str(payload.week_change_rate)
        if payload.week_change_rate is not None
        else None,
    )


def _merge_date_distribution(
    aggregate: Counter[str], payloads: list[SnapshotUpsert]
) -> None:
    aggregate.update(p.snapshot_date.isoformat() for p in payloads)


async def run_snapshot_build(request: SnapshotBuildRequest) -> SnapshotBuildResult:
    """Build snapshots and optionally persist them.

    commit=False is intentionally a no-write dry-run. The function still returns
    payload counts/date distributions so operators can build approval packets.
    """
    started_at = dt.datetime.now(dt.UTC)
    today = request.today or started_at.date()
    warnings: list[str] = []

    if request.all_symbols:
        symbols = await resolve_active_universe(
            request.market, common_stocks_only=request.common_stocks_only
        )
    else:
        symbols = await resolve_symbols(
            request.market,
            list(request.symbols),
            request.limit or 20,
            common_stocks_only=request.common_stocks_only,
        )

    logger.info(
        "resolved %d symbols for market=%s all_symbols=%s common_stocks_only=%s commit=%s",
        len(symbols),
        request.market,
        request.all_symbols,
        request.common_stocks_only,
        request.commit,
    )

    if not symbols:
        finished_at = dt.datetime.now(dt.UTC)
        return SnapshotBuildResult(
            market=request.market,
            symbols_resolved=0,
            snapshots_built=0,
            skipped=0,
            committed=request.commit,
            batches=0,
            started_at=started_at,
            finished_at=finished_at,
            warnings=("no symbols resolved",),
        )

    total_built = 0
    batch_count = 0
    date_distribution: Counter[str] = Counter()
    samples: list[SnapshotSample] = []

    effective_batch_size = request.batch_size if request.all_symbols else len(symbols)
    effective_batch_size = max(1, effective_batch_size)
    for start in range(0, len(symbols), effective_batch_size):
        batch_count += 1
        batch = symbols[start : start + effective_batch_size]
        payloads = await build_snapshots_for_market(
            market=request.market,
            symbols=batch,
            today=today,
            concurrency=request.concurrency,
        )
        total_built += len(payloads)
        _merge_date_distribution(date_distribution, payloads)
        remaining_sample_slots = max(0, 10 - len(samples))
        if remaining_sample_slots:
            samples.extend(_sample(p) for p in payloads[:remaining_sample_slots])
        if len(payloads) < len(batch):
            warnings.append(
                f"batch {batch_count}: skipped {len(batch) - len(payloads)} "
                "symbols with unavailable OHLCV data"
            )
        if request.commit:
            await _commit_payloads(payloads)

    finished_at = dt.datetime.now(dt.UTC)
    return SnapshotBuildResult(
        market=request.market,
        symbols_resolved=len(symbols),
        snapshots_built=total_built,
        skipped=max(0, len(symbols) - total_built),
        committed=request.commit,
        batches=batch_count,
        started_at=started_at,
        finished_at=finished_at,
        snapshot_date_distribution=dict(sorted(date_distribution.items())),
        samples=tuple(samples),
        warnings=tuple(warnings),
    )


async def run_snapshot_build_guarded(
    request: SnapshotBuildRequest,
) -> SnapshotBuildResult:
    """ROB-281 Stage 5: dry-run → guards → commit wrapper.

    Two-pass behavior: always runs :func:`run_snapshot_build` first with
    ``commit=False`` to obtain ``snapshot_date_distribution`` and
    ``snapshots_built``, then applies the guards from
    :mod:`app.services.invest_screener_snapshots.guards`.

    * Guards pass + ``request.commit=True`` → re-runs ``run_snapshot_build``
      with the original commit flag (performs the actual DB writes).
    * Guards pass + ``request.commit=False`` → returns the dry-run result.
    * Guard violation → raises
      :class:`~app.services.invest_screener_snapshots.guards.SuspiciousDistributionError`
      or
      :class:`~app.services.invest_screener_snapshots.guards.InsufficientRowsError`.
      The dry-run distribution is preserved in the exception message so
      callers (Stage 6 Discord alerts) can surface it.

    Trade-off note: the 2× external-fetch cost is acceptable here because
    schedule cadence is at most 4 KR + 1 US runs per weekday. A truly
    transactional single-pass would require refactoring the per-batch commit
    boundary in :func:`run_snapshot_build` — out of scope for ROB-281.
    """
    dry_run_request = replace(request, commit=False)
    dry_run_result = await run_snapshot_build(dry_run_request)

    try:
        assert_dominant_partition(dry_run_result.snapshot_date_distribution)
        assert_min_row_count(dry_run_result.snapshots_built, dry_run_result.market)
    except (SuspiciousDistributionError, InsufficientRowsError) as exc:
        # Enrich the exception with the full dry-run distribution so Stage 6
        # Discord alerts can render the offending context.
        exc.distribution = dict(dry_run_result.snapshot_date_distribution)
        raise

    if not request.commit:
        return dry_run_result
    return await run_snapshot_build(request)
