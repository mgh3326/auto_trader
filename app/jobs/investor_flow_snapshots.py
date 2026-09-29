"""Dry-run-first job runner for KR investor_flow_snapshots rows."""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.services.investor_flow_snapshots.builder import build_investor_flow_snapshots
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
    InvestorFlowSnapshotUpsert,
)
from app.services.market_events.session_calendar import trading_session_status

logger = logging.getLogger(__name__)


class InvestorFlowEmptyCommitError(RuntimeError):
    """A commit-enabled build produced zero rows on a day expected to have data."""


@dataclass(frozen=True)
class InvestorFlowSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    batch_size: int = 100
    concurrency: int = 4
    days: int = 20
    commit: bool = False
    today: dt.date | None = None


@dataclass(frozen=True)
class InvestorFlowSnapshotSample:
    market: str
    symbol: str
    snapshot_date: dt.date
    source: str
    foreign_net: int | None
    institution_net: int | None
    individual_net: int | None
    double_buy: bool
    double_sell: bool
    # ROB-640 market fields so dry-run approval packets show the wired values.
    close: Decimal | None = None
    change_rate: Decimal | None = None  # percent, e.g. 1.5 for 1.5%
    volume: int | None = None
    foreign_holding_shares: int | None = None
    foreign_holding_rate: Decimal | None = None  # percent 0-100


@dataclass(frozen=True)
class InvestorFlowSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    batches: int
    started_at: dt.datetime
    finished_at: dt.datetime
    snapshot_date_distribution: dict[str, int] = field(default_factory=dict)
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[InvestorFlowSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()


def _validate_market(market: str) -> None:
    if market.strip().lower() != "kr":
        raise ValueError(f"Unsupported investor-flow snapshot market: {market}")


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


async def resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    _validate_market(market)
    if override:
        return [_normalize_symbol(symbol) for symbol in override if symbol.strip()]
    async with AsyncSessionLocal() as session:
        from app.models.kr_symbol_universe import KRSymbolUniverse

        stmt = (
            sa.select(KRSymbolUniverse.symbol)
            .where(KRSymbolUniverse.is_active.is_(True))
            .order_by(KRSymbolUniverse.symbol)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


async def resolve_active_universe(market: str) -> list[str]:
    _validate_market(market)
    async with AsyncSessionLocal() as session:
        from app.models.kr_symbol_universe import KRSymbolUniverse

        stmt = (
            sa.select(KRSymbolUniverse.symbol)
            .where(KRSymbolUniverse.is_active.is_(True))
            .order_by(KRSymbolUniverse.symbol)
        )
        result = await session.execute(stmt)
        return [r[0] for r in result.all()]


def _payload_key(payload: InvestorFlowSnapshotUpsert) -> tuple[str, str, dt.date, str]:
    return (
        payload.market.strip().lower(),
        _normalize_symbol(payload.symbol),
        payload.snapshot_date,
        payload.source,
    )


async def _classify_idempotency(
    payloads: list[InvestorFlowSnapshotUpsert],
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
            InvestorFlowSnapshot.market == market,
            InvestorFlowSnapshot.symbol == symbol,
            InvestorFlowSnapshot.snapshot_date == snapshot_date,
            InvestorFlowSnapshot.source == source,
        )
        for market, symbol, snapshot_date, source in unique_keys
    ]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(
                InvestorFlowSnapshot.market,
                InvestorFlowSnapshot.symbol,
                InvestorFlowSnapshot.snapshot_date,
                InvestorFlowSnapshot.source,
            ).where(sa.or_(*conditions))
        )
        existing = set(result.all())
    would_update = len(existing)
    return {
        "wouldInsert": len(unique_keys) - would_update,
        "wouldUpdate": would_update,
        "duplicatePayloadKeys": duplicate_payload_keys,
    }


