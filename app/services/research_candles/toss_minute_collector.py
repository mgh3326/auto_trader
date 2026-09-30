"""#1086 event-triggered KR 1-minute candle collector (Toss -> research table).

Given ``(symbol, D0, D+1)`` this fetches both sessions' Toss 1-minute bars
(KRX regular plus the NXT pre/after-hours bars Toss returns) and writes them
to ``research.kr_candles_1m_toss`` idempotently.

Target table (director decision, #1086): Toss returns a combined KRX+NXT
minute product, so it goes to the venue-free ``research.kr_candles_1m_toss``
(migration ``20260804_toss_phase2``), never to ``research.kr_candles_1m``,
whose identity carries a KRX|NTX venue claim Toss cannot support.

Time convention (director decision, #1086): ``time_utc`` stores the raw Toss
``timestamp``, which openapi.json v1.2.19 documents as the bar END
(the bar covers ``[timestamp - 1 min, timestamp)``). This matches the existing
Toss loaders (``research/toss_phase2``), so a row can never be duplicated one
minute apart. Research that labels bars by START must read
``bar_start = time_utc - interval '1 minute'`` (see ``BAR_START_SQL``); the
#1054 v1.2 N4 entry rule is ``bar_start >= rcept_dt + 6 minutes``.

``session_segment`` is the same KST clock-time label the Phase-2 corpus uses
on ``time_utc``; it is never a venue claim. An unclassifiable timestamp stops
that symbol-day (recorded as a gap) instead of being stored as UNKNOWN, which
the table's CHECK does not admit anyway.

Write discipline: ``INSERT ... ON CONFLICT (time_utc, symbol) DO NOTHING``.
Existing rows always win (same rule as both existing loaders); a key that
already exists with different values is counted as a conflict and reported,
never overwritten. No DELETE, no UPDATE, no other table.

Gaps: when Toss does not answer for a session day (API/transport/contract
error, zero bars, no regular-session bar), a ``GapRecord`` is emitted to the
injected sink and returned in the result. There is no KIS fallback: the
target table's CHECK admits ``source = 'TOSS'`` only (director decision).

Nothing here registers a schedule or runs on import.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.toss.dto import TossMinuteCandle
from app.services.brokers.toss.errors import (
    TossApiErrorBase,
    TossApiResponseError,
    TossPaginationCapExceeded,
    TossResponseContractError,
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

TARGET_TABLE = "research.kr_candles_1m_toss"
SOURCE = "TOSS"
VALUE_SEMANTICS = "CLOSE_X_VOLUME_SYNTHETIC"
BATCH_PREFIX = "t1086-candles-1m"
#: Raw printed prices. An adjusted series is rewritten retroactively by every
#: later corporate action, so re-collecting the same bar could disagree with
#: the stored row; the unadjusted series is stable and idempotent.
ADJUSTED = False
#: SQL expression research must use to label a stored bar by its start.
BAR_START_SQL = "time_utc - interval '1 minute'"

#: Last Toss bar of a KST session day (NXT after-hours ends 20:00).
SESSION_DAY_LAST_BAR = time(20, 0)
#: A session day is collected only after this grace past its last bar, so a
#: still-forming minute can never be stored.
MATURITY_GRACE = timedelta(minutes=10)

SessionSegment = Literal["NXT_PRE", "KRX_REGULAR", "NXT_POST"]


class UnclassifiableSessionSegment(ValueError):
    """A bar outside the approved KST clock-time labels."""


def classify_session_segment(timestamp: datetime) -> SessionSegment:
    """KST clock-time label of a raw Toss ``timestamp``.

    Byte-for-byte the Phase-2 rule (``research/toss_phase2/collect.py``) so the
    shared table carries one labelling convention; a parity test pins it.
    """
    clock = timestamp.astimezone(KST).time()
    if time(8, 0) <= clock < time(9, 0):
        return "NXT_PRE"
    if time(9, 0) <= clock <= time(15, 30):
        return "KRX_REGULAR"
    if time(15, 30) < clock <= time(20, 0):
        return "NXT_POST"
    raise UnclassifiableSessionSegment(
        "session_segment_unclassifiable:" + timestamp.astimezone(KST).isoformat()
    )


def bar_start(time_utc: datetime) -> datetime:
    """Start of the bar stored at ``time_utc`` (Toss labels bars by END)."""
    return time_utc - timedelta(minutes=1)


def session_day_matured_at(session_date: date) -> datetime:
    return datetime.combine(session_date, SESSION_DAY_LAST_BAR, KST) + MATURITY_GRACE


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandleRow:
    time_utc: datetime
    session_date_kst: date
    symbol: str
    session_segment: SessionSegment
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal
    is_padding: bool
    retrieved_at: datetime
    batch_id: str

    @property
    def bar_start(self) -> datetime:
        return bar_start(self.time_utc)

    def values_key(self) -> tuple[Decimal, ...]:
        return (self.open, self.high, self.low, self.close, self.volume)


def candle_to_row(
    candle: TossMinuteCandle,
    *,
    symbol: str,
    retrieved_at: datetime,
    batch_id: str,
) -> CandleRow:
    if candle.currency != "KRW":
        raise TossResponseContractError(
            f"candles: KR bar currency {candle.currency!r} is not KRW"
        )
    timestamp_kst = candle.timestamp.astimezone(KST)
    return CandleRow(
        time_utc=candle.timestamp.astimezone(UTC),
        session_date_kst=timestamp_kst.date(),
        symbol=symbol,
        session_segment=classify_session_segment(candle.timestamp),
        open=candle.open_price,
        high=candle.high_price,
        low=candle.low_price,
        close=candle.close_price,
        volume=candle.volume,
        # Synthetic, not exchange-reported: the table pins VALUE_SEMANTICS.
        value=candle.close_price * candle.volume,
        is_padding=candle.volume == 0,
        retrieved_at=retrieved_at,
        batch_id=batch_id,
    )


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------


GapReason = Literal[
    "toss_api_error",
    "toss_transport_error",
    "toss_contract_error",
    "pagination_cap",
    "unclassifiable_session_segment",
    "no_bars",
    "no_regular_bars",
]


@dataclass(frozen=True)
class GapRecord:
    symbol: str
    session_date_kst: date
    reason: GapReason
    detail: str
    d0: date
    rcept_nos: tuple[str, ...]
    run_id: str
    recorded_at: datetime

    def to_json(self) -> str:
        payload = asdict(self)
        payload["session_date_kst"] = self.session_date_kst.isoformat()
        payload["d0"] = self.d0.isoformat()
        payload["recorded_at"] = self.recorded_at.isoformat()
        payload["rcept_nos"] = list(self.rcept_nos)
        payload["target_table"] = TARGET_TABLE
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class GapSink(Protocol):
    def record(self, gap: GapRecord) -> None: ...


class JsonlGapSink:
    """Append-only JSONL gap log; each record is flushed and fsynced.

    A durable DB gap table would need a migration, which #1086 does not add.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, gap: GapRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(gap.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())


