"""Immutable mock order intent encoding and vendor body construction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

BUY_PATH: Final = "/krstock/order/v1/cashBuy"
SELL_PATH: Final = "/krstock/order/v1/cashSell"
MODIFY_PATH: Final = "/krstock/order/v1/modify"
CANCEL_PATH: Final = "/krstock/order/v1/cancel"
ORDER_PATHS: Final = frozenset({BUY_PATH, SELL_PATH, MODIFY_PATH, CANCEL_PATH})
_SYMBOL = re.compile(r"^[0-9]{6}$")
_ORDER_NO = re.compile(r"^[1-9][0-9]{0,9}$")


class InvalidIntent(ValueError):
    """An order cannot be represented by the approved limit-order grammar."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    operation_kind: str
    side: str
    symbol: str
    quantity: int | None
    price: int | None
    original_order_id: str | None
    amend_scope: str | None
    account_ref: UUID
    body_schema_version: int = 1

    def validate(self) -> None:
        if type(self.operation_kind) is not str or self.operation_kind not in {
            "place",
            "modify",
            "cancel",
        }:
            raise InvalidIntent("operation kind is not approved")
        if type(self.side) is not str or self.side not in {"buy", "sell"}:
            raise InvalidIntent("side is not approved")
        if type(self.symbol) is not str or _SYMBOL.fullmatch(self.symbol) is None:
            raise InvalidIntent("symbol is not an exact six-digit KRX code")
        if (
            type(self.account_ref) is not UUID
            or type(self.body_schema_version) is not int
            or self.body_schema_version != 1
        ):
            raise InvalidIntent("account identity or body schema is invalid")
        if self.quantity is not None and (
            type(self.quantity) is not int or self.quantity <= 0
        ):
            raise InvalidIntent("quantity must be a positive integer")
        if self.price is not None and (type(self.price) is not int or self.price <= 0):
            raise InvalidIntent("price must be a positive integer")
        if self.operation_kind == "place":
            if (
                self.quantity is None
                or self.price is None
                or self.original_order_id is not None
                or self.amend_scope is not None
            ):
                raise InvalidIntent("place fields are incomplete")
        else:
            if (
                type(self.original_order_id) is not str
                or _ORDER_NO.fullmatch(self.original_order_id) is None
            ):
                raise InvalidIntent("original order number is invalid")
            if self.amend_scope not in {"full", "partial"}:
                raise InvalidIntent("amend scope is required")
            if self.operation_kind == "modify" and (
                self.quantity is None or self.price is None
            ):
                raise InvalidIntent("modify requires quantity and limit price")
            if self.operation_kind == "cancel" and (
                self.price is not None
                or ((self.amend_scope == "full") != (self.quantity is None))
            ):
                raise InvalidIntent("cancel scope and quantity differ")


def _field(tag: str, value: str | None) -> str:
    return (
        f"{tag}=-;" if value is None else f"{tag}={len(value.encode('ascii'))}:{value};"
    )


def body_digest(intent: OrderIntent) -> str:
    intent.validate()
    values = (
        ("op", intent.operation_kind),
        ("side", intent.side),
        ("sym", intent.symbol),
        ("qty", None if intent.quantity is None else str(intent.quantity)),
        ("px", None if intent.price is None else str(intent.price)),
        ("org", intent.original_order_id),
        ("scope", intent.amend_scope),
        ("acct", str(intent.account_ref)),
    )
    encoded = "nhplug-body-v1|" + "".join(_field(tag, value) for tag, value in values)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def build_body(intent: OrderIntent, verified_act_no: str) -> tuple[str, dict[str, Any]]:
    """Use only claimed fields and a broker-verified account number."""

    intent.validate()
    if type(verified_act_no) is not str or not verified_act_no:
        raise InvalidIntent("broker-verified mock account is required")
    if intent.operation_kind == "place":
        return (BUY_PATH if intent.side == "buy" else SELL_PATH), {
            "act_no": verified_act_no,
            "iem_cd": intent.symbol,
            "orr_qty": intent.quantity,
            "orr_pr": intent.price,
            "nmn_pr_tp_cd": "01",
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
    if intent.operation_kind == "modify":
        return MODIFY_PATH, {
            "act_no": verified_act_no,
            "org_mkt_orr_no": intent.original_order_id,
            "all_pat_dit_cd": "1" if intent.amend_scope == "full" else "2",
            "iem_cd": intent.symbol,
            "cor_qty": intent.quantity,
            "cor_pr": intent.price,
            "sop_cnd_pr": 0,
            "rmt_mkt_cd": "KRX",
            "sor_mkt_sli_yn": "N",
        }
    result: dict[str, Any] = {
        "act_no": verified_act_no,
        "org_mkt_orr_no": intent.original_order_id,
        "all_pat_dit_cd": "1" if intent.amend_scope == "full" else "2",
        "iem_cd": intent.symbol,
    }
    if intent.quantity is not None:
        result["cor_qty"] = intent.quantity
    return CANCEL_PATH, result
