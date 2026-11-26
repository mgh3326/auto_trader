"""Database reads for the daily-plan input bundle. SELECT statements only.

The caller owns the transaction. Rows are handed to the pure builder, which
decides what each one may prove at the cutoff, so most queries also return rows
written after it.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account_value import AccountValueSnapshot
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_events import MarketEvent
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.news import NewsArticle
from app.models.screener_pick_log import ScreenerPickLog
from app.models.symbol_news_relevance import SymbolNewsRelevance
from app.services.daily_plan.bundle_builder import (
    FLOW_SESSION_COUNT,
    RSI_BAR_COUNT,
    AccountBinding,
    AccountRow,
    CandidateSources,
    CandleBar,
    DisclosureRow,
    FlowRow,
    NewsRow,
    PickRow,
    ScreenerRow,
    SessionFacts,
    SymbolInputs,
    ValuationRow,
    build_bundle,
    select_candidates,
)
from app.services.market_events.session_calendar import (
    previous_trading_session,
    regular_session_bounds,
)

_KST = ZoneInfo("Asia/Seoul")
# How far back the newest snapshot of a symbol is looked for. Older ones would
# be stale anyway.
_LOOKBACK = timedelta(days=30)
_CANDLE_FLOOR = timedelta(days=750)
_ACCOUNT_LOOKBACK = timedelta(days=14)

_CANDLES = text(
    """
    SELECT symbol, time, open, high, low, close, volume, source, ingested_at
      FROM (
        SELECT symbol, time, open, high, low, close, volume, source, ingested_at,
               row_number() OVER (
                   PARTITION BY symbol ORDER BY time DESC, source
               ) AS bar_rank
          FROM public.kr_candles_1d
         WHERE venue = 'KRX' AND symbol IN :symbols
           AND time >= :floor AND time < :ceiling
      ) AS recent
     WHERE bar_rank <= :count
     ORDER BY symbol, time, source
    """
).bindparams(bindparam("symbols", expanding=True))


class CalendarUnavailable(Exception):
    """The calendar cannot confirm the plan day or the sessions before it."""


def _closes(dates: Iterable[date]) -> dict[date, datetime]:
    """Regular close per date; a date that is not a confirmed session is absent."""
    found = {item: regular_session_bounds("kr", item) for item in set(dates)}
    return {item: bounds[1] for item, bounds in found.items() if bounds is not None}


def session_facts(day: date) -> SessionFacts:
    """XKRX facts for ``day``; raises when ``day`` is not a confirmed session."""
    if regular_session_bounds("kr", day) is None:
        raise CalendarUnavailable("not_a_session")
    sessions: list[date] = []
    cursor = day
    for _ in range(FLOW_SESSION_COUNT):
        previous = previous_trading_session("kr", cursor)
        if previous is None:
            raise CalendarUnavailable("calendar_unknown")
        sessions.append(previous)
        cursor = previous
    return SessionFacts(
        session_date=day,
        previous_session=sessions[0],
        flow_sessions=tuple(reversed(sessions)),
        closes=_closes(sessions),
    )


def _aware(value: datetime | None) -> datetime | None:
    """The news writer stores naive UTC in its zone-less columns."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _day_end(facts: SessionFacts) -> datetime:
    """Rows first written after the plan day say nothing about its cutoff."""
    return datetime.combine(facts.session_date + timedelta(days=1), time(), _KST)


