"""Stage 2 KR daily-chart collection, isolated from the Stage 1 transport.

This module consumes the bounded Stage 1 read-only client.  It never extends
or changes that client: one ``ka10081`` response is normalized here and, when
explicitly committed, inserted only when the target candle key is absent.

The collection checkpoint is deliberately local to the collection node.  A
database commit happens before its success marker is advanced, so an
interruption can at worst repeat an idempotent insert on resume.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.kiwoom.chart_compare import (
    ChartKind,
    extract_rows,
    normalize_value,
)

logger = logging.getLogger(__name__)

SOURCE = "kiwoom_live"
VENUE = "KRX"
MIN_RATE_SECONDS = 0.5
MAX_BARS_PER_REQUEST = 600
VERIFY_RELATIVE_TOLERANCE = Decimal("0.001")

LIVE_GATE_ENV = "KIWOOM_LIVE_MARKETDATA_ENABLED"
COLLECT_GATE_ENV = "KIWOOM_STAGE2_COLLECT_ENABLED"
SCOPED_ENV_KEYS = (
    LIVE_GATE_ENV,
    COLLECT_GATE_ENV,
    "KIWOOM_LIVE_APP_KEY",
    "KIWOOM_LIVE_APP_SECRET",
)
_REQUIRED_CREDENTIAL_ENV_KEYS = (
    "KIWOOM_LIVE_APP_KEY",
    "KIWOOM_LIVE_APP_SECRET",
)
_KST = ZoneInfo("Asia/Seoul")


class KiwoomStage2CollectionDisabled(RuntimeError):
    """Raised before a Stage 2 request when either dispatch gate is closed."""


class KiwoomStage2ResponseError(RuntimeError):
    """A response envelope that cannot safely be treated as daily data."""


@dataclass(frozen=True, slots=True)
class KiwoomDailyCandle:
    symbol: str
    session_date: str
    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal


@dataclass(frozen=True, slots=True)
class StoredKrDailyCandle:
    """Read-only projection of an existing ``kr_candles_1d`` row."""

    symbol: str
    session_date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal


@dataclass(frozen=True, slots=True)
class SymbolFailure:
    symbol: str
    error_code: str


@dataclass(frozen=True, slots=True)
class VerificationMismatch:
    session_date: str
    field: str
    existing: Decimal
    kiwoom: Decimal
    relative_delta: Decimal


@dataclass(frozen=True, slots=True)
class VerificationResult:
    symbol: str
    common_dates: int
    mismatches: tuple[VerificationMismatch, ...]
    status: str


@dataclass(frozen=True, slots=True)
class CollectionResult:
    total_symbols: int
    processed_symbols: int
    rows_received: int
    rows_inserted: int
    rows_conflict_skipped: int
    invalid_rows: int
    failures: tuple[SymbolFailure, ...]
    verification: tuple[VerificationResult, ...]
    verification_failures: tuple[SymbolFailure, ...]
    resumed_from: str | None
    elapsed_seconds: float
    commit: bool

    @property
    def failed_symbols(self) -> tuple[str, ...]:
        return tuple(failure.symbol for failure in self.failures)

    @property
    def verification_mismatch_symbols(self) -> tuple[str, ...]:
        return tuple(
            result.symbol for result in self.verification if result.status == "MISMATCH"
        )


@dataclass(frozen=True, slots=True)
class _ParsedDailyPayload:
    rows: tuple[KiwoomDailyCandle, ...]
    invalid_rows: int


@dataclass(frozen=True, slots=True)
class _ResumeState:
    symbols_digest: str
    last_success_index: int
    last_success_symbol: str | None
    failed_symbols: tuple[str, ...]


class DailyChartClient(Protocol):
    async def fetch_daily_chart(
        self,
        *,
        symbol: str,
        base_dt: str,
        adjusted: bool = True,
    ) -> dict[str, Any]: ...


RowWriter = Callable[[Sequence[KiwoomDailyCandle]], Awaitable[int]]
SymbolLoader = Callable[[], Awaitable[list[str]]]
SampleSymbolLoader = Callable[[int], Awaitable[list[str]]]
ExistingRowsLoader = Callable[[str, int], Awaitable[list[StoredKrDailyCandle]]]
SleepFn = Callable[[float], Awaitable[None]]
AfterSuccess = Callable[[str], Awaitable[None] | None]


def load_scoped_env_file(path: Path) -> dict[str, str]:
    """Load only the scoped live-read keys, never printing their values."""

    if "prod" in path.name.casefold():
        raise ValueError(f"refusing to read a production env file: {path}")
    if not path.is_file():
        raise ValueError(f"env file not found: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in SCOPED_ENV_KEYS:
            values[key] = value.strip().strip('"').strip("'")

    missing = [key for key in _REQUIRED_CREDENTIAL_ENV_KEYS if not values.get(key)]
    if missing:
        raise ValueError("env file missing required keys: " + ", ".join(missing))
    return values


def arm_scoped_environment(*, env_file: Path, redis_url: str) -> None:
    """Apply the scoped key file and an operator-supplied isolated Redis URL."""

    if not str(redis_url).strip():
        raise ValueError("--redis-url must not be empty")
    values = load_scoped_env_file(env_file)
    # A missing gate must remain false even if a parent process happened to
    # carry an armed value.  Credentials are required above, so no inherited
    # credential can fill a gap in the scoped file either.
    for key in SCOPED_ENV_KEYS:
        os.environ[key] = values.get(key, "false" if key.endswith("ENABLED") else "")
    os.environ["REDIS_URL"] = str(redis_url).strip()


def assert_collection_enabled() -> None:
    """Fail closed before the collector can reach the Stage 1 client."""

    if os.getenv(LIVE_GATE_ENV) != "true":
        raise KiwoomStage2CollectionDisabled(
            "Kiwoom live market data is disabled; set "
            "KIWOOM_LIVE_MARKETDATA_ENABLED=true to arm read-only chart access."
        )
    if os.getenv(COLLECT_GATE_ENV) != "true":
        raise KiwoomStage2CollectionDisabled(
            "Kiwoom Stage 2 collection is disabled; set "
            "KIWOOM_STAGE2_COLLECT_ENABLED=true to arm KR daily collection."
        )


def _symbols_digest(symbols: Sequence[str]) -> str:
    canonical = "\n".join(symbols).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ResumeCheckpoint:
    """Atomic local state for a one-node serial collection run."""

    _VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_env_file(cls, env_file: Path) -> ResumeCheckpoint:
        return cls(env_file.with_name(f".{env_file.name}.stage2-kr-daily.resume.json"))

    def load(self, *, symbols: Sequence[str]) -> _ResumeState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = _ResumeState(
                symbols_digest=str(raw["symbols_digest"]),
                last_success_index=int(raw["last_success_index"]),
                last_success_symbol=(
                    str(raw["last_success_symbol"])
                    if raw.get("last_success_symbol") is not None
                    else None
                ),
                failed_symbols=tuple(
                    str(value) for value in raw.get("failed_symbols", [])
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Stage 2 resume checkpoint: {self.path}") from exc

        if raw.get("version") != self._VERSION:
            raise ValueError(f"unsupported Stage 2 resume checkpoint: {self.path}")
        if state.symbols_digest != _symbols_digest(symbols):
            raise ValueError(
                "resume checkpoint target symbols differ from this invocation; "
                "refusing to skip an unknown prefix"
            )
        if not -1 <= state.last_success_index < len(symbols):
            raise ValueError(f"invalid Stage 2 resume checkpoint: {self.path}")
        if state.last_success_index == -1:
            if state.last_success_symbol is not None:
                raise ValueError(f"invalid Stage 2 resume checkpoint: {self.path}")
        elif state.last_success_symbol != symbols[state.last_success_index]:
            raise ValueError(f"invalid Stage 2 resume checkpoint: {self.path}")
        if len(set(state.failed_symbols)) != len(state.failed_symbols) or any(
            symbol not in symbols for symbol in state.failed_symbols
        ):
            raise ValueError(f"invalid Stage 2 resume checkpoint: {self.path}")
        return state

    def save(self, state: _ResumeState) -> None:
        payload = {
            "version": self._VERSION,
            "symbols_digest": state.symbols_digest,
            "last_success_index": state.last_success_index,
            "last_success_symbol": state.last_success_symbol,
            "failed_symbols": list(state.failed_symbols),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class KiwoomDailyCandleRepository:
    """The narrow DB writer for Stage 2's no-overwrite candle policy."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def insert_missing(self, rows: Sequence[KiwoomDailyCandle]) -> int:
        if not rows:
            return 0
        payload = [
            {
                "time": row.time_utc,
                "symbol": row.symbol,
                "venue": VENUE,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
                "source": SOURCE,
            }
            for row in rows
        ]
        result = await self._session.execute(
            text(
                """
                INSERT INTO public.kr_candles_1d (
                    time, symbol, venue, open, high, low, close, volume, value, source
                ) VALUES (
                    :time, :symbol, :venue, :open, :high, :low, :close, :volume,
                    :value, :source
                )
                ON CONFLICT (time, symbol, venue) DO NOTHING
                """
            ),
            payload,
        )
        rowcount = getattr(result, "rowcount", 0)
        return max(int(rowcount or 0), 0)


