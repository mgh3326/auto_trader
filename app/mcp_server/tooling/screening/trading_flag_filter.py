"""hk 6503 — exclude 관리·경고·위험·정리매매·거래정지 names from KR screens.

Wraps ``app/services/kr_trading_flags/gate.py`` for the screener entrypoint.
Exclusions are reported in ``meta.trading_flag_excluded`` with their evidence.
투자주의 rows are labelled in ``trading_flag_labels`` and kept.

Fail-open: a failed read of the flag store keeps every row and records
``meta.trading_flags_check_skipped``. A DB hiccup is not evidence that a flag is
set, and dropping candidates on infrastructure noise is the opposite mistake.
Unknown symbols (no stored row) are kept too.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.kr_trading_flags.gate import (
    FLAG_MAX_AGE,
    apply_trading_flag_gate,
    kr_row_symbol,
)
from app.services.kr_trading_flags.store import load_flag_records

logger = logging.getLogger(__name__)

KR_SCREEN_MARKETS = frozenset({"kr", "kospi", "kosdaq", "konex", "all"})


async def exclude_trading_flag_rows(
    response: dict[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Return ``response`` with flagged KR rows removed and accounted for."""
    if market not in KR_SCREEN_MARKETS:
        return response
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        return response
    rows = [row for row in raw_results if isinstance(row, dict)]
    if not rows:
        return response

    symbols = [s for s in (kr_row_symbol(row) for row in rows) if s]
    meta_extra: dict[str, Any] = {}
    try:
        async with AsyncSessionLocal() as db:
            records = await load_flag_records(db, symbols)
    except Exception as exc:  # noqa: BLE001 — fail open, never drop on DB noise
        logger.warning("trading flag check skipped: %s", type(exc).__name__)
        records = {}
        meta_extra["trading_flags_check_skipped"] = type(exc).__name__

    outcome = apply_trading_flag_gate(
        rows,
        symbol_of=kr_row_symbol,
        records=records,
        now=datetime.now(UTC),
        max_age=FLAG_MAX_AGE,
    )
    if (
        not outcome.excluded
        and not outcome.labelled_rows
        and not outcome.stale
        and not meta_extra
    ):
        return response

    dropped = len(rows) - len(outcome.kept)
    previous_total = response.get("total_count")
    total_count = (
        max(0, int(previous_total) - dropped)
        if isinstance(previous_total, int)
        else len(outcome.kept)
    )
    meta = dict(response.get("meta") or {})
    if outcome.excluded:
        meta["trading_flag_excluded"] = outcome.excluded
    if outcome.stale:
        meta["trading_flags_stale"] = outcome.stale
    meta.update(meta_extra)
    updated = {
        **response,
        "results": outcome.kept,
        "total_count": total_count,
        "returned_count": len(outcome.kept),
        "meta": meta,
    }
    return updated


__all__ = ["exclude_trading_flag_rows"]
