"""Scheduleless, insert-only collection for NHPLUG live period quotes.

The collector deliberately lives outside ``live_quotes``: this module can
normalize rows, use the existing candle tables, and maintain a local resume
marker, but it cannot select an endpoint or a hostname.  Broker transport is
provided only by the bounded live quote client.

``indexfx`` intentionally has no writer.  A repository search found no
existing index/FX candle table with a compatible key, so this module emits a
schema proposal and preserves every fetched row in the result only.  It does
not create a table or migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, cast

from sqlalchemy import Column, DateTime, MetaData, Numeric, String, Table, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol, to_yahoo_symbol
from app.services.brokers.kiwoom.chart_compare import (
    FrozenBar,
    load_frozen_kis_sample,
)
from app.services.brokers.nhplug.live_quotes import (
    DEFAULT_RATE_SECONDS,
    LIVE_QUOTES_GATE_ENV,
    MAX_BARS_PER_REQUEST,
    MIN_RATE_SECONDS,
    NHPlugLiveQuotesClient,
)

logger = logging.getLogger(__name__)

SOURCE = "nhplug_live"
KR_VENUE = "KRX"
SUPPORTED_US_EXCHANGES: Final[frozenset[str]] = frozenset({"NASD", "NYSE", "AMEX"})
DEFAULT_BARS = 365
DEFAULT_INDEXFX_SYMBOLS: tuple[str, ...] = ()
SCOPED_ENV_KEYS: tuple[str, ...] = (
    LIVE_QUOTES_GATE_ENV,
    "NHPLUG_LIVE_APP_KEY",
    "NHPLUG_LIVE_APP_SECRET",
)
REQUIRED_CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "NHPLUG_LIVE_APP_KEY",
    "NHPLUG_LIVE_APP_SECRET",
)
INDEXFX_SCHEMA_PROPOSAL: tuple[str, ...] = (
    "No existing index/FX daily-candle table was found; no rows were written.",
    "Propose a separate source-aware daily series table keyed by "
    "(time, symbol, series_kind) with OHLCV and source fields.",
    "Require a dedicated migration and review before indexfx persistence.",
)

_KR_CANDLES_1D = Table(
    "kr_candles_1d",
    MetaData(),
    Column("time", DateTime(timezone=True)),
    Column("symbol", String),
    Column("venue", String),
    Column("open", Numeric),
    Column("high", Numeric),
    Column("low", Numeric),
    Column("close", Numeric),
    Column("volume", Numeric),
    Column("value", Numeric),
    Column("source", String),
    schema="public",
)
_US_CANDLES_1D = Table(
    "us_candles_1d",
    MetaData(),
    Column("time", DateTime(timezone=True)),
    Column("symbol", String),
    Column("exchange", String),
    Column("open", Numeric),
    Column("high", Numeric),
    Column("low", Numeric),
    Column("close", Numeric),
    Column("adj_close", Numeric),
    Column("volume", Numeric),
    Column("value", Numeric),
    Column("source", String),
    schema="public",
)


class NHPlugPeriodCollectionDisabled(RuntimeError):
    """Raised before collection reaches the quote client while the gate is closed."""


class NHPlugPeriodResponseError(RuntimeError):
    """A response that cannot safely be normalized as daily quote data."""


@dataclass(frozen=True, slots=True)
class PeriodTarget:
    symbol: str
    exchange: str | None = None

    @property
    def checkpoint_key(self) -> str:
        return f"{self.symbol}|{self.exchange or ''}"


@dataclass(frozen=True, slots=True)
class PeriodCandle:
    symbol: str
    session_date: str
    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal
    exchange: str | None = None


@dataclass(frozen=True, slots=True)
class StoredKiwoomCandle:
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
class SymbolFailure:
    symbol: str
    error_code: str


class VerificationClassification(StrEnum):
    MATCH = "MATCH"
    NHPLUG_MATCHES_KIS = "NHPLUG_MATCHES_KIS"
    KIWOOM_MATCHES_KIS = "KIWOOM_MATCHES_KIS"
    BOTH_DIVERGE_FROM_KIS = "BOTH_DIVERGE_FROM_KIS"
    UNDETERMINED = "UNDETERMINED"
    FROZEN_SAMPLE_UNAVAILABLE = "FROZEN_SAMPLE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    symbol: str
    common_dates: int
    mismatch_dates: tuple[str, ...]
    classification: VerificationClassification


@dataclass(frozen=True, slots=True)
class CollectionResult:
    market: str
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
    persistence_status: str

    @property
    def failed_symbols(self) -> tuple[str, ...]:
        return tuple(failure.symbol for failure in self.failures)


@dataclass(frozen=True, slots=True)
class _ParsedRows:
    rows: tuple[PeriodCandle, ...]
    invalid_rows: int


@dataclass(frozen=True, slots=True)
class _ResumeState:
    market: str
    targets_digest: str
    last_success_index: int
    last_success_key: str | None
    failed_keys: tuple[str, ...]


class PeriodQuoteClient(Protocol):
    async def fetch_kr_period(
        self, *, symbol: str, end_date: str, bars: int
    ) -> dict[str, Any]: ...

    async def fetch_us_period(
        self, *, symbol: str, end_date: str, bars: int
    ) -> dict[str, Any]: ...

    async def fetch_index_fx_period(
        self, *, symbol: str, end_date: str, bars: int
    ) -> dict[str, Any]: ...


class PeriodCollectionStore(Protocol):
    async def list_active_kr_symbols(self) -> list[str]: ...

    async def list_active_us_targets(self) -> list[tuple[str, str]]: ...

    async def resolve_us_symbols(
        self, symbols: Sequence[str]
    ) -> list[tuple[str, str]]: ...

    async def insert_missing_kr(self, rows: Sequence[PeriodCandle]) -> int: ...

    async def insert_missing_us(self, rows: Sequence[PeriodCandle]) -> int: ...

    async def sample_kiwoom_symbols(self, limit: int) -> list[str]: ...

    async def load_kiwoom_rows(
        self, symbol: str, bars: int
    ) -> list[StoredKiwoomCandle]: ...


AfterSuccess = Callable[[str], Awaitable[None] | None]
SleepFn = Callable[[float], Awaitable[None]]
FrozenLoader = Callable[[], dict[tuple[str, str], FrozenBar]]


def _gate_enabled() -> bool:
    return os.getenv(LIVE_QUOTES_GATE_ENV, "").strip().lower() == "true"


def assert_collection_enabled() -> None:
    if not _gate_enabled():
        raise NHPlugPeriodCollectionDisabled(
            "NHPLUG live period quotes are disabled; set "
            "NHPLUG_LIVE_QUOTES_ENABLED=true"
        )


def _assert_env_file_is_private(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("env file must be a regular file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("env file mode must be 0600")


def load_scoped_env_file(path: Path) -> dict[str, str]:
    """Read a dedicated 0600 credential file without exposing any values."""

    if "prod" in path.name.casefold():
        raise ValueError("refusing to read a production env file")
    _assert_env_file_is_private(path)
    values: dict[str, str] = {}
    unexpected: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("env file contains a malformed line")
        key, _, value = line.partition("=")
        normalized_key = key.strip()
        if normalized_key not in SCOPED_ENV_KEYS:
            unexpected.append(normalized_key)
            continue
        values[normalized_key] = value.strip().strip('"').strip("'")
    if unexpected:
        raise ValueError(
            "env file contains non-NHPLUG-live keys: " + ", ".join(sorted(unexpected))
        )
    missing = [key for key in REQUIRED_CREDENTIAL_ENV_KEYS if not values.get(key)]
    if missing:
        raise ValueError("env file missing required keys: " + ", ".join(missing))
    return values


def arm_scoped_environment(*, env_file: Path) -> None:
    """Erase inherited values before applying only the three scoped keys."""

    values = load_scoped_env_file(env_file)
    for key in SCOPED_ENV_KEYS:
        default = "false" if key == LIVE_QUOTES_GATE_ENV else ""
        os.environ[key] = values.get(key, default)


def _digest_targets(targets: Sequence[PeriodTarget]) -> str:
    raw = "\n".join(target.checkpoint_key for target in targets).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ResumeCheckpoint:
    """Atomic local progress marker; only committed success advances it."""

    _VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_env_file(cls, *, env_file: Path, market: str) -> ResumeCheckpoint:
        return cls(env_file.with_name(f".{env_file.name}.nhplug-{market}.resume.json"))

    def load(
        self, *, market: str, targets: Sequence[PeriodTarget]
    ) -> _ResumeState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = _ResumeState(
                market=str(raw["market"]),
                targets_digest=str(raw["targets_digest"]),
                last_success_index=int(raw["last_success_index"]),
                last_success_key=(
                    str(raw["last_success_key"])
                    if raw.get("last_success_key") is not None
                    else None
                ),
                failed_keys=tuple(str(item) for item in raw.get("failed_keys", [])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid NHPLUG period resume checkpoint") from exc
        if raw.get("version") != self._VERSION:
            raise ValueError("unsupported NHPLUG period resume checkpoint")
        if state.market != market or state.targets_digest != _digest_targets(targets):
            raise ValueError(
                "resume checkpoint target set differs from this invocation"
            )
        if not -1 <= state.last_success_index < len(targets):
            raise ValueError("invalid NHPLUG period resume checkpoint")
        if state.last_success_index == -1:
            if state.last_success_key is not None:
                raise ValueError("invalid NHPLUG period resume checkpoint")
        elif state.last_success_key != targets[state.last_success_index].checkpoint_key:
            raise ValueError("invalid NHPLUG period resume checkpoint")
        known_keys = {target.checkpoint_key for target in targets}
        if len(set(state.failed_keys)) != len(state.failed_keys) or any(
            key not in known_keys for key in state.failed_keys
        ):
            raise ValueError("invalid NHPLUG period resume checkpoint")
        return state

    def save(self, state: _ResumeState) -> None:
        payload = {
            "version": self._VERSION,
            "market": state.market,
            "targets_digest": state.targets_digest,
            "last_success_index": state.last_success_index,
            "last_success_key": state.last_success_key,
            "failed_keys": list(state.failed_keys),
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


def _assert_date(value: str, *, name: str) -> datetime:
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise ValueError(f"{name} must be YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"{name} must be a calendar date") from exc


def _assert_collection_args(
    *,
    market: str,
    start_date: str,
    end_date: str,
    bars: int,
    rate_seconds: float,
    verify_sample: int,
) -> tuple[datetime, datetime, int]:
    if market not in {"kr", "us", "indexfx"}:
        raise ValueError("market must be kr, us, or indexfx")
    start = _assert_date(start_date, name="start_date")
    end = _assert_date(end_date, name="end_date")
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    if isinstance(bars, bool) or not isinstance(bars, int):
        raise ValueError("bars must be an integer")
    if not 1 <= bars <= MAX_BARS_PER_REQUEST:
        raise ValueError(f"bars must be between 1 and {MAX_BARS_PER_REQUEST}")
    if float(rate_seconds) < MIN_RATE_SECONDS:
        raise ValueError(f"--rate-seconds must be at least {MIN_RATE_SECONDS}")
    if isinstance(verify_sample, bool) or int(verify_sample) < 0:
        raise ValueError("--verify-sample must be zero or greater")
    # The broker has no start-date field for these daily calls.  The explicit
    # count is increased to cover the requested calendar window, then rows are
    # filtered locally.  That avoids a server-default short lookback and the
    # resulting weekend/holiday zero-row trap.
    requested_count = max(bars, (end.date() - start.date()).days + 1)
    if requested_count > MAX_BARS_PER_REQUEST:
        raise ValueError("requested date window exceeds one period request")
    return start, end, requested_count


def _normalize_kr_symbols(values: Sequence[str]) -> list[PeriodTarget]:
    seen: set[str] = set()
    targets: list[PeriodTarget] = []
    for supplied in values:
        for raw in str(supplied).split(","):
            symbol = raw.strip()
            if not symbol:
                continue
            if len(symbol) != 6 or not symbol.isdigit():
                raise ValueError(f"invalid KR symbol: {symbol!r}")
            if symbol not in seen:
                seen.add(symbol)
                targets.append(PeriodTarget(symbol=symbol))
    return targets


def _normalize_us_targets(values: Sequence[tuple[str, str]]) -> list[PeriodTarget]:
    seen: set[tuple[str, str]] = set()
    targets: list[PeriodTarget] = []
    for raw_symbol, raw_exchange in values:
        symbol = to_db_symbol(str(raw_symbol).strip().upper())
        exchange = str(raw_exchange).strip().upper()
        key = (symbol, exchange)
        if not symbol or exchange not in SUPPORTED_US_EXCHANGES:
            raise ValueError(f"invalid US target: {(raw_symbol, raw_exchange)!r}")
        if key not in seen:
            seen.add(key)
            targets.append(PeriodTarget(symbol=symbol, exchange=exchange))
    return targets


def _normalize_indexfx_symbols(values: Sequence[str]) -> list[PeriodTarget]:
    seen: set[str] = set()
    targets: list[PeriodTarget] = []
    for supplied in values:
        for raw in str(supplied).split(","):
            symbol = raw.strip().upper()
            if not symbol:
                continue
            if len(symbol) > 15 or any(character.isspace() for character in symbol):
                raise ValueError(f"invalid index/FX symbol: {symbol!r}")
            if symbol not in seen:
                seen.add(symbol)
                targets.append(PeriodTarget(symbol=symbol))
    return targets


def _response_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_rows = payload.get("Output_1")
    if raw_rows is None:
        message = payload.get("message")
        message_parts = [
            str(value).casefold()
            for value in (payload.get("rsp_msg"),)
            if isinstance(value, str)
        ]
        if isinstance(message, Mapping):
            message_parts.extend(
                str(value).casefold()
                for value in message.values()
                if isinstance(value, str)
            )
        message_text = " ".join(message_parts)
        if any(
            marker in message_text
            for marker in ("error", "fail", "invalid", "오류", "실패", "입력")
        ):
            raise NHPlugPeriodResponseError("period response indicated a broker error")
        if any(
            marker in message_text
            for marker in ("no data", "no result", "0건", "없음", "없습니다")
        ):
            return []
        # The vendor documents omitted blocks as valid zero-row responses only
        # when the response message establishes that fact.  A missing block
        # without that witness is not silently converted into a successful
        # zero-row collection.
        raise NHPlugPeriodResponseError(
            "period response omitted Output_1 without a no-data message"
        )
    if not isinstance(raw_rows, list):
        raise NHPlugPeriodResponseError("period response Output_1 was not a list")
    return [row for row in raw_rows if isinstance(row, Mapping)]


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid {field}")
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid {field}")
    return parsed


def _session_date(value: object) -> tuple[str, datetime]:
    normalized = str(value or "").strip()
    if len(normalized) != 8 or not normalized.isdigit():
        raise ValueError("invalid session date")
    try:
        timestamp = datetime.strptime(normalized, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("invalid session date") from exc
    return normalized, timestamp


def _parse_rows(
    *,
    market: str,
    target: PeriodTarget,
    payload: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> _ParsedRows:
    parsed: dict[str, PeriodCandle] = {}
    invalid_rows = 0
    for row in _response_rows(payload):
        try:
            if market == "kr":
                # krstockQuotePeriod `Output_1` daily OHLCV field names.
                date, timestamp = _session_date(row.get("bsop_date"))
                open_value = _decimal(row.get("stck_oprc"), field="stck_oprc")
                high_value = _decimal(row.get("stck_hgpr"), field="stck_hgpr")
                low_value = _decimal(row.get("stck_lwpr"), field="stck_lwpr")
                close_value = _decimal(row.get("stck_prpr"), field="stck_prpr")
                volume_value = _decimal(row.get("vol"), field="vol")
                raw_value = row.get("tr_pbmn")
            elif market == "us":
                date, timestamp = _session_date(
                    row.get("trade_date") or row.get("bsop_date")
                )
                open_value = _decimal(row.get("open_prc"), field="open_prc")
                high_value = _decimal(row.get("high"), field="high")
                low_value = _decimal(row.get("low"), field="low")
                close_value = _decimal(row.get("close_prc"), field="close_prc")
                volume_value = _decimal(row.get("movolume"), field="movolume")
                raw_value = row.get("movalue")
            else:
                date, timestamp = _session_date(row.get("bsop_date"))
                open_value = _decimal(row.get("ovrs_oprc"), field="ovrs_oprc")
                high_value = _decimal(row.get("ovrs_hgpr"), field="ovrs_hgpr")
                low_value = _decimal(row.get("ovrs_lwpr"), field="ovrs_lwpr")
                close_value = _decimal(row.get("ovrs_prpr"), field="ovrs_prpr")
                volume_value = _decimal(row.get("vol"), field="vol")
                raw_value = None
            if timestamp < start or timestamp > end:
                continue
            value = (
                _decimal(raw_value, field="value")
                if raw_value not in {None, ""}
                else close_value * volume_value
            )
            if min(open_value, high_value, low_value, close_value, volume_value) < 0:
                raise ValueError("negative daily quote value")
            parsed[date] = PeriodCandle(
                symbol=target.symbol,
                session_date=date,
                time_utc=timestamp,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                volume=volume_value,
                value=value,
                exchange=target.exchange,
            )
        except (TypeError, ValueError, InvalidOperation):
            invalid_rows += 1
    return _ParsedRows(
        rows=tuple(parsed[date] for date in sorted(parsed, reverse=True)),
        invalid_rows=invalid_rows,
    )


def _same_ohlcv(left: PeriodCandle | StoredKiwoomCandle, right: object) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in ("open", "high", "low", "close", "volume")
    )


def _classify_verification(
    *,
    symbol: str,
    nhplug_rows: Sequence[PeriodCandle],
    kiwoom_rows: Sequence[StoredKiwoomCandle],
    frozen: Mapping[tuple[str, str], FrozenBar] | None,
) -> VerificationResult:
    nhplug_by_date = {row.session_date: row for row in nhplug_rows}
    kiwoom_by_date = {row.session_date: row for row in kiwoom_rows}
    common_dates = tuple(sorted(set(nhplug_by_date) & set(kiwoom_by_date)))
    mismatches = tuple(
        date
        for date in common_dates
        if not _same_ohlcv(nhplug_by_date[date], kiwoom_by_date[date])
    )
    if not mismatches:
        return VerificationResult(
            symbol=symbol,
            common_dates=len(common_dates),
            mismatch_dates=(),
            classification=VerificationClassification.MATCH,
        )
    if frozen is None:
        return VerificationResult(
            symbol=symbol,
            common_dates=len(common_dates),
            mismatch_dates=mismatches,
            classification=VerificationClassification.FROZEN_SAMPLE_UNAVAILABLE,
        )
    supported: set[VerificationClassification] = set()
    for date in mismatches:
        frozen_row = frozen.get((symbol, date))
        if frozen_row is None:
            supported.add(VerificationClassification.UNDETERMINED)
            continue
        nhplug_matches = _same_ohlcv(nhplug_by_date[date], frozen_row)
        kiwoom_matches = _same_ohlcv(kiwoom_by_date[date], frozen_row)
        if nhplug_matches and not kiwoom_matches:
            supported.add(VerificationClassification.NHPLUG_MATCHES_KIS)
        elif kiwoom_matches and not nhplug_matches:
            supported.add(VerificationClassification.KIWOOM_MATCHES_KIS)
        elif not nhplug_matches and not kiwoom_matches:
            supported.add(VerificationClassification.BOTH_DIVERGE_FROM_KIS)
        else:
            supported.add(VerificationClassification.UNDETERMINED)
    classification = (
        supported.pop()
        if len(supported) == 1
        else VerificationClassification.UNDETERMINED
    )
    return VerificationResult(
        symbol=symbol,
        common_dates=len(common_dates),
        mismatch_dates=mismatches,
        classification=classification,
    )


class NHPlugPeriodCollector:
    """Serial period collector with per-symbol isolation and optional writing."""

    def __init__(
        self,
        *,
        client: PeriodQuoteClient,
        store: PeriodCollectionStore | None = None,
        sleep: SleepFn = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        frozen_loader: FrozenLoader = load_frozen_kis_sample,
        after_success: AfterSuccess | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._sleep = sleep
        self._monotonic = monotonic
        self._frozen_loader = frozen_loader
        self._after_success = after_success

    async def _targets(
        self, *, market: str, supplied: Sequence[str] | None
    ) -> list[PeriodTarget]:
        if market == "kr":
            raw = supplied
            if raw is None:
                if self._store is None:
                    raise RuntimeError(
                        "default KR universe requires a collection store"
                    )
                raw = await self._store.list_active_kr_symbols()
            targets = _normalize_kr_symbols(raw)
        elif market == "us":
            if self._store is None:
                raise RuntimeError("US targets require a collection store")
            raw_us = (
                await self._store.list_active_us_targets()
                if supplied is None
                else await self._store.resolve_us_symbols(supplied)
            )
            targets = _normalize_us_targets(raw_us)
        else:
            targets = _normalize_indexfx_symbols(
                DEFAULT_INDEXFX_SYMBOLS if supplied is None else supplied
            )
        if not targets:
            if market == "indexfx":
                raise ValueError(
                    "indexfx requires --symbols; no approved default universe"
                )
            raise ValueError(f"{market} active universe has no symbols")
        return targets

    async def _fetch(
        self,
        *,
        market: str,
        target: PeriodTarget,
        end_date: str,
        query_bars: int,
        pacer: _SerialPacer,
        start: datetime,
        end: datetime,
    ) -> _ParsedRows:
        await pacer.wait_for_turn()
        if market == "kr":
            payload = await self._client.fetch_kr_period(
                symbol=target.symbol, end_date=end_date, bars=query_bars
            )
        elif market == "us":
            payload = await self._client.fetch_us_period(
                symbol=to_yahoo_symbol(target.symbol),
                end_date=end_date,
                bars=query_bars,
            )
        else:
            payload = await self._client.fetch_index_fx_period(
                symbol=target.symbol, end_date=end_date, bars=query_bars
            )
        return _parse_rows(
            market=market,
            target=target,
            payload=payload,
            start=start,
            end=end,
        )

    async def _run_kr_verification(
        self,
        *,
        sample_count: int,
        end_date: str,
        query_bars: int,
        pacer: _SerialPacer,
        start: datetime,
        end: datetime,
    ) -> tuple[tuple[VerificationResult, ...], tuple[SymbolFailure, ...]]:
        if sample_count == 0:
            return (), ()
        if self._store is None:
            raise RuntimeError("--verify-sample requires a collection store")
        try:
            frozen: Mapping[tuple[str, str], FrozenBar] | None = self._frozen_loader()
        except (OSError, ValueError, KeyError, InvalidOperation):
            frozen = None
        verification: list[VerificationResult] = []
        failures: list[SymbolFailure] = []
        for symbol in await self._store.sample_kiwoom_symbols(sample_count):
            try:
                target = PeriodTarget(symbol=symbol)
                parsed = await self._fetch(
                    market="kr",
                    target=target,
                    end_date=end_date,
                    query_bars=query_bars,
                    pacer=pacer,
                    start=start,
                    end=end,
                )
                existing = await self._store.load_kiwoom_rows(symbol, query_bars)
                verification.append(
                    _classify_verification(
                        symbol=symbol,
                        nhplug_rows=parsed.rows,
                        kiwoom_rows=existing,
                        frozen=frozen,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - isolate verification samples
                error_code = type(exc).__name__
                failures.append(SymbolFailure(symbol=symbol, error_code=error_code))
                logger.warning(
                    "NHPLUG period verification failed symbol=%s error_code=%s",
                    symbol,
                    error_code,
                )
        return tuple(verification), tuple(failures)

    async def _insert(self, *, market: str, rows: Sequence[PeriodCandle]) -> int:
        if self._store is None:
            raise RuntimeError("committed collection requires a collection store")
        if market == "kr":
            return await self._store.insert_missing_kr(rows)
        if market == "us":
            return await self._store.insert_missing_us(rows)
        return 0

    async def collect(
        self,
        *,
        market: str,
        symbols: Sequence[str] | None,
        start_date: str,
        end_date: str,
        bars: int = DEFAULT_BARS,
        rate_seconds: float = DEFAULT_RATE_SECONDS,
        commit: bool = False,
        resume: bool = False,
        checkpoint: ResumeCheckpoint | None = None,
        verify_sample: int = 0,
    ) -> CollectionResult:
        start, end, query_bars = _assert_collection_args(
            market=market,
            start_date=start_date,
            end_date=end_date,
            bars=bars,
            rate_seconds=rate_seconds,
            verify_sample=verify_sample,
        )
        assert_collection_enabled()
        if resume and not commit:
            raise ValueError("--resume requires --commit")
        if resume and checkpoint is None:
            raise ValueError("--resume requires a local resume checkpoint")
        if commit and market != "indexfx" and self._store is None:
            raise RuntimeError("committed collection requires a collection store")

        targets = await self._targets(market=market, supplied=symbols)
        started = self._monotonic()
        pacer = _SerialPacer(
            rate_seconds=float(rate_seconds),
            sleep=self._sleep,
            monotonic=self._monotonic,
        )
        verification: tuple[VerificationResult, ...] = ()
        verification_failures: tuple[SymbolFailure, ...] = ()
        if market == "kr":
            verification, verification_failures = await self._run_kr_verification(
                sample_count=int(verify_sample),
                end_date=end_date,
                query_bars=query_bars,
                pacer=pacer,
                start=start,
                end=end,
            )

        state: _ResumeState | None = None
        resumed_from: str | None = None
        if commit and checkpoint is not None:
            if resume:
                state = checkpoint.load(market=market, targets=targets)
                if state is not None:
                    resumed_from = (
                        targets[state.last_success_index].symbol
                        if state.last_success_index >= 0
                        else None
                    )
            if state is None:
                state = _ResumeState(
                    market=market,
                    targets_digest=_digest_targets(targets),
                    last_success_index=-1,
                    last_success_key=None,
                    failed_keys=(),
                )

        if state is not None and resume:
            retry_indices = [
                index
                for index, target in enumerate(targets)
                if target.checkpoint_key in set(state.failed_keys)
            ]
            pending_indices = list(
                dict.fromkeys(
                    [
                        *retry_indices,
                        *range(max(state.last_success_index + 1, 0), len(targets)),
                    ]
                )
            )
        else:
            pending_indices = list(range(len(targets)))

        rows_received = 0
        rows_inserted = 0
        rows_conflict_skipped = 0
        invalid_rows = 0
        failures: list[SymbolFailure] = []
        for index in pending_indices:
            target = targets[index]
            try:
                parsed = await self._fetch(
                    market=market,
                    target=target,
                    end_date=end_date,
                    query_bars=query_bars,
                    pacer=pacer,
                    start=start,
                    end=end,
                )
                rows_received += len(parsed.rows)
                invalid_rows += parsed.invalid_rows
                inserted = 0
                if commit and market != "indexfx":
                    inserted = await self._insert(market=market, rows=parsed.rows)
                    rows_inserted += inserted
                    rows_conflict_skipped += len(parsed.rows) - inserted
                if commit and state is not None and checkpoint is not None:
                    failed = tuple(
                        key for key in state.failed_keys if key != target.checkpoint_key
                    )
                    state = replace(
                        state,
                        last_success_index=max(state.last_success_index, index),
                        last_success_key=target.checkpoint_key,
                        failed_keys=failed,
                    )
                    checkpoint.save(state)
                if self._after_success is not None:
                    callback_result = self._after_success(target.symbol)
                    if isinstance(callback_result, Awaitable):
                        await callback_result
                logger.info(
                    "NHPLUG period %s [%d/%d] symbol=%s rows=%d inserted=%d",
                    market,
                    index + 1,
                    len(targets),
                    target.symbol,
                    len(parsed.rows),
                    inserted,
                )
            except Exception as exc:  # noqa: BLE001 - per-symbol isolation
                error_code = type(exc).__name__
                failures.append(
                    SymbolFailure(symbol=target.symbol, error_code=error_code)
                )
                if state is not None and checkpoint is not None:
                    state = replace(
                        state,
                        failed_keys=tuple(
                            dict.fromkeys([*state.failed_keys, target.checkpoint_key])
                        ),
                    )
                    checkpoint.save(state)
                logger.warning(
                    "NHPLUG period failed market=%s symbol=%s error_code=%s",
                    market,
                    target.symbol,
                    error_code,
                )

        if commit and checkpoint is not None and not failures:
            checkpoint.clear()
        return CollectionResult(
            market=market,
            total_symbols=len(targets),
            processed_symbols=len(pending_indices),
            rows_received=rows_received,
            rows_inserted=rows_inserted,
            rows_conflict_skipped=rows_conflict_skipped,
            invalid_rows=invalid_rows,
            failures=tuple(failures),
            verification=verification,
            verification_failures=verification_failures,
            resumed_from=resumed_from,
            elapsed_seconds=self._monotonic() - started,
            commit=commit,
            persistence_status=(
                "SCHEMA_PROPOSAL_REQUIRED" if market == "indexfx" else "READY"
            ),
        )


class NHPlugPeriodRepository:
    """Exact ``RETURNING`` witnesses for insert-only candle writes."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def insert_missing_kr(self, rows: Sequence[PeriodCandle]) -> int:
        if not rows:
            return 0
        payload = [
            {
                "time": row.time_utc,
                "symbol": row.symbol,
                "venue": KR_VENUE,
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
        statement = (
            pg_insert(_KR_CANDLES_1D)
            .values(payload)
            .on_conflict_do_nothing(index_elements=("time", "symbol", "venue"))
            .returning(_KR_CANDLES_1D.c.time)
        )
        result = await self._session.execute(statement)
        return len(result.scalars().all())

    async def insert_missing_us(self, rows: Sequence[PeriodCandle]) -> int:
        if not rows:
            return 0
        if any(row.exchange not in SUPPORTED_US_EXCHANGES for row in rows):
            raise ValueError("US candle row lacks a supported exchange")
        payload = [
            {
                "time": row.time_utc,
                "symbol": row.symbol,
                "exchange": row.exchange,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "adj_close": None,
                "volume": row.volume,
                "value": row.value,
                "source": SOURCE,
            }
            for row in rows
        ]
        statement = (
            pg_insert(_US_CANDLES_1D)
            .values(payload)
            .on_conflict_do_nothing(index_elements=("time", "symbol", "exchange"))
            .returning(_US_CANDLES_1D.c.time)
        )
        result = await self._session.execute(statement)
        return len(result.scalars().all())


class DatabaseNHPlugPeriodStore:
    """Short-lived DB operations; connections never span broker waits."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active_kr_symbols(self) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT symbol FROM public.kr_symbol_universe "
                    "WHERE is_active IS TRUE ORDER BY symbol"
                )
            )
            return [str(symbol) for symbol in result.scalars().all()]

    async def list_active_us_targets(self) -> list[tuple[str, str]]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT symbol, exchange FROM public.us_symbol_universe "
                    "WHERE is_active IS TRUE ORDER BY symbol, exchange"
                )
            )
            return [
                (str(row.symbol), str(row.exchange)) for row in result.mappings().all()
            ]

    async def resolve_us_symbols(self, symbols: Sequence[str]) -> list[tuple[str, str]]:
        normalized = [
            to_db_symbol(piece.strip().upper())
            for supplied in symbols
            for piece in str(supplied).split(",")
            if piece.strip()
        ]
        if not normalized:
            raise ValueError("--symbols did not contain a US symbol")
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT symbol, exchange FROM public.us_symbol_universe "
                    "WHERE is_active IS TRUE AND symbol = ANY(CAST(:symbols AS text[]))"
                ),
                {"symbols": normalized},
            )
            rows = [
                (str(row.symbol), str(row.exchange)) for row in result.mappings().all()
            ]
        by_symbol = dict(rows)
        missing = [symbol for symbol in normalized if symbol not in by_symbol]
        if missing:
            raise ValueError("active US symbols not found: " + ", ".join(missing))
        return [(symbol, by_symbol[symbol]) for symbol in normalized]

    async def insert_missing_kr(self, rows: Sequence[PeriodCandle]) -> int:
        async with self._session_factory() as session:
            try:
                inserted = await NHPlugPeriodRepository(
                    session=session
                ).insert_missing_kr(rows)
                await session.commit()
                return inserted
            except Exception:
                await session.rollback()
                raise

    async def insert_missing_us(self, rows: Sequence[PeriodCandle]) -> int:
        async with self._session_factory() as session:
            try:
                inserted = await NHPlugPeriodRepository(
                    session=session
                ).insert_missing_us(rows)
                await session.commit()
                return inserted
            except Exception:
                await session.rollback()
                raise

    async def sample_kiwoom_symbols(self, limit: int) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT symbol FROM public.kr_candles_1d "
                    "WHERE source = :source ORDER BY symbol LIMIT :limit"
                ),
                {"source": "kiwoom_live", "limit": int(limit)},
            )
            return [str(symbol) for symbol in result.scalars().all()]

    async def load_kiwoom_rows(
        self, symbol: str, bars: int
    ) -> list[StoredKiwoomCandle]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, symbol, open, high, low, close, volume, value
                    FROM public.kr_candles_1d
                    WHERE symbol = :symbol AND venue = :venue AND source = :source
                    ORDER BY time DESC LIMIT :limit
                    """
                ),
                {
                    "symbol": symbol,
                    "venue": KR_VENUE,
                    "source": "kiwoom_live",
                    "limit": int(bars),
                },
            )
            records = result.mappings().all()
        rows: list[StoredKiwoomCandle] = []
        for record in records:
            try:
                raw_time = record["time"]
                if not isinstance(raw_time, datetime):
                    raise ValueError("stored time was not a datetime")
                timestamp = (
                    raw_time.replace(tzinfo=UTC)
                    if raw_time.tzinfo is None
                    else raw_time.astimezone(UTC)
                )
                rows.append(
                    StoredKiwoomCandle(
                        symbol=str(record["symbol"]),
                        session_date=timestamp.strftime("%Y%m%d"),
                        time_utc=timestamp,
                        open=_decimal(record["open"], field="open"),
                        high=_decimal(record["high"], field="high"),
                        low=_decimal(record["low"], field="low"),
                        close=_decimal(record["close"], field="close"),
                        volume=_decimal(record["volume"], field="volume"),
                        value=_decimal(record["value"], field="value"),
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        return rows


def build_default_collector(*, token_cache_path: Path) -> NHPlugPeriodCollector:
    """Wire the client only after the dedicated scoped environment is armed."""

    from app.core.db import AsyncSessionLocal

    session_factory = cast(Callable[[], AsyncSession], AsyncSessionLocal)
    return NHPlugPeriodCollector(
        client=NHPlugLiveQuotesClient.from_scoped_env(
            token_cache_path=token_cache_path
        ),
        store=DatabaseNHPlugPeriodStore(session_factory=session_factory),
    )
