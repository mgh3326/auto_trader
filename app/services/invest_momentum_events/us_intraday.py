"""hk 6510: US intraday movers observation log (insert-only, manual job).

The US counterpart of hk 6154. One run fetches the Alpaca screener
(gainers/losers/most actives) and the KIS overseas up/down ranking, records the
rows insert-only into ``invest_momentum_event_snapshots`` (``market='us'``) and
one status row per (run, surface) into ``invest_momentum_event_runs``.

The surface names both axes the contract asks for — source and session, plus the
side: ``us:<source>:<session>:<side>`` where ``session`` is ``us_pre`` /
``us_regular`` / ``us_post`` (04:00-20:00 ET on an XNYS session) and ``side`` is
``up`` / ``down`` / ``active``. So a run says which source served each surface.

Rules:

* The run only collects inside 04:00-20:00 ET on an XNYS trading day; outside it
  reports ``outside_session`` and does nothing (no fetch, no rows). The trading
  date of a row is the ET calendar date, which is the trading day for every
  window that is collected (the day rolls over at 20:00 ET).
* A source that cannot be attempted (no reader, no credentials) is ``error`` with
  reason ``source_unavailable``; it never looks like an empty ranking.
* A surface's rows and its status row are one transaction. The status row is
  inserted first (``ON CONFLICT DO NOTHING``); an existing (run, surface) is
  ``already_recorded`` and nothing changes, so re-running a snapshot inserts
  nothing.
* A KIS side is one surface across NAS/NYS/AMS; if any exchange fails the whole
  side is ``error`` and no rows are written (a partial ranking must not look
  complete).
* A field the provider does not send is NULL with a closed reason in
  ``field_reasons`` — never 0. The asset class comes from ``us_symbol_universe``;
  a symbol without a master row is ``unknown`` + ``not_in_symbol_universe``.
* No scheduler, no order/ledger/approval/broker-mutation import, and nothing here
  writes the screener pick log (hard rules 6, 13).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.us_symbol_universe import USSymbolUniverse
from app.services.invest_momentum_events.intraday import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    RunReport,
    SurfaceOutcome,
    SurfaceReport,
    classify_error,
)
from app.services.invest_momentum_events.models import MomentumEventUpsert
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_momentum_events.us_sources import (
    ALPACA_TOP_ACTIVES,
    ALPACA_TOP_MOVERS,
    ASSET_UNKNOWN,
    KIS_EXCHANGES,
    KIS_GUBN_DOWN,
    KIS_GUBN_UP,
    KIS_MIN_VOLUME_SHARES,
    KIS_NDAY_TODAY,
    KIS_VOL_RANG,
    REASON_NOT_IN_SYMBOL_UNIVERSE,
    REASON_SOURCE_UNAVAILABLE,
    REASON_UNAVAILABLE,
    SOURCE_ALPACA,
    SOURCE_BENZINGA,
    SOURCE_KIS,
    SOURCE_KIS_NEW_HIGHLOW,
    SOURCE_KIS_PRICE_FLUCT,
    SOURCE_KIS_VOLUME_POWER,
    SOURCE_KIS_VOLUME_SURGE,
    SOURCE_TOSS,
    TOSS_RANKING_AMOUNT,
    TOSS_RANKING_GAINERS,
    TOSS_RANKING_LOSERS,
    TOSS_RANKING_VOLUME,
    AlpacaScreenerReader,
    KisOverseasRankingReader,
    SymbolMaster,
    TossRankingReader,
    UsRow,
    classify_alpaca_most_actives,
    classify_alpaca_movers,
    classify_benzinga_movers,
    classify_kis_mover_ranking,
    classify_kis_updown,
    classify_toss_rankings,
    derive_asset_type,
)
from app.services.research_candles.session_segment import (
    US_POST,
    US_PRE,
    US_REGULAR,
    session_segment_of,
)

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_RUN_NAMESPACE = uuid.UUID("6510c0de-0000-4000-8000-0000006510aa")

SESSION_PRE = "us_pre"
SESSION_REGULAR = "us_regular"
SESSION_POST = "us_post"
US_SESSIONS = (SESSION_PRE, SESSION_REGULAR, SESSION_POST)
#: The 6145 segment labels are the session guard (same windows: 04:00 ET up to the
#: XNYS open, regular hours incl. early closes, close to 20:00 ET; outside a
#: session day, or in a window the calendar cannot confirm, ``UNKNOWN`` = closed).
_SESSION_BY_SEGMENT = {
    US_PRE: SESSION_PRE,
    US_REGULAR: SESSION_REGULAR,
    US_POST: SESSION_POST,
}

SIDE_UP = "up"
SIDE_DOWN = "down"
SIDE_ACTIVE = "active"
SIDES = (SIDE_UP, SIDE_DOWN, SIDE_ACTIVE)

MARKET = "us"
#: ``trade_type`` of every US row. The exchange of a KIS row is ``market_type``;
#: Alpaca's screener does not report one, so its rows carry ``US``.
TRADE_TYPE = "US"
ALPACA_MARKET_TYPE = "US"

AssetTypeReader = Callable[[Sequence[str]], Awaitable[Mapping[str, SymbolMaster]]]


@dataclass
class UsSurfaceReport(SurfaceReport):
    """KR's surface line plus the provider's own computation time (Alpaca)."""

    source_updated_at: str | None = None


def surface_key(source: str, session: str, side: str) -> str:
    return f"us:{source}:{session}:{side}"


def run_id_for(snapshot_at: dt.datetime) -> uuid.UUID:
    return uuid.uuid5(_RUN_NAMESPACE, snapshot_at.astimezone(dt.UTC).isoformat())


def us_session_label(
    now: dt.datetime,
    *,
    segment_of: Callable[[dt.datetime], str] | None = None,
) -> str | None:
    """``us_pre`` / ``us_regular`` / ``us_post``, or ``None`` (closed)."""
    segment = (
        segment_of(now)
        if segment_of is not None
        else session_segment_of("us", "NASD", now)
    )
    return _SESSION_BY_SEGMENT.get(segment)


def us_trading_date(now: dt.datetime) -> dt.date:
    """The ET calendar date, i.e. the US trading day of a collected window."""
    return now.astimezone(_ET).date()


# ------------------------------------------------------------- asset classes


async def load_symbol_masters(
    session: AsyncSession, symbols: Sequence[str]
) -> dict[str, SymbolMaster]:
    """One query over ``us_symbol_universe`` for the run's symbols."""
    wanted = sorted({symbol for symbol in symbols if symbol})
    if not wanted:
        return {}
    rows = await session.execute(
        select(
            USSymbolUniverse.symbol,
            USSymbolUniverse.security_type,
            USSymbolUniverse.leverage_factor,
            USSymbolUniverse.is_common_stock,
            USSymbolUniverse.name_en,
        ).where(USSymbolUniverse.symbol.in_(wanted))
    )
    return {
        str(row.symbol): SymbolMaster(
            security_type=row.security_type,
            leverage_factor=row.leverage_factor,
            is_common_stock=row.is_common_stock,
            name_en=row.name_en,
        )
        for row in rows.all()
    }


