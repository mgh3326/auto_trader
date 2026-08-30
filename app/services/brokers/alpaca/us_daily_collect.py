"""Bounded, opt-in Alpaca IEX US daily-candle collection.

This module is deliberately separate from the Alpaca paper-trading surface.
It only calls the market-data bars endpoint after a dedicated environment file
has armed the collection gate.  Database writes are insert-only so an existing
KIS (or other canonical) candle is never overwritten.
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
from pathlib import Path
from typing import Protocol, cast

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.services.brokers.alpaca.endpoints import DATA_BASE_URL

logger = logging.getLogger(__name__)

SOURCE = "alpaca_iex"
FEED = "iex"
ADJUSTMENT = "split"
TIMEFRAME = "1Day"
MAX_BARS_PER_REQUEST = 10_000
DEFAULT_BARS = 600
DEFAULT_BATCH_SIZE = 200
MAX_BATCH_SIZE = 200
MIN_RATE_SECONDS = 0.3
VERIFY_RELATIVE_TOLERANCE = Decimal("0.001")
COLLECT_GATE_ENV = "ALPACA_US_COLLECT_ENABLED"
_SCOPED_ENV_KEYS = (
    COLLECT_GATE_ENV,
    "ALPACA_DATA_API_KEY_ID",
    "ALPACA_DATA_API_SECRET_KEY",
)
_CREDENTIAL_KEYS = _SCOPED_ENV_KEYS[1:]


class AlpacaUsDailyCollectionDisabled(RuntimeError):
    """Raised before any data request when the dedicated gate is closed."""


class AlpacaUsDailyResponseError(RuntimeError):
    """A bars response could not safely be treated as daily candles."""


@dataclass(frozen=True, slots=True)
class SymbolTarget:
    symbol: str
    exchange: str


@dataclass(frozen=True, slots=True)
class AlpacaUsDailyCandle:
    time_utc: datetime
    symbol: str
    exchange: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal
    volume: Decimal
    value: Decimal


@dataclass(frozen=True, slots=True)
class StoredUsDailyCandle:
    time_utc: datetime
    symbol: str
    exchange: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal | None
    volume: Decimal
    value: Decimal


@dataclass(frozen=True, slots=True)
class SymbolFailure:
    symbol: str
    error_code: str


@dataclass(frozen=True, slots=True)
class VerificationMismatch:
    time_utc: datetime
    field: str
    existing: Decimal
    alpaca: Decimal
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
class _ParsedBars:
    rows: tuple[AlpacaUsDailyCandle, ...]
    invalid_rows: int
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class _ResumeState:
    targets_digest: str
    last_success_index: int
    last_success_symbol: str | None
    failed_symbols: tuple[str, ...]


class AlpacaBarsClient(Protocol):
    async def fetch_bars(
        self, *, symbols: list[str], bars: int, page_token: str | None = None
    ) -> dict[str, object]: ...


RowWriter = Callable[[Sequence[AlpacaUsDailyCandle]], Awaitable[int]]
TargetLoader = Callable[[], Awaitable[list[SymbolTarget]]]
ExplicitTargetLoader = Callable[[Sequence[str]], Awaitable[list[SymbolTarget]]]
SampleTargetLoader = Callable[[int], Awaitable[list[SymbolTarget]]]
ExistingRowsLoader = Callable[[SymbolTarget, int], Awaitable[list[StoredUsDailyCandle]]]
SleepFn = Callable[[float], Awaitable[None]]


class HttpxAlpacaBarsClient:
    """Minimal read-only client for ``GET /v2/stocks/bars``."""

    def __init__(self, *, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    async def fetch_bars(
        self, *, symbols: list[str], bars: int, page_token: str | None = None
    ) -> dict[str, object]:
        # Keep the dispatch boundary fail-closed even if this concrete client
        # is constructed outside the CLI/collector wiring.
        assert_collection_enabled()
        params: dict[str, str | int] = {
            "symbols": ",".join(symbols),
            "timeframe": TIMEFRAME,
            "feed": FEED,
            # Split adjustment maintains a continuous price series while
            # avoiding dividend-total-return semantics in a candle table.
            "adjustment": ADJUSTMENT,
            "limit": min(int(bars), MAX_BARS_PER_REQUEST),
        }
        if page_token:
            params["page_token"] = page_token
        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
        }
        async with httpx.AsyncClient(
            base_url=DATA_BASE_URL, follow_redirects=False
        ) as client:
            response = await client.get(
                "/v2/stocks/bars", params=params, headers=headers
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AlpacaUsDailyResponseError("bars response is not an object")
        return cast(dict[str, object], payload)


def load_scoped_env_file(path: Path) -> dict[str, str]:
    """Read only the dedicated collector keys, without ever logging values."""

    if "prod" in path.name.casefold():
        raise ValueError(f"refusing to read a production env file: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"env file not found: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"env file must be a regular file: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("env file permissions must be exactly 0600")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in _SCOPED_ENV_KEYS:
            values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [
        key for key in (*_CREDENTIAL_KEYS, COLLECT_GATE_ENV) if not values.get(key)
    ]
    if missing:
        raise ValueError("env file missing required keys: " + ", ".join(missing))
    return values


def arm_scoped_environment(*, env_file: Path) -> None:
    """Set only collector credentials and force inherited gate values aside."""

    values = load_scoped_env_file(env_file)
    for key in _SCOPED_ENV_KEYS:
        os.environ[key] = values.get(key, "false" if key == COLLECT_GATE_ENV else "")


def assert_collection_enabled() -> None:
    if os.getenv(COLLECT_GATE_ENV) != "true":
        raise AlpacaUsDailyCollectionDisabled(
            "Alpaca US collection is disabled; set ALPACA_US_COLLECT_ENABLED=true "
            "in the dedicated env file."
        )


def _targets_digest(targets: Sequence[SymbolTarget]) -> str:
    canonical = "\n".join(f"{item.symbol}\t{item.exchange}" for item in targets)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResumeCheckpoint:
    """Atomic, 0600 checkpoint stored next to the dedicated env file."""

    _VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_env_file(cls, env_file: Path) -> ResumeCheckpoint:
        return cls(env_file.with_name(f".{env_file.name}.alpaca-us-daily.resume.json"))

    def load(self, *, targets: Sequence[SymbolTarget]) -> _ResumeState | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = _ResumeState(
                targets_digest=str(raw["targets_digest"]),
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
            raise ValueError(
                f"invalid Alpaca US resume checkpoint: {self.path}"
            ) from exc
        if raw.get(
            "version"
        ) != self._VERSION or state.targets_digest != _targets_digest(targets):
            raise ValueError(f"invalid Alpaca US resume checkpoint: {self.path}")
        if not -1 <= state.last_success_index < len(targets):
            raise ValueError(f"invalid Alpaca US resume checkpoint: {self.path}")
        if state.last_success_index == -1:
            valid_last = state.last_success_symbol is None
        else:
            valid_last = (
                state.last_success_symbol == targets[state.last_success_index].symbol
            )
        if not valid_last or len(set(state.failed_symbols)) != len(
            state.failed_symbols
        ):
            raise ValueError(f"invalid Alpaca US resume checkpoint: {self.path}")
        known = {target.symbol for target in targets}
        if any(symbol not in known for symbol in state.failed_symbols):
            raise ValueError(f"invalid Alpaca US resume checkpoint: {self.path}")
        return state

    def save(self, state: _ResumeState) -> None:
        payload = {
            "version": self._VERSION,
            "targets_digest": state.targets_digest,
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


class AlpacaUsDailyCandleRepository:
    """Insert-only writer with an exact ``RETURNING`` count, never rowcount."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def insert_missing(self, rows: Sequence[AlpacaUsDailyCandle]) -> int:
        if not rows:
            return 0
        # One INSERT statement + RETURNING is intentional. asyncpg executemany
        # rowcount is not a trustworthy inserted-row witness for this policy.
        result = await self._session.execute(
            text(
                """
                INSERT INTO public.us_candles_1d (
                    time, symbol, exchange, open, high, low, close, adj_close,
                    volume, value, source
                )
                SELECT * FROM unnest(
                    CAST(:times AS timestamptz[]), CAST(:symbols AS text[]),
                    CAST(:exchanges AS text[]), CAST(:opens AS numeric[]),
                    CAST(:highs AS numeric[]), CAST(:lows AS numeric[]),
                    CAST(:closes AS numeric[]), CAST(:adj_closes AS numeric[]),
                    CAST(:volumes AS numeric[]), CAST(:values AS numeric[]),
                    CAST(:sources AS text[])
                )
                ON CONFLICT (time, symbol, exchange) DO NOTHING
                RETURNING time
                """
            ),
            {
                "times": [row.time_utc for row in rows],
                "symbols": [row.symbol for row in rows],
                "exchanges": [row.exchange for row in rows],
                "opens": [row.open for row in rows],
                "highs": [row.high for row in rows],
                "lows": [row.low for row in rows],
                "closes": [row.close for row in rows],
                "adj_closes": [row.adj_close for row in rows],
                "volumes": [row.volume for row in rows],
                "values": [row.value for row in rows],
                "sources": [SOURCE for _ in rows],
            },
        )
        return len(result.fetchall())


