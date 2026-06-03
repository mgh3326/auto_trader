"""Dry-run-first job runner for financial_fundamentals_snapshots (ROB-422 PR1, KR-only)."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots.builder import (
    FundamentalsFetcher,
    build_financial_fundamentals_for_symbols,
    default_dart_fetcher,
)
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
    FinancialFundamentalsUpsert,
)
from app.services.snapshot_commit_guard import PartialCommitBlocked


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotBuildRequest:
    market: str = "kr"
    symbols: tuple[str, ...] = ()
    limit: int | None = 20
    all_symbols: bool = False
    include_quarterly: bool = False
    concurrency: int = 4
    commit: bool = False
    collected_at: dt.datetime | None = None
    estimate_only: bool = False
    allow_partial: bool = False


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotSample:
    symbol: str
    fiscal_period: str
    period_type: str
    filing_date: dt.date | None
    revenue: Decimal | None
    net_income: Decimal | None
    payout_ratio: Decimal | None
    data_state: str


@dataclass(frozen=True)
class FinancialFundamentalsSnapshotBuildResult:
    market: str
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    started_at: dt.datetime
    finished_at: dt.datetime
    idempotency: dict[str, int] = field(default_factory=dict)
    samples: tuple[FinancialFundamentalsSnapshotSample, ...] = ()
    warnings: tuple[str, ...] = ()
    projected_requests: int | None = None


def _validate_market(market: str) -> str:
    market_norm = market.strip().lower()
    if market_norm != "kr":
        raise ValueError(f"PR1 supports market='kr' only, got: {market}")
    return market_norm


async def resolve_symbols(market: str, override: list[str], limit: int) -> list[str]:
    _validate_market(market)
    if override:
        return [s.strip().upper() for s in override if s.strip()]
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


def _payload_key(p: FinancialFundamentalsUpsert) -> tuple[str, str, str, str]:
    return (
        p.market.strip().lower(),
        p.symbol.strip().upper(),
        p.fiscal_period,
        p.source.strip().lower(),
    )


async def _classify_idempotency(
    payloads: list[FinancialFundamentalsUpsert],
) -> dict[str, int]:
    keys = [_payload_key(p) for p in payloads]
    duplicate = sum(c - 1 for c in Counter(keys).values() if c > 1)
    unique = set(keys)
    if not unique:
        return {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": duplicate}
    conditions = [
        sa.and_(
            FinancialFundamentalsSnapshot.market == m,
            FinancialFundamentalsSnapshot.symbol == s,
            FinancialFundamentalsSnapshot.fiscal_period == fp,
            FinancialFundamentalsSnapshot.source == src,
        )
        for m, s, fp, src in unique
    ]
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.select(
                FinancialFundamentalsSnapshot.market,
                FinancialFundamentalsSnapshot.symbol,
                FinancialFundamentalsSnapshot.fiscal_period,
                FinancialFundamentalsSnapshot.source,
            ).where(sa.or_(*conditions))
        )
        existing = set(result.all())
    return {
        "wouldInsert": len(unique) - len(existing),
        "wouldUpdate": len(existing),
        "duplicatePayloadKeys": duplicate,
    }


async def _commit_payloads(payloads: list[FinancialFundamentalsUpsert]) -> None:
    async with AsyncSessionLocal() as session:
        await FinancialFundamentalsSnapshotsRepository(session).upsert(payloads)
        await session.commit()


def _sample(p: FinancialFundamentalsUpsert) -> FinancialFundamentalsSnapshotSample:
    return FinancialFundamentalsSnapshotSample(
        symbol=p.symbol,
        fiscal_period=p.fiscal_period,
        period_type=p.period_type,
        filing_date=p.filing_date,
        revenue=p.revenue,
        net_income=p.net_income,
        payout_ratio=p.payout_ratio,
        data_state=p.data_state,
    )


async def run_financial_fundamentals_snapshot_build(
    request: FinancialFundamentalsSnapshotBuildRequest,
    *,
    fetcher: FundamentalsFetcher | None = None,
) -> FinancialFundamentalsSnapshotBuildResult:
    import logging

    logger = logging.getLogger(__name__)

    from app.core.config import settings
    from app.services.financial_fundamentals_snapshots.builder import (
        DartDailyRequestBudgetExceeded,
        reset_request_count,
    )

    market = _validate_market(request.market)
    started_at = dt.datetime.now(dt.UTC)
    collected_at = request.collected_at or started_at
    use_fetcher = fetcher or default_dart_fetcher
    symbols = await (
        resolve_active_universe(market)
        if request.all_symbols
        else resolve_symbols(market, list(request.symbols), request.limit or 20)
    )
    if not symbols:
        finished_at = dt.datetime.now(dt.UTC)
        return FinancialFundamentalsSnapshotBuildResult(
            market=market,
            symbols_resolved=0,
            snapshots_built=0,
            committed=request.commit,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=("no symbols resolved",),
        )

    projected = len(symbols) * (41 if request.include_quarterly else 11)
    logger.info(
        "Projected DART requests for %d symbols (include_quarterly=%s): %d (budget: %d)",
        len(symbols),
        request.include_quarterly,
        projected,
        settings.opendart_daily_request_budget,
    )

    if request.estimate_only:
        finished_at = dt.datetime.now(dt.UTC)
        return FinancialFundamentalsSnapshotBuildResult(
            market=market,
            symbols_resolved=len(symbols),
            snapshots_built=0,
            committed=False,
            started_at=started_at,
            finished_at=finished_at,
            idempotency={"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0},
            warnings=(
                f"estimate-only: projected {projected} DART requests; "
                "no fetch performed",
            ),
            projected_requests=projected,
        )

    if request.commit and not request.allow_partial:
        raise PartialCommitBlocked(
            "fundamentals commit blocked: fundamentals is an incremental "
            "backfill (DART budget); pass --allow-partial to commit a partial "
            "backfill",
            market=market,
            metric="symbols",
            reason="incremental_backfill",
        )

    reset_request_count()
    should_commit = request.commit
    try:
        build = await build_financial_fundamentals_for_symbols(
            market=market,
            symbols=symbols,
            collected_at=collected_at,
            fetcher=use_fetcher,
            include_quarterly=request.include_quarterly,
            concurrency=request.concurrency,
        )
        payloads = list(build.payloads)
        warnings = build.warnings
    except DartDailyRequestBudgetExceeded as exc:
        logger.warning("DART daily request budget exceeded during build: %s", exc)
        payloads = list(exc.payloads)
        warnings = exc.warnings
        should_commit = False

    idempotency = (
        await _classify_idempotency(payloads)
        if payloads
        else {"wouldInsert": 0, "wouldUpdate": 0, "duplicatePayloadKeys": 0}
    )
    if should_commit and payloads:
        await _commit_payloads(payloads)
    finished_at = dt.datetime.now(dt.UTC)
    return FinancialFundamentalsSnapshotBuildResult(
        market=market,
        symbols_resolved=len(symbols),
        snapshots_built=len(payloads),
        committed=should_commit,
        started_at=started_at,
        finished_at=finished_at,
        idempotency=idempotency,
        samples=tuple(_sample(p) for p in payloads[:10]),
        warnings=warnings,
    )
