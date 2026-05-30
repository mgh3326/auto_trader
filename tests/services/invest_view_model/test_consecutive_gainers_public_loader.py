import datetime as dt
from decimal import Decimal

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_view_model.screener_service import (
    load_consecutive_gainers_from_snapshots,
)


@pytest.mark.asyncio
async def test_public_wrapper_returns_rows_or_none(db_session):
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM invest_screener_snapshots"))
    await db_session.commit()

    # No partition at all -> None (missing).
    assert (
        await load_consecutive_gainers_from_snapshots(db_session, market="kr")
    ) is None

    today = dt.date(2026, 5, 29)
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="005930",
            snapshot_date=today,
            latest_close=Decimal("70000"),
            change_rate=Decimal("1.5"),
            week_change_rate=Decimal("6.0"),
            consecutive_up_days=6,
            closes_window=[1, 2, 3],
            source="kis",
        )
    )
    await db_session.commit()

    rows = await load_consecutive_gainers_from_snapshots(db_session, market="kr")
    assert rows is not None
    assert isinstance(rows, list)
    assert rows[0]["symbol"] == "005930"
    # _screener_snapshot_state is the full DataState taxonomy (fresh/partial/
    # stale). This fixture's closes_window has length 3 (< _PARTIAL_MAX_LEN=5, so
    # week_change_rate is not computable), so classify_state returns "partial"
    # whenever the partition is current (snapshot_date == trading baseline) and
    # "stale" once the baseline rolls past snapshot_date. Both are valid usable
    # states; asserting the set keeps this date-independent — the prior
    # {"fresh", "stale"} only passed on weekdays and went red on weekends, when
    # the KR baseline rolls back to the Friday snapshot_date (ROB-378 baseline).
    assert rows[0]["_screener_snapshot_state"] in {"fresh", "stale", "partial"}
