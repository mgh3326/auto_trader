"""hk 6503 — single-symbol read of a stored KR confirmed trading halt.

Used by the halted_suspect path so the suspicion output can say when a
confirmed ``trading_halt`` flag exists. Fail-open: a read failure returns
``None`` and the suspicion output is unchanged. ``None`` here means "no stored
halted flag could be shown", not "the symbol is not halted".
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.kr_trading_flags import vocabulary as v
from app.services.kr_trading_flags.gate import evidence_of
from app.services.kr_trading_flags.store import load_flag_records

logger = logging.getLogger(__name__)


async def confirmed_halt_flag(symbol: str) -> dict[str, Any] | None:
    """Return the stored ``halted`` evidence for ``symbol``, else ``None``."""
    try:
        async with AsyncSessionLocal() as db:
            records = await load_flag_records(db, [symbol])
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning("confirmed halt lookup skipped: %s", type(exc).__name__)
        return None
    record = records.get(symbol, {}).get(v.CATEGORY_TRADING_HALT)
    if record is None or record.state != "halted":
        return None
    return evidence_of(record)
