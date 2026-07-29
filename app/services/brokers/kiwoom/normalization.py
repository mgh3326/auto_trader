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


def _required_non_negative_cash(payload: Mapping[str, Any], key: str) -> int:
    raw = payload.get(key)
    if raw is None:
        raise KiwoomMockEvidenceError(
            f"Kiwoom response missing required cash field {key}"
        )
    text = str(raw).strip()
    if not text:
        raise KiwoomMockEvidenceError(
            f"Kiwoom response missing required cash field {key}"
        )
    try:
        value = int(text.replace(",", ""))
    except ValueError as exc:
        raise KiwoomMockEvidenceError(
            f"Kiwoom cash field {key} is not an integer"
        ) from exc
    if value < 0:
        raise KiwoomMockEvidenceError(f"Kiwoom cash field {key} is negative")
    return value


def normalize_deposit(payload: dict[str, Any]) -> int:
    """Parse kt00001 deposit detail — ord_alow_amt only (ROB-891)."""
    return _required_non_negative_cash(payload, "ord_alow_amt")


def normalize_orderable_cash(payload: dict[str, Any]) -> int:
    """Parse kt00010 orderable amount — ord_alowa only (ROB-891)."""
    return _required_non_negative_cash(payload, "ord_alowa")


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


def normalize_order_detail(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse kt00007 ``acnt_ord_cntr_prps_dtl`` rows (ROB-1155).

    Deliberately a kt00007-only sibling of :func:`normalize_orders` rather than a
    generalization of it: kt00009 returns ``acnt_ord_cntr_prst_array`` with
    ``mdfy_cncl_tp``, while kt00007 returns ``acnt_ord_cntr_prps_dtl`` with
    ``mdfy_cncl`` plus ``ord_remnq`` (주문잔량) and a per-row ``dmst_stex_tp``.
    Widening the shared normalizer would put the already-shipped kt00009 read
    surface at regression risk for no gain.

    Evidence preserved beyond kt00009's shape:

    * ``remaining_quantity`` — broker-reported ``ord_remnq``, not derived.
    * ``unfilled_quantity`` — ``ord_qty - cntr_qty``, derived, for cross-checking.
    * ``remaining_quantity_consistent`` — whether the two agree. A partial cancel
      legitimately leaves ``ord_remnq`` below the unfilled amount, so a mismatch
      in that direction is surfaced as a flag rather than an error.
    * ``venue`` — raw per-row ``dmst_stex_tp``. Required=N in the official
      response table, so ``None`` when the broker omits it; never fabricated.
      This is the only field that can answer "which venue did the broker record
      this order on", so it is passed through untransformed.
    * ``change_type`` — raw ``mdfy_cncl`` text.

    Fail-close on malformed evidence: missing list, non-mapping rows, non-numeric
    ``ord_no``, missing/negative integers, non-positive ``ord_qty``, fills above
    the ordered quantity, or a remaining quantity above the unfilled quantity
    (arithmetically impossible).
    """

    orders: list[dict[str, Any]] = []
    for row in _required_rows(payload, constants.ACCOUNT_ORDER_DETAIL_LIST_KEY):
        ordered_quantity = _required_non_negative_int(row, "ord_qty")
        filled_quantity = _required_non_negative_int(row, "cntr_qty")
        remaining_quantity = _required_non_negative_int(row, "ord_remnq")
        if ordered_quantity <= 0:
            raise KiwoomMockEvidenceError("Kiwoom order quantity must be positive")
        if filled_quantity > ordered_quantity:
            raise KiwoomMockEvidenceError(
                "Kiwoom filled quantity exceeds ordered quantity"
            )
        unfilled_quantity = ordered_quantity - filled_quantity
        if remaining_quantity > unfilled_quantity:
            raise KiwoomMockEvidenceError(
                "Kiwoom remaining quantity exceeds unfilled quantity"
            )

        change_type = str(row.get("mdfy_cncl") or "").strip()
        change_type_key = change_type.lower()
        if "취소" in change_type or "cancel" in change_type_key:
            status = "cancelled"
        elif filled_quantity == 0:
            status = "open"
        elif filled_quantity < ordered_quantity:
            status = "partially_filled"
        else:
            status = "filled"

        raw_venue = row.get("dmst_stex_tp")
        venue = str(raw_venue).strip() if raw_venue is not None else ""

        orders.append(
            {
                "order_id": _required_order_id(row),
                "symbol": _normalize_kr_symbol(row),
                "status": status,
                "ordered_quantity": ordered_quantity,
                "ordered_price": _required_non_negative_int(row, "ord_uv"),
                "filled_quantity": filled_quantity,
                "average_price": _required_non_negative_int(row, "cntr_uv"),
                "remaining_quantity": remaining_quantity,
                "unfilled_quantity": unfilled_quantity,
                "remaining_quantity_consistent": (
                    remaining_quantity == unfilled_quantity
                ),
                "change_type": change_type or None,
                "venue": venue or None,
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


def validate_mock_response_provenance(
    payload: dict[str, Any],
    *,
    allowed_account_modes: frozenset[str] = frozenset({"kiwoom_mock"}),
) -> None:
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
                if key == "accountmode" and text not in allowed_account_modes:
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


def build_mock_provenance(
    api_id: str, *, account_mode: str = "kiwoom_mock"
) -> dict[str, str]:
    return {
        "broker": "kiwoom",
        "environment": "mock",
        "account_mode": account_mode,
        "host": urlparse(constants.MOCK_BASE_URL).hostname or "mockapi.kiwoom.com",
        "api_id": api_id,
    }


__all__ = [
    "REDACTED_VALUE",
    "KiwoomMockEvidenceError",
    "build_mock_provenance",
    "normalize_deposit",
    "normalize_orderable_cash",
    "normalize_order_detail",
    "normalize_orders",
    "normalize_positions",
    "redact_broker_response",
    "validate_mock_response_provenance",
]
