"""Fence-area locks F1..F4 and the basket authority row (hk 3795 T4c, §4.6).

The fence-area lock set is separate from the caller-side locks L0..L4 of
``locks.py``. It is taken by the send-fence transaction and by the writers of the
facts a fence reads, always in this order::

    F1   freeze scope        advisory, shared by a fence, exclusive by the freeze
                              writer (hk 4900): ``mandate_freeze:global`` then
                              ``mandate_freeze:<realm>:<basket_key>``
    F2   basket authority    ``review.mandate_basket_authority`` row, FOR SHARE by a
                              fence, bumped (row-exclusive) by a writer of a clause,
                              an activation profile, a sleeve version or a plan
    F3s  sleeve exposure     ``review.mandate_cap_mutex`` sleeve row, FOR UPDATE
    F3   basket day total    ``review.mandate_cap_mutex`` basket row (auto fence, T5)
    F4   global day total    ``review.mandate_cap_mutex`` global row (auto fence, T5)

A lock is a serialization aid inside one short transaction, never the authority to
send: authority is a committed ``tranche_send_fenced`` row (§4.6 "fence가 권한이다").
A writer takes its own caller lock (L2/L3) first and a fence-area lock second, and
a transaction that writes through one holds only the fence-area locks its role
needs (design: "한 트랜잭션에 fence 영역 lock은 하나만" except the documented
subsets), so no cycle can form inside this set.

This module imports no order or broker module and nothing from the mandate store
(the sleeve store imports it lazily, and the store imports the sleeve store).
"""

from __future__ import annotations

from datetime import date
from enum import IntEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REALMS = ("live", "mock")


class FenceLock(IntEnum):
    FREEZE = 1  # F1
    AUTHORITY = 2  # F2
    SLEEVE = 3  # F3s
    BASKET_DAY = 4  # F3
    GLOBAL_DAY = 5  # F4


#: The only legal acquisition order inside the fence area.
FENCE_LOCK_ORDER: tuple[FenceLock, ...] = (
    FenceLock.FREEZE,
    FenceLock.AUTHORITY,
    FenceLock.SLEEVE,
    FenceLock.BASKET_DAY,
    FenceLock.GLOBAL_DAY,
)


class AuthorityRowMissing(RuntimeError):
    """A basket has no authority row: nothing would be locked, so nothing may be written."""


def _realm(realm: str) -> str:
    if realm not in REALMS:
        raise ValueError(f"unknown realm {realm!r}")
    return realm


# --------------------------------------------------------------------------- F1
GLOBAL_FREEZE_KEY = "mandate_freeze:global"


def freeze_lock_keys(realm: str, basket_key: str) -> tuple[str, str]:
    """``(global, basket)`` in acquisition order."""
    return GLOBAL_FREEZE_KEY, f"mandate_freeze:{_realm(realm)}:{basket_key}"


async def acquire_freeze_shared(
    session: AsyncSession, realm: str, basket_key: str
) -> None:
    """F1 as a send fence takes it: shared, so fences never wait for each other."""
    for key in freeze_lock_keys(realm, basket_key):
        await session.execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
            {"key": key},
        )


async def acquire_freeze_exclusive(
    session: AsyncSession, realm: str, basket_key: str | None = None
) -> None:
    """F1 as the freeze writer (hk 4900) takes it: exclusive, and no other fence lock.

    ``basket_key=None`` freezes the whole account scope (the global key only).
    """
    keys = (
        (GLOBAL_FREEZE_KEY,)
        if basket_key is None
        else freeze_lock_keys(realm, basket_key)
    )
    for key in keys:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )


# --------------------------------------------------------------------------- F2
async def lock_authority_shared(
    session: AsyncSession, realm: str, basket_key: str
) -> int:
    """F2 as a send fence: ``FOR SHARE``. A missing row refuses the fence."""
    row = (
        await session.execute(
            text(
                "SELECT authority_seq FROM review.mandate_basket_authority "
                "WHERE realm = :realm AND basket_key = :basket FOR SHARE"
            ),
            {"realm": _realm(realm), "basket": basket_key},
        )
    ).first()
    if row is None:
        raise AuthorityRowMissing(f"{realm}:{basket_key}")
    return int(row[0])