class DatabaseAlpacaUsDailyStore:
    """Short-lived DB calls; a connection never spans an Alpaca request."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active_targets(self, *, all_active: bool) -> list[SymbolTarget]:
        common_clause = "" if all_active else " AND is_common_stock IS TRUE"
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT symbol, exchange FROM public.us_symbol_universe "
                    "WHERE is_active IS TRUE" + common_clause + " ORDER BY symbol"
                )
            )
            return [
                SymbolTarget(symbol=str(row.symbol), exchange=str(row.exchange))
                for row in result
            ]

    async def list_explicit_targets(self, symbols: Sequence[str]) -> list[SymbolTarget]:
        requested = _normalize_requested_symbols(symbols)
        if not requested:
            raise ValueError("--symbols did not contain a US symbol")
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT symbol, exchange FROM public.us_symbol_universe "
                    "WHERE is_active IS TRUE AND symbol = ANY(CAST(:symbols AS text[])) "
                    "ORDER BY symbol"
                ),
                {"symbols": requested},
            )
            targets = [
                SymbolTarget(symbol=str(row.symbol), exchange=str(row.exchange))
                for row in result
            ]
        resolved = {target.symbol for target in targets}
        missing = [symbol for symbol in requested if symbol not in resolved]
        if missing:
            raise ValueError(
                "active us_symbol_universe rows not found: " + ", ".join(missing)
            )
        by_symbol = {target.symbol: target for target in targets}
        return [by_symbol[symbol] for symbol in requested]

    async def insert_missing(self, rows: Sequence[AlpacaUsDailyCandle]) -> int:
        async with self._session_factory() as session:
            try:
                inserted = await AlpacaUsDailyCandleRepository(
                    session=session
                ).insert_missing(rows)
                await session.commit()
                return inserted
            except Exception:
                await session.rollback()
                raise

    async def sample_existing_targets(self, limit: int) -> list[SymbolTarget]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT symbol, exchange FROM public.us_candles_1d "
                    "ORDER BY symbol, exchange LIMIT :limit"
                ),
                {"limit": int(limit)},
            )
            return [
                SymbolTarget(symbol=str(row.symbol), exchange=str(row.exchange))
                for row in result
            ]

    async def load_existing_rows(
        self, target: SymbolTarget, bars: int
    ) -> list[StoredUsDailyCandle]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, symbol, exchange, open, high, low, close, adj_close, volume, value
                    FROM public.us_candles_1d
                    WHERE symbol = :symbol AND exchange = :exchange
                    ORDER BY time DESC LIMIT :limit
                    """
                ),
                {
                    "symbol": target.symbol,
                    "exchange": target.exchange,
                    "limit": int(bars),
                },
            )
            records = result.mappings().all()
        return [
            StoredUsDailyCandle(
                time_utc=_as_utc(record["time"]),
                symbol=str(record["symbol"]),
                exchange=str(record["exchange"]),
                open=_as_decimal(record["open"]),
                high=_as_decimal(record["high"]),
                low=_as_decimal(record["low"]),
                close=_as_decimal(record["close"]),
                adj_close=(
                    _as_decimal(record["adj_close"])
                    if record["adj_close"] is not None
                    else None
                ),
                volume=_as_decimal(record["volume"]),
                value=_as_decimal(record["value"]),
            )
            for record in records
        ]


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("candle time is not a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _normalize_targets(
    items: Sequence[tuple[str, str] | SymbolTarget],
) -> list[SymbolTarget]:
    normalized: list[SymbolTarget] = []
    seen: set[str] = set()
    for item in items:
        raw_symbol, raw_exchange = (
            (item.symbol, item.exchange) if isinstance(item, SymbolTarget) else item
        )
        symbol = to_db_symbol(str(raw_symbol).strip().upper())
        exchange = str(raw_exchange).strip().upper()
        if not symbol or exchange not in {"NASD", "NYSE", "AMEX"}:
            raise ValueError(
                f"invalid US collector target: {(raw_symbol, raw_exchange)!r}"
            )
        if symbol not in seen:
            seen.add(symbol)
            normalized.append(SymbolTarget(symbol=symbol, exchange=exchange))
    return normalized