class MemoryGapSink:
    def __init__(self) -> None:
        self.gaps: list[GapRecord] = []

    def record(self, gap: GapRecord) -> None:
        self.gaps.append(gap)


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteResult:
    attempted: int
    inserted: int
    existing_same: int
    existing_conflict: int
    conflict_keys: tuple[str, ...] = ()


class CandleWriter(Protocol):
    async def write(self, rows: Sequence[CandleRow]) -> WriteResult: ...


_INSERT_SQL = text(
    f"""
    INSERT INTO {TARGET_TABLE} (
        time_utc, session_date_kst, symbol, session_segment, source,
        open, high, low, close, volume, value, value_semantics,
        is_padding, pre_nxt, retrieved_at, batch_id
    )
    SELECT
        t.time_utc, t.session_date_kst, t.symbol, t.session_segment,
        '{SOURCE}', t.open, t.high, t.low, t.close, t.volume, t.value,
        '{VALUE_SEMANTICS}', t.is_padding, NULL, t.retrieved_at, t.batch_id
    FROM unnest(
        CAST(:time_utc AS timestamptz[]),
        CAST(:session_date_kst AS date[]),
        CAST(:symbol AS text[]),
        CAST(:session_segment AS text[]),
        CAST(:open AS numeric[]),
        CAST(:high AS numeric[]),
        CAST(:low AS numeric[]),
        CAST(:close AS numeric[]),
        CAST(:volume AS numeric[]),
        CAST(:value AS numeric[]),
        CAST(:is_padding AS boolean[]),
        CAST(:retrieved_at AS timestamptz[]),
        CAST(:batch_id AS text[])
    ) AS t(
        time_utc, session_date_kst, symbol, session_segment,
        open, high, low, close, volume, value,
        is_padding, retrieved_at, batch_id
    )
    ON CONFLICT (time_utc, symbol) DO NOTHING
    RETURNING time_utc
    """
)

