"""Wire-safe DTOs for funding advisory and external-cash declarations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


def canonical_decimal(value: Decimal) -> str:
    """Serialize a Decimal without float conversion or exponent notation."""

    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


class ExternalCashDeclarationRequest(BaseModel):
    """Admin-confirmed append-only declaration input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner_user_id: int = Field(gt=0)
    location_key: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_:-]*$",
    )
    display_label: str = Field(min_length=1, max_length=120)
    currency: str = Field(min_length=3, max_length=3)
    amount: Decimal = Field(ge=0, max_digits=38, decimal_places=12)
    as_of: datetime
    source_note: str = Field(min_length=1, max_length=500)
    expected_head_declaration_id: UUID | None = Field(...)
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        if value not in {"KRW", "USD"}:
            raise ValueError("currency must be one of KRW or USD")
        return value

    @field_validator("as_of")
    @classmethod
    def validate_aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_currency_precision(self) -> ExternalCashDeclarationRequest:
        if self.currency == "KRW" and self.amount != self.amount.to_integral_value():
            raise ValueError("KRW amount must use whole won")
        return self


class ExternalCashDeclarationRecord(BaseModel):
    """Immutable declaration as returned by the service."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    declaration_id: UUID
    owner_user_id: int
    location_key: str
    display_label: str
    currency: str
    amount: Decimal
    as_of: datetime
    fresh_until: datetime
    source_note: str
    declared_by_user_id: int
    origin: Literal["invest_ui"]
    supersedes_declaration_id: UUID | None
    idempotency_key: str
    recorded_at: datetime

    @field_serializer("amount", when_used="json")
    def serialize_amount(self, value: Decimal) -> str:
        return canonical_decimal(value)


ExternalCashCurrentStatus = Literal["missing", "fresh", "stale", "future", "ambiguous"]


class ExternalCashCurrentView(BaseModel):
    """One scope's current advisory-only value and freshness verdict."""

    model_config = ConfigDict(extra="forbid")

    status: ExternalCashCurrentStatus
    amount_status: Literal["known", "unknown"]
    current: ExternalCashDeclarationRecord | None
    route_fundable_amount: Decimal | None
    verification_badge: str = "운영자 선언 · 시스템 검증 불가"
    warning_code: str | None = None

    @field_serializer("route_fundable_amount", when_used="json")
    def serialize_route_amount(self, value: Decimal | None) -> str | None:
        return None if value is None else canonical_decimal(value)
