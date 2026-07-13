"""Stable normalized evidence helpers for Kiwoom mock account reads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.validation import normalize_krx_symbol

REDACTED_VALUE = "[REDACTED]"


class KiwoomMockEvidenceError(ValueError):
    """Raised when broker evidence cannot prove a safe mock account read."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "kiwoom_mock_evidence_invalid",
    ) -> None:
        super().__init__(message)
        self.code = code


_COMPACT_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "authorizationheader",
        "token",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "appkey",
        "appsecret",
        "secretkey",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "approval",
        "approvalkey",
        "approvalhash",
        "accountno",
        "accountnumber",
        "accountid",
        "accountidentifier",
        "acctno",
        "acctnumber",
        "acctid",
        "acctidentifier",
        "acntno",
        "acntnumber",
        "acntid",
        "acntidentifier",
    }
)
_SENSITIVE_PARTS = frozenset(
    {
        "authorization",
        "token",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "approval",
        "secret",
    }
)
_ACCOUNT_KEY_PREFIXES = frozenset({"account", "acct", "acnt"})
_ACCOUNT_IDENTIFIER_PARTS = frozenset({"no", "number", "id", "identifier"})


def _key_parts(value: Any) -> tuple[str, ...]:
    raw = str(value).strip()
    with_camel_boundaries = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    return tuple(
        part for part in re.split(r"[^a-z0-9]+", with_camel_boundaries.lower()) if part
    )


def _compact_key(value: Any) -> str:
    return "".join(_key_parts(value))


def _required_rows(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise KiwoomMockEvidenceError(f"Kiwoom response missing list field {key}")
    if not all(isinstance(row, Mapping) for row in rows):
        raise KiwoomMockEvidenceError(f"Kiwoom response field {key} has invalid rows")
    return rows


def _required_text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    text = str(value or "").strip()
    if not text:
        raise KiwoomMockEvidenceError(f"Kiwoom row missing required field {key}")
    return text


def _required_order_id(row: Mapping[str, Any]) -> str:
    value = row.get("ord_no")
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise KiwoomMockEvidenceError(
            "Kiwoom row field ord_no is not a numeric order id"
        )
    return value


def _required_non_negative_int(row: Mapping[str, Any], key: str) -> int:
    text = _required_text(row, key).replace(",", "")
    try:
        value = int(text)
    except ValueError as exc:
        raise KiwoomMockEvidenceError(
            f"Kiwoom row field {key} is not an integer"
        ) from exc
    if value < 0:
        raise KiwoomMockEvidenceError(f"Kiwoom row field {key} is negative")
    return value


def _normalize_kr_symbol(row: Mapping[str, Any]) -> str:
    symbol = _required_text(row, "stk_cd")
    if len(symbol) == 7 and symbol[0].upper() in {"A", "J", "Q"}:
        symbol = symbol[1:]
    try:
        return normalize_krx_symbol(symbol)
    except ValueError as exc:
        raise KiwoomMockEvidenceError("Kiwoom row has invalid KRX symbol") from exc


def normalize_positions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for row in _required_rows(payload, "acnt_evlt_remn_indv_tot"):
        positions.append(
            {
                "symbol": _normalize_kr_symbol(row),
                "quantity": _required_non_negative_int(row, "rmnd_qty"),
                "average_price": _required_non_negative_int(row, "pur_pric"),
                "currency": "KRW",
            }
        )
    return positions


def normalize_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for row in _required_rows(payload, "acnt_ord_cntr_prst_array"):
        ordered_quantity = _required_non_negative_int(row, "ord_qty")
        filled_quantity = _required_non_negative_int(row, "cntr_qty")
        if ordered_quantity <= 0:
            raise KiwoomMockEvidenceError("Kiwoom order quantity must be positive")
        if filled_quantity > ordered_quantity:
            raise KiwoomMockEvidenceError(
                "Kiwoom filled quantity exceeds ordered quantity"
            )

        change_type = str(row.get("mdfy_cncl_tp") or "").strip().lower()
        if "취소" in change_type or "cancel" in change_type:
            status = "cancelled"
        elif filled_quantity == 0:
            status = "open"
        elif filled_quantity < ordered_quantity:
            status = "partially_filled"
        else:
            status = "filled"

        orders.append(
            {
                "order_id": _required_order_id(row),
                "symbol": _normalize_kr_symbol(row),
                "status": status,
                "ordered_price": _required_non_negative_int(row, "ord_uv"),
                "filled_quantity": filled_quantity,
                "average_price": _required_non_negative_int(row, "cntr_uv"),
                "remaining_quantity": (
                    0 if status == "cancelled" else ordered_quantity - filled_quantity
                ),
            }
        )
    return orders


def redact_broker_response(payload: dict[str, Any]) -> dict[str, Any]:
    def is_sensitive_key(value: Any) -> bool:
        parts = _key_parts(value)
        compact = "".join(parts)
        compact_without_header_prefix = (
            "".join(parts[1:]) if parts and parts[0] == "x" else compact
        )
        if {
            compact,
            compact_without_header_prefix,
        }.intersection(_COMPACT_SENSITIVE_KEYS):
            return True
        if _SENSITIVE_PARTS.intersection(parts):
            return True
        return bool(
            parts
            and parts[0] in _ACCOUNT_KEY_PREFIXES
            and _ACCOUNT_IDENTIFIER_PARTS.intersection(parts[1:])
        )

    def redact(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): REDACTED_VALUE if is_sensitive_key(key) else redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, tuple):
            return [redact(item) for item in value]
        return value

    return redact(payload)


