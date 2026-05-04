"""Pure policy parser for watch intent payloads (ROB-103).

This module is a leaf:
- No DB, Redis, HTTP, settings, or logging side effects beyond a debug log on malformed JSON.
- Inputs are primitive / JSON-string; outputs are value objects.
- Trigger-time and add-time both call ``parse_policy``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

logger = logging.getLogger(__name__)

_PRICE_CONDITIONS: Final[frozenset[str]] = frozenset({"price_above", "price_below"})
_INTENT_MARKETS: Final[frozenset[str]] = frozenset({"kr", "us"})
_SIDES: Final[frozenset[str]] = frozenset({"buy", "sell"})


class WatchPolicyError(ValueError):
    """Validation error raised by :func:`parse_policy`.

    ``code`` is a stable string used by the MCP tool surface and tests.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class NotifyOnlyPolicy:
    action: Literal["notify_only"] = "notify_only"


@dataclass(frozen=True, slots=True)
class IntentPolicy:
    action: Literal["create_order_intent"]
    side: Literal["buy", "sell"]
    quantity: int | None
    notional_krw: Decimal | None
    limit_price: Decimal | None
    max_notional_krw: Decimal | None


WatchActionPolicy = NotifyOnlyPolicy | IntentPolicy


def _to_decimal_positive(value: object, code: str) -> Decimal:
    if isinstance(value, bool):
        raise WatchPolicyError(code)
    if not isinstance(value, (int, float, Decimal, str)):
        raise WatchPolicyError(code)
    try:
        decimal_value = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal raises a few different types
        raise WatchPolicyError(code) from exc
    if decimal_value <= 0:
        raise WatchPolicyError(code)
    return decimal_value


def _to_positive_int(value: object, code: str) -> int:
    if isinstance(value, bool):
        raise WatchPolicyError(code)
    if not isinstance(value, int):
        raise WatchPolicyError(code)
    if value <= 0:
        raise WatchPolicyError(code)
    return value


def parse_policy(
    *,
    market: str,
    target_kind: str,
    condition_type: str,
    raw_payload: str | None,
) -> WatchActionPolicy:
    if not raw_payload:
        return NotifyOnlyPolicy()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        logger.debug("watch payload is not JSON, treating as notify_only")
        return NotifyOnlyPolicy()
    if not isinstance(payload, dict):
        return NotifyOnlyPolicy()

    action = payload.get("action") or "notify_only"

    if action == "notify_only":
        for forbidden in ("side", "quantity", "notional_krw", "limit_price", "max_notional_krw"):
            if forbidden in payload:
                raise WatchPolicyError("notify_only_must_be_bare")
        return NotifyOnlyPolicy()

    if action != "create_order_intent":
        raise WatchPolicyError("action_unsupported")

    if market not in _INTENT_MARKETS:
        raise WatchPolicyError("intent_market_unsupported")
    if condition_type not in _PRICE_CONDITIONS:
        raise WatchPolicyError("intent_condition_unsupported")

    side = payload.get("side")
    if side not in _SIDES:
        raise WatchPolicyError("intent_side_invalid")

    has_quantity = "quantity" in payload
    has_notional = "notional_krw" in payload
    if has_quantity == has_notional:
        raise WatchPolicyError("intent_sizing_xor")

    if has_notional and market == "us":
        raise WatchPolicyError("intent_us_notional_krw_unsupported")

    quantity: int | None = None
    if has_quantity:
        quantity = _to_positive_int(payload["quantity"], "intent_quantity_invalid")

    notional_krw: Decimal | None = None
    if has_notional:
        notional_krw = _to_decimal_positive(
            payload["notional_krw"], "intent_notional_krw_invalid"
        )

    limit_price: Decimal | None = None
    if "limit_price" in payload:
        limit_price = _to_decimal_positive(
            payload["limit_price"], "intent_limit_price_invalid"
        )

    max_notional_krw: Decimal | None = None
    if "max_notional_krw" in payload:
        max_notional_krw = _to_decimal_positive(
            payload["max_notional_krw"], "intent_max_notional_invalid"
        )

    return IntentPolicy(
        action="create_order_intent",
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        notional_krw=notional_krw,
        limit_price=limit_price,
        max_notional_krw=max_notional_krw,
    )


__all__ = [
    "IntentPolicy",
    "NotifyOnlyPolicy",
    "WatchActionPolicy",
    "WatchPolicyError",
    "parse_policy",
]