async def ensure_authority_row(
    session: AsyncSession, realm: str, basket_key: str
) -> None:
    """Create the basket's row if clause or plan registration is the first to see it."""
    await session.execute(
        text(
            "INSERT INTO review.mandate_basket_authority (realm, basket_key) "
            "VALUES (:realm, :basket) ON CONFLICT DO NOTHING"
        ),
        {"realm": _realm(realm), "basket": basket_key},
    )


async def bump_authority(session: AsyncSession, realm: str, basket_key: str) -> int:
    """F2 as a writer: row-exclusive ``authority_seq + 1`` in the writer's transaction.

    Zero rows updated means no fence could have serialized against this writer, so
    the writer fails instead of writing.
    """
    row = (
        await session.execute(
            text(
                "UPDATE review.mandate_basket_authority "
                "SET authority_seq = authority_seq + 1 "
                "WHERE realm = :realm AND basket_key = :basket "
                "RETURNING authority_seq"
            ),
            {"realm": _realm(realm), "basket": basket_key},
        )
    ).first()
    if row is None:
        raise AuthorityRowMissing(f"{realm}:{basket_key}")
    return int(row[0])


async def sleeve_baskets(
    session: AsyncSession, sleeve_id: int
) -> list[tuple[str, str]]:
    """Every ``(realm, basket_key)`` a sleeve has had a clause or a plan in, sorted."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT review.mandate_realm(s.account_mode), t.basket_key "
                "FROM review.mandate_terms t "
                "JOIN review.strategy_sleeve_versions v ON v.id = t.sleeve_version_id "
                "JOIN review.strategy_sleeves s ON s.id = v.sleeve_id "
                "WHERE v.sleeve_id = :sleeve "
                "AND review.mandate_realm(s.account_mode) IS NOT NULL"
            ),
            {"sleeve": sleeve_id},
        )
    ).all()
    return sorted((str(r[0]), str(r[1])) for r in rows)


async def bump_for_sleeve_version(session: AsyncSession, sleeve_id: int) -> None:
    """A sleeve version was appended: F2 of its baskets (ascending), then F3s.

    A sleeve with no clause yet belongs to no basket: nothing is bumped and nothing
    changes for it. The sleeve advisory lock (L3) is already held by the caller.
    """
    baskets = await sleeve_baskets(session, sleeve_id)
    if not baskets:
        return
    for realm, basket_key in baskets:
        await bump_authority(session, realm, basket_key)
    await lock_mutex(session, sleeve_mutex_key(sleeve_id), "sleeve")


# ------------------------------------------------------------------ F3s / F3 / F4
def sleeve_mutex_key(sleeve_id: int) -> str:
    return f"sleeve:{int(sleeve_id)}"


def basket_day_mutex_key(realm: str, basket_key: str, kst_day: date) -> str:
    return f"basket:{_realm(realm)}:{basket_key}:{kst_day.isoformat()}"


def global_day_mutex_key(account_mode: str, market: str, kst_day: date) -> str:
    return f"global:{account_mode}:{market}:{kst_day.isoformat()}"


async def lock_mutex(session: AsyncSession, key: str, scope: str) -> None:
    """Create the row if needed, then ``FOR UPDATE`` it. The row holds no value."""
    await session.execute(
        text(
            "INSERT INTO review.mandate_cap_mutex (mutex_key, scope) "
            "VALUES (:key, :scope) ON CONFLICT DO NOTHING"
        ),
        {"key": key, "scope": scope},
    )
    await session.execute(
        text(
            "SELECT 1 FROM review.mandate_cap_mutex WHERE mutex_key = :key FOR UPDATE"
        ),
        {"key": key},
    )
