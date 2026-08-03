"""Unit tests for the research-candle promotion contract.

These run without a database: the fail-closed gates are what must never regress,
and they are reachable with a stub connection. DB-backed behaviour (insert,
no-op, conflict quarantine, watermark advance) is exercised separately against a
migrated database — see docs/runbooks/kr-research-candles-promotion.md.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.services.research_candles.promotion import (
    KST,
    MAX_PROMOTION_LAG_DAYS,
    PRODUCTION_RETENTION_DAYS,
    PromotionBlocked,
    classify_session_segment,
    last_completed_session,
    promote,
)

pytestmark = pytest.mark.unit


class StubConn:
    """Minimal asyncpg-shaped stub: only what promote() touches."""

    def __init__(self, watermark: date | None, rows: list | None = None):
        self._watermark = watermark
        self._rows = rows or []
        self.executed: list[str] = []

    async def fetchval(self, sql: str, *args):
        if "last_promoted_session_date_kst" in sql and "SELECT" in sql:
            return self._watermark
        if "min(time" in sql:
            return date(2026, 1, 1)
        return None

    async def fetch(self, sql: str, *args):
        return self._rows

    async def fetchrow(self, sql: str, *args):
        return None

    async def execute(self, sql: str, *args):
        self.executed.append(sql)


# --- session segment: fail closed, never guess ---------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "venue", "expected"),
    [
        (9, 0, "KRX", "KRX_REGULAR"),
        (12, 0, "KRX", "KRX_REGULAR"),
        (15, 30, "KRX", "KRX_REGULAR"),
        # A KRX-labelled bar outside regular hours is not silently accepted.
        (8, 59, "KRX", "UNKNOWN"),
        (15, 31, "KRX", "UNKNOWN"),
        (20, 0, "KRX", "UNKNOWN"),
        (8, 30, "NTX", "NXT_PRE"),
        (12, 0, "NTX", "NXT_OVERLAP"),
        (18, 0, "NTX", "NXT_POST"),
    ],
)
def test_classify_session_segment(hour, minute, venue, expected):
    ts = datetime(2026, 8, 3, hour, minute, tzinfo=KST)
    assert classify_session_segment(ts, venue) == expected


def test_unknown_venue_fails_closed():
    ts = datetime(2026, 8, 3, 10, 0, tzinfo=KST)
    assert classify_session_segment(ts, "SOMETHING_ELSE") == "UNKNOWN"


# --- completed sessions only --------------------------------------------


def test_today_excluded_while_session_open():
    during = datetime(2026, 8, 3, 11, 0, tzinfo=KST)
    assert last_completed_session(during) == date(2026, 8, 2)


def test_today_included_once_closed():
    after = datetime(2026, 8, 3, 18, 0, tzinfo=KST)
    assert last_completed_session(after) == date(2026, 8, 3)


def test_boundary_at_close_is_not_yet_complete():
    at_close = datetime(2026, 8, 3, 15, 30, tzinfo=KST)
    assert last_completed_session(at_close) == date(2026, 8, 2)


# --- fail-closed gates ---------------------------------------------------


@pytest.mark.asyncio
async def test_lag_beyond_limit_is_blocked():
    now = datetime(2026, 8, 3, 18, 0, tzinfo=KST)
    stale = now.date() - timedelta(days=MAX_PROMOTION_LAG_DAYS + 30)
    conn = StubConn(watermark=stale)

    with pytest.raises(PromotionBlocked, match="exceeds"):
        await promote(
            conn, source="UNKNOWN", venue="KRX", now_kst=now, dry_run=True, batch_id="t"
        )

    assert conn.executed == [], "a blocked promotion must not write anything"


@pytest.mark.asyncio
async def test_range_older_than_production_retention_is_blocked():
    now = datetime(2026, 8, 3, 18, 0, tzinfo=KST)
    # Inside the lag limit, but starts before rows still exist upstream.
    watermark = now.date() - timedelta(days=PRODUCTION_RETENTION_DAYS + 5)
    conn = StubConn(watermark=watermark)

    with pytest.raises(PromotionBlocked):
        await promote(
            conn, source="UNKNOWN", venue="KRX", now_kst=now, dry_run=True, batch_id="t"
        )


@pytest.mark.asyncio
async def test_up_to_date_is_a_noop_not_an_error():
    now = datetime(2026, 8, 3, 18, 0, tzinfo=KST)
    conn = StubConn(watermark=now.date())
    result = await promote(
        conn, source="UNKNOWN", venue="KRX", now_kst=now, dry_run=True, batch_id="t"
    )
    assert result.rows_read == 0
    assert "already up to date" in result.notes
    assert conn.executed == []


@pytest.mark.asyncio
async def test_dry_run_never_writes():
    now = datetime(2026, 8, 3, 18, 0, tzinfo=KST)
    conn = StubConn(watermark=now.date() - timedelta(days=2))
    await promote(
        conn, source="UNKNOWN", venue="KRX", now_kst=now, dry_run=True, batch_id="t"
    )
    assert conn.executed == []
