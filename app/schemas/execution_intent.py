"""ROB-1440 — versioned execution intent and child request contract (v1).

Python owns *what* may trade (an :class:`ExecutionIntentV1`: authorized, capped,
expiring). A mechanical executor owns *when and at what price* inside it and asks
for that with a child request. The design and wire contract are in
``docs/design/1440-intent-gateway-contract.md``.

v1 is KRX cash only, so quantity and price are whole shares and whole won. Exact
integers leave nothing for a broker layer to truncate and keep the cap arithmetic
exact.

v2 (hk 5441, ``docs/design/5438-go-execution-path.md`` §3) is the Upbit KRW
market. The account decides the field set: a v1 intent and its children are
unchanged byte for byte, and a v2 intent carries decimal quantities as strings
(at most eight places) so nothing is rounded on the way.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any, Final, Literal
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
    ValidationError,
    model_validator,
)

from app.core.upbit_tick import is_on_upbit_krw_tick, upbit_krw_tick
from app.services.kis_mock_attribution import InvalidStrategy, validate_strategy

SCHEMA_VERSION: Final = 1
SCHEMA_VERSION_V2: Final = 2
UPBIT_LIVE_ACCOUNT: Final = "upbit_live"

#: Equal to ``LOCKED_ENVELOPE.max_single_notional_krw``; a test pins the two.
#: One intent can never authorize more than one locked single-order notional.
MAX_INTENT_NOTIONAL_KRW: Final = 5_000_000
MAX_INTENT_QUANTITY: Final = 1_000_000
MAX_LIMIT_PRICE_KRW: Final = 10_000_000
MAX_SLIPPAGE_BPS: Final = 500
MAX_STRATEGY_LENGTH: Final = 128


def _attributable_strategy(value: str) -> str:
    """Refuse at authorization what the kis_mock attribution gate would refuse."""
    try:
        normalized = validate_strategy(value)
    except InvalidStrategy as exc:
        raise ValueError("strategy is blank or a placeholder") from exc
    # NFKC normalization can make the stored value longer than the raw input.
    if len(normalized) > MAX_STRATEGY_LENGTH:
        raise ValueError("strategy is too long after normalization")
    return normalized


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
    StringConstraints(strict=True, min_length=1, max_length=MAX_STRATEGY_LENGTH),
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


#: Upbit caps (design §3.2). A sell has no notional cap of its own (B7), so the
#: schema bound is only a sanity limit; a buy is bounded by its mandate.
MAX_UPBIT_QUANTITY: Final = Decimal("1000000")
MAX_UPBIT_LIMIT_PRICE_KRW: Final = 10_000_000_000
MIN_UPBIT_NOTIONAL_KRW: Final = 5_000
MAX_UPBIT_NOTIONAL_KRW: Final = 1_000_000_000_000
#: ``_place_order_impl`` refuses a client order id above 40 characters, and the
#: child key is the Upbit ``identifier``.
MAX_UPBIT_IDEMPOTENCY_KEY_LENGTH: Final = 40
_DECIMAL_PATTERN: Final = r"^[0-9]+(\.[0-9]{1,8})?$"


class PriceOffTick(ValueError):
    """The price is not a multiple of its Upbit KRW price unit."""


class PriceTickUnsupported(ValueError):
    """The price range uses a unit below 1 KRW, which a whole-KRW field cannot hold."""


def check_upbit_limit_price(price_krw: int) -> None:
    """Design §3.4. Raises :class:`PriceTickUnsupported` or :class:`PriceOffTick`."""
    price = Decimal(price_krw)
    if upbit_krw_tick(price) < 1:
        raise PriceTickUnsupported("price range uses a unit below 1 KRW")
    if not is_on_upbit_krw_tick(price):
        raise PriceOffTick("price is not on its Upbit KRW price unit")


def _positive_upbit_quantity(value: str) -> str:
    quantity = Decimal(value)
    if quantity <= 0 or quantity > MAX_UPBIT_QUANTITY:
        raise ValueError("quantity is outside the allowed range")
    return value


def _upbit_position(value: str) -> str:
    if Decimal(value) > MAX_UPBIT_QUANTITY * 1000:
        raise ValueError("position is outside the allowed range")
    return value


def _upbit_price_on_tick(value: int) -> int:
    check_upbit_limit_price(value)
    return value


_DecimalText = Annotated[str, StringConstraints(strict=True, pattern=_DECIMAL_PATTERN)]
UpbitQuantity = Annotated[_DecimalText, AfterValidator(_positive_upbit_quantity)]
_UpbitPosition = Annotated[_DecimalText, AfterValidator(_upbit_position)]
_UpbitPriceKrw = Annotated[StrictInt, Field(ge=1, le=MAX_UPBIT_LIMIT_PRICE_KRW)]
_UpbitSymbol = Annotated[
    str, StringConstraints(strict=True, pattern=r"^KRW-[A-Z0-9]{2,10}$")
]
_CodeSha = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]


class DecimalInventoryConstraint(_Contract):
    """The v2 form of :class:`InventoryConstraint`: exact decimal strings."""

    position_at_authorization: _UpbitPosition
    min_position_after: _UpbitPosition
    max_position_after: _UpbitPosition

    @model_validator(mode="after")
    def _range_is_ordered(self) -> DecimalInventoryConstraint:
        if Decimal(self.min_position_after) > Decimal(self.max_position_after):
            raise ValueError("min_position_after exceeds max_position_after")
        return self


class ExecutionIntentV2(_Contract):
    """An Upbit KRW intent. It exists only under a mandate (I1) and a sleeve.

    ``execution_mode`` has no default and never changes: a shadow intent can
    only ever reach the shadow forwarder (I6). ``origin='operator_exit'`` is the
    S6 operator exit and must name its exit row; every other intent comes from
    the strategy job.
    """

    schema_version: Literal[2]
    intent_id: UUID
    strategy: _Strategy
    account: Literal["upbit_live"]
    venue: Literal["upbit"]
    execution_mode: Literal["shadow", "live"]
    mandate_id: UUID
    sleeve_id: Annotated[StrictInt, Field(ge=1)]
    symbol: _UpbitSymbol
    side: Literal["buy", "sell"]
    max_quantity_decimal: UpbitQuantity
    max_notional_krw: Annotated[
        StrictInt, Field(ge=MIN_UPBIT_NOTIONAL_KRW, le=MAX_UPBIT_NOTIONAL_KRW)
    ]
    strategy_code_sha: _CodeSha
    limit_price_krw: Annotated[_UpbitPriceKrw, AfterValidator(_upbit_price_on_tick)]
    max_slippage_bps: Annotated[StrictInt, Field(ge=0, le=MAX_SLIPPAGE_BPS)]
    expires_at: AwareDatetime
    inventory: DecimalInventoryConstraint
    policy_version: _Reference
    model_version: _Reference | None = None
    authorization_ref: _Reference
    decision_ref: _Reference
    origin: Literal["strategy", "operator_exit"] = "strategy"
    operator_exit_id: UUID | None = None

    @model_validator(mode="after")
    def _origin_is_consistent(self) -> ExecutionIntentV2:
        if self.origin == "operator_exit":
            if self.operator_exit_id is None or self.side != "sell":
                raise ValueError("an operator exit is a sell that names its exit row")
            expected = f"operator-exit:{self.operator_exit_id}"
        else:
            if self.operator_exit_id is not None:
                raise ValueError("only an operator exit names an exit row")
            expected = f"mandate:{self.mandate_id}"
        if self.authorization_ref != expected:
            raise ValueError("authorization_ref does not name the authorization")
        return self


class UpbitSubmitChild(_Contract):
    """A submit against an ``upbit_live`` intent. ``quantity`` (integer) is not a
    field here, and ``quantity_decimal`` is not a field of :class:`SubmitChild`:
    the gateway refuses a child whose shape does not match its intent's account.
    """

    action: Literal["submit"]
    intent_id: UUID
    idempotency_key: IdempotencyKey
    quantity_decimal: UpbitQuantity
    limit_price_krw: _UpbitPriceKrw


_UPBIT_SUBMIT_ADAPTER: Final[TypeAdapter[UpbitSubmitChild]] = TypeAdapter(
    UpbitSubmitChild
)

AnyChild = SubmitChild | AmendChild | CancelChild | UpbitSubmitChild


def _is_upbit_submit(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("action") == "submit"
        and "quantity_decimal" in payload
    )


def parse_child_request(payload: object) -> AnyChild:
    """Validate one child request at the transport boundary.

    Only a submit that carries ``quantity_decimal`` is read as the Upbit shape.
    Every other payload takes the v1 path it always took, so a kis_mock request
    is validated, refused and digested exactly as before.
    """
    if _is_upbit_submit(payload):
        return _UPBIT_SUBMIT_ADAPTER.validate_python(payload)
    return _CHILD_REQUEST_ADAPTER.validate_python(payload)


class ChildIdentity(BaseModel):
    """The fields every child has, whatever its account (design 5438 §3.3)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    action: Literal["submit", "amend", "cancel"]
    intent_id: UUID
    idempotency_key: IdempotencyKey


def misshaped_submit_identity(payload: object) -> ChildIdentity | None:
    """Who a rejected submit was for, when only its field set can be at fault.

    A submit that carries the integer ``quantity``, or no ``quantity_decimal``,
    is read as the kis_mock shape and judged by that shape's limits, so a
    request meant for an ``upbit_live`` intent can fail validation before
    anybody knew which account it was for. This returns its identity so the
    gateway can look the intent up and answer the account-shape refusal the
    contract promises. Any other rejected payload returns ``None``.
    """
    if not isinstance(payload, dict) or payload.get("action") != "submit":
        return None
    if "quantity" not in payload and "quantity_decimal" in payload:
        return None
    try:
        return ChildIdentity.model_validate(payload)
    except ValidationError:
        return None


def child_quantity(child: Any) -> Decimal | None:
    """The quantity a child asks for, exact, whichever shape it has."""
    if isinstance(child, UpbitSubmitChild):
        return Decimal(child.quantity_decimal)
    quantity = getattr(child, "quantity", None)
    return None if quantity is None else Decimal(quantity)


def contract_digest(contract: BaseModel) -> str:
    """SHA-256 of the canonical JSON form; equal payloads have equal digests."""
    canonical = json.dumps(
        contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
