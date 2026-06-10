"""ROB-249 — read-only stock-detail analyst consensus + research citations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
)
from app.schemas.invest_stock_detail_research_consensus import (
    StockDetailAnalystConsensus,
    StockDetailResearchConsensusDataState,
    StockDetailResearchConsensusResponse,
    StockDetailResearchConsensusSourceOfTruth,
    StockDetailResearchConsensusState,
    StockDetailResearchFreshness,
)
from app.schemas.research_reports import (
    ResearchReportCitation,
    ResearchReportsReadinessResponse,
)
from app.services.analyst_normalizer import build_consensus
from app.services.invest_view_model.stock_detail_symbol_resolver import (
    ResolvedSymbol,
    resolve_symbol,
)
from app.services.research_reports.freshness import compute_research_reports_readiness
from app.services.research_reports.query_service import ResearchReportsQueryService

logger = logging.getLogger(__name__)

ResearchConsensusMarket = Literal["kr", "us"]
Resolver = Callable[
    [ResearchConsensusMarket, str, AsyncSession], Awaitable[ResolvedSymbol]
]
OpinionsProvider = Callable[
    [str, ResearchConsensusMarket, int], Awaitable[dict[str, Any]]
]
CitationsProvider = Callable[
    [AsyncSession, str, int], Awaitable[list[ResearchReportCitation]]
]
ReadinessProvider = Callable[
    [AsyncSession, str | None, int], Awaitable[ResearchReportsReadinessResponse]
]

DEFAULT_OPINION_LIMIT = 10
DEFAULT_CITATION_LIMIT = 5
DEFAULT_MAX_AGE_HOURS = 24


async def _default_opinions_provider(
    symbol: str, market: ResearchConsensusMarket, limit: int
) -> dict[str, Any]:
    return await handle_get_investment_opinions(
        symbol=symbol, market=market, limit=limit
    )


async def _default_citations_provider(
    db: AsyncSession, symbol: str, limit: int
) -> list[ResearchReportCitation]:
    result = await ResearchReportsQueryService(db).find_relevant(
        symbol=symbol, limit=limit
    )
    return result.citations


async def _default_readiness_provider(
    db: AsyncSession, source: str | None, max_age_hours: int
) -> ResearchReportsReadinessResponse:
    return await compute_research_reports_readiness(
        db, source=source, max_age_hours=max_age_hours
    )


@dataclass(frozen=True, slots=True)
class StockDetailResearchConsensusProviders:
    resolver: Resolver = resolve_symbol
    opinions: OpinionsProvider = _default_opinions_provider
    citations: CitationsProvider = _default_citations_provider
    readiness: ReadinessProvider = _default_readiness_provider


DEFAULT_RESEARCH_CONSENSUS_PROVIDERS = StockDetailResearchConsensusProviders()


async def build_stock_detail_research_consensus(
    *,
    market: ResearchConsensusMarket,
    symbol: str,
    db: AsyncSession,
    providers: StockDetailResearchConsensusProviders = DEFAULT_RESEARCH_CONSENSUS_PROVIDERS,
    opinion_limit: int = DEFAULT_OPINION_LIMIT,
    citation_limit: int = DEFAULT_CITATION_LIMIT,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> StockDetailResearchConsensusResponse:
    """Build a read-only research consensus panel for KR/US stock detail.

    Provider failures are isolated into warning codes. The response may still be
    useful when only compact research citations are available. It never includes
    report bodies, raw payloads, or PDF bytes.
    """

    resolved = await providers.resolver(market, symbol, db)
    warnings: list[str] = []
    as_of = datetime.now(UTC)

    opinions_task = _safe_opinions(
        providers.opinions(resolved.symbol_db, market, opinion_limit), warnings
    )
    citations_task = _safe_citations(
        providers.citations(db, resolved.symbol_db, citation_limit), warnings
    )
    readiness_task = _safe_readiness(
        providers.readiness(db, None, max_age_hours), warnings, max_age_hours
    )

    opinions_payload, citations, readiness = await asyncio.gather(
        opinions_task, citations_task, readiness_task
    )

    consensus = _build_consensus_model(opinions_payload, warnings)
    freshness = StockDetailResearchFreshness(
        isReady=readiness.is_ready,
        isStale=readiness.is_stale,
        latestRunUuid=readiness.latest_run_uuid,
        latestFinishedAt=readiness.latest_finished_at,
        latestReportCount=readiness.latest_report_count,
        maxAgeHours=readiness.max_age_hours,
    )
    warnings.extend(w for w in readiness.warnings if w not in warnings)

    has_consensus = consensus is not None and consensus.totalCount > 0
    has_citations = bool(citations)
    source_of_truth = _source_of_truth(has_consensus, has_citations)
    state = _state(has_consensus, has_citations)
    provider_error = _has_provider_error(warnings)
    data_state = _data_state(state, readiness.is_stale, warnings, provider_error)
    empty_reason = None
    if state == "missing":
        empty_reason = (
            "provider_error"
            if provider_error
            else "no_analyst_consensus_or_research_reports"
        )

    return StockDetailResearchConsensusResponse(
        symbol=resolved.symbol_db,
        market=market,
        displayName=resolved.display_name,
        state=state,
        dataState=data_state,
        emptyReason=empty_reason,
        warnings=warnings,
        sourceOfTruth=source_of_truth,
        asOf=as_of,
        stale=readiness.is_stale,
        consensus=consensus if has_consensus else None,
        citations=citations,
        freshness=freshness,
    )


async def _safe_opinions(
    coro: Awaitable[dict[str, Any]], warnings: list[str]
) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(coro, timeout=4)
    except TimeoutError:
        warnings.append("analyst_opinions_timeout")
    except Exception as exc:  # pragma: no cover - exercised via stubs/logging only
        logger.warning("stock-detail analyst opinions unavailable: %s", exc)
        warnings.append("analyst_opinions_unavailable")
    return None


async def _safe_citations(
    coro: Awaitable[list[ResearchReportCitation]], warnings: list[str]
) -> list[ResearchReportCitation]:
    try:
        return await asyncio.wait_for(coro, timeout=4)
    except TimeoutError:
        warnings.append("research_reports_timeout")
    except Exception as exc:  # pragma: no cover - exercised via stubs/logging only
        logger.warning("stock-detail research citations unavailable: %s", exc)
        warnings.append("research_reports_unavailable")
    return []


async def _safe_readiness(
    coro: Awaitable[ResearchReportsReadinessResponse],
    warnings: list[str],
    max_age_hours: int,
) -> ResearchReportsReadinessResponse:
    try:
        return await asyncio.wait_for(coro, timeout=4)
    except TimeoutError:
        warnings.append("research_reports_readiness_timeout")
    except Exception as exc:  # pragma: no cover - exercised via stubs/logging only
        logger.warning("stock-detail research readiness unavailable: %s", exc)
        warnings.append("research_reports_readiness_unavailable")
    return ResearchReportsReadinessResponse(
        source=None,
        is_ready=False,
        is_stale=False,
        latest_inserted_count=0,
        latest_skipped_count=0,
        latest_report_count=0,
        warnings=["research_reports_readiness_unavailable"],
        max_age_hours=max_age_hours,
    )


def _build_consensus_model(
    payload: dict[str, Any] | None, warnings: list[str]
) -> StockDetailAnalystConsensus | None:
    if not payload:
        return None
    if payload.get("error"):
        if "analyst_opinions_unavailable" not in warnings:
            warnings.append("analyst_opinions_unavailable")
        return None

    raw_opinions = payload.get("opinions") or payload.get("items") or []
    if not isinstance(raw_opinions, list) or not raw_opinions:
        return None

    # ROB-488: fall back to the embedded consensus dict's current_price —
    # without it, payloads lacking a top-level price leave current_price=None,
    # which nulls upside_pct AND disarms the outlier guard on the web path.
    embedded_consensus = payload.get("consensus")
    embedded_current = (
        embedded_consensus.get("current_price")
        if isinstance(embedded_consensus, dict)
        else None
    )
    current_price = _to_float(
        payload.get("current_price")
        or payload.get("currentPrice")
        or payload.get("price")
        or payload.get("current")
        or embedded_current
    )
    normalized_opinions = [_normalize_opinion(row) for row in raw_opinions]
    consensus = build_consensus(normalized_opinions, current_price=current_price)
    return StockDetailAnalystConsensus(
        source=payload.get("source") or payload.get("provider"),
        buyCount=consensus["buy_count"],
        holdCount=consensus["hold_count"],
        sellCount=consensus["sell_count"],
        strongBuyCount=consensus["strong_buy_count"],
        totalCount=consensus["total_count"],
        avgTargetPrice=consensus["avg_target_price"],
        medianTargetPrice=consensus["median_target_price"],
        minTargetPrice=consensus["min_target_price"],
        maxTargetPrice=consensus["max_target_price"],
        upsidePct=consensus["upside_pct"],
        currentPrice=consensus["current_price"],
    )


def _normalize_opinion(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {"rating": None, "target_price": None, "date": None}
    return {
        "rating": row.get("rating")
        or row.get("opinion")
        or row.get("investment_opinion")
        or row.get("recommendation"),
        "target_price": _to_float(
            row.get("target_price")
            or row.get("targetPrice")
            or row.get("target")
            or row.get("tp")
        ),
        # ROB-486+488: build_consensus 의 recency 윈도우가 패널에서도 동작하도록
        # 행별 date 를 보존한다. 키는 analyst_normalizer._OPINION_DATE_KEYS 의
        # alias 들을 미러링 — 대체 키로만 날짜가 오는 행이 undated 로 떨어지면
        # fail-open 으로 유지는 되지만 recency 제외(stale 차단)를 받지 못한다.
        "date": (
            row.get("date")
            or row.get("report_date")
            or row.get("published_date")
            or row.get("published_at")
        ),
    }


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _source_of_truth(
    has_consensus: bool, has_citations: bool
) -> StockDetailResearchConsensusSourceOfTruth:
    if has_consensus and has_citations:
        return "analyst_opinions_and_research_reports"
    if has_consensus:
        return "analyst_opinions"
    if has_citations:
        return "research_reports"
    return "none"


def _state(
    has_consensus: bool, has_citations: bool
) -> StockDetailResearchConsensusState:
    if has_consensus and has_citations:
        return "ready"
    if has_consensus or has_citations:
        return "partial"
    return "missing"


def _data_state(
    state: StockDetailResearchConsensusState,
    is_stale: bool,
    warnings: list[str],
    provider_error: bool = False,
) -> StockDetailResearchConsensusDataState:
    if any(w.endswith("_timeout") for w in warnings):
        return "error" if state == "missing" else "stale"
    if provider_error and state == "missing":
        return "error"
    if state == "missing":
        return "missing"
    if is_stale:
        return "stale"
    return "fresh"


def _has_provider_error(warnings: list[str]) -> bool:
    return any(
        w.endswith("_unavailable") or w.endswith("_timeout")
        for w in warnings
        if w != "research_reports_stale"
    )
