"""Shared fail-closed response shaping for Kiwoom MCP namespaces."""

from __future__ import annotations

import re
from typing import Any

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.normalization import redact_broker_response
from app.services.brokers.kiwoom.us_orders import validate_us_order_id

_PASSTHROUGH_KEYS = (
    "return_code",
    "return_msg",
    "continuation",
    "ord_no",
    "order_no",
)
_ORDER_ID_KEYS = ("ord_no", "order_no")
_RETURN_CODE_RE = re.compile(r"^-?[0-9]{1,18}$")


def derive_broker_success(broker_response: dict[str, Any]) -> bool:
    value = broker_response.get("return_code")
    if type(value) is int:  # bool is an int subclass and must fail closed.
        return value == constants.SUCCESS_RETURN_CODE
    return isinstance(value, str) and value == str(constants.SUCCESS_RETURN_CODE)


def _is_explicit_broker_rejection(broker_response: dict[str, Any]) -> bool:
    value = broker_response.get("return_code")
    if type(value) is int:
        return value != constants.SUCCESS_RETURN_CODE
    if not isinstance(value, str) or not _RETURN_CODE_RE.fullmatch(value):
        return False
    return int(value) != constants.SUCCESS_RETURN_CODE


def classify_capability_unsupported(
    broker_response: dict[str, Any],
) -> str | None:
    message = str(broker_response.get("return_msg") or "")
    try:
        code = int(broker_response.get("return_code"))
    except (TypeError, ValueError):
        return None
    if code == 20 and (
        "RC9000" in message or "모의투자에서는 해당업무가 제공되지 않습니다" in message
    ):
        return "capability_unsupported"
    return None


def finalize_broker_response(
    base: dict[str, Any], broker_response: dict[str, Any]
) -> dict[str, Any]:
    redacted_broker_response = redact_broker_response(broker_response)
    # ``success`` must always come from broker evidence: spread ``base`` first
    # so a caller-supplied ``success`` can never override the derivation.
    response = {
        **base,
        "success": derive_broker_success(broker_response),
        "broker_response": redacted_broker_response,
    }
    for key in _PASSTHROUGH_KEYS:
        if key in redacted_broker_response:
            response[key] = redacted_broker_response[key]
    if error_code := classify_capability_unsupported(broker_response):
        response["error_code"] = error_code
    return response


def finalize_place_broker_response(
    base: dict[str, Any], broker_response: dict[str, Any]
) -> dict[str, Any]:
    """Distinguish rejected, submitted, and accepted-but-untrackable places."""

    response = finalize_broker_response(base, broker_response)
    if not derive_broker_success(broker_response):
        if _is_explicit_broker_rejection(broker_response):
            response.update({"status": "rejected", "reconcile_required": False})
        else:
            response.update(
                {
                    "status": "acceptance_uncertain",
                    "reconcile_required": True,
                    "retry_allowed": False,
                }
            )
        return response

    order_id = None
    for key in _ORDER_ID_KEYS:
        raw_order_id = broker_response.get(key)
        if not isinstance(raw_order_id, str):
            continue
        try:
            order_id = validate_us_order_id(raw_order_id)
        except ValueError:
            continue
        break
    if order_id is None:
        response.update(
            {
                "success": False,
                "status": "accepted_untracked",
                "reconcile_required": True,
                "retry_allowed": False,
            }
        )
        return response

    response.update(
        {
            "success": True,
            "status": "submitted",
            "reconcile_required": False,
            "order_id": order_id,
        }
    )
    return response