class DatabaseStage2CollectionStore:
    """Short-lived DB operations; no connection spans broker waits."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active_symbols(self) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT symbol FROM public.kr_symbol_universe "
                    "WHERE is_active = TRUE ORDER BY symbol"
                )
            )
            return [str(symbol) for symbol in result.scalars().all()]

    async def insert_missing(self, rows: Sequence[KiwoomDailyCandle]) -> int:
        async with self._session_factory() as session:
            try:
                inserted = await KiwoomDailyCandleRepository(
                    session=session
                ).insert_missing(rows)
                await session.commit()
                return inserted
            except Exception:
                await session.rollback()
                raise

    async def sample_existing_symbols(self, limit: int) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT symbol FROM public.kr_candles_1d "
                    "WHERE venue = :venue ORDER BY symbol LIMIT :limit"
                ),
                {"venue": VENUE, "limit": int(limit)},
            )
            return [str(symbol) for symbol in result.scalars().all()]

    async def load_existing_rows(
        self, symbol: str, bars: int
    ) -> list[StoredKrDailyCandle]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, symbol, open, high, low, close, volume, value
                    FROM public.kr_candles_1d
                    WHERE symbol = :symbol AND venue = :venue
                    ORDER BY time DESC
                    LIMIT :bars
                    """
                ),
                {"symbol": symbol, "venue": VENUE, "bars": int(bars)},
            )
            records = result.mappings().all()

        rows: list[StoredKrDailyCandle] = []
        for record in records:
            raw_time = record["time"]
            if not isinstance(raw_time, datetime):
                continue
            timestamp = (
                raw_time.replace(tzinfo=UTC) if raw_time.tzinfo is None else raw_time
            )
            try:
                rows.append(
                    StoredKrDailyCandle(
                        symbol=str(record["symbol"]),
                        session_date=timestamp.astimezone(UTC).strftime("%Y%m%d"),
                        open=_as_decimal(record["open"]),
                        high=_as_decimal(record["high"]),
                        low=_as_decimal(record["low"]),
                        close=_as_decimal(record["close"]),
                        volume=_as_decimal(record["volume"]),
                        value=_as_decimal(record["value"]),
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        return rows


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _default_base_date() -> str:
    return datetime.now(UTC).astimezone(_KST).strftime("%Y%m%d")


def _normalize_symbols(symbols: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for supplied in symbols:
        for item in str(supplied).split(","):
            symbol = item.strip().upper()
            if not symbol:
                continue
            if len(symbol) != 6 or not symbol.isdigit():
                raise ValueError(
                    "KR daily collection symbols must be six-digit KRX codes; "
                    f"got {symbol!r}"
                )
            if symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
    return normalized


def _assert_collection_args(
    *, bars: int, rate_seconds: float, verify_sample: int
) -> None:
    if not 1 <= int(bars) <= MAX_BARS_PER_REQUEST:
        raise ValueError(
            f"--bars must be between 1 and {MAX_BARS_PER_REQUEST} for one ka10081 call"
        )
    if float(rate_seconds) < MIN_RATE_SECONDS:
        raise ValueError("--rate-seconds must be at least 0.5")
    if int(verify_sample) < 0:
        raise ValueError("--verify-sample must be zero or greater")


class _SerialPacer:
    def __init__(
        self,
        *,
        rate_seconds: float,
        sleep: SleepFn,
        monotonic: Callable[[], float],
    ) -> None:
        self._rate_seconds = rate_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_dispatch: float | None = None

    async def wait_for_turn(self) -> None:
        if self._last_dispatch is not None:
            remaining = self._rate_seconds - (self._monotonic() - self._last_dispatch)
            if remaining > 0:
                await self._sleep(remaining)
        self._last_dispatch = self._monotonic()


def _parse_response_code(payload: Mapping[str, Any]) -> None:
    raw_code = payload.get("return_code")
    if isinstance(raw_code, bool):
        raise KiwoomStage2ResponseError("daily chart returned an invalid return code")
    try:
        code = int(raw_code)
    except (TypeError, ValueError) as exc:
        raise KiwoomStage2ResponseError(
            "daily chart returned no usable return code"
        ) from exc
    if code != 0:
        raise KiwoomStage2ResponseError("daily chart returned a non-zero return code")


def _parse_daily_payload(
    *, symbol: str, payload: Mapping[str, Any], bars: int
) -> _ParsedDailyPayload:
    _parse_response_code(payload)
    parsed_by_date: dict[str, KiwoomDailyCandle] = {}
    invalid_rows = 0
    for raw in extract_rows(dict(payload), ChartKind.DAILY):
        try:
            session_date = str(raw.get("dt", "")).strip()
            if len(session_date) != 8 or not session_date.isdigit():
                raise ValueError("invalid session date")
            timestamp = datetime.strptime(session_date, "%Y%m%d").replace(tzinfo=UTC)
            open_value = normalize_value(raw.get("open_pric"), field_name="open_pric")
            high_value = normalize_value(raw.get("high_pric"), field_name="high_pric")
            low_value = normalize_value(raw.get("low_pric"), field_name="low_pric")
            close_value = normalize_value(raw.get("cur_prc"), field_name="cur_prc")
            volume_value = normalize_value(raw.get("trde_qty"), field_name="trde_qty")
            traded_value = normalize_value(
                raw.get("trde_prica"), field_name="trde_prica"
            )
            if (
                open_value is None
                or high_value is None
                or low_value is None
                or close_value is None
                or volume_value is None
            ):
                raise ValueError("missing daily OHLCV")
            value = (
                traded_value * Decimal("1000000")
                if traded_value is not None
                else close_value * volume_value
            )
            parsed_by_date[session_date] = KiwoomDailyCandle(
                symbol=symbol,
                session_date=session_date,
                time_utc=timestamp,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                volume=volume_value,
                value=value,
            )
        except (TypeError, ValueError, InvalidOperation):
            invalid_rows += 1

    ordered = tuple(parsed_by_date[key] for key in sorted(parsed_by_date))
    return _ParsedDailyPayload(rows=ordered[-bars:], invalid_rows=invalid_rows)


def _relative_delta(left: Decimal, right: Decimal) -> Decimal:
    denominator = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) / denominator


def compare_existing_rows(
    *, existing: Sequence[StoredKrDailyCandle], fetched: Sequence[KiwoomDailyCandle]
) -> VerificationResult:
    symbol = fetched[0].symbol if fetched else (existing[0].symbol if existing else "")
    existing_by_date = {row.session_date: row for row in existing}
    fetched_by_date = {row.session_date: row for row in fetched}
    common_dates = sorted(set(existing_by_date) & set(fetched_by_date))
    mismatches: list[VerificationMismatch] = []
    fields = ("open", "high", "low", "close", "volume", "value")
    for session_date in common_dates:
        stored = existing_by_date[session_date]
        kiwoom = fetched_by_date[session_date]
        for field in fields:
            existing_value = cast(Decimal, getattr(stored, field))
            kiwoom_value = cast(Decimal, getattr(kiwoom, field))
            delta = _relative_delta(existing_value, kiwoom_value)
            if delta > VERIFY_RELATIVE_TOLERANCE:
                mismatches.append(
                    VerificationMismatch(
                        session_date=session_date,
                        field=field,
                        existing=existing_value,
                        kiwoom=kiwoom_value,
                        relative_delta=delta,
                    )
                )
    status = (
        "NO_OVERLAP"
        if not common_dates
        else ("MATCH" if not mismatches else "MISMATCH")
    )
    return VerificationResult(
        symbol=symbol,
        common_dates=len(common_dates),
        mismatches=tuple(mismatches),
        status=status,
    )


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, KiwoomStage2ResponseError):
        return "broker_response_rejected"
    return type(exc).__name__


class KiwoomStage2DailyCollector:
    """Serial ka10081 collector with injectable I/O for offline testing."""

    def __init__(
        self,
        *,
        client: DailyChartClient,
        write_rows: RowWriter | None = None,
        load_active_symbols: SymbolLoader | None = None,
        sample_existing_symbols: SampleSymbolLoader | None = None,
        load_existing_rows: ExistingRowsLoader | None = None,
        sleep: SleepFn = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        after_success: AfterSuccess | None = None,
    ) -> None:
        self._client = client
        self._write_rows = write_rows
        self._load_active_symbols = load_active_symbols
        self._sample_existing_symbols = sample_existing_symbols
        self._load_existing_rows = load_existing_rows
        self._sleep = sleep
        self._monotonic = monotonic
        self._after_success = after_success

    async def _fetch(
        self,
        *,
        pacer: _SerialPacer,
        symbol: str,
        base_date: str,
        bars: int,
    ) -> _ParsedDailyPayload:
        await pacer.wait_for_turn()
        payload = await self._client.fetch_daily_chart(
            symbol=symbol,
            base_dt=base_date,
            adjusted=True,
        )
        return _parse_daily_payload(symbol=symbol, payload=payload, bars=bars)

    async def _run_verification(
        self,
        *,
        pacer: _SerialPacer,
        sample_count: int,
        base_date: str,
        bars: int,
    ) -> tuple[tuple[VerificationResult, ...], tuple[SymbolFailure, ...]]:
        if sample_count == 0:
            return (), ()
        if self._sample_existing_symbols is None or self._load_existing_rows is None:
            raise RuntimeError("verification requires existing candle readers")

        verification: list[VerificationResult] = []
        failures: list[SymbolFailure] = []
        for symbol in await self._sample_existing_symbols(sample_count):
            try:
                parsed = await self._fetch(
                    pacer=pacer,
                    symbol=symbol,
                    base_date=base_date,
                    bars=bars,
                )
                existing = await self._load_existing_rows(symbol, bars)
                result = compare_existing_rows(existing=existing, fetched=parsed.rows)
                verification.append(result)
                logger.info(
                    "Kiwoom Stage 2 verify symbol=%s status=%s common_dates=%d mismatches=%d",
                    symbol,
                    result.status,
                    result.common_dates,
                    len(result.mismatches),
                )
            except Exception as exc:  # noqa: BLE001 - per-symbol isolation
                failures.append(
                    SymbolFailure(symbol=symbol, error_code=_failure_code(exc))
                )
                logger.warning(
                    "Kiwoom Stage 2 verify failed symbol=%s error_code=%s",
                    symbol,
                    _failure_code(exc),
                )
        return tuple(verification), tuple(failures)

    async def collect(
        self,
        *,
        symbols: Sequence[str] | None,
        bars: int = MAX_BARS_PER_REQUEST,
        rate_seconds: float = 2.0,
        commit: bool = False,
        resume: bool = False,
        checkpoint: ResumeCheckpoint | None = None,
        verify_sample: int = 0,
        base_date: str | None = None,
    ) -> CollectionResult:
        _assert_collection_args(
            bars=bars, rate_seconds=rate_seconds, verify_sample=verify_sample
        )
        assert_collection_enabled()
        if resume and checkpoint is None:
            raise ValueError("--resume requires a local resume checkpoint")
        if commit and self._write_rows is None:
            raise RuntimeError("committed collection requires a candle writer")

        if symbols is None:
            if self._load_active_symbols is None:
                raise RuntimeError(
                    "collection without --symbols requires an active universe loader"
                )
            selected_symbols = _normalize_symbols(await self._load_active_symbols())
        else:
            selected_symbols = _normalize_symbols(symbols)
        if not selected_symbols:
            raise ValueError("kr_symbol_universe has no active symbols")

        started = self._monotonic()
        pacer = _SerialPacer(
            rate_seconds=float(rate_seconds),
            sleep=self._sleep,
            monotonic=self._monotonic,
        )
        effective_base_date = base_date or _default_base_date()
        verification, verification_failures = await self._run_verification(
            pacer=pacer,
            sample_count=int(verify_sample),
            base_date=effective_base_date,
            bars=int(bars),
        )

        state: _ResumeState | None = None
        resumed_from: str | None = None
        if commit and checkpoint is not None:
            if resume:
                state = checkpoint.load(symbols=selected_symbols)
                if state and state.last_success_symbol:
                    resumed_from = state.last_success_symbol
            if state is None:
                state = _ResumeState(
                    symbols_digest=_symbols_digest(selected_symbols),
                    last_success_index=-1,
                    last_success_symbol=None,
                    failed_symbols=(),
                )

        pending_indices: list[int]
        if state is not None and resume:
            retry_indices = [
                index
                for index, symbol in enumerate(selected_symbols)
                if symbol in set(state.failed_symbols)
            ]
            later_indices = list(
                range(max(state.last_success_index + 1, 0), len(selected_symbols))
            )
            pending_indices = list(dict.fromkeys([*retry_indices, *later_indices]))
        else:
            pending_indices = list(range(len(selected_symbols)))

        rows_received = 0
        rows_inserted = 0
        rows_conflict_skipped = 0
        invalid_rows = 0
        failures: list[SymbolFailure] = []
        for index in pending_indices:
            symbol = selected_symbols[index]
            try:
                parsed = await self._fetch(
                    pacer=pacer,
                    symbol=symbol,
                    base_date=effective_base_date,
                    bars=int(bars),
                )
                rows_received += len(parsed.rows)
                invalid_rows += parsed.invalid_rows
                inserted = 0
                if commit:
                    inserted = await cast(RowWriter, self._write_rows)(parsed.rows)
                    rows_inserted += inserted
                    rows_conflict_skipped += len(parsed.rows) - inserted
                    if state is not None and checkpoint is not None:
                        failed = tuple(
                            value for value in state.failed_symbols if value != symbol
                        )
                        state = replace(
                            state,
                            last_success_index=max(state.last_success_index, index),
                            last_success_symbol=symbol,
                            failed_symbols=failed,
                        )
                        checkpoint.save(state)
                if self._after_success is not None:
                    callback_result = self._after_success(symbol)
                    if isinstance(callback_result, Awaitable):
                        await callback_result
                elapsed = self._monotonic() - started
                remaining = max(len(selected_symbols) - (index + 1), 0)
                eta = max(float(rate_seconds), elapsed / max(index + 1, 1)) * remaining
                logger.info(
                    "Kiwoom Stage 2 daily [%d/%d] symbol=%s elapsed=%.1fs eta=%.1fs "
                    "rows=%d inserted=%d",
                    index + 1,
                    len(selected_symbols),
                    symbol,
                    elapsed,
                    eta,
                    len(parsed.rows),
                    inserted,
                )
            except Exception as exc:  # noqa: BLE001 - continue with other symbols
                failure = SymbolFailure(symbol=symbol, error_code=_failure_code(exc))
                failures.append(failure)
                if state is not None and checkpoint is not None:
                    state = replace(
                        state,
                        failed_symbols=tuple(
                            dict.fromkeys([*state.failed_symbols, symbol])
                        ),
                    )
                    checkpoint.save(state)
                logger.warning(
                    "Kiwoom Stage 2 daily failed symbol=%s error_code=%s",
                    symbol,
                    failure.error_code,
                )

        if commit and checkpoint is not None and not failures:
            checkpoint.clear()
        elapsed_seconds = self._monotonic() - started
        logger.info(
            "Kiwoom Stage 2 daily complete processed=%d/%d elapsed=%.1fs failures=%s",
            len(pending_indices),
            len(selected_symbols),
            elapsed_seconds,
            [failure.symbol for failure in failures],
        )
        return CollectionResult(
            total_symbols=len(selected_symbols),
            processed_symbols=len(pending_indices),
            rows_received=rows_received,
            rows_inserted=rows_inserted,
            rows_conflict_skipped=rows_conflict_skipped,
            invalid_rows=invalid_rows,
            failures=tuple(failures),
            verification=verification,
            verification_failures=verification_failures,
            resumed_from=resumed_from,
            elapsed_seconds=elapsed_seconds,
            commit=commit,
        )


def build_default_collector() -> KiwoomStage2DailyCollector:
    """Wire the operational collector after its scoped environment is loaded."""

    from app.core.db import AsyncSessionLocal
    from app.services.brokers.kiwoom.live_market_data import KiwoomLiveReadOnlyClient

    session_factory = cast(Callable[[], AsyncSession], AsyncSessionLocal)
    store = DatabaseStage2CollectionStore(session_factory=session_factory)
    return KiwoomStage2DailyCollector(
        client=KiwoomLiveReadOnlyClient.from_scoped_env(),
        write_rows=store.insert_missing,
        load_active_symbols=store.list_active_symbols,
        sample_existing_symbols=store.sample_existing_symbols,
        load_existing_rows=store.load_existing_rows,
    )
