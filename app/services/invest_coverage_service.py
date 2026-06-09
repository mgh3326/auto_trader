"""ROB-192 — read-only Toss-parity data coverage rollup for /invest.

The service only inspects local database/read-model tables. It intentionally does
not call broker/provider clients, start collectors, or infer buy/sell logic.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.manual_holdings import ManualHolding, MarketType
from app.models.market_events import MarketEventIngestionPartition
from app.models.news import NewsArticle, NewsArticleRelatedSymbol, NewsIngestionRun
from app.models.pending_order import PendingOrder
from app.models.research_reports import ResearchReport, ResearchReportIngestionRun
from app.models.us_symbol_universe import USSymbolUniverse
from app.schemas.invest_coverage import (
    CoverageActionability,
    CoverageApprovalGate,
    CoverageCandidateKind,
    CoverageCandidateReadiness,
    CoverageMarket,
    CoverageSourceCandidate,
    CoverageState,
    InvestCoverageCounts,
    InvestCoverageResponse,
    InvestCoverageSurface,
    InvestCoverageSymbol,
)
from app.services.invest_screener_snapshots.freshness import expected_baseline_date
from app.services.market_data_coverage.ohlcv_freshness import (
    kr_candles_freshness,
    us_candles_freshness,
)
from app.services.market_quote_snapshots.freshness import freshness_window_minutes
from app.services.market_quote_snapshots.repository import (
    MarketQuoteSnapshotsRepository,
)
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)

SUPPORTED_STATES: list[CoverageState] = [
    "fresh",
    "stale",
    "partial",
    "missing",
    "unsupported",
    "error",
    "provider_unwired",
]


async def build_invest_coverage(
    db: AsyncSession,
    *,
    market: CoverageMarket = "kr",
    symbols: list[str] | None = None,
    as_of: dt.date | None = None,
) -> InvestCoverageResponse:
    market_norm = market.lower()
    if market_norm not in {"kr", "us", "crypto", "all"}:
        raise ValueError("market must be one of kr, us, crypto, all")
    symbol_list = _normalize_symbols(symbols or [])
    # ROB-438 follow-up: default to the session-aware baseline (prior trading day
    # in the pre-market window) so coverage surfaces don't false-flag fresh
    # prior-day snapshots as stale; explicit as_of still overrides.
    trading_day = as_of or expected_baseline_date(
        "us" if market_norm == "all" else market_norm
    )
    now = dt.datetime.now(dt.UTC)

    surfaces: list[InvestCoverageSurface] = []
    surfaces.extend(await _symbol_universe_surfaces(db, market_norm))
    surfaces.extend(await _screener_surfaces(db, market_norm, trading_day))
    surfaces.extend(await _news_surfaces(db, market_norm, now, symbols=symbol_list))
    surfaces.extend(await _calendar_surfaces(db, market_norm, trading_day, now))
    surfaces.extend(await _research_report_surfaces(db, market_norm, now))
    surfaces.extend(
        await _investor_flow_surfaces(db, market_norm, trading_day, symbols=symbol_list)
    )
    surfaces.extend(await _holdings_surfaces(db, market_norm, now))
    surfaces.extend(await _pending_order_surfaces(db, market_norm, now))
    surfaces.extend(await _orderbook_nxt_surfaces(db, market_norm))
    surfaces.extend(await _ohlcv_surfaces(db, market_norm, trading_day))
    surfaces.extend(await _quote_surfaces(db, market_norm, now))
    surfaces.extend(await _valuation_surfaces(db, market_norm, trading_day))
    surfaces.extend(_provider_unwired_surfaces(market_norm))
    for surface in surfaces:
        surface.actionability = _actionability_for_surface(
            surface=surface.surface,
            state=surface.state,
            market=surface.market,
            source_of_truth=surface.sourceOfTruth,
        )

    symbol_rows = await _symbol_rows(db, market_norm, symbol_list, trading_day)
    gaps = [
        f"{surface.surface}: {', '.join(surface.warnings)}"
        for surface in surfaces
        if surface.state
        in {"missing", "partial", "stale", "provider_unwired", "unsupported", "error"}
        and surface.warnings
    ]

    return InvestCoverageResponse(
        market=market_norm,  # type: ignore[arg-type]
        asOf=now,
        tradingDate=trading_day,
        states=SUPPORTED_STATES,
        surfaces=surfaces,
        symbols=symbol_rows,
        gaps=gaps,
        notes=[
            "Read-only coverage report; auto_trader DB/read models are source of truth.",
            "Toss is used only as a parity benchmark/reference, not as a data source.",
            "Naver appears only as source-candidate or reference; discussion-signal and stock-detail PoCs remain fixture-backed under the aggregate-only contract.",
        ],
    )


def _normalize_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        s = symbol.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _state_from_counts(
    *, fresh: int, stale: int = 0, expected: int | None = None, total: int | None = None
) -> CoverageState:
    observed = fresh + stale if total is None else total
    if observed == 0:
        return "missing"
    if expected is not None and expected > 0 and fresh == 0 and stale == 0:
        return "missing"
    if expected is not None and expected > 0 and observed < expected:
        return "partial" if fresh > 0 else "stale"
    if stale > 0 and fresh > 0:
        return "partial"
    if stale > 0:
        return "stale"
    return "fresh"


_SURFACE_QUEUE: dict[str, str] = {
    "symbol_universe": "invest-data-read-models",
    "screener_snapshots": "invest-screener-snapshots",
    "news_feed": "news-ingestor",
    "calendar_events": "market-events-ingestion",
    "research_reports": "research-report-ingestion",
    "investor_flow": "investor-flow-ingestion",
    "holdings": "account-panel-read-model",
    "pending_orders": "order-reconciliation-read-model",
    "orderbook_nxt_capability": "kr-symbol-universe",
    "quotes": "market-quote-snapshots",
    "ohlcv": "market-candle-snapshots",
    "valuation_fundamentals": "market-valuation-snapshots",
}


_SCHEDULER_QUEUES = {
    "invest-screener-snapshots",
    "news-ingestor",
    "market-events-ingestion",
    "research-report-ingestion",
    "investor-flow-ingestion",
}


def _actionability_for_surface(
    *,
    surface: str,
    state: CoverageState,
    market: str | None,
    source_of_truth: str,
) -> CoverageActionability:
    """Return advisory remediation metadata without executing remediation."""

    queue = _SURFACE_QUEUE.get(surface, "invest-data-read-models")
    market_label = market or "all"
    if state == "fresh":
        return CoverageActionability(
            priority="none",
            action="monitor",
            queue="none",
            reason=f"{surface} coverage for {market_label} is fresh.",
        )
    if state == "unsupported":
        return CoverageActionability(
            priority="none",
            action="unsupported_no_action",
            queue="none",
            reason=f"{surface} is intentionally unsupported for {market_label} in the current /invest contract.",
        )
    if state == "provider_unwired":
        return CoverageActionability(
            priority="blocked",
            action="provider_contract_needed",
            queue="provider-contract",
            approvalGates=["code_review"],
            reason=f"{surface} has no durable read-model/provider contract wired yet; sourceOfTruth={source_of_truth}.",
        )
    if state == "error":
        return CoverageActionability(
            priority="high",
            action="investigate",
            queue=queue,
            approvalGates=["code_review"],
            reason=f"{surface} coverage needs investigation before remediation.",
        )

    gates: list[CoverageApprovalGate] = ["production_db_write_approval"]
    if queue in _SCHEDULER_QUEUES:
        gates.append("scheduler_activation_approval")
    if state == "missing":
        action = (
            "backfill_candidate"
            if queue != "provider-contract"
            else "provider_contract_needed"
        )
        priority = "high"
    else:
        action = "repair_read_model"
        priority = "medium"
    return CoverageActionability(
        priority=priority,
        action=action,
        queue=queue,
        approvalGates=gates,
        reason=f"{surface} is {state} for {market_label}; remediation is advisory and requires approval before writes or scheduler changes.",
    )


def _actionability_for_symbol(
    surfaces: dict[str, CoverageState],
) -> CoverageActionability:
    actionable_states = {
        name: state
        for name, state in surfaces.items()
        if state not in {"fresh", "unsupported"}
    }
    if not actionable_states:
        if surfaces and all(state == "unsupported" for state in surfaces.values()):
            return CoverageActionability(
                priority="none",
                action="unsupported_no_action",
                queue="none",
                reason="No symbol-level remediation is available for unsupported surfaces.",
            )
        return CoverageActionability(
            priority="none",
            action="monitor",
            queue="none",
            reason="Symbol-level diagnostics are fresh or intentionally unsupported.",
        )
    if "provider_unwired" in actionable_states.values():
        return CoverageActionability(
            priority="blocked",
            action="provider_contract_needed",
            queue="provider-contract",
            approvalGates=["code_review"],
            reason="One or more symbol diagnostics need a provider/read-model contract before remediation.",
        )
    if "missing" in actionable_states.values():
        return CoverageActionability(
            priority="high",
            action="backfill_candidate",
            queue="invest-data-read-models",
            approvalGates=[
                "production_db_write_approval",
                "scheduler_activation_approval",
            ],
            reason="One or more symbol diagnostics are missing; this is a candidate only, not an execution trigger.",
        )
    return CoverageActionability(
        priority="medium",
        action="repair_read_model",
        queue="invest-data-read-models",
        approvalGates=["production_db_write_approval"],
        reason="One or more symbol diagnostics are stale/partial; remediation needs separate approval.",
    )


async def _naver_finance_investor_flow_candidate(
    db: AsyncSession, trading_day: dt.date
) -> CoverageSourceCandidate:
    """Build Naver candidate coverage from local investor_flow_snapshots only."""

    row = (
        await db.execute(
            sa.select(
                sa.func.count()
                .filter(InvestorFlowSnapshot.snapshot_date >= trading_day)
                .label("fresh"),
                sa.func.count()
                .filter(InvestorFlowSnapshot.snapshot_date < trading_day)
                .label("stale"),
                sa.func.max(InvestorFlowSnapshot.collected_at).label("latest_at"),
                sa.func.max(InvestorFlowSnapshot.snapshot_date).label("latest_date"),
            ).where(
                InvestorFlowSnapshot.market == "kr",
                InvestorFlowSnapshot.source == "naver_finance",
            )
        )
    ).one()
    fresh = int(row.fresh or 0)
    stale = int(row.stale or 0)
    readiness: CoverageCandidateReadiness = "live" if fresh + stale > 0 else "not_wired"
    return CoverageSourceCandidate(
        name="naver_finance",
        surface="investor_flow",
        kind="secondary_source",
        readiness=readiness,
        latestAt=row.latest_at,
        latestDate=row.latest_date,
        counts=InvestCoverageCounts(fresh=fresh, stale=stale, total=fresh + stale),
        notes=[
            "naver_finance is one of several wired investor-flow sources; investor_flow_snapshots remains the source of truth."
        ],
    )


async def _naver_news_candidate(
    db: AsyncSession, market: str, now: dt.datetime
) -> CoverageSourceCandidate:
    """Build Naver news candidate coverage from local news_articles only."""

    stale_cutoff = now.replace(tzinfo=None) - dt.timedelta(hours=24)
    row = (
        await db.execute(
            sa.select(
                sa.func.count()
                .filter(NewsArticle.article_published_at >= stale_cutoff)
                .label("fresh"),
                sa.func.count()
                .filter(NewsArticle.article_published_at < stale_cutoff)
                .label("stale"),
                sa.func.max(NewsArticle.article_published_at).label("latest_at"),
            ).where(
                NewsArticle.market == market,
                NewsArticle.source.ilike("naver%"),
            )
        )
    ).one()
    fresh = int(row.fresh or 0)
    stale = int(row.stale or 0)
    wired = fresh + stale > 0
    readiness: CoverageCandidateReadiness = "live" if wired else "not_wired"
    return CoverageSourceCandidate(
        name="naver_finance_news",
        surface="news_feed",
        kind="secondary_source" if wired else "candidate",
        readiness=readiness,
        latestAt=row.latest_at,
        counts=InvestCoverageCounts(fresh=fresh, stale=stale, total=fresh + stale),
        notes=[
            "naver_finance is one of several news sources writing to news_articles."
            if wired
            else "No Naver-sourced news articles in the local read-model."
        ],
    )


def _naver_static_candidate(
    *,
    name: str,
    surface: str,
    kind: CoverageCandidateKind,
    readiness: CoverageCandidateReadiness,
    note: str,
) -> CoverageSourceCandidate:
    return CoverageSourceCandidate(
        name=name,
        surface=surface,
        kind=kind,
        readiness=readiness,
        notes=[note],
    )


async def _universe_count(db: AsyncSession, market: str) -> int | None:
    if market == "kr":
        stmt = (
            sa.select(sa.func.count())
            .select_from(KRSymbolUniverse)
            .where(KRSymbolUniverse.is_active.is_(True))
        )
    elif market == "us":
        stmt = (
            sa.select(sa.func.count())
            .select_from(USSymbolUniverse)
            .where(USSymbolUniverse.is_active.is_(True))
        )
    else:
        return None
    return int((await db.execute(stmt)).scalar() or 0)


async def _symbol_universe_surfaces(
    db: AsyncSession, market: str
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    for m in markets:
        if m == "crypto":
            rows.append(
                InvestCoverageSurface(
                    surface="symbol_universe",
                    label="Symbol universe",
                    market=m,
                    state="provider_unwired",
                    sourceOfTruth="upbit_symbol_universe/read_model_gap",
                    warnings=[
                        "Crypto universe exists separately and is not wired into /invest parity coverage yet."
                    ],
                )
            )
            continue
        expected = await _universe_count(db, m) or 0
        rows.append(
            InvestCoverageSurface(
                surface="symbol_universe",
                label="Symbol universe",
                market=m,
                state="fresh" if expected > 0 else "missing",
                sourceOfTruth=f"{m}_symbol_universe",
                counts=InvestCoverageCounts(
                    expected=expected, fresh=expected, total=expected
                ),
                warnings=[]
                if expected > 0
                else ["No active symbols found in local universe table."],
            )
        )
    return rows


async def _screener_surfaces(
    db: AsyncSession, market: str, trading_day: dt.date
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    for m in markets:
        if m == "crypto":
            rows.append(
                InvestCoverageSurface(
                    surface="screener_snapshots",
                    label="Screener snapshots",
                    market=m,
                    state="unsupported",
                    sourceOfTruth="invest_screener_snapshots",
                    warnings=[
                        "Screener snapshot read model currently supports KR/US equities only."
                    ],
                )
            )
            continue
        expected = await _universe_count(db, m)
        row = (
            await db.execute(
                sa.select(
                    sa.func.count()
                    .filter(InvestScreenerSnapshot.snapshot_date == trading_day)
                    .label("fresh"),
                    sa.func.count()
                    .filter(InvestScreenerSnapshot.snapshot_date < trading_day)
                    .label("stale"),
                    sa.func.max(InvestScreenerSnapshot.computed_at).label("latest_at"),
                    sa.func.max(InvestScreenerSnapshot.snapshot_date).label(
                        "latest_date"
                    ),
                ).where(InvestScreenerSnapshot.market == m)
            )
        ).one()
        fresh = int(row.fresh or 0)
        stale = int(row.stale or 0)
        missing = max(0, (expected or 0) - fresh - stale) if expected is not None else 0
        surface_row = InvestCoverageSurface(
            surface="screener_snapshots",
            label="Screener snapshots",
            market=m,
            state=_state_from_counts(fresh=fresh, stale=stale, expected=expected),
            sourceOfTruth="invest_screener_snapshots",
            latestAt=row.latest_at,
            latestDate=row.latest_date,
            counts=InvestCoverageCounts(
                expected=expected,
                fresh=fresh,
                stale=stale,
                missing=missing,
                total=fresh + stale,
            ),
            staleAfterHours=36,
            warnings=[]
            if fresh
            else ["No screener snapshots cover the selected trading date."],
        )
        if m == "kr":
            surface_row.sourceCandidates.append(
                _naver_static_candidate(
                    name="naver_finance",
                    surface="screener_snapshots",
                    kind="candidate",
                    readiness="request_time_only",
                    note="naver_finance valuation calls are request-time only; not persisted to invest_screener_snapshots.",
                )
            )
        rows.append(surface_row)
    return rows


async def _news_surfaces(
    db: AsyncSession,
    market: str,
    now: dt.datetime,
    *,
    symbols: list[str] | None = None,
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    # news_articles.article_published_at is a timestamp without timezone.
    stale_cutoff = now.replace(tzinfo=None) - dt.timedelta(hours=24)
    scoped_symbols = sorted(
        {symbol.strip().upper() for symbol in (symbols or []) if symbol.strip()}
    )
    for m in markets:
        run = (
            await db.execute(
                sa.select(NewsIngestionRun)
                .where(NewsIngestionRun.market == m)
                .order_by(
                    NewsIngestionRun.finished_at.desc().nullslast(),
                    NewsIngestionRun.created_at.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        article_query = sa.select(
            sa.func.count(sa.distinct(NewsArticle.id))
            .filter(NewsArticle.article_published_at >= stale_cutoff)
            .label("fresh"),
            sa.func.count(sa.distinct(NewsArticle.id))
            .filter(NewsArticle.article_published_at < stale_cutoff)
            .label("stale"),
            sa.func.max(NewsArticle.article_published_at).label("latest_at"),
        ).where(NewsArticle.market == m)
        if scoped_symbols:
            article_query = article_query.join(
                NewsArticleRelatedSymbol,
                NewsArticleRelatedSymbol.article_id == NewsArticle.id,
            ).where(
                NewsArticleRelatedSymbol.market == m,
                NewsArticleRelatedSymbol.symbol.in_(scoped_symbols),
            )
        article_row = (await db.execute(article_query)).one()
        fresh = int(article_row.fresh or 0)
        stale = int(article_row.stale or 0)
        state = _state_from_counts(fresh=fresh, stale=stale)
        warnings: list[str] = []
        if run is None:
            warnings.append("No local news ingestion run found.")
        elif run.status not in {"success", "dry_run_ok"}:
            state = "partial" if fresh else "error"
            warnings.append(f"Latest news ingestion status is {run.status}.")
        if not fresh:
            warnings.append("No articles published in the last 24 hours.")
        surface_row = InvestCoverageSurface(
            surface="news_feed",
            label="News feed",
            market=m,
            state=state,
            sourceOfTruth="news_articles/news_ingestion_runs",
            latestAt=article_row.latest_at or (run.finished_at if run else None),
            counts=InvestCoverageCounts(fresh=fresh, stale=stale, total=fresh + stale),
            staleAfterHours=24,
            warnings=warnings,
        )
        if m == "kr":
            surface_row.sourceCandidates.append(await _naver_news_candidate(db, m, now))
        rows.append(surface_row)
    return rows


async def _calendar_surfaces(
    db: AsyncSession, market: str, trading_day: dt.date, now: dt.datetime
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto", "global"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    stale_cutoff = now - dt.timedelta(hours=36)
    for m in markets:
        row = (
            await db.execute(
                sa.select(
                    sa.func.count()
                    .filter(MarketEventIngestionPartition.partition_date >= trading_day)
                    .label("fresh"),
                    sa.func.count()
                    .filter(MarketEventIngestionPartition.partition_date < trading_day)
                    .label("stale"),
                    sa.func.max(MarketEventIngestionPartition.finished_at).label(
                        "latest_at"
                    ),
                    sa.func.max(MarketEventIngestionPartition.partition_date).label(
                        "latest_date"
                    ),
                    sa.func.count()
                    .filter(
                        MarketEventIngestionPartition.status.notin_(
                            ["success", "empty"]
                        )
                    )
                    .label("partial"),
                ).where(MarketEventIngestionPartition.market == m)
            )
        ).one()
        fresh = int(row.fresh or 0)
        stale = int(row.stale or 0)
        partial = int(row.partial or 0)
        latest_at = row.latest_at
        state = _state_from_counts(fresh=fresh, stale=stale)
        warnings: list[str] = []
        if partial:
            state = "partial" if fresh else "error"
            warnings.append("One or more calendar partitions are not success/empty.")
        if latest_at and latest_at < stale_cutoff:
            state = "stale" if state == "fresh" else state
            warnings.append(
                "Latest calendar partition finished more than 36 hours ago."
            )
        if not fresh:
            warnings.append("No calendar partitions cover the selected trading date.")
        rows.append(
            InvestCoverageSurface(
                surface="calendar_events",
                label="Calendar events",
                market=m,
                state=state,
                sourceOfTruth="market_event_ingestion_partitions/market_events",
                latestAt=latest_at,
                latestDate=row.latest_date,
                counts=InvestCoverageCounts(
                    fresh=fresh, stale=stale, partial=partial, total=fresh + stale
                ),
                staleAfterHours=36,
                warnings=warnings,
            )
        )
    return rows


async def _research_report_surfaces(
    db: AsyncSession, market: str, now: dt.datetime
) -> list[InvestCoverageSurface]:
    if market == "crypto":
        return [
            InvestCoverageSurface(
                surface="research_reports",
                label="Research reports",
                market=market,
                state="unsupported",
                sourceOfTruth="research_reports",
                warnings=[
                    "Broker research reports are equity-only in the local read model."
                ],
            )
        ]

    # Research report metadata is stored as a shared compact feed without a
    # market column. Report one global row for all equity market selections.
    # Research report timestamps are timezone-aware in the metadata read model.
    cutoff = now - dt.timedelta(days=7)
    latest_run = (
        await db.execute(
            sa.select(ResearchReportIngestionRun)
            .order_by(ResearchReportIngestionRun.finished_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()
    report_row = (
        await db.execute(
            sa.select(
                sa.func.count()
                .filter(ResearchReport.published_at >= cutoff)
                .label("fresh"),
                sa.func.count()
                .filter(ResearchReport.published_at < cutoff)
                .label("stale"),
                sa.func.max(ResearchReport.published_at).label("latest_at"),
            )
        )
    ).one()
    fresh = int(report_row.fresh or 0)
    stale = int(report_row.stale or 0)
    warnings = (
        [] if fresh else ["No research report metadata published in the last 7 days."]
    )
    if latest_run is None:
        warnings.append("No research report ingestion run found.")
    surface = InvestCoverageSurface(
        surface="research_reports",
        label="Research reports",
        market="equity" if market == "all" else market,
        state=_state_from_counts(fresh=fresh, stale=stale),
        sourceOfTruth="research_reports/research_report_ingestion_runs",
        latestAt=report_row.latest_at
        or (latest_run.finished_at if latest_run else None),
        counts=InvestCoverageCounts(fresh=fresh, stale=stale, total=fresh + stale),
        staleAfterHours=168,
        warnings=warnings,
        notes=[
            "Only compact metadata is used; full report bodies are intentionally excluded."
        ],
    )
    surface.sourceCandidates.append(
        _naver_static_candidate(
            name="naver_research",
            surface="research_reports",
            kind="candidate",
            readiness="fixture_backed_poc",
            note="naver_stock_detail_poc exposes Naver research metadata via fixtures; not ingested.",
        )
    )
    return [surface]


async def _investor_flow_surfaces(
    db: AsyncSession,
    market: str,
    trading_day: dt.date,
    *,
    symbols: list[str] | None = None,
) -> list[InvestCoverageSurface]:
    if market in {"us", "crypto"}:
        return [
            InvestCoverageSurface(
                surface="investor_flow",
                label="Investor flow",
                market=market,
                state="unsupported",
                sourceOfTruth="investor_flow_snapshots",
                warnings=["Investor-flow snapshots currently cover KR equities only."],
            )
        ]
    markets = ["kr"] if market in {"kr", "all"} else []
    rows: list[InvestCoverageSurface] = []
    scoped_symbols = sorted(
        {symbol.strip().upper() for symbol in (symbols or []) if symbol.strip()}
    )
    expected = (
        len(scoped_symbols) if scoped_symbols else await _universe_count(db, "kr")
    )
    for m in markets:
        predicates = [InvestorFlowSnapshot.market == m]
        if scoped_symbols:
            predicates.append(InvestorFlowSnapshot.symbol.in_(scoped_symbols))
        row = (
            await db.execute(
                sa.select(
                    sa.func.count()
                    .filter(InvestorFlowSnapshot.snapshot_date >= trading_day)
                    .label("fresh"),
                    sa.func.count()
                    .filter(InvestorFlowSnapshot.snapshot_date < trading_day)
                    .label("stale"),
                    sa.func.max(InvestorFlowSnapshot.collected_at).label("latest_at"),
                    sa.func.max(InvestorFlowSnapshot.snapshot_date).label(
                        "latest_date"
                    ),
                ).where(*predicates)
            )
        ).one()
        fresh = int(row.fresh or 0)
        stale = int(row.stale or 0)
        missing = max(0, (expected or 0) - fresh - stale) if expected is not None else 0
        surface = InvestCoverageSurface(
            surface="investor_flow",
            label="Investor flow",
            market=m,
            state=_state_from_counts(fresh=fresh, stale=stale, expected=expected),
            sourceOfTruth="investor_flow_snapshots",
            latestAt=row.latest_at,
            latestDate=row.latest_date,
            counts=InvestCoverageCounts(
                expected=expected,
                fresh=fresh,
                stale=stale,
                missing=missing,
                total=fresh + stale,
            ),
            staleAfterHours=36,
            warnings=[]
            if fresh
            else ["No investor-flow snapshots cover the selected trading date."],
        )
        if m == "kr":
            surface.sourceCandidates.append(
                await _naver_finance_investor_flow_candidate(db, trading_day)
            )
        rows.append(surface)
    return rows


async def _holdings_surfaces(
    db: AsyncSession, market: str, now: dt.datetime
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto"] if market == "all" else [market]
    market_type = {
        "kr": MarketType.KR,
        "us": MarketType.US,
        "crypto": MarketType.CRYPTO,
    }
    stale_cutoff = now - dt.timedelta(days=1)
    rows: list[InvestCoverageSurface] = []
    for m in markets:
        row = (
            await db.execute(
                sa.select(
                    sa.func.count()
                    .filter(ManualHolding.updated_at >= stale_cutoff)
                    .label("fresh"),
                    sa.func.count()
                    .filter(ManualHolding.updated_at < stale_cutoff)
                    .label("stale"),
                    sa.func.max(ManualHolding.updated_at).label("latest_at"),
                ).where(ManualHolding.market_type == market_type[m])
            )
        ).one()
        fresh = int(row.fresh or 0)
        stale = int(row.stale or 0)
        rows.append(
            InvestCoverageSurface(
                surface="holdings",
                label="Holdings",
                market=m,
                state="fresh"
                if fresh + stale == 0
                else _state_from_counts(fresh=fresh, stale=stale),
                sourceOfTruth="manual_holdings/account_panel_home_read_model",
                latestAt=row.latest_at,
                counts=InvestCoverageCounts(
                    fresh=fresh, stale=stale, total=fresh + stale
                ),
                staleAfterHours=24,
                notes=[
                    "Empty is OK: the selected market may simply have no locally tracked holdings."
                ],
                warnings=[]
                if stale == 0
                else ["Some holdings rows have not been updated in the last 24 hours."],
            )
        )
    return rows


async def _pending_order_surfaces(
    db: AsyncSession, market: str, now: dt.datetime
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto"] if market == "all" else [market]
    stale_cutoff = now - dt.timedelta(minutes=30)
    rows: list[InvestCoverageSurface] = []
    for m in markets:
        db_markets = [m]
        if m == "kr":
            db_markets = ["kr", "equity_kr"]
        elif m == "us":
            db_markets = ["us", "equity_us"]
        row = (
            await db.execute(
                sa.select(
                    sa.func.count()
                    .filter(PendingOrder.last_seen_at >= stale_cutoff)
                    .label("fresh"),
                    sa.func.count()
                    .filter(PendingOrder.last_seen_at < stale_cutoff)
                    .label("stale"),
                    sa.func.max(PendingOrder.last_seen_at).label("latest_at"),
                ).where(PendingOrder.market.in_(db_markets))
            )
        ).one()
        fresh = int(row.fresh or 0)
        stale = int(row.stale or 0)
        rows.append(
            InvestCoverageSurface(
                surface="pending_orders",
                label="Pending orders",
                market=m,
                state="fresh"
                if fresh + stale == 0
                else _state_from_counts(fresh=fresh, stale=stale),
                sourceOfTruth="pending_orders",
                latestAt=row.latest_at,
                counts=InvestCoverageCounts(
                    fresh=fresh, stale=stale, total=fresh + stale
                ),
                staleAfterHours=1,
                notes=[
                    "Empty is OK for read-only order coverage: there may simply be no open orders."
                ],
                warnings=[]
                if stale == 0
                else [
                    "Some pending-order rows have not been seen in the last 30 minutes."
                ],
            )
        )
    return rows


async def _orderbook_nxt_surfaces(
    db: AsyncSession, market: str
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    for m in markets:
        if m == "kr":
            row = (
                await db.execute(
                    sa.select(
                        sa.func.count()
                        .filter(KRSymbolUniverse.is_active.is_(True))
                        .label("expected"),
                        sa.func.count()
                        .filter(
                            KRSymbolUniverse.is_active.is_(True),
                            KRSymbolUniverse.nxt_eligible.is_(True),
                        )
                        .label("fresh"),
                    )
                )
            ).one()
            expected = int(row.expected or 0)
            nxt_eligible = int(row.fresh or 0)
            rows.append(
                InvestCoverageSurface(
                    surface="orderbook_nxt_capability",
                    label="Orderbook / NXT capability",
                    market=m,
                    state="fresh"
                    if nxt_eligible > 0
                    else ("missing" if expected > 0 else "missing"),
                    sourceOfTruth="kr_symbol_universe.nxt_eligible/get_orderbook_capability",
                    counts=InvestCoverageCounts(
                        expected=expected,
                        fresh=nxt_eligible,
                        missing=max(0, expected - nxt_eligible),
                        total=nxt_eligible,
                    ),
                    warnings=[]
                    if nxt_eligible > 0
                    else [
                        "No active KR symbols are marked NXT-eligible in the local universe."
                    ],
                    notes=[
                        "KR orderbook capability exists provider-side; this row reports local NXT eligibility coverage, not a quote/order recommendation."
                    ],
                )
            )
        elif m == "crypto":
            rows.append(
                InvestCoverageSurface(
                    surface="orderbook_nxt_capability",
                    label="Orderbook / NXT capability",
                    market=m,
                    state="provider_unwired",
                    sourceOfTruth="get_orderbook_provider/read_model_gap",
                    warnings=[
                        "KRW crypto orderbook is provider-backed, but no durable local orderbook coverage read model is wired yet."
                    ],
                    notes=["NXT is not applicable to crypto."],
                )
            )
        else:
            rows.append(
                InvestCoverageSurface(
                    surface="orderbook_nxt_capability",
                    label="Orderbook / NXT capability",
                    market=m,
                    state="unsupported",
                    sourceOfTruth="get_orderbook_provider/read_model_gap",
                    warnings=[
                        "US orderbook/NXT capability is not supported by the current /invest provider/read-model contract."
                    ],
                )
            )
    return rows


async def _ohlcv_surfaces(
    db: AsyncSession, market: str, trading_day: dt.date
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    for m in markets:
        if m == "kr":
            state, latest_at, latest_date, fresh, stale = await kr_candles_freshness(
                db, trading_day=trading_day
            )
            rows.append(
                InvestCoverageSurface(
                    surface="ohlcv",
                    label="OHLCV candles",
                    market="kr",
                    state=state,
                    sourceOfTruth="kr_candles_1m",
                    latestAt=latest_at,
                    latestDate=latest_date,
                    counts=InvestCoverageCounts(
                        fresh=fresh, stale=stale, total=fresh + stale
                    ),
                    warnings=[]
                    if state == "fresh"
                    else [
                        "KR 1m candle read model is not fresh for the requested trading day."
                    ],
                )
            )
        elif m == "us":
            state, latest_at, latest_date, fresh, stale = await us_candles_freshness(
                db, trading_day=trading_day
            )
            rows.append(
                InvestCoverageSurface(
                    surface="ohlcv",
                    label="OHLCV candles",
                    market="us",
                    state=state,
                    sourceOfTruth="us_candles_1m",
                    latestAt=latest_at,
                    latestDate=latest_date,
                    counts=InvestCoverageCounts(
                        fresh=fresh, stale=stale, total=fresh + stale
                    ),
                    warnings=[]
                    if state == "fresh"
                    else [
                        "US 1m candle read model is not fresh for the requested trading day."
                    ],
                )
            )
        else:
            rows.append(
                InvestCoverageSurface(
                    surface="ohlcv",
                    label="OHLCV candles",
                    market=m,
                    state="unsupported",
                    sourceOfTruth="market_candles_read_model",
                    warnings=[
                        "Crypto OHLCV coverage is not in the current /invest durable read-model contract."
                    ],
                )
            )
    return rows


async def _quote_surfaces(
    db: AsyncSession, market: str, now: dt.datetime
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    repo = MarketQuoteSnapshotsRepository(db)
    for m in markets:
        cutoff = now - dt.timedelta(minutes=freshness_window_minutes(m))
        counts = await repo.coverage_counts(m, fresh_after=cutoff)
        state = _state_from_counts(
            fresh=counts.fresh_symbols,
            stale=counts.stale_symbols,
            total=counts.total_symbols,
        )
        row = InvestCoverageSurface(
            surface="quotes",
            label="Quotes",
            market=m,
            state=state,
            sourceOfTruth="market_quote_snapshots",
            latestAt=counts.latest_snapshot_at,
            counts=InvestCoverageCounts(
                fresh=counts.fresh_symbols,
                stale=counts.stale_symbols,
                total=counts.total_symbols,
            ),
            warnings=[]
            if state == "fresh"
            else [
                "Quote snapshot read model is stale or missing for one or more symbols."
            ],
        )
        if m == "kr":
            row.sourceCandidates.append(
                _naver_static_candidate(
                    name="naver_finance",
                    surface="quotes",
                    kind="candidate",
                    readiness="request_time_only",
                    note="naver_finance domestic quote endpoints are available request-time only; durable /invest coverage uses market_quote_snapshots.",
                )
            )
        rows.append(row)
    return rows


async def _valuation_surfaces(
    db: AsyncSession, market: str, trading_day: dt.date
) -> list[InvestCoverageSurface]:
    markets = ["kr", "us", "crypto"] if market == "all" else [market]
    rows: list[InvestCoverageSurface] = []
    repo = MarketValuationSnapshotsRepository(db)
    for m in markets:
        if m not in {"kr", "us"}:
            rows.append(
                InvestCoverageSurface(
                    surface="valuation_fundamentals",
                    label="Valuation/fundamentals",
                    market=m,
                    state="unsupported",
                    sourceOfTruth="market_valuation_snapshots",
                    warnings=[
                        "Crypto valuation/fundamentals are not in the current /invest durable read-model contract."
                    ],
                )
            )
            continue
        counts = await repo.coverage_counts(m, fresh_date=trading_day)
        state = _state_from_counts(
            fresh=counts.fresh_symbols,
            stale=counts.stale_symbols,
            total=counts.total_symbols,
        )
        row = InvestCoverageSurface(
            surface="valuation_fundamentals",
            label="Valuation/fundamentals",
            market=m,
            state=state,
            sourceOfTruth="market_valuation_snapshots",
            latestAt=counts.latest_at,
            latestDate=counts.latest_date,
            counts=InvestCoverageCounts(
                fresh=counts.fresh_symbols,
                stale=counts.stale_symbols,
                total=counts.total_symbols,
            ),
            warnings=[]
            if state == "fresh"
            else [
                "Valuation snapshot read model is stale or missing for one or more symbols."
            ],
        )
        if m == "kr":
            row.sourceCandidates.append(
                _naver_static_candidate(
                    name="naver_finance",
                    surface="valuation_fundamentals",
                    kind="candidate",
                    readiness="request_time_only",
                    note="naver_finance financial/profile endpoints are request-time only; durable /invest coverage uses market_valuation_snapshots.",
                )
            )
        rows.append(row)
    return rows


def _provider_unwired_surfaces(market: str) -> list[InvestCoverageSurface]:
    # ROB-206 wires durable OHLCV, quote, and valuation coverage surfaces for
    # KR/US via read-model freshness checks. Keep this hook for future gaps but
    # do not emit stale provider_unwired placeholders for those surfaces.
    return []


async def _symbol_rows(
    db: AsyncSession,
    market: str,
    symbols: list[str],
    trading_day: dt.date,
) -> list[InvestCoverageSymbol]:
    if not symbols:
        return []
    if market == "all":
        return await _symbol_rows_all_markets(db, symbols, trading_day)
    if market not in {"kr", "us"}:
        return [_unsupported_symbol_row(symbol, market) for symbol in symbols]
    return await _symbol_rows_for_market(db, market, symbols, trading_day)


async def _symbol_rows_for_market(
    db: AsyncSession,
    market: str,
    symbols: list[str],
    trading_day: dt.date,
) -> list[InvestCoverageSymbol]:
    screener_rows = (
        await db.execute(
            sa.select(
                InvestScreenerSnapshot.symbol,
                sa.func.max(InvestScreenerSnapshot.snapshot_date),
            )
            .where(
                InvestScreenerSnapshot.market == market,
                InvestScreenerSnapshot.symbol.in_(symbols),
            )
            .group_by(InvestScreenerSnapshot.symbol)
        )
    ).all()
    screener_dates = dict(screener_rows)

    investor_dates: dict[str, dt.date | None] = {}
    naver_flow_dates: dict[str, dt.date | None] = {}
    if market == "kr":
        flow_rows = (
            await db.execute(
                sa.select(
                    InvestorFlowSnapshot.symbol,
                    sa.func.max(InvestorFlowSnapshot.snapshot_date),
                )
                .where(
                    InvestorFlowSnapshot.market == "kr",
                    InvestorFlowSnapshot.symbol.in_(symbols),
                )
                .group_by(InvestorFlowSnapshot.symbol)
            )
        ).all()
        investor_dates = dict(flow_rows)
        naver_rows = (
            await db.execute(
                sa.select(
                    InvestorFlowSnapshot.symbol,
                    sa.func.max(InvestorFlowSnapshot.snapshot_date),
                )
                .where(
                    InvestorFlowSnapshot.market == "kr",
                    InvestorFlowSnapshot.source == "naver_finance",
                    InvestorFlowSnapshot.symbol.in_(symbols),
                )
                .group_by(InvestorFlowSnapshot.symbol)
            )
        ).all()
        naver_flow_dates = dict(naver_rows)

    news_rows = (
        await db.execute(
            sa.select(
                NewsArticleRelatedSymbol.symbol,
                sa.func.max(NewsArticle.article_published_at),
            )
            .join(NewsArticle, NewsArticle.id == NewsArticleRelatedSymbol.article_id)
            .where(
                NewsArticleRelatedSymbol.market == market,
                NewsArticleRelatedSymbol.symbol.in_(symbols),
            )
            .group_by(NewsArticleRelatedSymbol.symbol)
        )
    ).all()
    news_dates = {
        symbol: latest.date() if latest else None for symbol, latest in news_rows
    }

    out: list[InvestCoverageSymbol] = []
    for symbol in symbols:
        latest_screener = screener_dates.get(symbol)
        latest_flow = investor_dates.get(symbol)
        latest_news = news_dates.get(symbol)
        surfaces: dict[str, CoverageState] = {
            "screener_snapshots": _date_state(latest_screener, trading_day),
            "news_feed": "fresh"
            if latest_news and (trading_day - latest_news).days <= 1
            else ("stale" if latest_news else "missing"),
        }
        latest_dates: dict[str, dt.date | None] = {
            "screener_snapshots": latest_screener,
            "news_feed": latest_news,
        }
        if market == "kr":
            surfaces["investor_flow"] = _date_state(latest_flow, trading_day)
            latest_dates["investor_flow"] = latest_flow
            latest_naver_flow = naver_flow_dates.get(symbol)
            surfaces["naver_investor_flow"] = _date_state(
                latest_naver_flow, trading_day
            )
            latest_dates["naver_investor_flow"] = latest_naver_flow
        else:
            surfaces["investor_flow"] = "unsupported"
            latest_dates["investor_flow"] = None
            surfaces["naver_investor_flow"] = "unsupported"
            latest_dates["naver_investor_flow"] = None
        out.append(
            InvestCoverageSymbol(
                symbol=symbol,
                market=market,
                surfaces=surfaces,
                latestDates=latest_dates,
                warnings=[
                    name
                    for name, state in surfaces.items()
                    if state in {"missing", "stale", "unsupported"}
                ],
                actionability=_actionability_for_symbol(surfaces),
            )
        )
    return out


async def _symbol_rows_all_markets(
    db: AsyncSession,
    symbols: list[str],
    trading_day: dt.date,
) -> list[InvestCoverageSymbol]:
    market_by_symbol = await _resolve_symbol_markets(db, symbols)
    partitioned: dict[str, list[str]] = {"kr": [], "us": []}
    unsupported: dict[str, InvestCoverageSymbol] = {}
    for symbol in symbols:
        resolved_market = market_by_symbol.get(symbol)
        if resolved_market in partitioned:
            partitioned[resolved_market].append(symbol)
        else:
            unsupported[symbol] = _unsupported_symbol_row(
                symbol, resolved_market or "unknown"
            )

    rows_by_symbol: dict[str, InvestCoverageSymbol] = {}
    for resolved_market, market_symbols in partitioned.items():
        if not market_symbols:
            continue
        for row in await _symbol_rows_for_market(
            db, resolved_market, market_symbols, trading_day
        ):
            rows_by_symbol[row.symbol] = row
    rows_by_symbol.update(unsupported)
    return [rows_by_symbol[symbol] for symbol in symbols if symbol in rows_by_symbol]


async def _resolve_symbol_markets(
    db: AsyncSession, symbols: list[str]
) -> dict[str, str]:
    kr_symbols = {
        symbol
        for (symbol,) in (
            await db.execute(
                sa.select(KRSymbolUniverse.symbol).where(
                    KRSymbolUniverse.symbol.in_(symbols),
                    KRSymbolUniverse.is_active.is_(True),
                )
            )
        ).all()
    }
    us_symbols = {
        symbol
        for (symbol,) in (
            await db.execute(
                sa.select(USSymbolUniverse.symbol).where(
                    USSymbolUniverse.symbol.in_(symbols),
                    USSymbolUniverse.is_active.is_(True),
                )
            )
        ).all()
    }
    resolved: dict[str, str] = {}
    for symbol in symbols:
        if symbol in kr_symbols:
            resolved[symbol] = "kr"
        elif symbol in us_symbols:
            resolved[symbol] = "us"
        elif symbol.isdigit() and len(symbol) == 6:
            resolved[symbol] = "kr"
        elif symbol.isalpha() and symbol.upper() == symbol:
            resolved[symbol] = "us"
        elif symbol.startswith("KRW-"):
            resolved[symbol] = "crypto"
        else:
            resolved[symbol] = "unknown"
    return resolved


def _unsupported_symbol_row(symbol: str, market: str) -> InvestCoverageSymbol:
    surfaces: dict[str, CoverageState] = {
        "screener_snapshots": "unsupported",
        "news_feed": "unsupported",
        "investor_flow": "unsupported",
        "naver_investor_flow": "unsupported",
    }
    latest_dates: dict[str, dt.date | None] = dict.fromkeys(surfaces)
    display_market = market if market in {"crypto", "unknown"} else market
    return InvestCoverageSymbol(
        symbol=symbol,
        market=display_market,
        surfaces=surfaces,
        latestDates=latest_dates,
        warnings=[
            f"symbol-level diagnostics are not implemented for {display_market} symbols"
        ],
        actionability=_actionability_for_symbol(surfaces),
    )


def _date_state(latest: dt.date | None, trading_day: dt.date) -> CoverageState:
    if latest is None:
        return "missing"
    return "fresh" if latest >= trading_day else "stale"