def _raw_payload(row: UsRow) -> dict[str, Any] | None:
    payload = dict(row.raw)
    if row.trade_value_basis:
        payload["trade_value_basis"] = row.trade_value_basis
    return payload or None


def us_momentum_upsert(
    row: UsRow,
    *,
    snapshot_at: dt.datetime,
    trading_date: dt.date,
    session: str,
    side: str,
    first_received_at: dt.datetime,
    master: SymbolMaster | None,
    master_lookup_failed: bool = False,
) -> MomentumEventUpsert:
    """One source row -> snapshot payload (asset class from the symbol master)."""
    reasons = dict(row.reasons)
    asset_type = ASSET_UNKNOWN
    if master is None:
        reasons["asset_type"] = (
            REASON_UNAVAILABLE
            if master_lookup_failed
            else REASON_NOT_IN_SYMBOL_UNIVERSE
        )
    else:
        asset_type = derive_asset_type(master)
        if asset_type == ASSET_UNKNOWN:
            reasons["asset_type"] = REASON_UNAVAILABLE
    return MomentumEventUpsert(
        snapshot_at=snapshot_at,
        trading_date=trading_date,
        source=row.source,
        surface=surface_key(row.source, session, side),
        market=MARKET,
        trade_type=TRADE_TYPE,
        market_type=row.market_type,
        order_type=side,
        rank=row.rank,
        symbol=row.symbol,
        name=row.name,
        price=row.price,
        change_amount=row.change_amount,
        change_rate=row.change_rate,
        volume=row.volume,
        trade_value=row.trade_value,
        asset_type=asset_type,
        first_received_at=first_received_at,
        field_reasons=reasons or None,
        raw_payload=_raw_payload(row),
    )


