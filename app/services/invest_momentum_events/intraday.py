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
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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
from app.services.naver_stock.types import NaverStockRow

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_RUN_NAMESPACE = uuid.UUID("6e3f0c8a-6154-4d1e-9c55-0a8d3b1f6154")

TRADE_TYPE = "KRX"
MARKET_TYPE = "ALL"
#: rising, falling, trading value. Naver's falling ranking is ``down``
#: (probe 2026-11-24, see the hk 6154 PR body); ``low`` / ``lowTop`` are HTTP 400.
ORDER_TYPES = ("up", "down", "priceTop")
EXTENDED_ORDER_TYPES = ("up", "down")
PAGE_SIZE = 50
KIS_SOURCE = "kis_domestic_ranking"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"


class NaverRankingFetcher(Protocol):
    async def fetch_domestic_stock_default(self, **kwargs: Any) -> Any: ...


class KisRankingFetcher(Protocol):
    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict[str, Any]]: ...

    async def volume_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict[str, Any]]: ...


def surface_key(order_type: str) -> str:
    return f"stock:{TRADE_TYPE}:{MARKET_TYPE}:{order_type}"


def extended_surface_key(source: str, venue: str, session: str, order_type: str) -> str:
    return f"stock:{source}:{venue}:{session}:{MARKET_TYPE}:{order_type}"


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


def observation_window(now: dt.datetime) -> str | None:
    """Return the exact KR mover window containing ``now`` (KST, end exclusive)."""
    local = now.astimezone(_KST)
    clock = local.time()
    if KR_REGULAR_OPEN_KST <= clock < KR_REGULAR_CLOSE_KST:
        return "regular"
    if dt.time(8, 0) <= clock < dt.time(8, 50):
        return "nxt_pre"
    if dt.time(16, 0) <= clock < dt.time(20, 0):
        return "after"
    return None


# ------------------------------------------------------------------ classification


@dataclass(frozen=True)
class SurfaceOutcome:
    status: str
    reason: str | None
    rows: tuple[Any, ...] = ()
    source: str = "naver_stock"
    http_status: int | None = None


#: HTTP statuses that mean "this ranking combination is unsupported" — the only
#: ones allowed to trigger the KIS NX fallback (hk 7092). 429/401/403 and every
#: other client error stay Naver surface errors.
_NAVER_UNSUPPORTED_HTTP = frozenset({400, 404})


def classify_error(exc: BaseException) -> str:
    """Closed reason for a failed fetch. Never carries an exception message."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code >= 500:
            return "http_5xx"
        if code in (401, 403):
            return "auth"
        if code == 429:
            return "rate_limited"
        return "http_4xx"
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
    fetcher: NaverRankingFetcher, order_type: str, *, trade_type: str = TRADE_TYPE
) -> SurfaceOutcome:
    try:
        payload = await fetcher.fetch_domestic_stock_default(
            trade_type=trade_type,
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
        http_status = (
            exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        )
        return SurfaceOutcome(
            STATUS_ERROR, classify_error(exc), http_status=http_status
        )
    return classify_payload(payload)


#: KIS ``hts_avls`` is denominated in 100-million-KRW units, unlike the Naver
#: ``marketSum`` KRW figure stored in the same column (hk 7092).
_HTS_AVLS_UNIT_KRW = Decimal("100000000")


def _kr_short_code(value: Any) -> str | None:
    """KRX 6-character short code: digits or upper-case ASCII letters (hk 7092)."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) != 6:
        return None
    if not all(ch.isdigit() or "A" <= ch <= "Z" for ch in code):
        return None
    return code


def classify_kis_payload(payload: Any) -> SurfaceOutcome:
    """Normalize recorded KIS domestic ranking rows without float conversion."""
    if not isinstance(payload, list) or any(
        not isinstance(row, Mapping) for row in payload
    ):
        return SurfaceOutcome(STATUS_ERROR, "bad_response", source=KIS_SOURCE)
    if not payload:
        return SurfaceOutcome(STATUS_EMPTY, RUN_EMPTY_REASON, source=KIS_SOURCE)
    rows: list[NaverStockRow] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload, start=1):
        symbol = _kr_short_code(raw.get("stck_shrn_iscd") or raw.get("mksc_shrn_iscd"))
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        avls = _decimal_value(raw.get("hts_avls"))
        stored = dict(raw)
        stored["hts_avls_unit_krw"] = str(_HTS_AVLS_UNIT_KRW)
        rows.append(
            NaverStockRow(
                symbol=symbol,
                name=str(raw["hts_kor_isnm"]) if raw.get("hts_kor_isnm") else None,
                rank=index,
                price=_decimal_value(raw.get("stck_prpr")),
                change_amount=_decimal_value(raw.get("prdy_vrss")),
                change_rate=_decimal_value(raw.get("prdy_ctrt")),
                volume=_integer_value(raw.get("acml_vol")),
                trade_value=_decimal_value(raw.get("acml_tr_pbmn")),
                market_cap=(avls * _HTS_AVLS_UNIT_KRW if avls is not None else None),
                raw_payload=stored,
            )
        )
    if rows:
        return SurfaceOutcome(STATUS_OK, None, tuple(rows), KIS_SOURCE)
    return SurfaceOutcome(STATUS_ERROR, "unparseable", source=KIS_SOURCE)