async def _scalars(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


def _screener(row: InvestScreenerSnapshot) -> ScreenerRow:
    return ScreenerRow(
        row_id=row.id,
        symbol=row.symbol,
        snapshot_date=row.snapshot_date,
        daily_turnover=row.daily_turnover,
        change_rate=row.change_rate,
        consecutive_up_days=row.consecutive_up_days,
        dist_to_support_pct=row.dist_to_support_pct,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _candidate_sources(db: AsyncSession, facts: SessionFacts) -> CandidateSources:
    screener = InvestScreenerSnapshot
    partition = await _scalars(
        db,
        select(screener)
        .where(
            screener.market == "kr",
            screener.snapshot_date == facts.previous_session,
        )
        .order_by(screener.symbol, screener.id),
    )
    latest = await db.scalar(
        select(func.max(screener.snapshot_date)).where(
            screener.market == "kr",
            screener.snapshot_date <= facts.previous_session,
        )
    )
    window_start = datetime.combine(facts.previous_session, time(), _KST)
    day_end = _day_end(facts)
    picks = await _scalars(
        db,
        select(ScreenerPickLog)
        .where(
            ScreenerPickLog.market == "kr",
            ScreenerPickLog.admission == "admitted",
            ScreenerPickLog.recorded_at >= window_start,
            ScreenerPickLog.recorded_at < day_end,
        )
        .order_by(ScreenerPickLog.id),
    )
    snapshot = AccountValueSnapshot
    accounts = await _scalars(
        db,
        select(snapshot)
        .where(
            snapshot.market == "account",
            or_(
                snapshot.account.like("kis:kis_live:%"),
                snapshot.account.like("toss:toss_live:%"),
            ),
            snapshot.day >= facts.session_date - _ACCOUNT_LOOKBACK,
            snapshot.day <= facts.session_date,
        )
        .order_by(snapshot.account, snapshot.day, snapshot.id),
    )
    return CandidateSources(
        screener_partition=[_screener(row) for row in partition],
        screener_latest_date=latest,
        picks=[
            PickRow(
                row_id=row.id,
                call_id=str(row.call_id),
                source=row.source,
                symbol=row.symbol,
                selection_seq=row.selection_seq,
                recorded_at=row.recorded_at,
            )
            for row in picks
        ],
        accounts=[
            AccountRow(
                row_id=row.id,
                account=row.account,
                day=row.day,
                observed_at=row.observed_at,
                created_at=row.created_at,
                holdings=row.holdings,
                cash=row.cash,
                reasons=row.reasons,
            )
            for row in accounts
        ],
    )


def _newest_per_symbol(model: Any, symbols: list[str], facts: SessionFacts, *extra):
    return (
        select(model)
        .distinct(model.symbol)
        .where(
            model.market == "kr",
            model.symbol.in_(symbols),
            model.snapshot_date >= facts.previous_session - _LOOKBACK,
            model.snapshot_date <= facts.previous_session,
            *extra,
        )
        .order_by(model.symbol, model.snapshot_date.desc(), model.id.desc())
    )


async def _symbol_inputs(
    db: AsyncSession, symbols: list[str], facts: SessionFacts
) -> dict[str, SymbolInputs]:
    if not symbols:
        return {}
    ceiling = datetime.combine(facts.previous_session + timedelta(days=1), time(), _KST)
    bars: dict[str, list[CandleBar]] = {}
    result = await db.execute(
        _CANDLES,
        {
            "symbols": symbols,
            "floor": ceiling - _CANDLE_FLOOR,
            "ceiling": ceiling,
            "count": RSI_BAR_COUNT,
        },
    )
    for row in result:
        bars.setdefault(row.symbol, []).append(
            CandleBar(
                session=row.time.astimezone(_KST).date(),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                source=row.source,
                ingested_at=row.ingested_at,
            )
        )

    screener = {
        row.symbol: _screener(row)
        for row in await _scalars(
            db, _newest_per_symbol(InvestScreenerSnapshot, symbols, facts)
        )
    }
    valuation = {
        row.symbol: ValuationRow(
            row_id=row.id,
            snapshot_date=row.snapshot_date,
            pbr=row.pbr,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in await _scalars(
            db,
            _newest_per_symbol(
                MarketValuationSnapshot,
                symbols,
                facts,
                MarketValuationSnapshot.source == "naver_finance",
            ),
        )
    }

    flow = InvestorFlowSnapshot
    flows: dict[str, list[FlowRow]] = {}
    for row in await _scalars(
        db,
        select(flow)
        .where(
            flow.market == "kr",
            flow.source == "naver_finance",
            flow.symbol.in_(symbols),
            flow.snapshot_date >= facts.previous_session - _LOOKBACK,
            flow.snapshot_date <= facts.previous_session,
        )
        .order_by(flow.symbol, flow.snapshot_date, flow.id),
    ):
        flows.setdefault(row.symbol, []).append(
            FlowRow(
                row_id=row.id,
                snapshot_date=row.snapshot_date,
                foreign_net=row.foreign_net,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )

    link = SymbolNewsRelevance
    news: dict[str, list[NewsRow]] = {}
    linked = await db.execute(
        select(
            link.symbol,
            link.article_id,
            link.first_seen_at,
            link.status,
            link.judged_at,
            NewsArticle.published_at,
            NewsArticle.published_precision,
            NewsArticle.first_received_at,
            NewsArticle.revised_at,
        )
        .join(NewsArticle, NewsArticle.id == link.article_id)
        .where(
            link.market == "kr",
            link.symbol.in_(symbols),
            link.first_seen_at >= _naive_utc(facts.stale_before),
            link.first_seen_at < _naive_utc(_day_end(facts)),
        )
        .order_by(link.symbol, link.article_id)
    )
    for row in linked:
        news.setdefault(row.symbol, []).append(
            NewsRow(
                article_id=row.article_id,
                link_first_seen_at=_aware(row.first_seen_at),
                status=row.status,
                judged_at=_aware(row.judged_at),
                published_at=_aware(row.published_at),
                published_precision=row.published_precision,
                first_received_at=_aware(row.first_received_at),
                revised_at=_aware(row.revised_at),
            )
        )

    event = MarketEvent
    disclosures: dict[str, list[DisclosureRow]] = {}
    for row in await _scalars(
        db,
        select(event)
        .where(
            event.market == "kr",
            event.category == "disclosure",
            event.source.in_(("dart", "naver_disclosure")),
            event.symbol.in_(symbols),
            event.event_date >= facts.previous_session,
            event.event_date <= facts.session_date,
        )
        .order_by(event.symbol, event.id),
    ):
        receipt = (row.raw_payload_json or {}).get("rcept_dt")
        disclosures.setdefault(row.symbol, []).append(
            DisclosureRow(
                event_id=row.id,
                source=row.source,
                event_date=row.event_date,
                published_at=row.published_at,
                published_precision=row.published_precision,
                provider_created_at=row.provider_created_at,
                receipt_text=receipt if type(receipt) is str else None,
                first_received_at=row.first_received_at,
                revised_at=row.revised_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )

    universe = KRSymbolUniverse
    security_types = dict(
        (
            await db.execute(
                select(universe.symbol, universe.security_type).where(
                    universe.symbol.in_(symbols)
                )
            )
        ).all()
    )
    return {
        symbol: SymbolInputs(
            security_type=security_types.get(symbol),
            bars=bars.get(symbol, ()),
            screener=screener.get(symbol),
            valuation=valuation.get(symbol),
            flows=flows.get(symbol, ()),
            news=news.get(symbol, ()),
            disclosures=disclosures.get(symbol, ()),
        )
        for symbol in symbols
    }


async def assemble_bundle(
    db: AsyncSession,
    *,
    day: date,
    closed_at: datetime,
    cutoff_at: datetime,
    bindings: Mapping[str, AccountBinding],
) -> dict[str, Any]:
    """Read the inputs of ``day`` and return the canonical bundle body."""
    facts = session_facts(day)
    sources = await _candidate_sources(db, facts)
    selection = select_candidates(
        sources, facts=facts, closed_at=closed_at, bindings=bindings
    )
    inputs = await _symbol_inputs(db, list(selection.symbols), facts)
    # Bar, snapshot and mark dates need their closes too.
    dates = {
        item
        for data in inputs.values()
        for item in (
            *(bar.session for bar in data.bars),
            *(row.snapshot_date for row in data.flows),
            *(
                row.snapshot_date
                for row in (data.screener, data.valuation)
                if row is not None
            ),
        )
    }
    dates.update(
        marked for row in sources.accounts for marked in _mark_dates(row.holdings)
    )
    return build_bundle(
        facts=replace(facts, closes={**_closes(dates), **facts.closes}),
        closed_at=closed_at,
        cutoff_at=cutoff_at,
        selection=selection,
        inputs=inputs,
        accounts=sources.accounts,
        bindings=bindings,
    )


def _mark_dates(holdings: Sequence[Any]) -> list[date]:
    dates = []
    for holding in holdings:
        raw = holding.get("mark_asof") if isinstance(holding, Mapping) else None
        if type(raw) is not str:
            continue
        try:
            marked = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if marked.utcoffset() is not None:
            dates.append(marked.astimezone(_KST).date())
    return dates
