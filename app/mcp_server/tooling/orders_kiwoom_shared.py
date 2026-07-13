"""Shared fail-closed response shaping for Kiwoom MCP namespaces."""

from __future__ import annotations

from typing import Any

from app.services.brokers.kiwoom import constants

_PASSTHROUGH_KEYS = (
    "return_code",
    "return_msg",
    "continuation",
    "ord_no",
    "order_no",
)


def derive_broker_success(broker_response: dict[str, Any]) -> bool:
    if "return_code" not in broker_response:
        return False
    value = broker_response["return_code"]
    if value is None:
        return False
    try:
        return int(value) == constants.SUCCESS_RETURN_CODE
    except (TypeError, ValueError):
        return False


def classify_capability_unsupported(
    broker_response: dict[str, Any],
) -> str | None:
    message = str(broker_response.get("return_msg") or "")
    try:
        code = int(broker_response.get("return_code"))
    except (TypeError, ValueError):
        return None
    if code == 20 and (
        "RC9000" in message
        or "모의투자에서는 해당업무가 제공되지 않습니다" in message
    ):
        return "capability_unsupported"
    return None


def finalize_broker_response(
    base: dict[str, Any], broker_response: dict[str, Any]
) -> dict[str, Any]:
    response = {
        "success": derive_broker_success(broker_response),
        **base,
        "broker_response": broker_response,
    }
    for key in _PASSTHROUGH_KEYS:
        if key in broker_response:
            response[key] = broker_response[key]
    if error_code := classify_capability_unsupported(broker_response):
        response["error_code"] = error_code
    return response
