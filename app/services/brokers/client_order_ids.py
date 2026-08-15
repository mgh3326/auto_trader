"""Broker-facing client-order-ID constraints for mock/paper/demo lanes.

The internal lineage identifiers remain full SHA-256 values.  This module
holds only the bounded representation that is allowed to cross a broker
transport boundary; it performs no network or persistence work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

BROKER_CLIENT_ID_CONSTRAINT_VIOLATION: Final[str] = (
    "broker_client_id_constraint_violation"
)


class BrokerClientIdTarget(StrEnum):
    """Broker adapters with a confirmed client-order-ID boundary."""

    TOSS = "toss"
    BINANCE_SPOT_DEMO = "binance_spot_demo"
    ALPACA_PAPER = "alpaca_paper"


@dataclass(frozen=True)
class BrokerClientOrderIdConstraint:
    """The bounded, portable subset used by the corresponding adapter."""

    max_length: int
    allowed_pattern: re.Pattern[str]


# Toss's existing submit boundary already pins 36 and this alphabet.  The
# lineage factory emits the same portable subset for every supported target.
_PORTABLE_CLIENT_ORDER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_-]+"
)
TOSS_CLIENT_ORDER_ID_MAX_LENGTH: Final[int] = 36

# The existing Spot Demo adapter and its submit tests retain the 36-character
# boundary.  Explicitly centralizing it makes the pre-send assertion auditable.
BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH: Final[int] = 36

# Alpaca Paper's current POST /v2/orders contract permits client_order_id
# values up to 128 characters.  We send the portable ASCII subset above.
ALPACA_PAPER_CLIENT_ORDER_ID_MAX_LENGTH: Final[int] = 128

BROKER_CLIENT_ORDER_ID_CONSTRAINTS: Final[
    dict[BrokerClientIdTarget, BrokerClientOrderIdConstraint]
] = {
    BrokerClientIdTarget.TOSS: BrokerClientOrderIdConstraint(
        max_length=TOSS_CLIENT_ORDER_ID_MAX_LENGTH,
        allowed_pattern=_PORTABLE_CLIENT_ORDER_ID_PATTERN,
    ),
    BrokerClientIdTarget.BINANCE_SPOT_DEMO: BrokerClientOrderIdConstraint(
        max_length=BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH,
        allowed_pattern=_PORTABLE_CLIENT_ORDER_ID_PATTERN,
    ),
    BrokerClientIdTarget.ALPACA_PAPER: BrokerClientOrderIdConstraint(
        max_length=ALPACA_PAPER_CLIENT_ORDER_ID_MAX_LENGTH,
        allowed_pattern=_PORTABLE_CLIENT_ORDER_ID_PATTERN,
    ),
}


class BrokerClientOrderIdConstraintViolation(ValueError):
    """A broker-facing identifier cannot safely cross the send boundary."""

    reason_code: Final[str] = BROKER_CLIENT_ID_CONSTRAINT_VIOLATION

    def __init__(self, *, target: BrokerClientIdTarget) -> None:
        super().__init__(f"{BROKER_CLIENT_ID_CONSTRAINT_VIOLATION}: {target.value}")


def assert_broker_client_order_id(
    *, target: BrokerClientIdTarget, client_order_id: str
) -> None:
    """Fail closed unless an ID fits the target's pre-send constraints."""

    constraint = BROKER_CLIENT_ORDER_ID_CONSTRAINTS[target]
    if (
        not isinstance(client_order_id, str)
        or len(client_order_id) > constraint.max_length
        or constraint.allowed_pattern.fullmatch(client_order_id) is None
    ):
        raise BrokerClientOrderIdConstraintViolation(target=target)


__all__ = [
    "ALPACA_PAPER_CLIENT_ORDER_ID_MAX_LENGTH",
    "BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH",
    "BROKER_CLIENT_ID_CONSTRAINT_VIOLATION",
    "BROKER_CLIENT_ORDER_ID_CONSTRAINTS",
    "BrokerClientIdTarget",
    "BrokerClientOrderIdConstraint",
    "BrokerClientOrderIdConstraintViolation",
    "TOSS_CLIENT_ORDER_ID_MAX_LENGTH",
    "assert_broker_client_order_id",
]
