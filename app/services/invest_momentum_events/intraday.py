"""hk 6154: intraday KRX movers observation log (insert-only, manual job).

Every run fetches the KRX rising (``up``), falling (``down``) and trading-value
(``priceTop``) rankings from Naver, records the rows insert-only into
``invest_momentum_event_snapshots`` and one status row per (run, surface) into
``invest_momentum_event_runs``: ``ok`` / ``empty`` / ``error`` with a closed
reason. A fetch failure therefore never looks like an empty ranking, and an
``error`` surface never writes ranking rows.

Rules:

* The run writes only inside KRX regular hours (09:00-15:30 KST, end exclusive)
  on an XKRX trading day; otherwise it reports ``outside_session`` and does
  nothing (no fetch, no rows).
* A surface's rows and its status row are one transaction. The status row is
  inserted first (``ON CONFLICT DO NOTHING``); when it already exists for this
  ``snapshot_at`` the surface is ``already_recorded`` and nothing is written, so
  re-running a snapshot changes nothing.
* ``run_id`` is derived from ``snapshot_at`` (uuid5), so the same snapshot is the
  same run.
* No scheduler, no order/ledger/approval/broker-mutation import, and nothing here
  writes the screener pick log (hard rules 6, 13).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_momentum_event_snapshot import (
    RUN_EMPTY_REASON,
    InvestMomentumEventSnapshot,
)
from app.services.brokers.kis.live_order_expiry import (
    KR_REGULAR_CLOSE_KST,
    KR_REGULAR_OPEN_KST,
)
from app.services.invest_momentum_events.builder import momentum_upsert_from_row
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.market_events.session_calendar import is_trading_session
from app.services.naver_stock.parser import parse_domestic_stock_default

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_RUN_NAMESPACE = uuid.UUID("6e3f0c8a-6154-4d1e-9c55-0a8d3b1f6154")

TRADE_TYPE = "KRX"
MARKET_TYPE = "ALL"
#: rising, falling, trading value. Naver's falling ranking is ``down``
#: (probe 2026-11-24, see the hk 6154 PR body); ``low`` / ``lowTop`` are HTTP 400.
ORDER_TYPES = ("up", "down", "priceTop")
PAGE_SIZE = 50

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"


class NaverRankingFetcher(Protocol):
    async def fetch_domestic_stock_default(self, **kwargs: Any) -> Any: ...


def surface_key(order_type: str) -> str:
    return f"stock:{TRADE_TYPE}:{MARKET_TYPE}:{order_type}"


def run_id_for(snapshot_at: dt.datetime) -> uuid.UUID:
    return uuid.uuid5(_RUN_NAMESPACE, snapshot_at.astimezone(dt.UTC).isoformat())


# ---------------------------------------------------------------- session guard


def krx_session_open(
    now: dt.datetime, *, is_session_day: Callable[[dt.date], bool] | None = None
) -> bool:
    """True inside KRX regular hours on a confirmed XKRX trading day.

    Window is ``[09:00, 15:30)`` KST. The calendar check is fail-closed
    (``is_trading_session`` returns ``False`` for anything it cannot confirm).
    """
    local = now.astimezone(_KST)
    if not (KR_REGULAR_OPEN_KST <= local.time() < KR_REGULAR_CLOSE_KST):
        return False
    check = is_session_day or (lambda day: is_trading_session("kr", day))
    return bool(check(local.date()))


# ------------------------------------------------------------------ classification


@dataclass(frozen=True)
class SurfaceOutcome:
    status: str
    reason: str | None
    rows: tuple[Any, ...] = ()


def classify_error(exc: BaseException) -> str:
    """Closed reason for a failed fetch. Never carries an exception message."""
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_5xx" if exc.response.status_code >= 500 else "http_4xx"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "transport"
    if isinstance(exc, ValueError):  # response.json() on a non-JSON body
        return "bad_response"
    return "unexpected"


def _is_explicit_empty(payload: Any) -> bool:
    """True when the payload carries a ranking list and that list is empty."""
    if isinstance(payload, list):
        return len(payload) == 0
    if isinstance(payload, Mapping):
        lists = [v for v in payload.values() if isinstance(v, list)]
        if lists:
            return all(len(v) == 0 for v in lists)
        return any(_is_explicit_empty(v) for v in payload.values())
    return False


def classify_payload(payload: Any) -> SurfaceOutcome:
    """Naver payload -> ok / empty / error. Rows are distinct symbols in rank order."""
    if payload is None or not isinstance(payload, (list, Mapping)):
        return SurfaceOutcome(STATUS_ERROR, "bad_response")
    parsed = parse_domestic_stock_default(payload)
    seen: set[str] = set()
    rows = []
    for row in parsed.rows:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        rows.append(row)
    if rows:
        return SurfaceOutcome(STATUS_OK, None, tuple(rows))
    if _is_explicit_empty(payload):
        return SurfaceOutcome(STATUS_EMPTY, RUN_EMPTY_REASON)
    return SurfaceOutcome(STATUS_ERROR, "unparseable")


async def fetch_surface(
    fetcher: NaverRankingFetcher, order_type: str
) -> SurfaceOutcome:
    try:
        payload = await fetcher.fetch_domestic_stock_default(
            trade_type=TRADE_TYPE,
            market_type=MARKET_TYPE,
            order_type=order_type,
            start_idx=0,
            page_size=PAGE_SIZE,
        )
    except Exception as exc:  # noqa: BLE001 - cancellation is BaseException and propagates
        logger.warning(
            "intraday movers fetch failed surface=%s error=%s",
            surface_key(order_type),
            type(exc).__name__,
        )
        return SurfaceOutcome(STATUS_ERROR, classify_error(exc))
    return classify_payload(payload)


# --------------------------------------------------------------------------- run


@dataclass
class SurfaceReport:
    surface: str
    status: str
    reason: str | None
    row_count: int
    written: bool = False
    inserted_rows: int = 0
    already_recorded: bool = False
    db_error: bool = False


@dataclass
class RunReport:
    status: str  # "recorded" | "dry_run" | "outside_session"
    snapshot_at: dt.datetime
    trading_date: dt.date
    run_id: uuid.UUID
    surfaces: list[SurfaceReport] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(s.status == STATUS_ERROR or s.db_error for s in self.surfaces)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "snapshot_at": self.snapshot_at.isoformat(),
            "trading_date": self.trading_date.isoformat(),
            "run_id": str(self.run_id),
            "surfaces": [vars(s) for s in self.surfaces],
        }


async def run_intraday_movers(
    *,
    fetcher: NaverRankingFetcher,
    session: AsyncSession | None,
    now: dt.datetime,
    commit: bool,
    order_types: Sequence[str] = ORDER_TYPES,
    is_session_day: Callable[[dt.date], bool] | None = None,
) -> RunReport:
    """One observation run. ``session`` is unused unless ``commit``."""
    snapshot_at = now.astimezone(dt.UTC).replace(microsecond=0)
    trading_date = snapshot_at.astimezone(_KST).date()
    report = RunReport(
        status="recorded" if commit else "dry_run",
        snapshot_at=snapshot_at,
        trading_date=trading_date,
        run_id=run_id_for(snapshot_at),
    )
    if not krx_session_open(snapshot_at, is_session_day=is_session_day):
        report.status = "outside_session"
        return report

    for order_type in order_types:
        surface = surface_key(order_type)
        outcome = await fetch_surface(fetcher, order_type)
        line = SurfaceReport(
            surface=surface,
            status=outcome.status,
            reason=outcome.reason,
            row_count=len(outcome.rows),
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
                trading_date=trading_date,
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
                    momentum_upsert_from_row(
                        row,
                        snapshot_at=snapshot_at,
                        trading_date=trading_date,
                        trade_type=TRADE_TYPE,
                        market_type=MARKET_TYPE,
                        order_type=order_type,
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
                "intraday movers write failed surface=%s error=%s",
                surface,
                type(exc).__name__,
            )
    return report


# ------------------------------------------------------------------ first capture


@dataclass(frozen=True)
class FirstCapture:
    """A symbol's earliest ranking appearance of a trading day."""

    symbol: str
    name: str | None
    snapshot_at: dt.datetime
    surface: str
    rank: int
    change_rate: Any  # Decimal | None
    trade_value: Any  # Decimal | None


