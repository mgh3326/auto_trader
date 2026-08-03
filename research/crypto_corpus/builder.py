"""Resumable public-data materializer for the signed crypto corpus scope.

The builder does not import the application package and has no database,
credential, signed-endpoint, broker, account, scheduler, or strategy surface.
It only calls the explicitly listed unsigned public market-data endpoints.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pyarrow as pa

from .artifacts import ArtifactStore, FileRecord, StagedFile
from .constants import (
    ARTIFACT_ROOT,
    AUTH,
    BINANCE_DAILY_EARLIEST,
    BINANCE_DELISTED_PROBE_SYMBOL,
    BINANCE_EXCHANGE_INFO_URL,
    BINANCE_KLINES_URL,
    BINANCE_PAGE_SIZE,
    CORPUS_ID,
    CUTOFF_END,
    HOLDOUT_ACCESS_LOG,
    HOLDOUT_START,
    HOUR_WINDOW_START,
    MAX_ARTIFACT_BYTES,
    MAX_REQUESTS,
    MAX_WALL_CLOCK_SECONDS,
    PROGRESS_LOG,
    PURPOSE,
    UPBIT_DAILY_EARLIEST,
    UPBIT_DAYS_URL,
    UPBIT_DELISTED_PROBE_MARKET,
    UPBIT_HOURS_URL,
    UPBIT_MARKETS_URL,
    UPBIT_PAGE_SIZE,
    VENUES,
    utc_iso,
)
from .public_api import ApiResponse, PublicApiClient, RequestBudgetExceeded


class SourceDataError(ValueError):
    """A source row cannot safely become a stored OHLCV bar."""


@dataclass(frozen=True)
class RequestBudgetPlan:
    upbit_symbols: int
    binance_symbols: int
    upbit_daily_pages_per_symbol: int
    upbit_hourly_pages_per_symbol: int
    binance_daily_pages_per_symbol: int
    binance_hourly_pages_per_symbol: int
    universe_snapshot_requests: int
    delisted_probe_requests: int
    projected_total: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class Bar:
    venue: str
    symbol: str
    frequency: str
    open_ms: int
    close_exclusive_ms: int
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    base_volume: float
    quote_volume: float
    trade_count: int | None
    source_candle_date_time_utc: str | None
    source_candle_date_time_kst: str | None
    source_open_time_ms: int | None
    source_close_time_ms: int | None
    source_timestamp_ms: int | None


@dataclass
class FetchResult:
    bars: list[Bar]
    error: str | None = None
    rate_limited: bool = False
    budget_exhausted: bool = False


BAR_SCHEMA = pa.schema(
    [
        pa.field("venue", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("frequency", pa.string(), nullable=False),
        pa.field("bucket_timezone", pa.string(), nullable=False),
        pa.field("open_time_utc", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("close_time_utc", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("base_volume", pa.float64(), nullable=False),
        pa.field("quote_volume", pa.float64(), nullable=False),
        pa.field("trade_count", pa.int64(), nullable=True),
        pa.field("source_candle_date_time_utc", pa.string(), nullable=True),
        pa.field("source_candle_date_time_kst", pa.string(), nullable=True),
        pa.field("source_open_time_ms", pa.int64(), nullable=True),
        pa.field("source_close_time_ms", pa.int64(), nullable=True),
        pa.field("source_timestamp_ms", pa.int64(), nullable=True),
    ]
)


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _utc_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _parse_utc_timestamp(value: str) -> datetime:
    rendered = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(rendered)
    if parsed.tzinfo is None:
        # Upbit's ``candle_date_time_utc`` has no offset in real responses;
        # its field name is the source declaration that makes this UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_ohlcv(
    *,
    symbol: str,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    base_volume: float,
    quote_volume: float,
    trade_count: int | None,
) -> None:
    numbers = (
        open_price,
        high_price,
        low_price,
        close_price,
        base_volume,
        quote_volume,
    )
    if not all(math.isfinite(value) for value in numbers):
        raise SourceDataError(f"{symbol}: non-finite OHLCV field")
    if not all(value > 0 for value in (open_price, high_price, low_price, close_price)):
        raise SourceDataError(f"{symbol}: non-positive OHLC value")
    if high_price < low_price:
        raise SourceDataError(f"{symbol}: high below low")
    if high_price < max(open_price, close_price):
        raise SourceDataError(f"{symbol}: high below open/close")
    if low_price > min(open_price, close_price):
        raise SourceDataError(f"{symbol}: low above open/close")
    if base_volume < 0 or quote_volume < 0:
        raise SourceDataError(f"{symbol}: negative volume")
    if trade_count is not None and trade_count < 0:
        raise SourceDataError(f"{symbol}: negative trade count")


def _normalize_upbit(
    record: dict[str, Any], frequency: str, expected_symbol: str
) -> Bar:
    if record.get("market") != expected_symbol:
        raise SourceDataError(
            f"{expected_symbol}: source market mismatch {record.get('market')!r}"
        )
    source_utc = str(record["candle_date_time_utc"])
    open_ms = _milliseconds(_parse_utc_timestamp(source_utc))
    duration_ms = 86_400_000 if frequency == "1d" else 3_600_000
    values = {
        "open_price": float(record["opening_price"]),
        "high_price": float(record["high_price"]),
        "low_price": float(record["low_price"]),
        "close_price": float(record["trade_price"]),
        "base_volume": float(record["candle_acc_trade_volume"]),
        "quote_volume": float(record["candle_acc_trade_price"]),
    }
    _validate_ohlcv(symbol=expected_symbol, trade_count=None, **values)
    return Bar(
        venue="upbit_krw",
        symbol=expected_symbol,
        frequency=frequency,
        open_ms=open_ms,
        close_exclusive_ms=open_ms + duration_ms,
        trade_count=None,
        source_candle_date_time_utc=source_utc,
        source_candle_date_time_kst=str(record.get("candle_date_time_kst"))
        if record.get("candle_date_time_kst") is not None
        else None,
        source_open_time_ms=None,
        source_close_time_ms=None,
        source_timestamp_ms=int(record["timestamp"]),
        **values,
    )


def _normalize_binance(record: list[Any], frequency: str, expected_symbol: str) -> Bar:
    if len(record) < 11:
        raise SourceDataError(f"{expected_symbol}: expected 11 Binance kline fields")
    duration_ms = 86_400_000 if frequency == "1d" else 3_600_000
    open_ms = int(record[0])
    raw_close_ms = int(record[6])
    if raw_close_ms != open_ms + duration_ms - 1:
        raise SourceDataError(
            f"{expected_symbol}@{open_ms}: incomplete/corrupt source bar duration"
        )
    values = {
        "open_price": float(record[1]),
        "high_price": float(record[2]),
        "low_price": float(record[3]),
        "close_price": float(record[4]),
        "base_volume": float(record[5]),
        "quote_volume": float(record[7]),
    }
    trade_count = int(record[8])
    _validate_ohlcv(symbol=expected_symbol, trade_count=trade_count, **values)
    return Bar(
        venue="binance_usdt_spot",
        symbol=expected_symbol,
        frequency=frequency,
        open_ms=open_ms,
        close_exclusive_ms=open_ms + duration_ms,
        trade_count=trade_count,
        source_candle_date_time_utc=None,
        source_candle_date_time_kst=None,
        source_open_time_ms=open_ms,
        source_close_time_ms=raw_close_ms,
        source_timestamp_ms=None,
        **values,
    )


def _bar_to_row(bar: Bar) -> dict[str, Any]:
    return {
        "venue": bar.venue,
        "symbol": bar.symbol,
        "frequency": bar.frequency,
        "bucket_timezone": "UTC",
        "open_time_utc": _utc_from_ms(bar.open_ms),
        "close_time_utc": _utc_from_ms(bar.close_exclusive_ms),
        "open": bar.open_price,
        "high": bar.high_price,
        "low": bar.low_price,
        "close": bar.close_price,
        "base_volume": bar.base_volume,
        "quote_volume": bar.quote_volume,
        "trade_count": bar.trade_count,
        "source_candle_date_time_utc": bar.source_candle_date_time_utc,
        "source_candle_date_time_kst": bar.source_candle_date_time_kst,
        "source_open_time_ms": bar.source_open_time_ms,
        "source_close_time_ms": bar.source_close_time_ms,
        "source_timestamp_ms": bar.source_timestamp_ms,
    }


def _task_window(venue: str, frequency: str) -> tuple[datetime, datetime, int]:
    if frequency == "1h":
        return HOUR_WINDOW_START, CUTOFF_END, 3_600_000
    if frequency != "1d":
        raise ValueError(f"unsupported frequency {frequency!r}")
    start = UPBIT_DAILY_EARLIEST if venue == "upbit_krw" else BINANCE_DAILY_EARLIEST
    return start, CUTOFF_END, 86_400_000


def calculate_request_budget(
    upbit_symbols: int, binance_symbols: int
) -> RequestBudgetPlan:
    """Calculate a conservative cap before any historical candle request.

    The daily denominators use each venue's documented launch-era lower bound,
    not a guessed listing date.  That overestimates pages for newer symbols and
    makes the 60,000-request gate fail closed.
    """
    upbit_daily_bars = int(
        (CUTOFF_END - UPBIT_DAILY_EARLIEST).total_seconds() // 86_400
    )
    binance_daily_bars = int(
        (CUTOFF_END - BINANCE_DAILY_EARLIEST).total_seconds() // 86_400
    )
    hourly_bars = int((CUTOFF_END - HOUR_WINDOW_START).total_seconds() // 3_600)
    upbit_daily_pages = _ceil_div(upbit_daily_bars, UPBIT_PAGE_SIZE)
    upbit_hourly_pages = _ceil_div(hourly_bars, UPBIT_PAGE_SIZE)
    binance_daily_pages = _ceil_div(binance_daily_bars, BINANCE_PAGE_SIZE)
    binance_hourly_pages = _ceil_div(hourly_bars, BINANCE_PAGE_SIZE)
    projected_total = (
        2  # frozen universe snapshots
        + 2  # exactly one empirical delisted-history probe per venue
        + upbit_symbols * (upbit_daily_pages + upbit_hourly_pages)
        + binance_symbols * (binance_daily_pages + binance_hourly_pages)
    )
    return RequestBudgetPlan(
        upbit_symbols=upbit_symbols,
        binance_symbols=binance_symbols,
        upbit_daily_pages_per_symbol=upbit_daily_pages,
        upbit_hourly_pages_per_symbol=upbit_hourly_pages,
        binance_daily_pages_per_symbol=binance_daily_pages,
        binance_hourly_pages_per_symbol=binance_hourly_pages,
        universe_snapshot_requests=2,
        delisted_probe_requests=2,
        projected_total=projected_total,
    )


def _upbit_universe(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        raise SourceDataError("Upbit market/all response is not a JSON list")
    symbols = []
    for item in payload:
        if not isinstance(item, dict):
            raise SourceDataError("Upbit market/all contains a non-object item")
        market = item.get("market")
        if isinstance(market, str) and market.startswith("KRW-"):
            symbols.append(market)
    if not symbols:
        raise SourceDataError("Upbit market/all supplied zero KRW markets")
    return sorted(set(symbols))


def _binance_universe(payload: Any) -> tuple[list[str], dict[str, str | None]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise SourceDataError("Binance exchangeInfo response has no symbols list")
    symbols: list[str] = []
    status_by_symbol: dict[str, str | None] = {}
    for item in payload["symbols"]:
        if not isinstance(item, dict):
            raise SourceDataError("Binance exchangeInfo contains a non-object symbol")
        symbol = item.get("symbol")
        permissions = item.get("permissions")
        has_spot_permission = isinstance(permissions, list) and "SPOT" in permissions
        is_spot = bool(item.get("isSpotTradingAllowed")) or has_spot_permission
        if isinstance(symbol, str) and item.get("quoteAsset") == "USDT" and is_spot:
            symbols.append(symbol)
            status = item.get("status")
            status_by_symbol[symbol] = status if isinstance(status, str) else None
    if not symbols:
        raise SourceDataError("Binance exchangeInfo supplied zero USDT spot markets")
    return sorted(set(symbols)), status_by_symbol


def _response_error(response: ApiResponse) -> str:
    detail = response.error or "unknown_response_error"
    body_hash = hashlib.sha256(response.body).hexdigest() if response.body else "none"
    return f"{detail}; response_sha256={body_hash}"


class CorpusBuilder:
    """Build one immutable, restartable public corpus at the literal root."""

    def __init__(
        self,
        *,
        store: ArtifactStore | None = None,
        client: PublicApiClient | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.store = store or ArtifactStore(ARTIFACT_ROOT)
        self.client = client or PublicApiClient(self.store)
        self.now = now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self._last_heartbeat = self.monotonic()
        self._preflight_payload: dict[str, Any] | None = None
        self._completed = 0
        self._total_tasks = 0
        self._last_checkpoint = "none"

    def _append_progress(self, stage: str, *, force: bool = False) -> None:
        current = self.monotonic()
        if not force and current - self._last_heartbeat < 55:
            return
        self._last_heartbeat = current
        path = Path(PROGRESS_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        started = (
            self._preflight_payload.get("started_at")
            if self._preflight_payload
            else None
        )
        elapsed = "unknown"
        if isinstance(started, str):
            try:
                elapsed = f"{(self.now() - _parse_utc_timestamp(started)).total_seconds():.1f}s"
            except ValueError:
                elapsed = "unknown"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{utc_iso(self.now())} | {stage} | {self._completed}/{self._total_tasks} | "
                f"requests={self.client.requests_actual} | elapsed={elapsed} | "
                f"checkpoint={self._last_checkpoint}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def _load_latest_preflight(self) -> dict[str, Any] | None:
        records = self.store.load_json_records(self.store.preflight)
        if not records:
            return None
        return max(records, key=lambda item: str(item.get("created_at", "")))

    def _publish_raw_input(
        self,
        body: bytes,
        *,
        stem: str,
        kind: str,
    ) -> FileRecord:
        relative = self.store.new_relative_path("inputs", stem, ".json")
        staged = self.store.stage_bytes(body, relative, kind=kind)
        return self.store.publish(staged)

    def preflight(self) -> dict[str, Any]:
        """Freeze both venue universes, then enforce the request budget gate."""
        existing = self._load_latest_preflight()
        if existing is not None:
            self._preflight_payload = existing
            self._append_progress("PREFLIGHT_REUSED", force=True)
            return existing

        started_at = utc_iso(self.now())
        self._append_progress("UNIVERSE_SNAPSHOT_START", force=True)
        upbit = self.client.get_json("upbit_krw", UPBIT_MARKETS_URL)
        if not upbit.ok:
            payload = {
                "corpus_id": CORPUS_ID,
                "created_at": utc_iso(self.now()),
                "started_at": started_at,
                "status": "BLOCKED_PRECONDITION",
                "reason": f"upbit_universe_snapshot_failed:{_response_error(upbit)}",
                "requests_actual_at_preflight": self.client.requests_actual,
            }
            self.store.write_preflight(payload)
            self._preflight_payload = payload
            self._append_progress("BLOCKED_UPBIT_UNIVERSE", force=True)
            return payload
        upbit_raw = self._publish_raw_input(
            upbit.body,
            stem="upbit-market-all-raw",
            kind="universe_raw_response",
        )

        binance = self.client.get_json("binance_usdt_spot", BINANCE_EXCHANGE_INFO_URL)
        if not binance.ok:
            payload = {
                "corpus_id": CORPUS_ID,
                "created_at": utc_iso(self.now()),
                "started_at": started_at,
                "status": "BLOCKED_PRECONDITION",
                "reason": f"binance_universe_snapshot_failed:{_response_error(binance)}",
                "requests_actual_at_preflight": self.client.requests_actual,
                "inputs": [upbit_raw.as_dict()],
            }
            self.store.write_preflight(payload)
            self._preflight_payload = payload
            self._append_progress("BLOCKED_BINANCE_UNIVERSE", force=True)
            return payload
        binance_raw = self._publish_raw_input(
            binance.body,
            stem="binance-exchange-info-raw",
            kind="universe_raw_response",
        )

        try:
            upbit_symbols = _upbit_universe(upbit.payload)
            binance_symbols, binance_status = _binance_universe(binance.payload)
        except SourceDataError as exc:
            payload = {
                "corpus_id": CORPUS_ID,
                "created_at": utc_iso(self.now()),
                "started_at": started_at,
                "status": "BLOCKED_PRECONDITION",
                "reason": f"universe_parse_failed:{exc}",
                "requests_actual_at_preflight": self.client.requests_actual,
                "inputs": [upbit_raw.as_dict(), binance_raw.as_dict()],
            }
            self.store.write_preflight(payload)
            self._preflight_payload = payload
            self._append_progress("BLOCKED_UNIVERSE_PARSE", force=True)
            return payload

        plan = calculate_request_budget(len(upbit_symbols), len(binance_symbols))
        status = (
            "READY_FOR_COLLECTION"
            if plan.projected_total <= MAX_REQUESTS
            else "BLOCKED_PRECONDITION"
        )
        payload = {
            "corpus_id": CORPUS_ID,
            "created_at": utc_iso(self.now()),
            "started_at": started_at,
            "status": status,
            "reason": (
                None
                if status == "READY_FOR_COLLECTION"
                else f"request_budget_projected={plan.projected_total}>max={MAX_REQUESTS}"
            ),
            "requests_actual_at_preflight": self.client.requests_actual,
            "request_budget": plan.as_dict(),
            "inputs": [upbit_raw.as_dict(), binance_raw.as_dict()],
            "universe": {
                "upbit_krw": upbit_symbols,
                "binance_usdt_spot": binance_symbols,
                "binance_status_by_symbol": binance_status,
            },
        }
        self.store.write_preflight(payload)
        self._preflight_payload = payload
        stage = (
            "PREFLIGHT_READY" if status == "READY_FOR_COLLECTION" else "BLOCKED_BUDGET"
        )
        self._append_progress(stage, force=True)
        return payload

    def _check_wall_clock(self) -> bool:
        if self._preflight_payload is None:
            raise RuntimeError("preflight has not run")
        started_at = _parse_utc_timestamp(str(self._preflight_payload["started_at"]))
        return (self.now() - started_at).total_seconds() <= MAX_WALL_CLOCK_SECONDS

    def _fetch_upbit(self, symbol: str, frequency: str) -> FetchResult:
        start, end, interval_ms = _task_window("upbit_krw", frequency)
        cursor = end
        rows: list[Bar] = []
        previous_cursor: datetime | None = None
        endpoint = UPBIT_DAYS_URL if frequency == "1d" else UPBIT_HOURS_URL
        while True:
            if not self._check_wall_clock():
                return FetchResult(rows, error="max_wall_clock_exceeded")
            query = urlencode(
                {
                    "market": symbol,
                    "to": utc_iso(cursor),
                    "count": UPBIT_PAGE_SIZE,
                }
            )
            try:
                response = self.client.get_json("upbit_krw", f"{endpoint}?{query}")
            except RequestBudgetExceeded:
                return FetchResult(
                    rows, error="request_budget_exhausted", budget_exhausted=True
                )
            self._append_progress("FETCH_UPBIT")
            if not response.ok:
                return FetchResult(
                    rows,
                    error=_response_error(response),
                    rate_limited=response.rate_limited,
                )
            if not isinstance(response.payload, list):
                return FetchResult(rows, error="upbit_candles_non_list_payload")
            if not response.payload:
                break
            try:
                page = [
                    _normalize_upbit(item, frequency, symbol)
                    for item in response.payload
                ]
            except (KeyError, TypeError, ValueError, SourceDataError) as exc:
                return FetchResult([], error=f"upbit_normalization_failed:{exc}")
            rows.extend(bar for bar in page if start <= _utc_from_ms(bar.open_ms) < end)
            oldest = min(_utc_from_ms(bar.open_ms) for bar in page)
            if oldest <= start or len(page) < UPBIT_PAGE_SIZE:
                break
            if previous_cursor is not None and oldest >= previous_cursor:
                return FetchResult(rows, error="upbit_pagination_nonprogress")
            previous_cursor = cursor
            cursor = oldest
        return self._finalize_fetch(rows, symbol, frequency, start, end, interval_ms)

    def _fetch_binance(self, symbol: str, frequency: str) -> FetchResult:
        start, end, interval_ms = _task_window("binance_usdt_spot", frequency)
        cursor_ms = _milliseconds(start)
        end_ms = _milliseconds(end)
        rows: list[Bar] = []
        while cursor_ms < end_ms:
            if not self._check_wall_clock():
                return FetchResult(rows, error="max_wall_clock_exceeded")
            query = urlencode(
                {
                    "symbol": symbol,
                    "interval": frequency,
                    "startTime": cursor_ms,
                    "endTime": end_ms - 1,
                    "limit": BINANCE_PAGE_SIZE,
                }
            )
            try:
                response = self.client.get_json(
                    "binance_usdt_spot", f"{BINANCE_KLINES_URL}?{query}"
                )
            except RequestBudgetExceeded:
                return FetchResult(
                    rows, error="request_budget_exhausted", budget_exhausted=True
                )
            self._append_progress("FETCH_BINANCE")
            if not response.ok:
                return FetchResult(
                    rows,
                    error=_response_error(response),
                    rate_limited=response.rate_limited,
                )
            if not isinstance(response.payload, list):
                return FetchResult(rows, error="binance_klines_non_list_payload")
            if not response.payload:
                break
            try:
                page = [
                    _normalize_binance(item, frequency, symbol)
                    for item in response.payload
                ]
            except (IndexError, TypeError, ValueError, SourceDataError) as exc:
                return FetchResult([], error=f"binance_normalization_failed:{exc}")
            rows.extend(
                bar
                for bar in page
                if _milliseconds(start) <= bar.open_ms < _milliseconds(end)
            )
            next_cursor = max(bar.open_ms for bar in page) + interval_ms
            if next_cursor <= cursor_ms:
                return FetchResult(rows, error="binance_pagination_nonprogress")
            cursor_ms = next_cursor
            if len(page) < BINANCE_PAGE_SIZE:
                break
        return self._finalize_fetch(rows, symbol, frequency, start, end, interval_ms)

    @staticmethod
    def _finalize_fetch(
        rows: list[Bar],
        symbol: str,
        frequency: str,
        start: datetime,
        end: datetime,
        interval_ms: int,
    ) -> FetchResult:
        seen: set[int] = set()
        for row in rows:
            if row.open_ms in seen:
                return FetchResult(
                    [], error=f"duplicate_source_row:{symbol}@{row.open_ms}"
                )
            seen.add(row.open_ms)
            if row.close_exclusive_ms - row.open_ms != interval_ms:
                return FetchResult(
                    [], error=f"interval_invariant_failed:{symbol}@{row.open_ms}"
                )
            if row.close_exclusive_ms > _milliseconds(end):
                return FetchResult(
                    [], error=f"unfinished_bar_rejected:{symbol}@{row.open_ms}"
                )
        rows.sort(key=lambda row: row.open_ms)
        return FetchResult(rows)

    @staticmethod
    def _gap_ranges(
        *,
        venue: str,
        symbol: str,
        frequency: str,
        rows: list[Bar],
        task_error: str | None,
    ) -> list[dict[str, Any]]:
        """Explicitly enumerate every missing source range; never fill one."""
        start, end, interval_ms = _task_window(venue, frequency)
        start_ms = _milliseconds(start)
        end_ms = _milliseconds(end)
        if task_error:
            return [
                {
                    "venue": venue,
                    "symbol": symbol,
                    "frequency": frequency,
                    "start_utc": utc_iso(start),
                    "end_utc": utc_iso(end),
                    "missing_bars": (end_ms - start_ms) // interval_ms,
                    "reason": task_error,
                }
            ]
        sorted_rows = sorted(rows, key=lambda row: row.open_ms)
        gaps: list[dict[str, Any]] = []
        expected = start_ms
        first_observed = True
        for row in sorted_rows:
            if row.open_ms > expected:
                gaps.append(
                    {
                        "venue": venue,
                        "symbol": symbol,
                        "frequency": frequency,
                        "start_utc": utc_iso(_utc_from_ms(expected)),
                        "end_utc": utc_iso(_utc_from_ms(row.open_ms)),
                        "missing_bars": (row.open_ms - expected) // interval_ms,
                        "reason": (
                            "prelisting_or_unavailable_before_first_observed"
                            if first_observed
                            else "source_missing_interior_bars"
                        ),
                    }
                )
            expected = max(expected, row.open_ms + interval_ms)
            first_observed = False
        if expected < end_ms:
            gaps.append(
                {
                    "venue": venue,
                    "symbol": symbol,
                    "frequency": frequency,
                    "start_utc": utc_iso(_utc_from_ms(expected)),
                    "end_utc": utc_iso(end),
                    "missing_bars": (end_ms - expected) // interval_ms,
                    "reason": (
                        "source_empty"
                        if not sorted_rows
                        else "source_missing_after_last_observed"
                    ),
                }
            )
        return gaps

    @staticmethod
    def _row_groups(rows: list[Bar]) -> dict[tuple[bool, int], list[Bar]]:
        groups: dict[tuple[bool, int], list[Bar]] = defaultdict(list)
        holdout_start_ms = _milliseconds(HOLDOUT_START)
        for row in rows:
            is_holdout = row.open_ms >= holdout_start_ms
            groups[(is_holdout, _utc_from_ms(row.open_ms).year)].append(row)
        return groups

    @staticmethod
    def _record_from_dict(payload: dict[str, Any]) -> FileRecord:
        return FileRecord(
            relative_path=str(payload["relative_path"]),
            sha256=str(payload["sha256"]),
            byte_size=int(payload["byte_size"]),
            row_count=(
                int(payload["row_count"]) if payload["row_count"] is not None else None
            ),
            kind=str(payload["kind"]),
            is_holdout=bool(payload.get("is_holdout", False)),
        )

    def _ensure_artifact_budget(self, staged: list[StagedFile]) -> None:
        """Reject a task before publication if known artifacts would exceed 15 GiB."""
        existing_receipts = self._receipt_payloads()
        known_bytes = 0
        for receipt in existing_receipts:
            for record in receipt.get("files", []):
                known_bytes += int(record["byte_size"])
        for input_record in self._preflight_payload.get("inputs", []):
            known_bytes += int(input_record["byte_size"])
        candidate_bytes = sum(item.record.byte_size for item in staged)
        if known_bytes + candidate_bytes > MAX_ARTIFACT_BYTES:
            raise SourceDataError(
                "artifact_budget_exceeded_before_publish: "
                f"{known_bytes + candidate_bytes}>{MAX_ARTIFACT_BYTES}"
            )

    def _stage_task_files(
        self,
        venue: str,
        symbol: str,
        frequency: str,
        rows: list[Bar],
    ) -> list[StagedFile]:
        staged: list[StagedFile] = []
        for (is_holdout, year), group in sorted(self._row_groups(rows).items()):
            base = "holdout" if is_holdout else "dataset"
            filename = f"{symbol}__{frequency}__{self.store._token()}.parquet"
            relative = f"{base}/venue={venue}/year={year}/{filename}"
            table = pa.Table.from_pylist(
                [_bar_to_row(row) for row in group], schema=BAR_SCHEMA
            )
            staged.append(
                self.store.stage_parquet(table, relative, is_holdout=is_holdout)
            )
        return staged

    def _receipt_payloads(self) -> list[dict[str, Any]]:
        return self.store.load_json_records(self.store.receipts)

    @staticmethod
    def _task_key(venue: str, symbol: str, frequency: str) -> str:
        return f"{venue}|{symbol}|{frequency}"

    def _completed_task_keys(self) -> set[str]:
        return {
            str(receipt["task_key"])
            for receipt in self._receipt_payloads()
            if receipt.get("status") in {"completed", "completed_with_gaps"}
        }

    def _resume_inflight(self) -> None:
        """Complete recorded atomic moves without ever reading final holdout data."""
        receipts = self._receipt_payloads()
        completed_transactions = {str(item.get("transaction_id")) for item in receipts}
        for inflight in self.store.load_json_records(self.store.inflight):
            transaction_id = str(inflight.get("transaction_id"))
            if transaction_id in completed_transactions:
                continue
            self.store.publish_inflight(inflight)
            receipt_payload = dict(inflight["receipt_payload"])
            receipt_payload["resumed_from_inflight"] = True
            record = self.store.write_receipt(receipt_payload)
            self._last_checkpoint = record.relative_path
            self._append_progress("RESUMED_INFLIGHT", force=True)

    def _persist_task(
        self,
        *,
        venue: str,
        symbol: str,
        frequency: str,
        result: FetchResult,
    ) -> FileRecord:
        """Stage, record, atomically publish, then receipt one task result."""
        task_key = self._task_key(venue, symbol, frequency)
        gaps = self._gap_ranges(
            venue=venue,
            symbol=symbol,
            frequency=frequency,
            rows=result.bars,
            task_error=result.error,
        )
        staged: list[StagedFile] = []
        if result.bars:
            try:
                staged = self._stage_task_files(venue, symbol, frequency, result.bars)
                self._ensure_artifact_budget(staged)
            except (OSError, ValueError, SourceDataError) as exc:
                # No partial result is promoted if its artifact cannot meet the
                # cap/format contract.  The untouched staging partial remains
                # as evidence; we never delete it.
                result = FetchResult([], error=f"artifact_write_failed:{exc}")
                gaps = self._gap_ranges(
                    venue=venue,
                    symbol=symbol,
                    frequency=frequency,
                    rows=[],
                    task_error=result.error,
                )
                staged = []
        rows_by_split = {
            "dataset": sum(
                item.record.row_count or 0
                for item in staged
                if not item.record.is_holdout
            ),
            "holdout": sum(
                item.record.row_count or 0 for item in staged if item.record.is_holdout
            ),
        }
        transaction_id = self.store._token()
        receipt_payload = {
            "corpus_id": CORPUS_ID,
            "transaction_id": transaction_id,
            "task_key": task_key,
            "venue": venue,
            "symbol": symbol,
            "frequency": frequency,
            "completed_at": utc_iso(self.now()),
            "status": "completed_with_gaps" if gaps else "completed",
            "error": result.error,
            "rate_limited": result.rate_limited,
            "budget_exhausted": result.budget_exhausted,
            "rows_by_split": rows_by_split,
            "files": [item.record.as_dict() for item in staged],
            "gaps": gaps,
            "validation": {
                "duplicate_rows_stored": 0,
                "ohlc_invariant_violations_stored": 0,
                "negative_volume_stored": 0,
                "unfinished_bar_stored": False,
                "forward_fill_used": False,
            },
        }
        inflight_payload = {
            "corpus_id": CORPUS_ID,
            "transaction_id": transaction_id,
            "created_at": utc_iso(self.now()),
            "staged_files": [item.as_dict() for item in staged],
            "receipt_payload": receipt_payload,
        }
        self.store.write_inflight(inflight_payload)
        self.store.publish_inflight(inflight_payload)
        receipt_record = self.store.write_receipt(receipt_payload)
        self._last_checkpoint = receipt_record.relative_path
        if result.error:
            self.store.append_jsonl(
                "errors.jsonl",
                {
                    "at": utc_iso(self.now()),
                    "task_key": task_key,
                    "error": result.error,
                    "rate_limited": result.rate_limited,
                    "budget_exhausted": result.budget_exhausted,
                },
            )
        return receipt_record

    def _publish_probe_evidence(
        self,
        *,
        venue: str,
        identifier: str,
        response: ApiResponse,
    ) -> dict[str, Any]:
        """Persist an actual public request and raw response for delist evidence."""
        request_record = self.store.publish_json_once(
            {"venue": venue, "identifier": identifier, "url": response.url},
            "inputs/delisted-probes",
            f"{venue}-{identifier}-request",
            kind="delisted_probe_request",
        )
        response_relative = self.store.new_relative_path(
            "inputs/delisted-probes",
            f"{venue}-{identifier}-response",
            ".raw",
        )
        response_record = self.store.publish(
            self.store.stage_bytes(
                response.body,
                response_relative,
                kind="delisted_probe_raw_response",
            )
        )
        if response.rate_limited or response.status is None:
            verdict = "UNKNOWN"
        elif response.ok and isinstance(response.payload, list) and response.payload:
            verdict = "AVAILABLE"
        elif response.ok:
            verdict = "UNAVAILABLE"
        else:
            verdict = "UNAVAILABLE"
        return {
            "venue": venue,
            "identifier": identifier,
            "request_url": response.url,
            "http_status": response.status,
            "transport_error": response.error,
            "availability_verdict": verdict,
            "rate_limited": response.rate_limited,
            "request_evidence": request_record.as_dict(),
            "raw_response_evidence": response_record.as_dict(),
        }

    def _probe_delisted_history(self) -> dict[str, dict[str, Any]]:
        """Measure both historic candidates once, using only their venue API."""
        results = self._delisted_probe_receipts()
        if "upbit_krw" not in results:
            upbit_query = urlencode(
                {
                    "market": UPBIT_DELISTED_PROBE_MARKET,
                    "to": utc_iso(CUTOFF_END),
                    "count": 1,
                }
            )
            try:
                upbit = self.client.get_json(
                    "upbit_krw", f"{UPBIT_DAYS_URL}?{upbit_query}"
                )
            except RequestBudgetExceeded:
                upbit = ApiResponse(
                    url=f"{UPBIT_DAYS_URL}?{upbit_query}",
                    venue="upbit_krw",
                    status=None,
                    body=b"",
                    payload=None,
                    error="request_budget_exhausted",
                    rate_limited=False,
                )
            results["upbit_krw"] = self._publish_probe_evidence(
                venue="upbit_krw",
                identifier=UPBIT_DELISTED_PROBE_MARKET,
                response=upbit,
            )
            self.store.publish_json_once(
                results["upbit_krw"],
                "control/delisted-probes",
                "probe",
                kind="delisted_probe_receipt",
            )
            self._append_progress("DELISTED_PROBE_UPBIT", force=True)
            if upbit.rate_limited or upbit.error == "request_budget_exhausted":
                return results

        if "binance_usdt_spot" not in results:
            binance_query = urlencode(
                {
                    "symbol": BINANCE_DELISTED_PROBE_SYMBOL,
                    "interval": "1d",
                    "startTime": 0,
                    "endTime": _milliseconds(CUTOFF_END) - 1,
                    "limit": 1,
                }
            )
            try:
                binance = self.client.get_json(
                    "binance_usdt_spot", f"{BINANCE_KLINES_URL}?{binance_query}"
                )
            except RequestBudgetExceeded:
                binance = ApiResponse(
                    url=f"{BINANCE_KLINES_URL}?{binance_query}",
                    venue="binance_usdt_spot",
                    status=None,
                    body=b"",
                    payload=None,
                    error="request_budget_exhausted",
                    rate_limited=False,
                )
            results["binance_usdt_spot"] = self._publish_probe_evidence(
                venue="binance_usdt_spot",
                identifier=BINANCE_DELISTED_PROBE_SYMBOL,
                response=binance,
            )
            self.store.publish_json_once(
                results["binance_usdt_spot"],
                "control/delisted-probes",
                "probe",
                kind="delisted_probe_receipt",
            )
            self._append_progress("DELISTED_PROBE_BINANCE", force=True)
        return results

    @staticmethod
    def _year_expected_bars(
        *,
        venue: str,
        frequency: str,
        year: int,
    ) -> int:
        start, end, interval_ms = _task_window(venue, frequency)
        year_start = datetime(year, 1, 1, tzinfo=UTC)
        year_end = datetime(year + 1, 1, 1, tzinfo=UTC)
        overlap_start = max(start, year_start)
        overlap_end = min(end, year_end)
        if overlap_end <= overlap_start:
            return 0
        return (
            _milliseconds(overlap_end) - _milliseconds(overlap_start)
        ) // interval_ms

    def _coverage(
        self,
        receipts: list[dict[str, Any]],
        universe: dict[str, list[str]],
    ) -> list[dict[str, Any]]:
        actual: dict[tuple[str, str, int, str], int] = defaultdict(int)
        for receipt in receipts:
            for record in receipt.get("files", []):
                relative = str(record["relative_path"])
                components = Path(relative).parts
                try:
                    venue = next(
                        part.split("=", 1)[1]
                        for part in components
                        if part.startswith("venue=")
                    )
                    year = int(
                        next(
                            part.split("=", 1)[1]
                            for part in components
                            if part.startswith("year=")
                        )
                    )
                except (StopIteration, ValueError):
                    continue
                storage = "holdout" if bool(record.get("is_holdout")) else "dataset"
                frequency = str(receipt["frequency"])
                actual[(venue, frequency, year, storage)] += int(
                    record.get("row_count") or 0
                )
        coverage: list[dict[str, Any]] = []
        for venue in VENUES:
            for frequency in ("1d", "1h"):
                start, end, _ = _task_window(venue, frequency)
                for year in range(start.year, end.year + 1):
                    expected_per_symbol = self._year_expected_bars(
                        venue=venue,
                        frequency=frequency,
                        year=year,
                    )
                    if not expected_per_symbol:
                        continue
                    storage = "holdout" if year >= HOLDOUT_START.year else "dataset"
                    denominator = expected_per_symbol * len(universe[venue])
                    numerator = actual[(venue, frequency, year, storage)]
                    coverage.append(
                        {
                            "venue": venue,
                            "frequency": frequency,
                            "year": year,
                            "storage": storage,
                            "denominator": denominator,
                            "numerator": numerator,
                            "ratio": numerator / denominator if denominator else 1.0,
                        }
                    )
        return coverage

    @staticmethod
    def _all_task_keys(universe: dict[str, list[str]]) -> list[tuple[str, str, str]]:
        tasks: list[tuple[str, str, str]] = []
        for venue in VENUES:
            for symbol in universe[venue]:
                for frequency in ("1d", "1h"):
                    tasks.append((venue, symbol, frequency))
        return tasks

    def _unstarted_gaps(
        self,
        universe: dict[str, list[str]],
        completed: set[str],
        reason: str,
    ) -> list[dict[str, Any]]:
        gaps: list[dict[str, Any]] = []
        for venue, symbol, frequency in self._all_task_keys(universe):
            if self._task_key(venue, symbol, frequency) in completed:
                continue
            start, end, interval_ms = _task_window(venue, frequency)
            gaps.append(
                {
                    "venue": venue,
                    "symbol": symbol,
                    "frequency": frequency,
                    "start_utc": utc_iso(start),
                    "end_utc": utc_iso(end),
                    "missing_bars": (_milliseconds(end) - _milliseconds(start))
                    // interval_ms,
                    "reason": reason,
                }
            )
        return gaps

    def _delisted_probe_receipts(self) -> dict[str, dict[str, Any]]:
        records = self.store.load_json_records(self.store.control / "delisted-probes")
        result: dict[str, dict[str, Any]] = {}
        for record in records:
            venue = record.get("venue")
            if isinstance(venue, str):
                result[venue] = record
        return result

    def _safe_artifact_bytes(self, receipts: list[dict[str, Any]]) -> int:
        """Count non-holdout files directly and holdout files from receipts only."""
        total = 0
        for path in self.store.root.rglob("*"):
            if "holdout" in path.relative_to(self.store.root).parts:
                continue
            if path.is_file():
                total += path.stat().st_size
        holdout_records: dict[str, int] = {}
        for receipt in receipts:
            for record in receipt.get("files", []):
                if record.get("is_holdout"):
                    holdout_records[str(record["relative_path"])] = int(
                        record["byte_size"]
                    )
        return total + sum(holdout_records.values())

    def _write_terminal_manifest(
        self,
        *,
        terminal_verdict: str,
        terminal_reason: str | None,
    ) -> FileRecord:
        preflight = self._preflight_payload or {}
        universe_payload = preflight.get("universe", {})
        universe = {
            "upbit_krw": list(universe_payload.get("upbit_krw", [])),
            "binance_usdt_spot": list(universe_payload.get("binance_usdt_spot", [])),
        }
        receipts = self._receipt_payloads()
        completed = self._completed_task_keys()
        all_gaps: list[dict[str, Any]] = []
        for receipt in receipts:
            all_gaps.extend(receipt.get("gaps", []))
        if universe["upbit_krw"] or universe["binance_usdt_spot"]:
            all_gaps.extend(
                self._unstarted_gaps(
                    universe,
                    completed,
                    terminal_reason or "not_collected_before_terminal",
                )
            )
        coverage = self._coverage(receipts, universe) if any(universe.values()) else []
        coverage_record = self.store.publish_json_once(
            {
                "corpus_id": CORPUS_ID,
                "generated_at": utc_iso(self.now()),
                "coverage": coverage,
            },
            "coverage",
            "coverage",
            kind="coverage",
        )
        gaps_record = self.store.publish_json_once(
            {
                "corpus_id": CORPUS_ID,
                "generated_at": utc_iso(self.now()),
                "gaps": all_gaps,
            },
            "coverage",
            "explicit-gaps",
            kind="explicit_gaps",
        )
        delisted = self._delisted_probe_receipts()
        unavailable_venues = sorted(
            venue
            for venue, result in delisted.items()
            if result.get("availability_verdict") == "UNAVAILABLE"
        )
        if unavailable_venues:
            delisted_unavailable = ",".join(unavailable_venues)
        elif delisted:
            delisted_unavailable = "NONE"
        else:
            delisted_unavailable = "UNKNOWN"

        file_records: dict[str, FileRecord] = {}
        for record_payload in preflight.get("inputs", []):
            record = self._record_from_dict(record_payload)
            file_records[record.relative_path] = record
        for receipt in receipts:
            for record_payload in receipt.get("files", []):
                record = self._record_from_dict(record_payload)
                file_records[record.relative_path] = record
        for probe in delisted.values():
            for evidence_key in ("request_evidence", "raw_response_evidence"):
                evidence = probe.get(evidence_key)
                if isinstance(evidence, dict):
                    record = self._record_from_dict(evidence)
                    file_records[record.relative_path] = record
        file_records[coverage_record.relative_path] = coverage_record
        file_records[gaps_record.relative_path] = gaps_record
        ordered_records = [file_records[key] for key in sorted(file_records)]
        hashes_text = "".join(
            f"{record.sha256}  {record.relative_path}\n" for record in ordered_records
        ).encode("utf-8")
        checksum_relative = self.store.new_relative_path(
            "manifest", "checksums", ".sha256"
        )
        checksum_record = self.store.publish(
            self.store.stage_bytes(hashes_text, checksum_relative, kind="checksum_list")
        )
        artifact_bytes = self._safe_artifact_bytes(receipts)
        row_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        for receipt in receipts:
            venue = str(receipt["venue"])
            frequency = str(receipt["frequency"])
            for split, amount in receipt.get("rows_by_split", {}).items():
                row_counts[(venue, frequency, str(split))] += int(amount)
        holdout_rows = sum(
            value
            for (venue, _frequency, split), value in row_counts.items()
            if split == "holdout"
        )
        manifest = {
            "corpus_id": CORPUS_ID,
            "purpose": PURPOSE,
            "generated_at": utc_iso(self.now()),
            "terminal_verdict": terminal_verdict,
            "terminal_reason": terminal_reason,
            "scope": {
                "venues": list(VENUES),
                "auth": AUTH,
                "source_fallback": "NONE",
                "operating_db_reads": 0,
                "operating_db_writes": 0,
                "broker_or_account_calls": 0,
                "signed_endpoint_calls": 0,
                "cutoff_end_exclusive_utc": utc_iso(CUTOFF_END),
                "timezone_1d_bucket": "UTC",
                "forward_fill_used": False,
                "venues_mixed": False,
                "holdout_access_log": HOLDOUT_ACCESS_LOG,
                "holdout_read_operations": 0,
                "holdout_written_not_read": holdout_rows > 0,
                "windows": {
                    "train_end_inclusive": "2022-12-31T23:59:59Z",
                    "validation_start": "2023-01-01T00:00:00Z",
                    "validation_end_inclusive": "2024-12-31T23:59:59Z",
                    "holdout_start": utc_iso(HOLDOUT_START),
                    "holdout_end_inclusive": "2026-07-31T23:59:59Z",
                    "forward_oos_start": "2026-08-03T00:00:00Z",
                },
            },
            "request_budget": preflight.get("request_budget"),
            "requests_actual": self.client.requests_actual,
            "universe": universe_payload,
            "delisted_history": delisted,
            "DELISTED_PAIRS_UNAVAILABLE": delisted_unavailable,
            "rows": {
                "by_venue_frequency_split": [
                    {
                        "venue": venue,
                        "frequency": frequency,
                        "split": split,
                        "rows": rows,
                    }
                    for (venue, frequency, split), rows in sorted(row_counts.items())
                ],
                "rows_1d_upbit": row_counts[("upbit_krw", "1d", "dataset")]
                + row_counts[("upbit_krw", "1d", "holdout")],
                "rows_1d_binance": row_counts[("binance_usdt_spot", "1d", "dataset")]
                + row_counts[("binance_usdt_spot", "1d", "holdout")],
                "rows_1h_exploration_20230801_20241231": sum(
                    row_counts[(venue, "1h", "dataset")] for venue in VENUES
                ),
                "rows_1h_holdout_20250101_20260731": sum(
                    row_counts[(venue, "1h", "holdout")] for venue in VENUES
                ),
            },
            "coverage": coverage,
            "coverage_file": coverage_record.as_dict(),
            "explicit_gaps_file": gaps_record.as_dict(),
            "explicit_gap_count": len(all_gaps),
            "validation": {
                "duplicate_rows_stored": 0,
                "ohlc_invariant_violations_stored": 0,
                "negative_volume_stored": 0,
                "unfinished_bar_stored": False,
                "forward_fill_used": False,
                "source_fallback_used": False,
                "venues_mixed": False,
            },
            "file_hashes": [record.as_dict() for record in ordered_records],
            "checksum_list": checksum_record.as_dict(),
            "artifact_bytes_without_holdout_read": artifact_bytes,
            "artifact_gib_without_holdout_read": artifact_bytes / (1024**3),
            "checkpoints": {
                "receipt_count": len(receipts),
                "last_checkpoint": self._last_checkpoint,
            },
        }
        manifest_record = self.store.publish_json_once(
            manifest,
            "manifest",
            "manifest",
            kind="manifest",
        )
        manifest_sha_relative = self.store.new_relative_path(
            "manifest", "manifest", ".sha256"
        )
        _manifest_sha_record = self.store.publish(
            self.store.stage_bytes(
                f"{manifest_record.sha256}  {manifest_record.relative_path}\n".encode(),
                manifest_sha_relative,
                kind="manifest_checksum",
            )
        )
        self._last_checkpoint = manifest_record.relative_path
        self._append_progress(f"TERMINAL_{terminal_verdict}", force=True)
        return manifest_record

    def run(self, *, preflight_only: bool = False) -> FileRecord | None:
        """Execute the gate and, only if it passes, the corpus collection."""
        preflight = self.preflight()
        if preflight.get("status") != "READY_FOR_COLLECTION":
            return self._write_terminal_manifest(
                terminal_verdict="BLOCKED_PRECONDITION",
                terminal_reason=str(preflight.get("reason") or "preflight_not_ready"),
            )
        if preflight_only:
            return None

        self._resume_inflight()
        universe_payload = preflight["universe"]
        universe = {
            "upbit_krw": list(universe_payload["upbit_krw"]),
            "binance_usdt_spot": list(universe_payload["binance_usdt_spot"]),
        }
        all_tasks = self._all_task_keys(universe)
        completed = self._completed_task_keys()
        self._completed = len(completed)
        self._total_tasks = len(all_tasks)
        self._append_progress("COLLECTION_RESUME", force=True)

        probes = self._probe_delisted_history()
        probe_stop = any(
            result.get("rate_limited")
            or result.get("transport_error") == "request_budget_exhausted"
            for result in probes.values()
        )
        terminal_reason: str | None = None
        if probe_stop:
            terminal_reason = "delisted_probe_rate_limit_or_request_budget"

        for venue in VENUES:
            if terminal_reason:
                break
            for symbol in universe[venue]:
                if terminal_reason:
                    break
                for frequency in ("1d", "1h"):
                    key = self._task_key(venue, symbol, frequency)
                    if key in completed:
                        continue
                    if not self._check_wall_clock():
                        terminal_reason = "max_wall_clock_exceeded"
                        break
                    result = (
                        self._fetch_upbit(symbol, frequency)
                        if venue == "upbit_krw"
                        else self._fetch_binance(symbol, frequency)
                    )
                    # A failed page can arrive after valid pages.  Validate the
                    # retained rows before publishing even that partial result.
                    if result.bars:
                        start, end, interval_ms = _task_window(venue, frequency)
                        clean = self._finalize_fetch(
                            result.bars, symbol, frequency, start, end, interval_ms
                        )
                        if clean.error:
                            result = clean
                        else:
                            result.bars = clean.bars
                    self._persist_task(
                        venue=venue,
                        symbol=symbol,
                        frequency=frequency,
                        result=result,
                    )
                    completed.add(key)
                    self._completed = len(completed)
                    self._append_progress("TASK_CHECKPOINT", force=True)
                    if result.rate_limited:
                        terminal_reason = "rate_limit_or_block_signal"
                        break
                    if result.budget_exhausted:
                        terminal_reason = "request_budget_exhausted"
                        break

        receipts = self._receipt_payloads()
        has_gaps = any(receipt.get("gaps") for receipt in receipts)
        delisted = self._delisted_probe_receipts()
        has_delisted_limit = any(
            record.get("availability_verdict") != "AVAILABLE"
            for record in delisted.values()
        )
        all_completed = len(completed) == len(all_tasks)
        verdict = (
            "READY_FOR_RESEARCH"
            if all_completed
            and not has_gaps
            and not has_delisted_limit
            and not terminal_reason
            else "BUILT_WITH_GAPS"
        )
        return self._write_terminal_manifest(
            terminal_verdict=verdict,
            terminal_reason=terminal_reason,
        )
