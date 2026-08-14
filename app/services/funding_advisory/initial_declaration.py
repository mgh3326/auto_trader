"""Pure template for the operator-confirmed initial parking declaration.

No timestamp is invented here.  The operator must supply the observed time,
including timezone, before the request can be sent through the invest UI.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.schemas.funding_advisory import ExternalCashDeclarationRequest

INITIAL_PARKING_AMOUNT = Decimal("640000")
INITIAL_PARKING_DATE_KST = date(2026, 8, 15)


def build_initial_parking_declaration(
    *,
    owner_user_id: int,
    as_of: datetime,
    idempotency_key: str,
) -> ExternalCashDeclarationRequest:
    """Build, but never submit, the first 640,000 KRW declaration request."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("operator-confirmed as_of timezone is required")
    if as_of.astimezone(ZoneInfo("Asia/Seoul")).date() != INITIAL_PARKING_DATE_KST:
        raise ValueError(
            "initial parking declaration must retain its 2026-08-15 KST as_of"
        )
    return ExternalCashDeclarationRequest(
        owner_user_id=owner_user_id,
        location_key="parking_primary",
        display_label="파킹통장",
        currency="KRW",
        amount=INITIAL_PARKING_AMOUNT,
        as_of=as_of,
        source_note="토스증권 → 파킹통장 이동",
        expected_head_declaration_id=None,
        idempotency_key=idempotency_key,
    )
