"""Strict decoding of fillwire's decrypted domestic KIS notification frame."""

import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.timezone import KST


class KisFillwireFrameError(ValueError):
    """The supplied raw frame cannot prove a notifiable domestic fill."""


def decode_fillwire_kis_frame(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Read the official KIS domestic indices, without compact-frame fallback.

    This is fillwire RawPayload (tr/fields/received_at), not the parsed dict
    produced by the Python websocket client. Errors contain no raw values.
    Overseas layouts require separate support and must never use these indices.
    """
    if raw.get("tr") not in ("H0STCNI0", "H0STCNI9"):
        raise KisFillwireFrameError("KIS fillwire domestic TR required")
    fields = raw.get("fields")
    if (
        not isinstance(fields, list)
        or len(fields) < 15
        or not all(isinstance(value, str) for value in fields)
    ):
        raise KisFillwireFrameError("KIS fillwire fields invalid")
    fields = [value.strip() for value in fields]

    # Same evidence-first classification as fillwire internal/decode/notice.go:
    # RFUS_YN, RCTF_CLS, ACPT_YN, CNTG_YN. Accept/cancel/reject is not a fill.
    codes = [fields[index] for index in (12, 5, 14, 13)]
    codes = [
        value.lstrip("0") or "0" if value.isascii() and value.isdigit() else value
        for value in codes
    ]
    if not (
        codes[0] == "0"
        and codes[1] in {"0", "1"}
        and codes[2] in {"1", "2"}
        and codes[3] == "2"
    ):
        raise KisFillwireFrameError("KIS fillwire frame is not a confirmed fill")

    order_id, symbol = fields[2], fields[8]
    side = {
        "01": "sell",
        "1": "sell",
        "S": "sell",
        "02": "buy",
        "2": "buy",
        "B": "buy",
    }.get(fields[4])
    if not order_id or not re.fullmatch(r"[0-9A-Z]{6}", symbol) or side is None:
        raise KisFillwireFrameError("KIS fillwire fill identity invalid")
    qty, price = fields[9], fields[10]
    if not re.fullmatch(r"[0-9]+", qty) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)?", price
    ):
        raise KisFillwireFrameError("KIS fillwire fill values invalid")
    quantity, unit_price = Decimal(qty), Decimal(price)
    if (
        quantity <= 0
        or unit_price <= 0
        or not all(
            math.isfinite(float(value)) and float(value) > 0
            for value in (quantity, unit_price, quantity * unit_price)
        )
    ):
        raise KisFillwireFrameError("KIS fillwire fill values invalid")

    received_at = raw.get("received_at")
    if not isinstance(received_at, str) or not re.fullmatch(r"[0-9]{6}", fields[11]):
        raise KisFillwireFrameError("KIS fillwire timestamps invalid")
    try:
        received = datetime.fromisoformat(received_at)
        clock = datetime.strptime(fields[11], "%H%M%S").time()
        if received.tzinfo is None or received.utcoffset() is None:
            raise KisFillwireFrameError("KIS fillwire received_at requires a timezone")
        received = received.astimezone(KST)
        filled_at = datetime.combine(received.date(), clock, tzinfo=KST)
        # A fill cannot follow receipt. Delayed frames can carry yesterday's clock.
        if filled_at > received:
            filled_at -= timedelta(days=1)
    except (ValueError, OverflowError):
        raise KisFillwireFrameError("KIS fillwire timestamps invalid") from None

    return {
        "symbol": symbol,
        "side": side,
        "order_id": order_id,
        "filled_qty": qty,
        "filled_price": price,
        "filled_at": filled_at.isoformat(),
        "market": "kr",
        "currency": "KRW",
        "fill_yn": "2",
        "fill_status": "filled",
    }
