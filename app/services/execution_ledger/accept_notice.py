"""KIS H0STCNI0 accept-notice frame predicate (#1175).

fillwire stores each KIS domestic execution notice as
``{"tr", "fields", "received_at"}`` with the decrypted record fields in go-kis
``kis/ws`` order. ``CNTG_YN`` (field 13) is ``1`` for an order accept/confirm
notice and ``2`` for an execution. Pure and stdlib-only so both the quarantine
service and the repository can share it without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: KIS domestic execution-notice TR (live). Mock/overseas frames never match.
ACCEPT_NOTICE_TR = "H0STCNI0"
FIELD_ORDER_NO = 2
FIELD_SIDE = 4
FIELD_SYMBOL = 8
FIELD_CNTG_YN = 13
CNTG_YN_ACCEPT = "1"
CNTG_YN_FILL = "2"


def notice_fields(raw: Any) -> list[str] | None:
    """The frame's field list when ``raw`` is a readable H0STCNI0 frame."""
    if not isinstance(raw, Mapping) or raw.get("tr") != ACCEPT_NOTICE_TR:
        return None
    fields = raw.get("fields")
    if (
        not isinstance(fields, list)
        or len(fields) <= FIELD_CNTG_YN
        or not all(isinstance(item, str) for item in fields)
    ):
        return None
    return fields


def is_accept_notice_frame(raw: Any) -> bool:
    """True only for a readable H0STCNI0 frame whose CNTG_YN is exactly 1."""
    fields = notice_fields(raw)
    return fields is not None and fields[FIELD_CNTG_YN].strip() == CNTG_YN_ACCEPT
