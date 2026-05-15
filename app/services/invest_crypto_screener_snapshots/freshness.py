from __future__ import annotations

import datetime as dt
from typing import Literal

DataState = Literal["fresh", "partial", "stale", "missing", "fallback"]
CRYPTO_STALE_AFTER = dt.timedelta(hours=3)
_CRYPTO_MIN_FRESH_ROWS = 20
_KST = dt.timezone(dt.timedelta(hours=9))


def today_crypto_snapshot_date(now: dt.datetime | None = None) -> dt.date:
    """Return the current KST calendar date for 24/7 crypto snapshots."""
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.UTC)
    return current.astimezone(_KST).date()


def classify_crypto_partition(
    *,
    latest_partition_date: dt.date | None,
    row_count: int,
    last_computed_at: dt.datetime | None,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
    min_fresh_rows: int = _CRYPTO_MIN_FRESH_ROWS,
    stale_after: dt.timedelta = CRYPTO_STALE_AFTER,
) -> DataState:
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.UTC)
    today_value = today or today_crypto_snapshot_date(current)
    if latest_partition_date is None or row_count <= 0 or last_computed_at is None:
        return "missing"
    if latest_partition_date < today_value:
        return "stale"
    computed_at = last_computed_at
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=dt.UTC)
    if current - computed_at.astimezone(dt.UTC) >= stale_after:
        return "stale"
    if row_count < min_fresh_rows:
        return "partial"
    return "fresh"
