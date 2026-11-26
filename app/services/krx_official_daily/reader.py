"""Read-only zero-volume fact with its date (hk 6586).

This is evidence about one session. It is not a confirmed trading halt:
``halted_suspect`` and the screeners do not read it.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.krx_official_daily import KRXOfficialDaily

_SHORT_CODE = re.compile(r"[0-9A-Z]{6}")


async def read_zero_volume_fact(
    session: AsyncSession, *, symbol: str, session_date: date
) -> dict[str, Any]:
    """Return a closed-state fact; an absent or unreadable row is ``unknown``."""
    if not _SHORT_CODE.fullmatch(symbol):
        raise ValueError("symbol must be a 6-character KRX short code")
    base: dict[str, Any] = {
        "symbol": symbol,
        "session_date": session_date.isoformat(),
        "evidence_kind": "krx_official_zero_volume",
        "is_halt_confirmation": False,
    }
    row = await session.get(KRXOfficialDaily, (symbol, session_date))
    if row is None:
        return {
            **base,
            "state": "unknown",
            "reason": "no_official_row",
            "source": None,
            "first_received_at": None,
        }
    base.update(
        source=row.source,
        first_received_at=row.first_received_at.isoformat(),
    )
    if row.volume is None:
        reason = (row.field_reasons or {}).get("volume", "missing")
        return {**base, "state": "unknown", "reason": reason}
    state = "full_day_zero_volume" if row.full_day_zero_volume else "traded"
    return {**base, "state": state, "reason": None}