# -------------------------------------------------------------------- fetching

MAX_COLLECTION_SECONDS = 120.0


def _apply_kis_volume_floor(outcome: SurfaceOutcome) -> SurfaceOutcome:
    """Defensive client-side mirror of the ``VOL_RANG`` request floor (hk 6572).

    The provider filters server-side; this drops a returned row whose reported
    volume is *below* the floor anyway. A row with unreadable volume stays —
    NULL + reason is honest, dropping it would hide a provider gap.
    """
    if outcome.status != STATUS_OK:
        return outcome
    rows = tuple(
        row
        for row in outcome.rows
        if row.volume is None or row.volume >= KIS_MIN_VOLUME_SHARES
    )
    if len(rows) == len(outcome.rows):
        return outcome
    return SurfaceOutcome(outcome.status, outcome.reason, rows, outcome.source)


async def _fetch_alpaca(
    alpaca: AlpacaScreenerReader | None,
    *,
    clock: Callable[[], dt.datetime],
    top_movers: int,
    top_actives: int,
) -> list[tuple[str, SurfaceOutcome, dt.datetime, str | None]]:
    """Three Alpaca surfaces: up, down (one movers call) and active."""
    if alpaca is None:
        unavailable = SurfaceOutcome(STATUS_ERROR, REASON_SOURCE_UNAVAILABLE)
        received = clock()
        return [
            (SIDE_UP, unavailable, received, None),
            (SIDE_DOWN, unavailable, received, None),
            (SIDE_ACTIVE, unavailable, received, None),
        ]
    received = clock()
    try:
        payload = await alpaca.screener_movers(top=top_movers)
        received = clock()
        up = classify_alpaca_movers(payload, SIDE_UP)
        down = classify_alpaca_movers(payload, SIDE_DOWN)
        updated = payload.get("last_updated")
        updated_text = str(updated) if updated else None
    except Exception as exc:  # noqa: BLE001 - cancellation is BaseException
        logger.warning(
            "us movers alpaca movers fetch failed error=%s", type(exc).__name__
        )
        received = clock()
        error = SurfaceOutcome(STATUS_ERROR, classify_error(exc))
        up = down = error
        updated_text = None
    surfaces = [
        (SIDE_UP, up, received, updated_text),
        (SIDE_DOWN, down, received, updated_text),
    ]
    try:
        actives_payload = await alpaca.screener_most_actives(top=top_actives)
        received = clock()
        active = classify_alpaca_most_actives(actives_payload)
        updated = actives_payload.get("last_updated")
        surfaces.append(
            (SIDE_ACTIVE, active, received, str(updated) if updated else None)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "us movers alpaca actives fetch failed error=%s", type(exc).__name__
        )
        surfaces.append(
            (
                SIDE_ACTIVE,
                SurfaceOutcome(STATUS_ERROR, classify_error(exc)),
                clock(),
                None,
            )
        )
    return surfaces


async def _fetch_kis_side(
    kis: KisOverseasRankingReader | None,
    *,
    side: str,
    clock: Callable[[], dt.datetime],
) -> tuple[SurfaceOutcome, dt.datetime]:
    """One KIS side across every exchange; any exchange failure fails the side."""
    if kis is None:
        return SurfaceOutcome(STATUS_ERROR, REASON_SOURCE_UNAVAILABLE), clock()
    direction = KIS_GUBN_UP if side == SIDE_UP else KIS_GUBN_DOWN
    received = clock()
    try:
        outcomes: list[SurfaceOutcome] = []
        for exchange in KIS_EXCHANGES:
            payload = await kis.inquire_overseas_updown_rate(
                exchange,
                direction=direction,
                nday=KIS_NDAY_TODAY,
                vol_rang=KIS_VOL_RANG,
            )
            outcomes.append(
                _apply_kis_volume_floor(classify_kis_updown(payload, exchange))
            )
        received = clock()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "us movers kis fetch failed side=%s error=%s", side, type(exc).__name__
        )
        return SurfaceOutcome(STATUS_ERROR, classify_error(exc)), clock()
    failed = [outcome for outcome in outcomes if outcome.status == STATUS_ERROR]
    if failed:
        return SurfaceOutcome(STATUS_ERROR, failed[0].reason), received
    rows = tuple(row for outcome in outcomes for row in outcome.rows)
    if rows:
        return SurfaceOutcome(STATUS_OK, None, rows), received
    return SurfaceOutcome(STATUS_EMPTY, "no_rows"), received