async def first_captures(
    session: AsyncSession, trading_date: dt.date, market: str = "kr"
) -> list[FirstCapture]:
    """Per symbol, the earliest snapshot of ``trading_date`` with its ranking facts.

    Computed at read time from ``invest_momentum_event_snapshots`` (no table).
    Ties on ``snapshot_at`` break by best (lowest) rank, then ``order_type`` and
    ``trade_type`` / ``market_type`` alphabetically, so the answer is stable.
    Result order: ``snapshot_at``, then ``symbol``.
    """
    row = InvestMomentumEventSnapshot
    stmt = (
        select(row)
        .where(row.market == market, row.trading_date == trading_date)
        .distinct(row.symbol)
        .order_by(
            row.symbol.asc(),
            row.snapshot_at.asc(),
            row.rank.asc(),
            row.order_type.asc(),
            row.trade_type.asc().nulls_last(),
            row.market_type.asc().nulls_last(),
        )
    )
    result = await session.execute(stmt)
    captures = [
        FirstCapture(
            symbol=r.symbol,
            name=r.name,
            snapshot_at=r.snapshot_at,
            # KR rows keep the computed Naver surface (hk 6154 output unchanged);
            # a US row's surface (``us:<source>:<session>:<side>``) is stored
            # because the session is not a column (hk 6510).
            surface=(
                f"stock:{r.trade_type}:{r.market_type}:{r.order_type}"
                if r.market == "kr"
                else r.surface
            ),
            rank=r.rank,
            change_rate=r.change_rate,
            trade_value=r.trade_value,
        )
        for r in result.scalars().all()
    ]
    captures.sort(key=lambda c: (c.snapshot_at, c.symbol))
    return captures
