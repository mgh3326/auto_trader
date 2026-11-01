"""ROB-1440 — versioned execution intent and child request contract (v1).

Python owns *what* may trade (an :class:`ExecutionIntentV1`: authorized, capped,
expiring). A mechanical executor owns *when and at what price* inside it and asks
for that with a child request. The design and wire contract are in
``docs/design/1440-intent-gateway-contract.md``.

v1 is KRX cash only, so quantity and price are whole shares and whole won. Exact
integers leave nothing for a broker layer to truncate and keep the cap arithmetic
exact.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from app.services.kis_mock_attribution import InvalidStrategy, validate_strategy

SCHEMA_VERSION: Final = 1

#: Equal to ``LOCKED_ENVELOPE.max_single_notional_krw``; a test pins the two.
#: One intent can never authorize more than one locked single-order notional.
MAX_INTENT_NOTIONAL_KRW: Final = 5_000_000
MAX_INTENT_QUANTITY: Final = 1_000_000
MAX_LIMIT_PRICE_KRW: Final = 10_000_000
MAX_SLIPPAGE_BPS: Final = 500


def _attributable_strategy(value: str) -> str:
    """Refuse at authorization what the kis_mock attribution gate would refuse."""
    try:
        return validate_strategy(value)
    except InvalidStrategy as exc:
        raise ValueError("strategy is blank or a placeholder") from exc


_Reference = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
_KrxSymbol = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9][0-9A-Z]{5}$"),
]
_Strategy = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128),
    AfterValidator(_attributable_strategy),
]
_Quantity = Annotated[StrictInt, Field(ge=1, le=MAX_INTENT_QUANTITY)]
_PriceKrw = Annotated[StrictInt, Field(ge=1, le=MAX_LIMIT_PRICE_KRW)]
_Position = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InventoryConstraint(_Contract):
    """The position the intent assumes and the range it may leave it in.

    ``min_position_after`` cannot go below zero, so an intent cannot authorize a
    short position.
    """

    position_at_authorization: _Position
    min_position_after: _Position
    max_position_after: _Position

    @model_validator(mode="after")
    def _range_is_ordered(self) -> InventoryConstraint:
        if self.min_position_after > self.max_position_after:
            raise ValueError("min_position_after exceeds max_position_after")
        return self


class ExecutionIntentV1(_Contract):
    schema_version: Literal[1]
    intent_id: UUID
    strategy: _Strategy
    account: Literal["kis_mock"]
    venue: Literal["krx"]
    symbol: _KrxSymbol
    side: Literal["buy", "sell"]
    max_quantity: _Quantity
    max_notional_krw: Annotated[StrictInt, Field(ge=1, le=MAX_INTENT_NOTIONAL_KRW)]
    limit_price_krw: _PriceKrw
    max_slippage_bps: Annotated[StrictInt, Field(ge=0, le=MAX_SLIPPAGE_BPS)]
    expires_at: AwareDatetime
    inventory: InventoryConstraint
    policy_version: _Reference
    model_version: _Reference | None = None
    authorization_ref: _Reference


class SubmitChild(_Contract):
    action: Literal["submit"]
    intent_id: UUID
    idempotency_key: IdempotencyKey
    quantity: _Quantity
    limit_price_krw: _PriceKrw


class AmendChild(_Contract):
    """Replaces a working order. Both values are required: a partial amend would
    leave the resulting exposure depending on what the broker holds.
    """

    action: Literal["amend"]
    intent_id: UUID
    idempotency_key: IdempotencyKey
    target_idempotency_key: IdempotencyKey
    quantity: _Quantity
    limit_price_krw: _PriceKrw


class CancelChild(_Contract):
    action: Literal["cancel"]
    intent_id: UUID
    idempotency_key: IdempotencyKey
    target_idempotency_key: IdempotencyKey


ChildRequest = Annotated[
    SubmitChild | AmendChild | CancelChild, Field(discriminator="action")
]

_CHILD_REQUEST_ADAPTER: Final[TypeAdapter[SubmitChild | AmendChild | CancelChild]] = (
    TypeAdapter(ChildRequest)
)


def parse_child_request(payload: object) -> SubmitChild | AmendChild | CancelChild:
    """Validate one child request at the transport boundary."""
    return _CHILD_REQUEST_ADAPTER.validate_python(payload)


def contract_digest(contract: BaseModel) -> str:
    """SHA-256 of the canonical JSON form; equal payloads have equal digests."""
    canonical = json.dumps(
        contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