def validate_mock_response_provenance(payload: dict[str, Any]) -> None:
    def reject(message: str) -> None:
        raise KiwoomMockEvidenceError(
            message,
            code="kiwoom_mock_provenance_conflict",
        )

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = _compact_key(raw_key)
                is_scalar = isinstance(item, (str, int, bool))
                text = str(item).strip().lower() if is_scalar else ""
                if key == "environment" and text != "mock":
                    reject(
                        "Kiwoom mock response contains non-mock environment provenance"
                    )
                if key == "accountmode" and text != "kiwoom_mock":
                    reject(
                        "Kiwoom mock response contains conflicting account provenance"
                    )
                if key in {"source", "broker"} and text not in {
                    "kiwoom",
                    "kiwoom_mock",
                }:
                    reject("Kiwoom mock response contains non-mock broker provenance")
                if key == "ismock" and not (
                    item is True
                    or type(item) is int
                    and item == 1
                    or isinstance(item, str)
                    and text in {"true", "1"}
                ):
                    reject("Kiwoom mock response contains non-mock provenance")
                if key in {"host", "baseurl"}:
                    if not isinstance(item, str) or not text:
                        reject(
                            "Kiwoom mock response contains malformed host provenance"
                        )
                    allowed_values = (
                        {"mockapi.kiwoom.com"}
                        if key == "host"
                        else {
                            constants.MOCK_BASE_URL.lower(),
                            f"{constants.MOCK_BASE_URL.lower()}/",
                        }
                    )
                    if text not in allowed_values:
                        reject("Kiwoom mock response contains non-mock host provenance")
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)


def build_mock_provenance(api_id: str) -> dict[str, str]:
    return {
        "broker": "kiwoom",
        "environment": "mock",
        "account_mode": "kiwoom_mock",
        "host": urlparse(constants.MOCK_BASE_URL).hostname or "mockapi.kiwoom.com",
        "api_id": api_id,
    }


__all__ = [
    "REDACTED_VALUE",
    "KiwoomMockEvidenceError",
    "build_mock_provenance",
    "normalize_orders",
    "normalize_positions",
    "redact_broker_response",
    "validate_mock_response_provenance",
]