async def _fetch_kis_ranking(
    kis: KisOverseasRankingReader | None,
    *,
    source: str,
    side: str,
    clock: Callable[[], dt.datetime],
) -> tuple[SurfaceOutcome, dt.datetime]:
    if kis is None:
        return SurfaceOutcome(STATUS_ERROR, REASON_SOURCE_UNAVAILABLE), clock()
    try:
        outcomes = []
        for exchange in KIS_EXCHANGES:
            payload = await kis.inquire_overseas_mover_ranking(
                source, exchange, side=side, vol_rang=KIS_VOL_RANG
            )
            outcomes.append(
                _apply_kis_volume_floor(
                    classify_kis_mover_ranking(
                        payload, exchange, source=source, side=side
                    )
                )
            )
        received = clock()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "us movers kis ranking failed source=%s side=%s error=%s",
            source,
            side,
            type(exc).__name__,
        )
        return SurfaceOutcome(STATUS_ERROR, classify_error(exc)), clock()
    failed = [item for item in outcomes if item.status == STATUS_ERROR]
    if failed:
        return SurfaceOutcome(STATUS_ERROR, failed[0].reason), received
    rows = tuple(row for item in outcomes for row in item.rows)
    return (
        SurfaceOutcome(STATUS_OK, None, rows)
        if rows
        else SurfaceOutcome(STATUS_EMPTY, "no_rows")
    ), received


