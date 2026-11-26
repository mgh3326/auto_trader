"""Evidence from the official VTTO5201R per-order output1 rows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .constants import SLL_BUY_DVSN

# Require explicit terminal wording. Remaining or rejected quantity alone does
# not distinguish expiry, cancellation, rejection, or a working order.
_NO_FILL_TERMINALS = {"취소완료": "cancelled", "만료": "expired"}


@dataclass(frozen=True)
class TerminalEvidence:
    status: str
    quantity: Decimal
    price: Decimal | None
    fee: Decimal | None
    order_time: str


def _number(value: object) -> Decimal | None:
    if isinstance(value, str) and re.fullmatch(
        r"[0-9]{1,12}(?:\.[0-9]{1,4})?", value.strip()
    ):
        return Decimal(value.strip())
    return None


def terminal_evidence(
    row: dict[str, Any], *, broker_symbol: str, side: str, quantity: Decimal
) -> TerminalEvidence | None:
    """Validate the facts after the caller has joined on order id and trade date."""
    if (
        row.get("pdno") != broker_symbol
        or row.get("sll_buy_dvsn_cd") != SLL_BUY_DVSN.get(side)
        or _number(row.get("ord_qty")) != quantity
        or quantity != 1
    ):
        return None
    filled = _number(row.get("tot_ccld_qty"))
    remaining = _number(row.get("qty"))
    price = _number(row.get("avg_idx"))
    fee = _number(row.get("fee"))  # fee_smtl belongs to output2 and is never allocated.
    if row.get("fee") not in (None, "") and fee is None:
        return None
    time = row.get("ord_tmd")
    if isinstance(time, str) and re.fullmatch(
        r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]", time
    ):
        if not "084500" <= time <= "154500":
            return None
    else:
        return None

    label = row.get("trad_dvsn_name")
    terminal = _NO_FILL_TERMINALS.get(label.strip()) if isinstance(label, str) else None
    if (
        filled == quantity
        and remaining == 0
        and _number(row.get("rjct_qty")) == 0
        and price is not None
        and price > 0
        and terminal is None
    ):
        return TerminalEvidence("filled", filled, price, fee, time)
    if terminal is not None and filled == 0 and remaining == 0 and price in (None, 0):
        return TerminalEvidence(terminal, filled, None, fee, time)
    return None
