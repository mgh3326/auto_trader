"""hk 6148: closed 1-minute futures bars -> ``research.kr_futures_candles_1m``.

Front and next contract of the closed product set (KOSPI200 futures, mini KOSPI200
futures) are resolved from the KIS futures master — never from guessed codes — and
their day-session 1-minute bars are written insert-only. ``public.*`` tables, orders,
ledgers and the 5939 mock order path are not touched.

Rules (same as the 6145 stock collector, design hk:doc design/2026-11-24/at-session-timeline):

* KIS labels a minute bar by its START in KST and the chart includes the forming
  current minute. A bar is kept only when ``bar_start + 1 min + 10 s <= observed_before``
  (read BEFORE the request goes out). ``[unverified]`` that ``stck_cntg_hour`` is a
  start label; the closed rule is conservative for either reading, the stored time is
  not, so the operator's first smoke must compare one bar with the exchange.
* ``retrieved_at`` is the first receipt: ``INSERT ... ON CONFLICT (time_utc, symbol)
  DO NOTHING`` — a re-run never touches an existing row.
* ``session_segment`` / ``session_date`` come from ``session_segment_of("futures", ...)``
  and ``futures_session_date``; an unconfirmed calendar gives ``UNKNOWN`` / NULL.
* ``roll_state`` is the contract's state on the bar's trading date (KST date when the
  trading date is unknown).
* Night session: the official KIS samples document no night quote/minute TR, so the
  report says ``night: "unsupported"`` — it is reported, never silently zero rows.
* No backfill, no scheduler. The KIS view is injected; only ``master`` and
  ``minute_bars`` are called.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.kis.futures_quotes import FuturesQuotesUnsupported
from app.services.kis_futures_quotes.contracts import (
    CLOSED_QUOTE_PRODUCTS,
    ResolvedContract,
    resolve_front_next,
    roll_state,
)
from app.services.research_candles.session_segment import (
    futures_session_date,
    session_segment_of,
)

logger = logging.getLogger(__name__)

SOURCE = "KIS"
TABLE = "research.kr_futures_candles_1m"
BAR = timedelta(minutes=1)
CLOSE_GRACE = timedelta(seconds=10)
BATCH_PREFIX = "futures-minutes"
NIGHT_UNSUPPORTED = "unsupported"

_KST = ZoneInfo("Asia/Seoul")


class QuotesReader(Protocol):
    """The only KIS surface the collector may touch (read-only)."""

    async def master(self) -> list[dict[str, str]]: ...

    async def minute_bars(
        self, symbol: str, *, session: str = "day", day: date
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class FuturesBar:
    time_utc: datetime  # bar START
    symbol: str
    product: str
    contract_month: str  # YYMM
    roll_state: str
    session_segment: str
    session_date: date | None
    source: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(slots=True)
class RowsResult:
    bars: list[FuturesBar] = field(default_factory=list)
    skipped_forming: int = 0
    skipped_invalid: int = 0


def is_closed(bar_start_utc: datetime, observed_before: datetime) -> bool:
    return bar_start_utc + BAR + CLOSE_GRACE <= observed_before


def _decimal(value: object) -> Decimal | None:
    try:
        if value is None or value == "":
            return None
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _start_utc(day_text: object, hour_text: object) -> datetime | None:
    try:
        stamp = datetime.strptime(f"{day_text}{hour_text}", "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=_KST, second=0).astimezone(UTC)


def bars_from_rows(
    rows: Sequence[dict[str, Any]],
    *,
    contract: ResolvedContract,
    observed_before: datetime,
) -> RowsResult:
    """KIS output2 rows (``stck_bsop_date`` + ``stck_cntg_hour`` KST start label) -> bars.

    Field names follow the official sample (``futs_oprc``/``futs_hgpr``/``futs_lwpr``/
    ``futs_prpr``/``cntg_vol``). ``acml_tr_pbmn`` is cumulative and is not stored.
    """
    result = RowsResult()
    seen: set[datetime] = set()
    for row in rows:
        start = _start_utc(row.get("stck_bsop_date"), row.get("stck_cntg_hour"))
        values = [
            _decimal(row.get(key))
            for key in ("futs_oprc", "futs_hgpr", "futs_lwpr", "futs_prpr", "cntg_vol")
        ]
        if start is None or any(v is None for v in values):
            result.skipped_invalid += 1
            continue
        if start in seen:
            continue
        seen.add(start)
        if not is_closed(start, observed_before):
            result.skipped_forming += 1
            continue
        trading_date = futures_session_date(start)
        on_date = trading_date or start.astimezone(_KST).date()
        o, h, low, c, vol = values  # type: ignore[misc]
        result.bars.append(
            FuturesBar(
                time_utc=start,
                symbol=contract.symbol,
                product=contract.product.code,
                contract_month=contract.yymm,
                roll_state=roll_state(
                    contract.contract_month, on_date, list(contract.listed_months)
                ),
                session_segment=session_segment_of("futures", "KRX", start),
                session_date=trading_date,
                source=SOURCE,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=vol,
            )
        )
    result.bars.sort(key=lambda bar: bar.time_utc)
    return result


class FuturesMinuteWriter(Protocol):
    async def insert(
        self, bars: Sequence[FuturesBar], *, batch_id: str, retrieved_at: datetime
    ) -> tuple[int, int]:
        """Return ``(inserted, already_present)``."""
        ...


_INSERT = text(
    f"""
    INSERT INTO {TABLE}
        (time_utc, symbol, product, contract_month, roll_state, session_segment,
         session_date, source, open, high, low, close, volume, retrieved_at, batch_id)
    VALUES
        (:time_utc, :symbol, :product, :contract_month, :roll_state,
         :session_segment, :session_date, :source, :open, :high, :low, :close,
         :volume, :retrieved_at, :batch_id)
    ON CONFLICT (time_utc, symbol) DO NOTHING
    """
)


class SqlFuturesMinuteWriter:
    """Insert-only writer; commits once per call (one contract)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self, bars: Sequence[FuturesBar], *, batch_id: str, retrieved_at: datetime
    ) -> tuple[int, int]:
        inserted = 0
        try:
            for bar in bars:
                outcome = await self._session.execute(
                    _INSERT,
                    {
                        "time_utc": bar.time_utc,
                        "symbol": bar.symbol,
                        "product": bar.product,
                        "contract_month": bar.contract_month,
                        "roll_state": bar.roll_state,
                        "session_segment": bar.session_segment,
                        "session_date": bar.session_date,
                        "source": bar.source,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "retrieved_at": retrieved_at,
                        "batch_id": batch_id,
                    },
                )
                inserted += 1 if (getattr(outcome, "rowcount", 0) or 0) > 0 else 0
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return inserted, len(bars) - inserted