_EXISTING_SQL = text(
    f"""
    SELECT time_utc, open, high, low, close, volume
    FROM {TARGET_TABLE}
    WHERE symbol = :symbol AND time_utc = ANY(CAST(:times AS timestamptz[]))
    """
)

_INSERT_PRIVILEGE_SQL = text(
    "SELECT has_table_privilege(current_user, :table, 'INSERT')"
)


_REGULAR_COVERAGE_SQL = text(
    f"""
    SELECT session_date_kst, count(*) AS bars
    FROM {TARGET_TABLE}
    WHERE symbol = :symbol
      AND session_date_kst = ANY(CAST(:days AS date[]))
      AND session_segment = 'KRX_REGULAR'
    GROUP BY session_date_kst
    """
)


async def regular_bar_counts(
    session: AsyncSession, symbol: str, days: Sequence[date]
) -> dict[date, int]:
    """Stored KRX_REGULAR bar count per session day (SELECT only)."""
    result = await session.execute(
        _REGULAR_COVERAGE_SQL, {"symbol": symbol, "days": list(days)}
    )
    return {row.session_date_kst: int(row.bars) for row in result}


async def assert_insert_privilege(session: AsyncSession) -> None:
    """Fail closed before any Toss call if the target is not writable."""
    allowed = (
        await session.execute(_INSERT_PRIVILEGE_SQL, {"table": TARGET_TABLE})
    ).scalar_one()
    if not allowed:
        raise PermissionError(f"current_user lacks INSERT on {TARGET_TABLE}")


