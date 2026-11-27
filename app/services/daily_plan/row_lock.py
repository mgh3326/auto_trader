"""hk 6991 — the per-row supersession lock and change rule (design 6939 §5.1).

One transaction-scoped advisory lock keyed on (account, normalized symbol,
venue) serializes every supersession of a plan row against the E1 gateway's
reservation and pre-send re-read. It is taken by

* daily_plan publish, for every row whose execution fields it changes;
* the E1 child reservation of a v3 intent;
* the E1 pre-send hook, held through the adapter's first send.

The order is decided by commit order under this lock, never by timestamps.
A ``fill``-kind version and a publish that changes only other rows never
supersede a row, so they never take its lock.

Pure apart from :func:`lock_plan_row`. The gateway imports this module; this
module imports nothing from the gateway.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: The body fields that decide what an intent bound to the row may do. A
#: version changes the row when one of these differs from the row in force.
EXECUTION_FIELDS: Final[tuple[str, ...]] = (
    "action",
    "side",
    "venue",
    "session",
    "valid_until",
    "trigger",
    "invalidation_conditions",
    "max_buy_price",
    "min_sell_price",
    "would_be_limit_price",
    "would_be_quantity",
    "would_be_notional",
    "account_ref",
    "would_be_account_ref",
    "disposition",
    "not_executable_reason",
)

#: A version of this kind records observation only and never supersedes.
OBSERVATION_KINDS: Final = frozenset({"fill"})


def execution_projection(body: Mapping[str, Any]) -> str:
    """The canonical text of a row's execution fields."""
    return json.dumps(
        {field: body.get(field) for field in EXECUTION_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def changes_execution(
    previous: Mapping[str, Any] | None, body: Mapping[str, Any]
) -> bool:
    return previous is None or execution_projection(previous) != execution_projection(
        body
    )


def lock_account(body: Mapping[str, Any]) -> str | None:
    """The account a row can be executed on: its own, else the would-be one."""
    return body.get("account_ref") or body.get("would_be_account_ref")


def lock_identity(body: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """(account, symbol, venue), or ``None`` when no intent can bind the row."""
    account = lock_account(body)
    symbol = body.get("symbol")
    venue = body.get("venue")
    if not account or not symbol or not venue:
        return None
    return account, str(symbol).strip().upper(), str(venue).strip().upper()


def lock_name(account: str, symbol: str, venue: str) -> str:
    return (
        f"review.daily_plan_rows.execution:{account}:{symbol.upper()}:{venue.upper()}"
    )


async def lock_plan_row(
    session: AsyncSession, account: str, symbol: str, venue: str
) -> None:
    """Take the row's lock until the caller's transaction ends."""
    key = lock_name(account, symbol, venue)
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
    )


def latest_change_seq(
    history: Iterable[tuple[int, str, Mapping[str, Any]]],
) -> int | None:
    """The latest published seq in which this row's execution fields changed.

    ``history`` is every (seq, version kind, row body) of one row key. A
    version that repeats the row unchanged, and every observation version, are
    not changes.
    """
    latest: int | None = None
    previous: Mapping[str, Any] | None = None
    for seq, kind, body in sorted(history, key=lambda item: item[0]):
        if kind in OBSERVATION_KINDS:
            continue
        if changes_execution(previous, body):
            latest = seq
        previous = body
    return latest