def new_batch_id(now: datetime) -> str:
    return f"{BATCH_PREFIX}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


async def collect(
    *,
    view: QuotesReader,
    writer: FuturesMinuteWriter | None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    batch_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Resolve front/next contracts, read closed day bars, write. Night is reported
    as unsupported. ``writer`` is unused when ``dry_run``."""
    batch = batch_id or new_batch_id(clock())
    inserted = already_present = skipped_forming = skipped_invalid = 0
    would_write = reads = 0
    errors: list[dict[str, str]] = []
    contracts: list[ResolvedContract] = []
    unresolved: list[str] = []

    today = clock().astimezone(_KST).date()
    try:
        master = await view.master()
        contracts, unresolved = resolve_front_next(master, today, CLOSED_QUOTE_PRODUCTS)
    except Exception as exc:  # noqa: BLE001 - reported, nonzero exit
        logger.warning("futures master read failed error=%s", type(exc).__name__)
        errors.append({"stage": "master", "error_class": type(exc).__name__})
    for code in unresolved:
        errors.append({"product": code, "error_class": "UnresolvedProduct"})

    for contract in contracts:
        observed_before = clock()
        try:
            rows = await view.minute_bars(contract.short_code, session="day", day=today)
            reads += 1
            parsed = bars_from_rows(
                rows, contract=contract, observed_before=observed_before
            )
            skipped_forming += parsed.skipped_forming
            skipped_invalid += parsed.skipped_invalid
            if dry_run:
                would_write += len(parsed.bars)
            elif parsed.bars:
                assert writer is not None
                new, present = await writer.insert(
                    parsed.bars, batch_id=batch, retrieved_at=clock()
                )
                inserted += new
                already_present += present
        except Exception as exc:  # noqa: BLE001 - one contract must not stop the run
            logger.warning(
                "futures minutes read/write failed symbol=%s error=%s",
                contract.symbol,
                type(exc).__name__,
            )
            errors.append(
                {"symbol": contract.symbol, "error_class": type(exc).__name__}
            )

    night = NIGHT_UNSUPPORTED
    if contracts:
        try:
            await view.minute_bars(contracts[0].short_code, session="night", day=today)
        except FuturesQuotesUnsupported:
            night = NIGHT_UNSUPPORTED
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "night", "error_class": type(exc).__name__})
        else:  # pragma: no cover - a night TR would need its own collector rules
            night = "unexpected_rows_not_stored"

    return {
        "batch_id": batch,
        "dry_run": dry_run,
        "products_resolved": len({c.product.code for c in contracts}),
        "contracts": [c.symbol for c in contracts],
        "reads": reads,
        "inserted": inserted,
        "already_present": already_present,
        "skipped_forming": skipped_forming,
        "skipped_invalid": skipped_invalid,
        "would_write": would_write if dry_run else None,
        "night": night,
        "errors": len(errors),
        "error_details": errors,
    }