class SqlCandleWriter:
    """Writes through the caller's AsyncSession; the caller commits."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def write(self, rows: Sequence[CandleRow]) -> WriteResult:
        if not rows:
            return WriteResult(0, 0, 0, 0)
        symbols = {row.symbol for row in rows}
        if len(symbols) != 1:
            raise ValueError("SqlCandleWriter.write takes one symbol per call")
        keys = [row.time_utc for row in rows]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate time_utc in one write batch")
        params: dict[str, list[Any]] = {
            "time_utc": keys,
            "session_date_kst": [row.session_date_kst for row in rows],
            "symbol": [row.symbol for row in rows],
            "session_segment": [row.session_segment for row in rows],
            "open": [row.open for row in rows],
            "high": [row.high for row in rows],
            "low": [row.low for row in rows],
            "close": [row.close for row in rows],
            "volume": [row.volume for row in rows],
            "value": [row.value for row in rows],
            "is_padding": [row.is_padding for row in rows],
            "retrieved_at": [row.retrieved_at for row in rows],
            "batch_id": [row.batch_id for row in rows],
        }
        inserted_keys = {
            value.astimezone(UTC)
            for value in (await self.session.execute(_INSERT_SQL, params)).scalars()
        }
        skipped = [row for row in rows if row.time_utc not in inserted_keys]
        conflicts: list[str] = []
        if skipped:
            result = await self.session.execute(
                _EXISTING_SQL,
                {"symbol": skipped[0].symbol, "times": [r.time_utc for r in skipped]},
            )
            existing = {
                record["time_utc"].astimezone(UTC): tuple(
                    Decimal(record[name])
                    for name in ("open", "high", "low", "close", "volume")
                )
                for record in result.mappings()
            }
            for row in skipped:
                if existing.get(row.time_utc) != row.values_key():
                    conflicts.append(row.time_utc.isoformat())
        return WriteResult(
            attempted=len(rows),
            inserted=len(inserted_keys),
            existing_same=len(skipped) - len(conflicts),
            existing_conflict=len(conflicts),
            conflict_keys=tuple(conflicts),
        )


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


class MinuteCandleClient(Protocol):
    async def collect_minute_candles(
        self,
        symbol: str,
        *,
        adjusted: bool,
        before: str,
        not_before: datetime,
        max_pages: int = ...,
        page_count: int = ...,
        pace: Callable[[], Awaitable[None]] | None = ...,
    ) -> list[TossMinuteCandle]: ...


@dataclass(frozen=True)
class CollectionRequest:
    """One symbol and the two KRX sessions to collect (D0 and D+1)."""

    symbol: str
    d0: date
    d1: date
    rcept_nos: tuple[str, ...] = ()
    cohorts: tuple[str, ...] = ()

    @property
    def ready_at(self) -> datetime:
        return session_day_matured_at(self.d1)

    @property
    def sessions(self) -> tuple[date, date]:
        return (self.d0, self.d1)


CollectionStatus = Literal["written", "partial_gap", "gap", "immature", "dry_run"]


@dataclass
class CollectionResult:
    request: CollectionRequest
    status: CollectionStatus
    bars_by_session: dict[str, dict[str, int]] = field(default_factory=dict)
    write: WriteResult | None = None
    gaps: list[GapRecord] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.request.symbol,
            "d0": self.request.d0.isoformat(),
            "d1": self.request.d1.isoformat(),
            "status": self.status,
            "bars_by_session": self.bars_by_session,
            "write": asdict(self.write) if self.write is not None else None,
            "gaps": [
                {"session_date_kst": g.session_date_kst.isoformat(), "reason": g.reason}
                for g in self.gaps
            ],
        }


def _error_detail(exc: BaseException) -> tuple[GapReason, str]:
    # Only closed, non-secret fields: exception class and the Toss error code.
    if isinstance(exc, TossResponseContractError):
        return "toss_contract_error", type(exc).__name__
    if isinstance(exc, TossApiResponseError):
        return (
            "toss_api_error",
            f"{type(exc).__name__}:{exc.status_code}:{exc.envelope.code}",
        )
    if isinstance(exc, TossApiErrorBase):
        return "toss_api_error", type(exc).__name__
    if isinstance(exc, httpx.HTTPError):
        return "toss_transport_error", type(exc).__name__
    if isinstance(exc, TossPaginationCapExceeded):
        return "pagination_cap", type(exc).__name__
    raise exc


class TossMinuteCollector:
    def __init__(
        self,
        *,
        client: MinuteCandleClient,
        writer: CandleWriter | None,
        gap_sink: GapSink,
        run_id: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        pace: Callable[[], Awaitable[None]] | None = None,
        max_pages: int = 16,
    ) -> None:
        self.client = client
        self.writer = writer
        self.gap_sink = gap_sink
        self.run_id = run_id
        self.batch_id = f"{BATCH_PREFIX}:{run_id}"
        self.now = now
        self.pace = pace
        self.max_pages = max_pages

    def _gap(
        self,
        request: CollectionRequest,
        session_date: date,
        reason: GapReason,
        detail: str,
    ) -> GapRecord:
        gap = GapRecord(
            symbol=request.symbol,
            session_date_kst=session_date,
            reason=reason,
            detail=detail,
            d0=request.d0,
            rcept_nos=request.rcept_nos,
            run_id=self.run_id,
            recorded_at=self.now(),
        )
        self.gap_sink.record(gap)
        logger.warning(
            "kr_candles_1m_toss gap symbol=%s session=%s reason=%s detail=%s",
            gap.symbol,
            gap.session_date_kst,
            gap.reason,
            gap.detail,
        )
        return gap

    async def collect(self, request: CollectionRequest) -> CollectionResult:
        if request.d1 <= request.d0:
            raise ValueError("d1 must be after d0")
        if self.now() < request.ready_at:
            # Not a gap: D+1 has not finished yet. The caller retries later.
            return CollectionResult(request=request, status="immature")

        before = datetime.combine(request.d1, SESSION_DAY_LAST_BAR, KST).isoformat()
        not_before = datetime.combine(request.d0, time(0, 0), KST)
        try:
            candles = await self.client.collect_minute_candles(
                request.symbol,
                adjusted=ADJUSTED,
                before=before,
                not_before=not_before,
                max_pages=self.max_pages,
                pace=self.pace,
            )
        except (TossApiErrorBase, httpx.HTTPError, TossPaginationCapExceeded) as exc:
            reason, detail = _error_detail(exc)
            gaps = [self._gap(request, d, reason, detail) for d in request.sessions]
            return CollectionResult(request=request, status="gap", gaps=gaps)

        retrieved_at = self.now()
        wanted = set(request.sessions)
        by_session: dict[date, list[CandleRow]] = {d: [] for d in request.sessions}
        gaps: list[GapRecord] = []
        broken: set[date] = set()
        for candle in candles:
            session_date = candle.timestamp.astimezone(KST).date()
            if session_date not in wanted or session_date in broken:
                continue
            try:
                row = candle_to_row(
                    candle,
                    symbol=request.symbol,
                    retrieved_at=retrieved_at,
                    batch_id=self.batch_id,
                )
            except UnclassifiableSessionSegment as exc:
                broken.add(session_date)
                by_session[session_date] = []
                gaps.append(
                    self._gap(
                        request,
                        session_date,
                        "unclassifiable_session_segment",
                        str(exc),
                    )
                )
                continue
            except TossResponseContractError as exc:
                broken.add(session_date)
                by_session[session_date] = []
                reason, detail = _error_detail(exc)
                gaps.append(self._gap(request, session_date, reason, detail))
                continue
            by_session[session_date].append(row)

        rows: list[CandleRow] = []
        counts: dict[str, dict[str, int]] = {}
        for session_date in request.sessions:
            session_rows = by_session[session_date]
            per_segment: dict[str, int] = {}
            for row in session_rows:
                per_segment[row.session_segment] = (
                    per_segment.get(row.session_segment, 0) + 1
                )
            counts[session_date.isoformat()] = per_segment
            if session_date in broken:
                continue
            if not session_rows:
                gaps.append(self._gap(request, session_date, "no_bars", "0 bars"))
                continue
            if "KRX_REGULAR" not in per_segment:
                # Pre/after-hours bars alone are stored, but the missing
                # regular session is still a recorded gap.
                gaps.append(
                    self._gap(
                        request, session_date, "no_regular_bars", "0 KRX_REGULAR bars"
                    )
                )
            rows.extend(session_rows)

        write: WriteResult | None = None
        if rows and self.writer is not None:
            write = await self.writer.write(rows)
            if write.existing_conflict:
                logger.warning(
                    "kr_candles_1m_toss existing rows differ symbol=%s count=%d; "
                    "existing rows kept",
                    request.symbol,
                    write.existing_conflict,
                )
        if not rows:
            status: CollectionStatus = "gap"
        elif gaps:
            status = "partial_gap"
        elif self.writer is None:
            status = "dry_run"
        else:
            status = "written"
        return CollectionResult(
            request=request,
            status=status,
            bars_by_session=counts,
            write=write,
            gaps=gaps,
        )