async def _commit_payloads(payloads: list[InvestorFlowSnapshotUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        repo = InvestorFlowSnapshotsRepository(session)
        for payload in payloads:
            await repo.upsert(payload)
        await session.commit()


def _double_buy(payload: InvestorFlowSnapshotUpsert) -> bool:
    return bool(
        payload.foreign_net is not None
        and payload.institution_net is not None
        and payload.foreign_net > 0
        and payload.institution_net > 0
    )


def _double_sell(payload: InvestorFlowSnapshotUpsert) -> bool:
    return bool(
        payload.foreign_net is not None
        and payload.institution_net is not None
        and payload.foreign_net < 0
        and payload.institution_net < 0
    )


def _sample(payload: InvestorFlowSnapshotUpsert) -> InvestorFlowSnapshotSample:
    return InvestorFlowSnapshotSample(
        market=payload.market,
        symbol=payload.symbol,
        snapshot_date=payload.snapshot_date,
        source=payload.source,
        foreign_net=payload.foreign_net,
        institution_net=payload.institution_net,
        individual_net=payload.individual_net,
        double_buy=_double_buy(payload),
        double_sell=_double_sell(payload),
        close=payload.close,
        change_rate=payload.change_rate,
        volume=payload.volume,
        foreign_holding_shares=payload.foreign_holding_shares,
        foreign_holding_rate=payload.foreign_holding_rate,
    )


def _merge_date_distribution(
    aggregate: Counter[str], payloads: list[InvestorFlowSnapshotUpsert]
) -> None:
    aggregate.update(p.snapshot_date.isoformat() for p in payloads)


def _enforce_zero_commit_floor(
    *,
    commit: bool,
    run_date: dt.date,
    symbols_resolved: int,
) -> None:
    """Fail a commit run that produced zero rows on an expected-data day.

    #895: the upstream Naver page stopped returning investor-flow rows while the
    scheduled run kept reporting Completed with committed=0, leaving the
    investor_flow_momentum / double_buy snapshots silently stale. A commit run
    that builds zero rows is only legitimate on a confirmed non-trading day.

    ``run_date`` is the KST business date the run is responsible for. The XKRX
    session calendar is the authority: ``closed`` (weekend/holiday) stays OK;
    ``open`` fails; ``unknown`` also fails — a calendar that cannot classify the
    date cannot excuse an empty commit (fail-closed, matching
    ``session_calendar``'s contract).
    """
    if not commit:
        return
    status = trading_session_status("kr", run_date)
    if status == "closed":
        logger.info(
            "investor-flow build committed 0 rows for %d symbols on %s: "
            "XKRX session closed, treated as non-trading day",
            symbols_resolved,
            run_date,
        )
        return
    if status == "unknown":
        raise InvestorFlowEmptyCommitError(
            f"investor-flow snapshot build committed 0 rows for "
            f"{symbols_resolved} symbols on {run_date}: XKRX calendar could not "
            f"classify the run date, so an empty commit cannot be excused as a "
            f"non-trading day — failing loudly"
        )
    raise InvestorFlowEmptyCommitError(
        f"investor-flow snapshot build committed 0 rows on KRX trading day "
        f"{run_date} ({symbols_resolved} symbols resolved, upstream returned no "
        f"rows for any symbol) — failing loudly instead of reporting success"
    )


async def run_investor_flow_snapshot_build(
    request: InvestorFlowSnapshotBuildRequest,
) -> InvestorFlowSnapshotBuildResult:
    """Build KR investor-flow snapshots and optionally persist them.

    commit=False is intentionally a no-write dry-run. commit=True is the only
    write path and is reserved for an explicit production approval flow.
    """
    _validate_market(request.market)
    if request.days < 1:
        raise ValueError("days must be >= 1")
    started_at = dt.datetime.now(dt.UTC)
    today = request.today or started_at.date()
    # The zero-commit floor is keyed to the KST business date, not the UTC date
    # embedded in started_at (an early-morning KST run is still the previous UTC
    # day, which would misclassify a trading day as a weekend).
    run_date_kst = request.today or now_kst().date()
    warnings: list[str] = []

    if request.all_symbols:
        symbols = await resolve_active_universe(request.market)
    else:
        symbols = await resolve_symbols(
            request.market, list(request.symbols), request.limit or 20
        )

    logger.info(
        "resolved %d KR investor-flow symbols all_symbols=%s commit=%s",
        len(symbols),
        request.all_symbols,
        request.commit,
    )

    if not symbols:
        _enforce_zero_commit_floor(
            commit=request.commit, run_date=run_date_kst, symbols_resolved=0
        )
        finished_at = dt.datetime.now(dt.UTC)
        return InvestorFlowSnapshotBuildResult(
            market="kr",
            symbols_resolved=0,
            snapshots_built=0,
            committed=request.commit,
            batches=0,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=("no symbols resolved",),
        )

    total_built = 0
    batch_count = 0
    idempotency = Counter(
        {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0}
    )
    date_distribution: Counter[str] = Counter()
    samples: list[InvestorFlowSnapshotSample] = []

    effective_batch_size = request.batch_size if request.all_symbols else len(symbols)
    effective_batch_size = max(1, effective_batch_size)
    collected_at = started_at
    for start in range(0, len(symbols), effective_batch_size):
        batch_count += 1
        batch = symbols[start : start + effective_batch_size]
        build_result = await build_investor_flow_snapshots(
            symbols=batch,
            days=request.days,
            today=today,
            collected_at=collected_at,
            concurrency=request.concurrency,
        )
        payloads = build_result.payloads
        warnings.extend(
            f"batch {batch_count}: {warning}" for warning in build_result.warnings
        )
        total_built += len(payloads)
        _merge_date_distribution(date_distribution, payloads)
        idempotency.update(await _classify_idempotency(payloads))
        remaining_sample_slots = max(0, 10 - len(samples))
        if remaining_sample_slots:
            samples.extend(_sample(p) for p in payloads[:remaining_sample_slots])
        if request.commit and payloads:
            await _commit_payloads(payloads)

    if total_built == 0:
        _enforce_zero_commit_floor(
            commit=request.commit,
            run_date=run_date_kst,
            symbols_resolved=len(symbols),
        )

    finished_at = dt.datetime.now(dt.UTC)
    return InvestorFlowSnapshotBuildResult(
        market="kr",
        symbols_resolved=len(symbols),
        snapshots_built=total_built,
        committed=request.commit,
        batches=batch_count,
        started_at=started_at,
        finished_at=finished_at,
        snapshot_date_distribution=dict(sorted(date_distribution.items())),
        idempotency=dict(idempotency),
        samples=tuple(samples),
        warnings=tuple(warnings),
    )