def _decimal_value(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _integer_value(value: Any) -> int | None:
    number = _decimal_value(value)
    return (
        int(number)
        if number is not None and number == number.to_integral_value()
        else None
    )


async def fetch_kis_surface(
    fetcher: KisRankingFetcher | None, order_type: str
) -> SurfaceOutcome:
    if fetcher is None:
        return SurfaceOutcome(STATUS_ERROR, "source_unavailable", source=KIS_SOURCE)
    try:
        if order_type == "priceTop":
            payload = await fetcher.volume_rank(market="NX", limit=PAGE_SIZE)
        else:
            payload = await fetcher.fluctuation_rank(
                market="NX", direction=order_type, limit=PAGE_SIZE
            )
    except Exception as exc:  # noqa: BLE001 - exception text is never persisted
        return SurfaceOutcome(STATUS_ERROR, classify_error(exc), source=KIS_SOURCE)
    return classify_kis_payload(payload)


async def fetch_extended_surface(
    fetcher: NaverRankingFetcher,
    kis_fetcher: KisRankingFetcher | None,
    *,
    venue: str,
    order_type: str,
) -> SurfaceOutcome:
    outcome = await fetch_surface(fetcher, order_type, trade_type=venue)
    # Naver accepts a venue selector but does not expose an NX venue in all
    # rankings. Only its documented unsupported response (400/404) triggers the
    # KIS NX read; rate limits, auth failures and other errors stay Naver
    # surface errors (hk 7092).
    if (
        venue == "NXT"
        and outcome.status == STATUS_ERROR
        and outcome.reason == "http_4xx"
        and outcome.http_status in _NAVER_UNSUPPORTED_HTTP
    ):
        return await fetch_kis_surface(kis_fetcher, order_type)
    return outcome


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
    kis_fetcher: KisRankingFetcher | None = None,
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
    window = observation_window(snapshot_at)
    local_day = snapshot_at.astimezone(_KST).date()
    check = is_session_day or (lambda day: is_trading_session("kr", day))
    if window is None or not check(local_day):
        report.status = "outside_session"
        return report

    if window == "regular":
        plans = [(TRADE_TYPE, "regular", order_type) for order_type in order_types]
    elif window == "nxt_pre":
        plans = [("NXT", "nxt_pre", order_type) for order_type in EXTENDED_ORDER_TYPES]
    else:
        plans = [
            ("NXT", "nxt_post", order_type) for order_type in EXTENDED_ORDER_TYPES
        ] + [("KRX", "krx_after", order_type) for order_type in order_types]

    for venue, session_label, order_type in plans:
        outcome = (
            await fetch_surface(fetcher, order_type)
            if window == "regular"
            else await fetch_extended_surface(
                fetcher,
                kis_fetcher,
                venue=venue,
                order_type=order_type,
            )
        )
        surface = (
            surface_key(order_type)
            if window == "regular"
            else extended_surface_key(outcome.source, venue, session_label, order_type)
        )
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
                        trade_type=venue,
                        market_type=MARKET_TYPE,
                        order_type=order_type,
                        source=outcome.source,
                        surface=(
                            "domestic_market_stock_default"
                            if window == "regular"
                            else surface
                        ),
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


async def us_mover_rows_today(session: AsyncSession, trading_day: dt.date) -> list[Any]:
    """Every ``market='us'`` mover row of ``trading_day`` (one per symbol per run).

    hk 6554: the US promotion order needs every run's rank and dollar volume, not
    only the earliest capture ``first_captures`` keeps. Returns rows with
    ``symbol``, ``snapshot_at``, ``rank``, ``trade_value`` and ``asset_type``;
    scoring is ``research_candles.us_mover_score``. Read-only.
    """
    row = InvestMomentumEventSnapshot
    stmt = select(
        row.symbol,
        row.snapshot_at,
        row.rank,
        row.trade_value,
        row.asset_type,
    ).where(row.market == "us", row.trading_date == trading_day)
    if settings.SESSION_MINUTES_BENZINGA_PRO_MOVERS_ENABLED:
        stmt = stmt.where(row.order_type != "excluded")
    else:
        stmt = stmt.where(row.source != "benzinga_pro")
    result = await session.execute(stmt)
    return list(result.all())


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
                r.surface
                if r.market == "kr" and r.surface.startswith("stock:")
                else f"stock:{r.trade_type}:{r.market_type}:{r.order_type}"
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
