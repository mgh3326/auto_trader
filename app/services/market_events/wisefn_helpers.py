"""WiseFn KR earnings calendar fetch helper (ROB-171, fixture-only PoC).

This module exposes a single public coroutine, `fetch_wisefn_earnings_for_date`,
that returns a list of row dicts shaped for `normalize_wisefn_earnings_row`.

The actual upstream HTTP fetch is encapsulated in `_fetch_calendar_payload`,
which is intentionally a `NotImplementedError` seam: the upstream WiseFn /
WiseReport contract has not yet been confirmed, and tests / CI must never call
live. Tests inject fixture payloads via `unittest.mock.patch.object` against
`_fetch_calendar_payload`. Production runs are additionally gated behind
`settings.wisefn_earnings_enabled` in the CLI.

Expected row shape returned by `fetch_wisefn_earnings_for_date`:

    {
        "stock_code": "005930",          # KR 6-digit ticker
        "corp_name": "삼성전자",
        "release_date": "2026-05-13",    # ISO date string
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "release_type": "scheduled",     # or "released"
        "title": "삼성전자 2026년 1분기 실적발표 예정",
        "time_hint": "after_close",      # before_open|after_close|during_market|unknown
    }
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


async def _fetch_calendar_payload(target_date: date) -> dict[str, Any]:
    """Fetch the upstream WiseFn calendar payload for `target_date`.

    Module-level seam — tests patch this with `unittest.mock.patch.object`.
    Default raises NotImplementedError; the live wiring is a follow-up that
    requires confirmed upstream contract + permission (see ROB-171 follow-ups
    in docs/runbooks/market-events-ingestion.md).
    """
    raise NotImplementedError(
        "ROB-171: WiseFn calendar endpoint is not wired yet. "
        "Set WISEFN_EARNINGS_ENABLED=false (default) or inject fetch_rows "
        "directly in tests."
    )


def _row_matches_date(row: dict[str, Any], target_date: date) -> bool:
    raw = row.get("release_date") or row.get("date")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)) == target_date
    except ValueError:
        return False


async def fetch_wisefn_earnings_for_date(target_date: date) -> list[dict[str, Any]]:
    """Return WiseFn earnings rows for one calendar day.

    The returned rows are passed through to
    `app.services.market_events.normalizers.normalize_wisefn_earnings_row`.
    """
    payload = await _fetch_calendar_payload(target_date)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        logger.warning(
            "wisefn payload missing 'items' list for %s; got keys=%s",
            target_date,
            list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        return []
    return [row for row in items if _row_matches_date(row, target_date)]
