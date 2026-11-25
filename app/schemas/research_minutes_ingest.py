"""Wire contract for the hk 6343 research minute-bar HTTP ingest.

One request carries a bounded batch for one market. Per-item outcomes are
positionally aligned with ``bars`` (the execution-ledger ingest shape), so a
producer can resend only what was refused. Unknown fields are refused: the
server — not the producer — owns ``session_segment``, ``session_date``,
``retrieved_at`` and ``batch_id`` generation.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.research_candles.minute_ingest import BATCH_ID_PATTERN

#: Hard bound for one batch. The Python collector reads at most ~150 symbols per
#: market and a 5-minute poll yields about one bar per symbol per minute, so
#: 1,000 covers a restart overlap without letting one request grow unbounded.
MAX_INGEST_BARS = 1000

IngestMarket = Literal["kr", "us", "futures"]
IngestItemStatus = Literal["inserted", "already_present", "rejected"]


class ResearchMinuteBarItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: KR/US: the ticker. Futures: the canonical ``<PRODUCT>.<YYMM>`` symbol.
    symbol: str
    #: KR/US: ``KRX``/``NTX``, ``NASD``/``NYSE``/``AMEX``. Futures: ``KRX``.
    venue: str
    #: Bar START in UTC (KIS labels a bar by its start).
    time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    #: KR: per-minute value, optional (NULL when unproven). US: required.
    #: Futures: must be absent (the futures table has no value column).
    value: Decimal | None = None
    #: The collector's actual clock when it received the response.
    received_at: datetime
    #: Closed vocabulary per market (see ``minute_ingest.SOURCES_BY_MARKET``).
    source: str
    #: Futures only, all optional. The server recomputes each from the symbol and
    #: the bar start; a sent value that disagrees is refused per item. KR/US
    #: items must not carry any of them.
    product: str | None = None
    #: ``YYMM`` as stored in ``contract_month`` (not ``YYYY-MM``).
    contract_month: str | None = None
    roll_state: str | None = None
    session_segment: str | None = None
    session_date: date | None = None

    def futures_fields_sent(self) -> bool:
        return any(
            value is not None
            for value in (
                self.product,
                self.contract_month,
                self.roll_state,
                self.session_segment,
                self.session_date,
            )
        )


class ResearchMinutesIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: IngestMarket
    #: Optional producer run id (``go-<collector>-<run>``); ``backfill`` in the
    #: name keeps the diagnostic first-receipt split. Generated when absent.
    batch_id: str | None = None
    bars: list[ResearchMinuteBarItem] = Field(min_length=1, max_length=MAX_INGEST_BARS)

    @field_validator("batch_id")
    @classmethod
    def _batch_id_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not BATCH_ID_PATTERN.fullmatch(value):
            raise ValueError("batch_id must be 1-100 characters of [A-Za-z0-9._:-]")
        return value


class ResearchMinutesIngestItemResult(BaseModel):
    """Per-item outcome, positionally aligned with the request ``bars`` list."""

    model_config = ConfigDict(extra="forbid")

    status: IngestItemStatus
    #: Closed rejection reason (``minute_ingest.REASONS``) or an exception class
    #: name for a write failure. Never a raw payload or message echo.
    reason: str | None = None


class ResearchMinutesIngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: IngestMarket
    batch_id: str
    received: int
    inserted: int
    already_present: int
    rejected: int
    results: list[ResearchMinutesIngestItemResult]


__all__ = [
    "MAX_INGEST_BARS",
    "ResearchMinuteBarItem",
    "ResearchMinutesIngestItemResult",
    "ResearchMinutesIngestRequest",
    "ResearchMinutesIngestResponse",
]
