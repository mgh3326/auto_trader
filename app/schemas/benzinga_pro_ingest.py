"""Strict wire contract for a Benzinga Pro capture."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

from app.services.invest_momentum_events.us_sources import normalize_us_symbol

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PAGES = 40
Session = Literal["us_pre", "us_regular", "us_post"]
WireDecimal = Annotated[str, Field(max_length=40, pattern=r"^-?[0-9]+(?:\.[0-9]+)?$")]
Volume = Annotated[str, Field(max_length=19, pattern=r"^[0-9]+(?:\.0+)?$")]
WireTimestamp = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)$"
    ),
]


def _normalize_symbol(value: str) -> str:
    symbol = normalize_us_symbol(value)
    if symbol is None:
        raise ValueError("invalid US symbol")
    return symbol


Symbol = Annotated[
    str,
    Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9]+(?:[./-][A-Za-z0-9]+)*$"),
    AfterValidator(_normalize_symbol),
]


class _CaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*")
    @classmethod
    def _postgres_text(cls, value: object) -> object:
        if isinstance(value, str):
            value.encode("utf-8")
            if "\x00" in value:
                raise ValueError("NUL is not allowed")
        return value


class ScannerRequest(_CaptureModel):
    fields: str = Field(min_length=1, max_length=2048)
    filter: str = Field(max_length=2048)
    sort_field: str = Field(min_length=1, max_length=128)
    page_size: int = Field(ge=25, le=25)
    pages: int = Field(ge=1, le=MAX_PAGES)


class Mover(_CaptureModel):
    symbol: Symbol
    rank: int = Field(ge=1, le=MAX_PAGES * 25)
    change_pct: WireDecimal | None
    volume: Volume | None
    price: WireDecimal | None
    previous_close: WireDecimal | None
    market_cap: WireDecimal | None
    sector: str | None = Field(max_length=128)
    excluded_reason: Literal["no_session_price"] | None

    @field_validator("change_pct", "price", "market_cap", "volume")
    @classmethod
    def _stored_number_range(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        limit = {
            "change_pct": Decimal("999999.9999"),
            "price": Decimal("99999999999999.999999"),
            "market_cap": Decimal("9999999999999999999999999999.99"),
            "volume": Decimal(2**63 - 1),
        }.get(info.field_name or "")
        if limit is not None and value is not None and abs(Decimal(value)) > limit:
            raise ValueError("number exceeds storage range")
        return value


class Movers(_CaptureModel):
    up: list[Mover] = Field(max_length=MAX_PAGES * 25)
    down: list[Mover] = Field(max_length=MAX_PAGES * 25)


class BreakingEvent(_CaptureModel):
    provider_id: str = Field(min_length=1, max_length=128)
    headline: str = Field(min_length=1, max_length=2000)
    symbols: list[Symbol] = Field(max_length=50)
    created_raw: str | None = Field(max_length=128)
    url: str | None = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def _public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username is not None
            or any(
                name.casefold().replace("-", "").replace("_", "")
                in {"key", "apikey", "sessionkey", "token", "accesstoken"}
                for name, _ in parse_qsl(url.query)
            )
        ):
            raise ValueError("invalid event URL")
        return value


class BenzingaProCapture(_CaptureModel):
    schema_version: Literal["benzinga-pro-capture/v1"] = Field(alias="schema")
    captured_at: WireTimestamp
    session: Session
    scanner_request: ScannerRequest
    movers: Movers
    breaking_events: list[BreakingEvent] = Field(max_length=200)

    @field_validator("captured_at")
    @classmethod
    def _valid_capture_time(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value


class IngestCounts(BaseModel):
    runs_inserted: int = 0
    movers_inserted: int = 0
    excluded_inserted: int = 0
    events_inserted: int = 0