def _normalize_requested_symbols(items: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for supplied in items:
        for item in str(supplied).split(","):
            symbol = to_db_symbol(item.strip().upper())
            if not symbol:
                continue
            if symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
    return normalized


def _assert_collection_args(
    *, bars: int, batch_size: int, rate_seconds: float, verify_sample: int
) -> None:
    if not 1 <= int(bars) <= MAX_BARS_PER_REQUEST:
        raise ValueError(f"--bars must be between 1 and {MAX_BARS_PER_REQUEST}")
    if not 1 <= int(batch_size) <= MAX_BATCH_SIZE:
        raise ValueError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if float(rate_seconds) < MIN_RATE_SECONDS:
        raise ValueError(f"--rate-seconds must be at least {MIN_RATE_SECONDS}")
    if int(verify_sample) < 0:
        raise ValueError("--verify-sample must be zero or greater")


class _SerialPacer:
    def __init__(
        self, *, rate_seconds: float, sleep: SleepFn, monotonic: Callable[[], float]
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


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("bar timestamp missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _parse_bars_payload(
    *, payload: Mapping[str, object], targets: Sequence[SymbolTarget]
) -> _ParsedBars:
    raw_bars = payload.get("bars")
    if not isinstance(raw_bars, Mapping):
        raise AlpacaUsDailyResponseError("bars response missing bars object")
    targets_by_symbol = {target.symbol: target for target in targets}
    parsed: dict[tuple[datetime, str, str], AlpacaUsDailyCandle] = {}
    invalid_rows = 0
    for response_symbol, entries in raw_bars.items():
        symbol = to_db_symbol(str(response_symbol).strip().upper())
        target = targets_by_symbol.get(symbol)
        if target is None or not isinstance(entries, list):
            invalid_rows += len(entries) if isinstance(entries, list) else 1
            continue
        for entry in entries:
            try:
                if not isinstance(entry, Mapping):
                    raise ValueError("bar is not an object")
                bar_symbol = to_db_symbol(str(entry.get("S", symbol)).strip().upper())
                if bar_symbol != symbol:
                    raise ValueError("bar symbol does not match response symbol")
                candle = AlpacaUsDailyCandle(
                    time_utc=_parse_timestamp(entry.get("t")),
                    symbol=symbol,
                    exchange=target.exchange,
                    open=_as_decimal(entry["o"]),
                    high=_as_decimal(entry["h"]),
                    low=_as_decimal(entry["l"]),
                    close=_as_decimal(entry["c"]),
                    adj_close=_as_decimal(entry["c"]),
                    volume=_as_decimal(entry["v"]),
                    value=_as_decimal(entry["c"]) * _as_decimal(entry["v"]),
                )
                parsed[(candle.time_utc, candle.symbol, candle.exchange)] = candle
            except (KeyError, TypeError, ValueError, InvalidOperation):
                invalid_rows += 1
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise AlpacaUsDailyResponseError("next_page_token is invalid")
    return _ParsedBars(
        rows=tuple(sorted(parsed.values(), key=lambda row: (row.symbol, row.time_utc))),
        invalid_rows=invalid_rows,
        next_page_token=token,
    )


def _relative_delta(left: Decimal, right: Decimal) -> Decimal:
    return abs(left - right) / max(abs(left), abs(right), Decimal("1"))


def compare_existing_rows(
    *, existing: Sequence[StoredUsDailyCandle], fetched: Sequence[AlpacaUsDailyCandle]
) -> VerificationResult:
    symbol = fetched[0].symbol if fetched else (existing[0].symbol if existing else "")
    existing_by_time = {row.time_utc: row for row in existing}
    fetched_by_time = {row.time_utc: row for row in fetched}
    common = sorted(set(existing_by_time) & set(fetched_by_time))
    mismatches: list[VerificationMismatch] = []
    for time_utc in common:
        for field in ("open", "high", "low", "close", "volume", "value"):
            left = cast(Decimal, getattr(existing_by_time[time_utc], field))
            right = cast(Decimal, getattr(fetched_by_time[time_utc], field))
            delta = _relative_delta(left, right)
            if delta > VERIFY_RELATIVE_TOLERANCE:
                mismatches.append(
                    VerificationMismatch(time_utc, field, left, right, delta)
                )
    return VerificationResult(
        symbol,
        len(common),
        tuple(mismatches),
        "NO_OVERLAP" if not common else ("MATCH" if not mismatches else "MISMATCH"),
    )


def _failure_code(exc: Exception) -> str:
    return (
        "broker_response_rejected"
        if isinstance(exc, AlpacaUsDailyResponseError)
        else type(exc).__name__
    )


class AlpacaUsDailyCollector:
    """Serial, batch-isolated IEX collector with injectable offline I/O."""

    def __init__(
        self,
        *,
        client: AlpacaBarsClient,
        write_rows: RowWriter | None = None,
        load_active_targets: TargetLoader | None = None,
        load_explicit_targets: ExplicitTargetLoader | None = None,
        sample_existing_targets: SampleTargetLoader | None = None,
        load_existing_rows: ExistingRowsLoader | None = None,
        sleep: SleepFn = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._write_rows = write_rows
        self._load_active_targets = load_active_targets
        self._load_explicit_targets = load_explicit_targets
        self._sample_existing_targets = sample_existing_targets
        self._load_existing_rows = load_existing_rows
        self._sleep = sleep
        self._monotonic = monotonic

    async def _fetch_batch(
        self, *, pacer: _SerialPacer, targets: Sequence[SymbolTarget], bars: int
    ) -> tuple[tuple[AlpacaUsDailyCandle, ...], int]:
        rows: dict[tuple[datetime, str, str], AlpacaUsDailyCandle] = {}
        invalid_rows = 0
        token: str | None = None
        while True:
            await pacer.wait_for_turn()
            payload = await self._client.fetch_bars(
                symbols=[target.symbol for target in targets],
                bars=bars,
                page_token=token,
            )
            parsed = _parse_bars_payload(payload=payload, targets=targets)
            rows.update(
                {(row.time_utc, row.symbol, row.exchange): row for row in parsed.rows}
            )
            invalid_rows += parsed.invalid_rows
            token = parsed.next_page_token
            if token is None:
                break
        per_symbol: dict[str, list[AlpacaUsDailyCandle]] = {}
        for row in rows.values():
            per_symbol.setdefault(row.symbol, []).append(row)
        selected = [
            row
            for symbol_rows in per_symbol.values()
            for row in sorted(symbol_rows, key=lambda candle: candle.time_utc)[-bars:]
        ]
        return tuple(
            sorted(selected, key=lambda row: (row.symbol, row.time_utc))
        ), invalid_rows

    async def _run_verification(
        self, *, pacer: _SerialPacer, count: int, bars: int
    ) -> tuple[tuple[VerificationResult, ...], tuple[SymbolFailure, ...]]:
        if count == 0:
            return (), ()
        if self._sample_existing_targets is None or self._load_existing_rows is None:
            raise RuntimeError("verification requires existing candle readers")
        results: list[VerificationResult] = []
        failures: list[SymbolFailure] = []
        for target in await self._sample_existing_targets(count):
            try:
                fetched, _ = await self._fetch_batch(
                    pacer=pacer, targets=[target], bars=bars
                )
                results.append(
                    compare_existing_rows(
                        existing=await self._load_existing_rows(target, bars),
                        fetched=fetched,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - isolated sample diagnostic
                failures.append(SymbolFailure(target.symbol, _failure_code(exc)))
        return tuple(results), tuple(failures)

    async def collect(
        self,
        *,
        symbols: Sequence[tuple[str, str] | SymbolTarget | str] | None,
        bars: int = DEFAULT_BARS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        rate_seconds: float = 0.35,
        commit: bool = False,
        resume: bool = False,
        checkpoint: ResumeCheckpoint | None = None,
        verify_sample: int = 0,
    ) -> CollectionResult:
        _assert_collection_args(
            bars=bars,
            batch_size=batch_size,
            rate_seconds=rate_seconds,
            verify_sample=verify_sample,
        )
        assert_collection_enabled()
        if resume and (not commit or checkpoint is None):
            raise ValueError("--resume requires --commit and a local resume checkpoint")
        if commit and self._write_rows is None:
            raise RuntimeError("committed collection requires a candle writer")
        if symbols is None:
            if self._load_active_targets is None:
                raise RuntimeError(
                    "collection without --symbols requires an active universe loader"
                )
            targets = _normalize_targets(await self._load_active_targets())
        else:
            if all(isinstance(item, str) for item in symbols):
                if self._load_explicit_targets is None:
                    raise RuntimeError(
                        "string --symbols requires an active universe loader"
                    )
                targets = _normalize_targets(
                    await self._load_explicit_targets(cast(Sequence[str], symbols))
                )
            else:
                targets = _normalize_targets(
                    cast(Sequence[tuple[str, str] | SymbolTarget], symbols)
                )
        if not targets:
            raise ValueError("us_symbol_universe has no selected active symbols")

        started = self._monotonic()
        pacer = _SerialPacer(
            rate_seconds=float(rate_seconds),
            sleep=self._sleep,
            monotonic=self._monotonic,
        )
        verification, verification_failures = await self._run_verification(
            pacer=pacer, count=int(verify_sample), bars=int(bars)
        )
        state: _ResumeState | None = None
        resumed_from: str | None = None
        if commit and checkpoint is not None:
            state = checkpoint.load(targets=targets) if resume else None
            if state is not None and state.last_success_symbol:
                resumed_from = state.last_success_symbol
            if state is None:
                state = _ResumeState(_targets_digest(targets), -1, None, ())
        failed_set = set(state.failed_symbols) if state and resume else set()
        start_index = max(state.last_success_index + 1, 0) if state and resume else 0
        pending = [
            target
            for index, target in enumerate(targets)
            if index >= start_index or target.symbol in failed_set
        ]

        rows_received = rows_inserted = rows_conflict_skipped = invalid_rows = 0
        failures: list[SymbolFailure] = []
        processed = 0
        for batch_start in range(0, len(pending), int(batch_size)):
            batch = pending[batch_start : batch_start + int(batch_size)]
            try:
                fetched, invalid = await self._fetch_batch(
                    pacer=pacer, targets=batch, bars=int(bars)
                )
                rows_received += len(fetched)
                invalid_rows += invalid
                inserted = 0
                if commit:
                    inserted = await cast(RowWriter, self._write_rows)(fetched)
                    if not 0 <= inserted <= len(fetched):
                        raise RuntimeError(
                            "candle writer returned an invalid inserted-row count"
                        )
                    rows_inserted += inserted
                    rows_conflict_skipped += len(fetched) - inserted
                    if state is not None and checkpoint is not None:
                        last_index = max(targets.index(target) for target in batch)
                        state = replace(
                            state,
                            last_success_index=max(
                                state.last_success_index, last_index
                            ),
                            last_success_symbol=targets[last_index].symbol,
                            failed_symbols=tuple(
                                symbol
                                for symbol in state.failed_symbols
                                if symbol not in {target.symbol for target in batch}
                            ),
                        )
                        checkpoint.save(state)
                processed += len(batch)
                elapsed = self._monotonic() - started
                remaining = max(len(pending) - processed, 0)
                eta = max(float(rate_seconds), elapsed / max(processed, 1)) * remaining
                logger.info(
                    "Alpaca US daily [%d/%d] symbols=%s elapsed=%.1fs eta=%.1fs rows=%d inserted=%d",
                    processed,
                    len(pending),
                    ",".join(target.symbol for target in batch),
                    elapsed,
                    eta,
                    len(fetched),
                    inserted,
                )
            except Exception as exc:  # noqa: BLE001 - isolate the failed request batch
                code = _failure_code(exc)
                failures.extend(SymbolFailure(target.symbol, code) for target in batch)
                processed += len(batch)
                if state is not None and checkpoint is not None:
                    state = replace(
                        state,
                        failed_symbols=tuple(
                            dict.fromkeys(
                                [
                                    *state.failed_symbols,
                                    *(target.symbol for target in batch),
                                ]
                            )
                        ),
                    )
                    checkpoint.save(state)
                logger.warning(
                    "Alpaca US daily batch failed symbols=%s error_code=%s",
                    ",".join(target.symbol for target in batch),
                    code,
                )
        if commit and checkpoint is not None and not failures:
            checkpoint.clear()
        return CollectionResult(
            len(targets),
            len(pending),
            rows_received,
            rows_inserted,
            rows_conflict_skipped,
            invalid_rows,
            tuple(failures),
            verification,
            verification_failures,
            resumed_from,
            self._monotonic() - started,
            commit,
        )


def build_default_collector(*, all_active: bool = False) -> AlpacaUsDailyCollector:
    """Wire operational I/O only after the scoped env has armed this process."""

    from app.core.db import AsyncSessionLocal

    api_key = os.getenv("ALPACA_DATA_API_KEY_ID", "")
    api_secret = os.getenv("ALPACA_DATA_API_SECRET_KEY", "")
    if not api_key or not api_secret:
        raise ValueError(
            "collector environment missing required Alpaca data credential keys"
        )
    session_factory = cast(Callable[[], AsyncSession], AsyncSessionLocal)
    store = DatabaseAlpacaUsDailyStore(session_factory=session_factory)

    async def load_targets() -> list[SymbolTarget]:
        return await store.list_active_targets(all_active=all_active)

    return AlpacaUsDailyCollector(
        client=HttpxAlpacaBarsClient(api_key=api_key, api_secret=api_secret),
        write_rows=store.insert_missing,
        load_active_targets=load_targets,
        load_explicit_targets=store.list_explicit_targets,
        sample_existing_targets=store.sample_existing_targets,
        load_existing_rows=store.load_existing_rows,
    )
