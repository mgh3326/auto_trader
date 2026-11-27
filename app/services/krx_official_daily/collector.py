"""Fetch both markets for one session, then insert once (hk 6586).

All-or-nothing per run: if any market fails, or any market returns an empty
OutBlock_1, nothing is written. An empty list on HTTP 200 means the session is
not published yet (operator capture 2026-11-27: basDd 20261126 at 06:40 KST the
next morning). It is never read as "no trades". Zero-volume rows are normal
exchange data and stay evidence only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from app.services.krx_official_daily.errors import (
    KrxOpenApiError,
    KrxServiceNotApprovedError,
    KrxUnauthorizedKeyError,
)
from app.services.krx_official_daily.parse import ParsedDaily, parse_daily_payload
from app.services.krx_official_daily.store import insert_official_daily

MARKETS = ("kospi", "kosdaq")
NOT_PUBLISHED_EXIT = 4


def exit_code_for(error: KrxOpenApiError) -> int:
    """0 ok, 1 fetch/parse failure, 2 key rejected, 3 service not approved.

    4 (not published yet) is returned by ``collect_official_daily`` itself.
    """
    if isinstance(error, KrxUnauthorizedKeyError):
        return 2
    if isinstance(error, KrxServiceNotApprovedError):
        return 3
    return 1


async def collect_official_daily(
    *,
    session_date: date,
    markets: Sequence[str] = MARKETS,
    client: Any,
    session: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    parsed: dict[str, ParsedDaily] = {}
    for market in markets:
        try:
            payload = await client.fetch_daily(market, session_date)
            parsed[market] = parse_daily_payload(
                payload, market=market, session_date=session_date
            )
        except KrxOpenApiError as error:
            return {
                "session_date": session_date.isoformat(),
                "status": "failed",
                "error_code": error.code,
                "exit_code": exit_code_for(error),
                "markets": {},
                "inserted": None,
            }

    all_rejected = [m for m in markets if _is_all_rejected(parsed[m])]
    if all_rejected:
        # Rows arrived but none passed a check (hk 6925): the exchange payload changed
        # shape or meaning. Failing loudly keeps the daily DAG red instead of green-empty.
        # A market with no rows and no rejections is still `not_published_yet`,
        # not `ok` (hk 7059).
        return {
            "session_date": session_date.isoformat(),
            "status": "failed",
            "error_code": "all_rows_rejected",
            "exit_code": 1,
            "markets": {
                market: {
                    "status": _all_rejected_market_status(
                        parsed[market], market, all_rejected
                    ),
                    "rows": len(parsed[market].rows),
                    "rejected": dict(parsed[market].rejected),
                }
                for market in markets
            },
            "inserted": None,
        }

    unpublished = [m for m in markets if _is_unpublished(parsed[m])]
    if unpublished:
        return {
            "session_date": session_date.isoformat(),
            "status": "not_published_yet",
            "error_code": "not_published_yet",
            "exit_code": NOT_PUBLISHED_EXIT,
            "markets": {
                market: {
                    "status": "not_published_yet" if market in unpublished else "ok",
                    "rows": len(parsed[market].rows),
                    "rejected": dict(parsed[market].rejected),
                }
                for market in markets
            },
            "inserted": None,
        }

    received_at = now or datetime.now(UTC)
    rows = [row for market in markets for row in parsed[market].rows]
    inserted: int | None = None
    if session is not None:
        inserted = await insert_official_daily(
            session, rows=rows, first_received_at=received_at
        )
        await session.commit()
    return {
        "session_date": session_date.isoformat(),
        "status": "ok",
        "error_code": None,
        "exit_code": 0,
        "markets": {
            market: {
                "status": "ok",
                "rows": len(parsed[market].rows),
                "rejected": dict(parsed[market].rejected),
            }
            for market in markets
        },
        "inserted": inserted,
        "first_received_at": received_at.isoformat(),
        **({"rows": [_json_row(row) for row in rows]} if session is None else {}),
    }


def _is_unpublished(parsed: ParsedDaily) -> bool:
    """An OutBlock_1 with no entries at all; rejected rows are not this case."""
    return not parsed.rows and not parsed.rejected


def _is_all_rejected(parsed: ParsedDaily) -> bool:
    """Rows were present but every one failed a check; nothing accepted."""
    return not parsed.rows and bool(parsed.rejected)


def _all_rejected_market_status(
    parsed: ParsedDaily, market: str, all_rejected: Sequence[str]
) -> str:
    """Per-market label inside an ``all_rows_rejected`` run (hk 7059)."""
    if market in all_rejected:
        return "failed"
    if _is_unpublished(parsed):
        return "not_published_yet"
    return "ok"


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "session_date":
            out[key] = value.isoformat()
        elif hasattr(value, "as_tuple"):
            out[key] = str(value)
        else:
            out[key] = value
    return out
