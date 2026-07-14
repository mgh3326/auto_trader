"""FastAPI router for /invest retrospectives read surface (ROB-662).

ROB-691 adds trade-history filters to the list endpoint (win/loss/decided,
symbol prefix search, KST date range) and a `/scoreboard` read endpoint that
mirrors the existing deterministic `build_retrospective_aggregate` (win-rate /
realized-PnL / win-loss). The service is not touched beyond additive query
params — this router only adds a totals rollup on top of its per-group output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.symbol import to_db_symbol
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_retrospectives import (
    CanonicalActionRow,
    CanonicalActionsResponse,
    NextActionRow,
    NextActionsResponse,
    RetrospectiveRow,
    RetrospectivesResponse,
    ScoreboardGroupRow,
    ScoreboardResponse,
    ScoreboardTotals,
)
from app.schemas.trade_retrospective import (
    VALID_ROOT_CAUSE_CLASSES,
    VALID_TRIGGER_TYPES,
)
from app.services.trade_journal import trade_retrospective_service as retro_svc
from app.services.trade_journal.trade_retrospective_service import (
    VALID_OUTCOME_FILTERS,
)

router = APIRouter(
    prefix="/trading/api/invest/retrospectives",
    tags=["invest-retrospectives"],
)

Market = Literal["all", "kr", "us", "crypto"]

_VALID_SCOREBOARD_GROUP_BY = frozenset(
    {"strategy", "day", "trigger_type", "root_cause"}
)


def _normalize_symbol(symbol: str | None, market: Market) -> str | None:
    if not symbol:
        return None
    sym = symbol.strip()
    return to_db_symbol(sym) if market == "us" else sym.upper()


def _parse_kst_date(label: str, value: str | None) -> str | None:
    """Validate a `YYYY-MM-DD` KST-calendar-date query param, 422 on malformed input."""
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid {label}: {value} (expected YYYY-MM-DD)"
        ) from exc
    return value


@router.get("")
async def list_retrospectives(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[Market, Query()] = "all",
    trigger_type: Annotated[str | None, Query()] = None,
    root_cause_class: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    outcome_filter: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=32)] = None,
    kst_date_from: Annotated[str | None, Query()] = None,
    kst_date_to: Annotated[str | None, Query()] = None,
    days: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RetrospectivesResponse:
    if trigger_type is not None and trigger_type not in VALID_TRIGGER_TYPES:
        raise HTTPException(
            status_code=422, detail=f"invalid trigger_type: {trigger_type}"
        )
    if (
        root_cause_class is not None
        and root_cause_class not in VALID_ROOT_CAUSE_CLASSES
    ):
        raise HTTPException(
            status_code=422, detail=f"invalid root_cause_class: {root_cause_class}"
        )
    if outcome_filter is not None and outcome_filter not in VALID_OUTCOME_FILTERS:
        raise HTTPException(
            status_code=422, detail=f"invalid outcome_filter: {outcome_filter}"
        )
    date_from = _parse_kst_date("kst_date_from", kst_date_from)
    date_to = _parse_kst_date("kst_date_to", kst_date_to)
    db_symbol = _normalize_symbol(symbol, market)
    result = await retro_svc.get_retrospectives(
        db,
        market=None if market == "all" else market,
        trigger_type=trigger_type,
        root_cause_class=root_cause_class,
        symbol=db_symbol,
        outcome_filter=outcome_filter,
        symbol_search=q,
        kst_date_from=date_from,
        kst_date_to=date_to,
        days=days,
        limit=limit,
        offset=offset,
    )
    summary = result["summary"]
    return RetrospectivesResponse(
        market=market,
        trigger_type=trigger_type,
        root_cause_class=root_cause_class,
        symbol=db_symbol,
        outcome_filter=outcome_filter,
        q=q,
        kst_date_from=date_from,
        kst_date_to=date_to,
        count=summary["count"],
        total=summary["total"],
        items=[RetrospectiveRow(**e) for e in result["entries"]],
        as_of=datetime.now(UTC),
    )


@router.get("/scoreboard")
async def get_retrospective_scoreboard(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    group_by: Annotated[str, Query()] = "strategy",
    market: Annotated[Market, Query()] = "all",
    account_mode: Annotated[str | None, Query()] = None,
    strategy_key: Annotated[str | None, Query()] = None,
    kst_date_from: Annotated[str | None, Query()] = None,
    kst_date_to: Annotated[str | None, Query()] = None,
) -> ScoreboardResponse:
    """Judgment scoreboard: win-rate / realized-PnL / win-loss.

    Thin read wrapper over `build_retrospective_aggregate` (no new business
    logic in the service) plus a router-side totals rollup across groups. Per
    plan §3.4/§4 the headline `totals` are only meaningful for the PnL-oriented
    groupings (strategy/day) — trigger_type/root_cause include no-fill-evidence
    rows (`include_no_evidence`) and dilute the win/loss meaning, so callers
    that want the headline tile should request `group_by=strategy`.
    """
    if group_by not in _VALID_SCOREBOARD_GROUP_BY:
        raise HTTPException(status_code=422, detail=f"invalid group_by: {group_by}")
    date_from = _parse_kst_date("kst_date_from", kst_date_from)
    date_to = _parse_kst_date("kst_date_to", kst_date_to)
    result = await retro_svc.build_retrospective_aggregate(
        db,
        kst_date_from=date_from,
        kst_date_to=date_to,
        account_mode=account_mode,
        market=None if market == "all" else market,
        strategy_key=strategy_key,
        group_by=group_by,
    )
    groups = result["groups"]

    total_sample_size = sum(g["sample_size"] for g in groups)
    total_wins = sum(g["wins"] for g in groups)
    total_misses = sum(g["misses"] for g in groups)
    decided = total_wins + total_misses
    win_rate_pct = (total_wins / decided * 100.0) if decided else None
    realized_pnl_sum: dict[str, float] = {}
    for g in groups:
        for currency, amount in g["realized_pnl_sum"].items():
            realized_pnl_sum[currency] = realized_pnl_sum.get(currency, 0.0) + amount
    fx_pnl_krw_sum = sum(g["fx_pnl_krw_sum"] for g in groups)
    total_pnl_krw_sum = sum(g["total_pnl_krw_sum"] for g in groups)

    totals = ScoreboardTotals(
        sample_size=total_sample_size,
        wins=total_wins,
        misses=total_misses,
        decided=decided,
        win_rate_pct=win_rate_pct,
        realized_pnl_sum=realized_pnl_sum,
        fx_pnl_krw_sum=fx_pnl_krw_sum,
        total_pnl_krw_sum=total_pnl_krw_sum,
        excluded_no_fill_evidence=result["excluded_no_fill_evidence"],
    )
    return ScoreboardResponse(
        group_by=result["group_by"],
        market=market,
        kst_date_from=date_from,
        kst_date_to=date_to,
        count=len(groups),
        groups=[ScoreboardGroupRow(**g) for g in groups],
        totals=totals,
        as_of=datetime.now(UTC),
    )


@router.get("/next-actions")
async def list_open_next_actions(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[Market, Query()] = "all",
    symbol: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> NextActionsResponse:
    statuses = (
        frozenset(s.strip() for s in status.split(",") if s.strip()) if status else None
    )
    db_symbol = _normalize_symbol(symbol, market)
    result = await retro_svc.get_open_next_actions(
        db,
        market=None if market == "all" else market,
        symbol=db_symbol,
        statuses=statuses,
    )
    return NextActionsResponse(
        market=market,
        symbol=db_symbol,
        count=result["count"],
        scan_limit=result["scan_limit"],
        items=[NextActionRow(**i) for i in result["items"]],
    )


@router.get("/actions")
async def list_canonical_actions(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[str | None, Query()] = None,
    market: Annotated[Market, Query()] = "all",
    symbol: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=32)] = None,
    owner: Annotated[str | None, Query()] = None,
    issue_id: Annotated[str | None, Query()] = None,
    overdue_only: Annotated[bool, Query()] = False,
    trigger_type: Annotated[str | None, Query()] = None,
    outcome_filter: Annotated[str | None, Query()] = None,
    kst_date_from: Annotated[str | None, Query()] = None,
    kst_date_to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CanonicalActionsResponse:
    """Canonical paginated action list with overdue-first ordering.

    Omitted status defaults to open,in_progress (active only).
    Terminal history requires an explicit status filter.
    """
    if trigger_type is not None and trigger_type not in VALID_TRIGGER_TYPES:
        raise HTTPException(
            status_code=422, detail=f"invalid trigger_type: {trigger_type}"
        )
    if outcome_filter is not None and outcome_filter not in VALID_OUTCOME_FILTERS:
        raise HTTPException(
            status_code=422, detail=f"invalid outcome_filter: {outcome_filter}"
        )
    date_from = _parse_kst_date("kst_date_from", kst_date_from)
    date_to = _parse_kst_date("kst_date_to", kst_date_to)
    db_symbol = _normalize_symbol(symbol, market)

    statuses = (
        frozenset(s.strip() for s in status.split(",") if s.strip()) if status else None
    )

    result = await retro_svc.get_canonical_actions(
        db,
        statuses=statuses,
        market=None if market == "all" else market,
        symbol=db_symbol,
        symbol_search=q,
        owner=owner,
        issue_id=issue_id,
        overdue_only=overdue_only,
        trigger_type=trigger_type,
        outcome_filter=outcome_filter,
        kst_date_from=date_from,
        kst_date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return CanonicalActionsResponse(
        total=result["total"],
        count=result["count"],
        limit=result["limit"],
        offset=result["offset"],
        as_of=result["as_of"],
        items=[CanonicalActionRow(**item) for item in result["items"]],
    )
