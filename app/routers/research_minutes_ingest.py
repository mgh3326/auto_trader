"""hk 6343: research minute-bar HTTP ingest and target read (token-authed).

Design hk:doc design/6321-go-quote-collector.md §1·§3.1. The Go quote collector
never writes the database itself: it posts closed 1-minute bars here and this
surface writes them through ``SqlMinuteWriter`` — the same insert-only
``ON CONFLICT (time_utc, symbol, venue) DO NOTHING`` rule the Python collector
uses. Whichever process arrives first fixes ``retrieved_at`` (first receipt).

* ``POST /api/research-minutes/ingest`` — a bounded batch for one market. Each
  item gets its own savepoint, so a refused or failing bar never discards the
  rest of the batch (the execution-ledger ingest shape).
* ``GET  /api/research-minutes/targets?market=kr|us`` — read-only: the
  ``build_targets`` result (hk 6299 tiers) the collector would use, so the tier
  rule stays in one place instead of being re-implemented in Go.

Auth and gating live in ``AuthMiddleware``: the gate is off by default and a
disabled surface answers 404 before any DB access; an unset token is 403 and a
wrong token is 401. A session cookie is never a substitute. This router imports
no order, ledger, approval or broker-mutation module (static guard test).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.mcp_server.env_utils import _env_int
from app.schemas.research_minutes_ingest import (
    ResearchMinuteBarItem,
    ResearchMinutesIngestItemResult,
    ResearchMinutesIngestRequest,
    ResearchMinutesIngestResponse,
)
from app.services.research_candles import (
    futures_listing,
    minute_ingest,
    us_day_calendar,
)
from app.services.research_candles import session_minutes_collector as collector
from app.services.research_candles.futures_minutes_collector import (
    FuturesBar,
    SqlFuturesMinuteWriter,
)
from app.services.research_candles.session_minutes_collector import SqlMinuteWriter
from app.services.research_candles.session_segment import KIS_DAY_VENUE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trading", tags=["research-minutes-ingest"])

_INGEST_PATH = "/api/research-minutes/ingest"
_TARGETS_PATH = "/api/research-minutes/targets"
_KST = ZoneInfo("Asia/Seoul")

#: Same source the MCP portfolio tools use for manual holdings (default 1).
#: Read here instead of importing ``app.mcp_server.tooling.shared``, which drags
#: the whole tool graph (including order tools) into this surface.
MCP_USER_ID = _env_int("MCP_USER_ID", 1)

#: A write failure is reported by class name only — a driver message can carry
#: payload fragments and is never echoed. Bounded to the same shape either way.
_MAX_REASON_CHARS = 120


def _write_failure_reason(exc: BaseException) -> str:
    return (type(exc).__name__ or "Error")[:_MAX_REASON_CHARS]


class _HoldingsOnlyReader:
    """Targets needs the two holdings reads and nothing else.

    The KIS client is built once and wrapped so this surface cannot reach an
    order method even by mistake; the wrapper is the router's whole KIS
    attribute surface (static guard test).
    """

    def __init__(self, client: collector.KisMinuteReader) -> None:
        self._client = client

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        return await self._client.fetch_my_stocks()

    async def fetch_my_us_stocks(self) -> list[dict[str, Any]]:
        return await self._client.fetch_my_us_stocks()


def _is_kis_day(venue: object) -> bool:
    return str(venue or "").strip().upper() == KIS_DAY_VENUE


def _kst_date(item: ResearchMinuteBarItem) -> date | None:
    if item.time_utc.tzinfo is None:
        return None
    try:
        return item.time_utc.astimezone(_KST).date()
    except (OverflowError, ValueError):
        # Refused per item by decide_bar (bar_time_invalid); never fails the batch.
        return None


async def _kis_day_calendars(
    bars: list[ResearchMinuteBarItem],
) -> dict[date, Any | None]:
    """One Toss US calendar read per KST date of this batch's KIS_DAY bars.

    A KIS_DAY bar is labelled ``US_DAY`` only from this calendar (hk 7044). Other
    venues never read it, so their labels stay exactly the KIS windows.
    """
    days: set[date] = set()
    for item in bars:
        if _is_kis_day(item.venue):
            day = _kst_date(item)
            if day is not None:
                days.add(day)
    return {day: await us_day_calendar.read_us_calendar(day) for day in sorted(days)}


def _calendar_for_item(
    item: ResearchMinuteBarItem, calendars: dict[date, Any | None]
) -> Any | None:
    if not _is_kis_day(item.venue):
        return None
    day = _kst_date(item)
    return None if day is None else calendars.get(day)


class _UnavailableKisReader:
    """Refuses every holdings read: targets still answers, with error rows.

    ``build_targets`` already treats a failing origin as a reported error rather
    than a failed run; without credentials the holdings tier is simply absent
    from the answer instead of turning the read into a 500.
    """

    async def fetch_my_stocks(self) -> list[dict[str, Any]]:
        raise RuntimeError("kis client unavailable")

    async def fetch_my_us_stocks(self) -> list[dict[str, Any]]:
        raise RuntimeError("kis client unavailable")


@router.post(_INGEST_PATH, response_model=ResearchMinutesIngestResponse)
async def ingest_research_minutes(
    request: ResearchMinutesIngestRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ResearchMinutesIngestResponse:
    # Auth/gate: validated upstream by AuthMiddleware (dedicated machine token,
    # never a session user).
    now = datetime.now(UTC)
    batch_id = request.batch_id or minute_ingest.build_batch_id(request.market, now)
    # Futures: the master listing recomputes roll_state. Read once per batch; a
    # failed read refuses every futures item, never stores a guessed state.
    listing = (
        await futures_listing.FUTURES_LISTING.listed_months_by_product()
        if request.market == "futures"
        else None
    )
    futures_writer = SqlFuturesMinuteWriter(db) if request.market == "futures" else None
    calendars = await _kis_day_calendars(request.bars) if request.market == "us" else {}
    stock_writer = (
        None if request.market == "futures" else SqlMinuteWriter(db, request.market)
    )
    results: list[ResearchMinutesIngestItemResult] = []
    inserted = already_present = 0

    for item in request.bars:
        if request.market == "futures":
            decision = minute_ingest.decide_futures_bar(
                symbol=item.symbol,
                venue=item.venue,
                time_utc=item.time_utc,
                open_price=item.open,
                high_price=item.high,
                low_price=item.low,
                close_price=item.close,
                volume=item.volume,
                value=item.value,
                received_at=item.received_at,
                source=item.source,
                product=item.product,
                contract_month=item.contract_month,
                roll_state=item.roll_state,
                session_segment=item.session_segment,
                session_date=item.session_date,
                listing=listing,
                now=now,
            )
        elif item.futures_fields_sent():
            # Stock markets carry none of the futures-only fields.
            decision = minute_ingest.BarDecision(
                reason=minute_ingest.REASON_FIELD_NOT_APPLICABLE
            )
        else:
            decision = minute_ingest.decide_bar(
                market=request.market,
                symbol=item.symbol,
                venue=item.venue,
                time_utc=item.time_utc,
                open_price=item.open,
                high_price=item.high,
                low_price=item.low,
                close_price=item.close,
                volume=item.volume,
                value=item.value,
                received_at=item.received_at,
                source=item.source,
                now=now,
                us_calendar=_calendar_for_item(item, calendars),
            )
        bar = decision.bar
        retrieved_at = decision.retrieved_at
        if bar is None or retrieved_at is None:
            results.append(
                ResearchMinutesIngestItemResult(
                    status="rejected",
                    reason=decision.reason or minute_ingest.REASON_BAR_TIME_INVALID,
                )
            )
            continue

        try:
            # A per-item savepoint keeps one failing bar from discarding the
            # batch's already-applied rows.
            async with db.begin_nested():
                if isinstance(bar, FuturesBar):
                    assert futures_writer is not None
                    created = await futures_writer.insert_one(
                        bar, batch_id=batch_id, retrieved_at=retrieved_at
                    )
                else:
                    assert stock_writer is not None
                    created = await stock_writer.insert_one(
                        bar, batch_id=batch_id, retrieved_at=retrieved_at
                    )
        except Exception as exc:  # noqa: BLE001 - reported per item
            logger.warning(
                "research minute ingest refused one bar: market=%s symbol=%s "
                "venue=%s error=%s",
                request.market,
                bar.symbol,
                item.venue,
                type(exc).__name__,
            )
            results.append(
                ResearchMinutesIngestItemResult(
                    status="rejected", reason=_write_failure_reason(exc)
                )
            )
            continue

        if created:
            inserted += 1
            results.append(ResearchMinutesIngestItemResult(status="inserted"))
        else:
            already_present += 1
            results.append(ResearchMinutesIngestItemResult(status="already_present"))

    await db.commit()
    return ResearchMinutesIngestResponse(
        market=request.market,
        batch_id=batch_id,
        received=len(request.bars),
        inserted=inserted,
        already_present=already_present,
        rejected=len(request.bars) - inserted - already_present,
        results=results,
    )


@router.get(_TARGETS_PATH)
async def get_research_minute_targets(
    market: Literal["kr", "us"],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    # Auth/gate: validated upstream by AuthMiddleware (same token as the ingest).
    # Read-only: build_targets selects symbols; this handler never reads a quote
    # and never writes.
    try:
        from app.services.brokers.kis.client import KISClient

        kis: collector.KisMinuteReader = _HoldingsOnlyReader(
            cast(collector.KisMinuteReader, KISClient())
        )
    except Exception as exc:  # noqa: BLE001 - targets still answers, holdings absent
        logger.warning(
            "research minute targets: kis client unavailable error=%s",
            type(exc).__name__,
        )
        kis = cast(collector.KisMinuteReader, _UnavailableKisReader())

    now = datetime.now(UTC)
    fixed_raw = (
        settings.SESSION_MINUTES_FIXED_SYMBOLS_KR
        if market == "kr"
        else settings.SESSION_MINUTES_FIXED_SYMBOLS_US
    )
    max_symbols = (
        settings.SESSION_MINUTES_MAX_SYMBOLS_KR
        if market == "kr"
        else settings.SESSION_MINUTES_MAX_SYMBOLS_US
    )
    return await minute_ingest.build_targets_payload(
        market=market,
        session=db,
        kis=kis,
        user_id=MCP_USER_ID,
        fixed_symbols_raw=fixed_raw,
        now=now,
        screener_top_n=settings.SESSION_MINUTES_SCREENER_TOP_N,
        max_symbols=max_symbols,
        fixed_budget=settings.SESSION_MINUTES_BUDGET_FIXED,
        intraday_budget=settings.SESSION_MINUTES_BUDGET_INTRADAY,
        screener_budget=settings.SESSION_MINUTES_BUDGET_SCREENER,
    )


__all__ = ["router"]
