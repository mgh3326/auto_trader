"""Broker-facing client-order-ID constraints for mock/paper/demo lanes.

The internal lineage identifiers remain full SHA-256 values.  This module
holds only the bounded representation that is allowed to cross a broker
transport boundary; it performs no network or persistence work.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

BROKER_CLIENT_ID_CONSTRAINT_VIOLATION: Final[str] = (
    "broker_client_id_constraint_violation"
)


class BrokerClientIdTarget(StrEnum):
    """Broker adapters with a confirmed client-order-ID boundary."""

    TOSS = "toss"
    BINANCE_SPOT_DEMO = "binance_spot_demo"
    ALPACA_PAPER = "alpaca_paper"


# These exact pairs are signed J2B contract inputs, not a dependency on the
# later lane registry. The read-only map prevents runtime reconfiguration of
# the factory's confirmed target boundary.
BROKER_CLIENT_ID_TARGET_PLAN_BROKERS: Final[Mapping[BrokerClientIdTarget, str]] = (
    MappingProxyType(
        {
            BrokerClientIdTarget.ALPACA_PAPER: "alpaca",
            BrokerClientIdTarget.BINANCE_SPOT_DEMO: "binance",
            BrokerClientIdTarget.TOSS: "toss",
        }
    )
)


@dataclass(frozen=True)
class BrokerClientOrderIdConstraint:
    """The bounded, portable subset used by the corresponding adapter."""

    max_length: int
    allowed_pattern: re.Pattern[str]


# This intentionally conservative portable subset is derived from the Toss
# boundary; it is not an assertion of Alpaca or Binance character constraints.
# The lineage factory emits the same subset for every supported target.
_PORTABLE_CLIENT_ORDER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_-]+"
)
TOSS_CLIENT_ORDER_ID_MAX_LENGTH: Final[int] = 36

BINANCE_SPOT_DEMO_CLIENT_ORDER_ID_MAX_LENGTH: Final[int] = 36

# Shared by the preview validator and the pre-send transport boundary.
ALPACA_PAPER_CLIENT_ORDER_ID_MAX_LENGTH: Final[int] = 48

BROKER_CLIENT_ORDER_ID_CONSTRAINTS: Final[
    Mapping[BrokerClientIdTarget, BrokerClientOrderIdConstraint]
] = MappingProxyType(
    {
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
)


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
    "BROKER_CLIENT_ID_TARGET_PLAN_BROKERS",
    "BrokerClientIdTarget",
    "BrokerClientOrderIdConstraint",
    "BrokerClientOrderIdConstraintViolation",
    "TOSS_CLIENT_ORDER_ID_MAX_LENGTH",
    "assert_broker_client_order_id",
]