async def _fetch_toss(
    toss: TossRankingReader | None, *, clock: Callable[[], dt.datetime]
):
    result = []
    for side, ranking_type in (
        (SIDE_UP, TOSS_RANKING_GAINERS),
        (SIDE_DOWN, TOSS_RANKING_LOSERS),
    ):
        if toss is None:
            result.append(
                (side, SurfaceOutcome(STATUS_ERROR, REASON_SOURCE_UNAVAILABLE), clock())
            )
            continue
        try:
            payload = await toss.rankings(
                ranking_type=ranking_type, market_country="US", duration="1d", count=50
            )
            outcome = classify_toss_rankings(payload, side=side)
            result.append((side, outcome, clock()))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "us movers toss ranking failed side=%s error=%s",
                side,
                type(exc).__name__,
            )
            result.append(
                (side, SurfaceOutcome(STATUS_ERROR, classify_error(exc)), clock())
            )
    if toss is None:
        result.append(
            (
                SIDE_ACTIVE,
                SurfaceOutcome(STATUS_ERROR, REASON_SOURCE_UNAVAILABLE),
                clock(),
            )
        )
    else:
        try:
            amount = await toss.rankings(
                ranking_type=TOSS_RANKING_AMOUNT,
                market_country="US",
                duration="realtime",
                count=50,
            )
            volume = await toss.rankings(
                ranking_type=TOSS_RANKING_VOLUME,
                market_country="US",
                duration="realtime",
                count=50,
            )
            result.append(
                (
                    SIDE_ACTIVE,
                    classify_toss_rankings(
                        amount, side=TOSS_RANKING_AMOUNT, volume_payload=volume
                    ),
                    clock(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "us movers toss activity ranking failed error=%s", type(exc).__name__
            )
            result.append(
                (
                    SIDE_ACTIVE,
                    SurfaceOutcome(STATUS_ERROR, classify_error(exc)),
                    clock(),
                )
            )
    return result


async def _fetch_benzinga(benzinga: Any | None, *, clock: Callable[[], dt.datetime]):
    result = []
    if benzinga is None:
        unavailable = SurfaceOutcome(STATUS_ERROR, REASON_SOURCE_UNAVAILABLE)
        return [(side, unavailable, clock()) for side in (SIDE_UP, SIDE_DOWN)]
    try:
        payload = await benzinga.market_movers()
        for side in (SIDE_UP, SIDE_DOWN):
            result.append((side, classify_benzinga_movers(payload, side), clock()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("us movers Benzinga fetch failed error=%s", type(exc).__name__)
        error = SurfaceOutcome(STATUS_ERROR, classify_error(exc))
        return [(side, error, clock()) for side in (SIDE_UP, SIDE_DOWN)]
    return result


async def _collect_surfaces(
    *,
    alpaca: AlpacaScreenerReader | None,
    kis: KisOverseasRankingReader | None,
    toss: TossRankingReader | None,
    benzinga: Any | None,
    clock: Callable[[], dt.datetime],
    top_movers: int,
    top_actives: int,
) -> list[tuple[str, str, SurfaceOutcome, dt.datetime, str | None]]:
    """``(source, side, outcome, first_received_at, source_updated_at)`` rows."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_COLLECTION_SECONDS

    async def within_budget(factory, fallback):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return fallback()
        try:
            return await asyncio.wait_for(factory(), timeout=remaining)
        except TimeoutError:
            return fallback()

    def timeout_outcome():
        return SurfaceOutcome(STATUS_ERROR, "timeout")

    surfaces: list[tuple[str, str, SurfaceOutcome, dt.datetime, str | None]] = []

    def alpaca_fallback():
        return [
            (side, timeout_outcome(), clock(), None)
            for side in (SIDE_UP, SIDE_DOWN, SIDE_ACTIVE)
        ]

    alpaca_results = await within_budget(
        lambda: _fetch_alpaca(
            alpaca, clock=clock, top_movers=top_movers, top_actives=top_actives
        ),
        alpaca_fallback,
    )
    for side, outcome, received, updated in alpaca_results:
        surfaces.append((SOURCE_ALPACA, side, outcome, received, updated))
    for side in (SIDE_UP, SIDE_DOWN):
        outcome, received = await within_budget(
            lambda side=side: _fetch_kis_side(kis, side=side, clock=clock),
            lambda: (timeout_outcome(), clock()),
        )
        surfaces.append((SOURCE_KIS, side, outcome, received, None))
    for source, sides in (
        (SOURCE_KIS_PRICE_FLUCT, (SIDE_UP, SIDE_DOWN)),
        (SOURCE_KIS_VOLUME_SURGE, (SIDE_ACTIVE,)),
        (SOURCE_KIS_NEW_HIGHLOW, (SIDE_UP, SIDE_DOWN)),
        (SOURCE_KIS_VOLUME_POWER, (SIDE_ACTIVE,)),
    ):
        for side in sides:
            outcome, received = await within_budget(
                lambda source=source, side=side: _fetch_kis_ranking(
                    kis, source=source, side=side, clock=clock
                ),
                lambda: (timeout_outcome(), clock()),
            )
            surfaces.append((source, side, outcome, received, None))

    def toss_fallback():
        return [(side, timeout_outcome(), clock()) for side in (SIDE_UP, SIDE_DOWN)]

    toss_results = await within_budget(
        lambda: _fetch_toss(toss, clock=clock), toss_fallback
    )
    for side, outcome, received in toss_results:
        surfaces.append((SOURCE_TOSS, side, outcome, received, None))

    def benzinga_fallback():
        return [(side, timeout_outcome(), clock()) for side in (SIDE_UP, SIDE_DOWN)]

    benzinga_results = await within_budget(
        lambda: _fetch_benzinga(benzinga, clock=clock), benzinga_fallback
    )
    for side, outcome, received in benzinga_results:
        surfaces.append((SOURCE_BENZINGA, side, outcome, received, None))
    return surfaces


# ------------------------------------------------------------------------- run


async def run_us_intraday_movers(
    *,
    alpaca: AlpacaScreenerReader | None,
    kis: KisOverseasRankingReader | None,
    toss: TossRankingReader | None = None,
    benzinga: Any | None = None,
    session: AsyncSession | None,
    now: dt.datetime,
    commit: bool,
    clock: Callable[[], dt.datetime] | None = None,
    asset_types: AssetTypeReader | None = None,
    top_movers: int = ALPACA_TOP_MOVERS,
    top_actives: int = ALPACA_TOP_ACTIVES,
    segment_of: Callable[[dt.datetime], str] | None = None,
) -> RunReport:
    """One US observation run. ``session`` is unused unless ``commit``."""
    tick = clock or (lambda: dt.datetime.now(dt.UTC))
    snapshot_at = now.astimezone(dt.UTC).replace(microsecond=0)
    report = RunReport(
        status="recorded" if commit else "dry_run",
        snapshot_at=snapshot_at,
        trading_date=us_trading_date(snapshot_at),
        run_id=run_id_for(snapshot_at),
    )
    session_label = us_session_label(snapshot_at, segment_of=segment_of)
    if session_label is None:
        report.status = "outside_session"
        return report

    collected = await _collect_surfaces(
        alpaca=alpaca,
        kis=kis,
        toss=toss,
        benzinga=benzinga,
        clock=tick,
        top_movers=top_movers,
        top_actives=top_actives,
    )

    load_masters = asset_types
    if commit and load_masters is None:
        assert session is not None

        async def _load(symbols: Sequence[str]) -> Mapping[str, SymbolMaster]:
            return await load_symbol_masters(session, symbols)

        load_masters = _load
    masters: dict[str, SymbolMaster] = {}
    master_lookup_failed = False
    if load_masters is not None:
        symbols = [
            row.symbol
            for _, _, outcome, _, _ in collected
            if outcome.status == STATUS_OK
            for row in outcome.rows
        ]
        try:
            masters = dict(await load_masters(symbols))
        except Exception as exc:  # noqa: BLE001 - rows stay asset_type unknown
            # A DB-level failure leaves the session in an aborted transaction;
            # without this rollback the first surface's write would raise
            # ``PendingRollbackError`` and lose that observation.
            if session is not None:
                await session.rollback()
            logger.warning(
                "us movers asset class lookup failed error=%s", type(exc).__name__
            )
            master_lookup_failed = True

    for source, side, outcome, received, updated in collected:
        surface = surface_key(source, session_label, side)
        # ``source_updated_at`` is Alpaca's own computation time, recorded so a
        # pre-open capture cannot be mistaken for live movement.
        line = UsSurfaceReport(
            surface=surface,
            status=outcome.status,
            reason=outcome.reason,
            row_count=len(outcome.rows),
            source_updated_at=updated,
        )
        report.surfaces.append(line)
        if not commit:
            continue
        assert session is not None
        repo = InvestMomentumEventSnapshotsRepository(session)
        try:
            created = await repo.insert_run_status(
                run_id=report.run_id,
                snapshot_at=snapshot_at,
                trading_date=report.trading_date,
                surface=surface,
                status=outcome.status,
                reason=outcome.reason,
                row_count=len(outcome.rows),
            )
            if not created:
                line.already_recorded = True
                await session.rollback()
                continue
            for row in outcome.rows:
                inserted = await repo.insert_momentum(
                    us_momentum_upsert(
                        row,
                        snapshot_at=snapshot_at,
                        trading_date=report.trading_date,
                        session=session_label,
                        side=side,
                        first_received_at=received,
                        master=masters.get(row.symbol),
                        master_lookup_failed=master_lookup_failed,
                    )
                )
                line.inserted_rows += 1 if inserted else 0
            await session.commit()
            line.written = True
        except Exception as exc:  # noqa: BLE001 - one surface must not stop the rest
            await session.rollback()
            line.db_error = True
            line.inserted_rows = 0
            logger.warning(
                "us movers write failed surface=%s error=%s",
                surface,
                type(exc).__name__,
            )
    return report


__all__ = [
    "ALPACA_MARKET_TYPE",
    "MARKET",
    "SESSION_POST",
    "SESSION_PRE",
    "SESSION_REGULAR",
    "SIDES",
    "SIDE_ACTIVE",
    "SIDE_DOWN",
    "SIDE_UP",
    "TRADE_TYPE",
    "UsSurfaceReport",
    "US_SESSIONS",
    "load_symbol_masters",
    "run_id_for",
    "run_us_intraday_movers",
    "surface_key",
    "us_momentum_upsert",
    "us_session_label",
    "us_trading_date",
]
