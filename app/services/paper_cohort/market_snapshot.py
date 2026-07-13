"""All-or-nothing canonical Binance public Spot snapshot capture."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.services.brokers.binance.dto import BinanceBookTicker, BinanceKlineRow
from app.services.brokers.binance.rest_client import BinancePublicRestClient
from app.services.paper_cohort.contracts import PaperCohortError
from app.services.research_canonical_hash import canonical_sha256

_SYMBOLS = ("BTCUSDT", "ETHUSDT")
_INTERVAL = "1m"
_SOURCE = "binance_public_spot"
_HOST = "https://api.binance.com"
_SCHEMA_ID = "canonical_market_snapshot.v1"

Identifier128 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
DecimalString = Annotated[str, StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]+)?$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenSnapshotContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SnapshotCaptureRequest(FrozenSnapshotContract):
    snapshot_id: Identifier128
    cohort_id: Identifier128
    run_id: Identifier128
    round_decision_id: Identifier128
    required_lookback: int = Field(gt=0, le=1000)
    max_capture_skew_ms: int = Field(gt=0)
    max_ticker_age_ms: int = Field(gt=0)


class CanonicalCandle(FrozenSnapshotContract):
    open_time: datetime
    close_time: datetime
    open: DecimalString
    high: DecimalString
    low: DecimalString
    close: DecimalString
    base_volume: DecimalString
    quote_volume: DecimalString
    trade_count: int = Field(gt=0)
    taker_buy_base_volume: DecimalString
    taker_buy_quote_volume: DecimalString


class CanonicalBookTicker(FrozenSnapshotContract):
    bid_price: DecimalString
    bid_qty: DecimalString
    ask_price: DecimalString
    ask_qty: DecimalString
    fetched_at: datetime


class CanonicalSymbolSnapshot(FrozenSnapshotContract):
    symbol: Literal["BTCUSDT", "ETHUSDT"]
    candles: tuple[CanonicalCandle, ...]
    ticker: CanonicalBookTicker


class CanonicalSnapshotPayload(FrozenSnapshotContract):
    schema_id: Literal["canonical_market_snapshot.v1"]
    snapshot_id: Identifier128
    cohort_id: Identifier128
    run_id: Identifier128
    round_decision_id: Identifier128
    source: Literal["binance_public_spot"]
    host: Literal["https://api.binance.com"]
    interval: Literal["1m"]
    required_lookback: int = Field(gt=0)
    max_capture_skew_ms: int = Field(gt=0)
    max_ticker_age_ms: int = Field(gt=0)
    capture_started_at: datetime
    capture_completed_at: datetime
    symbols: tuple[CanonicalSymbolSnapshot, CanonicalSymbolSnapshot]
    content_hash: Sha256

    def recomputed_content_hash(self) -> str:
        return canonical_sha256(
            self.model_dump(mode="python", exclude={"content_hash"})
        )


def _positive_finite(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


class CanonicalSnapshotCapture:
    def __init__(
        self,
        client: BinancePublicRestClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _validate_candles(
        symbol: str,
        rows: list[BinanceKlineRow],
        *,
        request: SnapshotCaptureRequest,
        first_open: datetime,
        end_time: datetime,
    ) -> None:
        if len(rows) != request.required_lookback:
            raise PaperCohortError("invalid_canonical_snapshot")
        expected_opens = [
            first_open + timedelta(minutes=index)
            for index in range(request.required_lookback)
        ]
        if [row.open_time for row in rows] != expected_opens:
            raise PaperCohortError("invalid_canonical_snapshot")
        if len({row.open_time for row in rows}) != len(rows):
            raise PaperCohortError("invalid_canonical_snapshot")
        for row in rows:
            numeric = (
                row.open,
                row.high,
                row.low,
                row.close,
                row.base_volume,
                row.quote_volume,
                row.taker_buy_base_volume,
                row.taker_buy_quote_volume,
            )
            if not all(_positive_finite(value) for value in numeric):
                raise PaperCohortError("invalid_canonical_snapshot")
            if not all(
                (
                    row.symbol == symbol,
                    row.interval == _INTERVAL,
                    row.is_closed,
                    _aware(row.open_time),
                    _aware(row.close_time),
                    row.close_time <= end_time,
                    row.close_time > row.open_time,
                    row.trade_count is not None and row.trade_count > 0,
                    row.high >= max(row.open, row.close),
                    row.low <= min(row.open, row.close),
                    row.high >= row.low,
                )
            ):
                raise PaperCohortError("invalid_canonical_snapshot")

    @staticmethod
    def _validate_tickers(
        tickers: dict[str, BinanceBookTicker],
        *,
        request: SnapshotCaptureRequest,
        capture_started_at: datetime,
        capture_completed_at: datetime,
    ) -> None:
        if tuple(tickers) != _SYMBOLS:
            raise PaperCohortError("invalid_canonical_snapshot")
        fetched_times: list[datetime] = []
        for symbol, ticker in tickers.items():
            numeric = (
                ticker.bid_price,
                ticker.bid_qty,
                ticker.ask_price,
                ticker.ask_qty,
            )
            if not all(_positive_finite(value) for value in numeric):
                raise PaperCohortError("invalid_canonical_snapshot")
            if not _aware(ticker.fetched_at):
                raise PaperCohortError("invalid_canonical_snapshot")
            if not all(
                (
                    ticker.symbol == symbol,
                    ticker.bid_price < ticker.ask_price,
                    ticker.fetched_at >= capture_started_at,
                    ticker.fetched_at <= capture_completed_at,
                    capture_completed_at - ticker.fetched_at
                    <= timedelta(milliseconds=request.max_ticker_age_ms),
                )
            ):
                raise PaperCohortError("invalid_canonical_snapshot")
            fetched_times.append(ticker.fetched_at)
        if max(fetched_times) - min(fetched_times) > timedelta(
            milliseconds=request.max_capture_skew_ms
        ):
            raise PaperCohortError("invalid_canonical_snapshot")

    async def capture(
        self, request: SnapshotCaptureRequest
    ) -> CanonicalSnapshotPayload:
        capture_started_at = self._clock()
        if not _aware(capture_started_at):
            raise PaperCohortError("invalid_canonical_snapshot")
        closed_boundary = capture_started_at.replace(second=0, microsecond=0)
        end_time = closed_boundary - timedelta(microseconds=1)
        first_open = closed_boundary - timedelta(minutes=request.required_lookback)

        rows_by_symbol: dict[str, list[BinanceKlineRow]] = {}
        tickers: dict[str, BinanceBookTicker] = {}
        try:
            for symbol in _SYMBOLS:
                rows_by_symbol[symbol] = await self._client.klines(
                    symbol,
                    _INTERVAL,
                    start_time=first_open,
                    end_time=end_time,
                    limit=request.required_lookback,
                )
            for symbol in _SYMBOLS:
                tickers[symbol] = await self._client.book_ticker(symbol)
        except Exception as exc:
            raise PaperCohortError("canonical_provider_error") from exc

        capture_completed_at = self._clock()
        if (
            not _aware(capture_completed_at)
            or capture_completed_at < capture_started_at
            or capture_completed_at - capture_started_at
            > timedelta(milliseconds=request.max_capture_skew_ms)
        ):
            raise PaperCohortError("invalid_canonical_snapshot")

        for symbol in _SYMBOLS:
            self._validate_candles(
                symbol,
                rows_by_symbol[symbol],
                request=request,
                first_open=first_open,
                end_time=end_time,
            )
        self._validate_tickers(
            tickers,
            request=request,
            capture_started_at=capture_started_at,
            capture_completed_at=capture_completed_at,
        )

        symbol_payloads = tuple(
            CanonicalSymbolSnapshot(
                symbol=symbol,
                candles=tuple(
                    CanonicalCandle(
                        open_time=row.open_time,
                        close_time=row.close_time,
                        open=_decimal_text(row.open),
                        high=_decimal_text(row.high),
                        low=_decimal_text(row.low),
                        close=_decimal_text(row.close),
                        base_volume=_decimal_text(row.base_volume),
                        quote_volume=_decimal_text(row.quote_volume),  # type: ignore[arg-type]
                        trade_count=row.trade_count,  # type: ignore[arg-type]
                        taker_buy_base_volume=_decimal_text(
                            row.taker_buy_base_volume  # type: ignore[arg-type]
                        ),
                        taker_buy_quote_volume=_decimal_text(
                            row.taker_buy_quote_volume  # type: ignore[arg-type]
                        ),
                    )
                    for row in rows_by_symbol[symbol]
                ),
                ticker=CanonicalBookTicker(
                    bid_price=_decimal_text(tickers[symbol].bid_price),
                    bid_qty=_decimal_text(tickers[symbol].bid_qty),
                    ask_price=_decimal_text(tickers[symbol].ask_price),
                    ask_qty=_decimal_text(tickers[symbol].ask_qty),
                    fetched_at=tickers[symbol].fetched_at,
                ),
            )
            for symbol in _SYMBOLS
        )
        content: dict[str, object] = {
            "schema_id": _SCHEMA_ID,
            "snapshot_id": request.snapshot_id,
            "cohort_id": request.cohort_id,
            "run_id": request.run_id,
            "round_decision_id": request.round_decision_id,
            "source": _SOURCE,
            "host": _HOST,
            "interval": _INTERVAL,
            "required_lookback": request.required_lookback,
            "max_capture_skew_ms": request.max_capture_skew_ms,
            "max_ticker_age_ms": request.max_ticker_age_ms,
            "capture_started_at": capture_started_at,
            "capture_completed_at": capture_completed_at,
            "symbols": symbol_payloads,
        }
        provisional = CanonicalSnapshotPayload(
            **content,
            content_hash="0" * 64,  # type: ignore[arg-type]
        )
        return provisional.model_copy(
            update={"content_hash": provisional.recomputed_content_hash()}
        )


__all__ = [
    "CanonicalBookTicker",
    "CanonicalCandle",
    "CanonicalSnapshotCapture",
    "CanonicalSnapshotPayload",
    "CanonicalSymbolSnapshot",
    "SnapshotCaptureRequest",
]
