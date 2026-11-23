"""hk 6145: collector writer against a throwaway DB (CI only).

Re-running the same bars inserts 0 rows and leaves ``retrieved_at`` (first
receipt) unchanged; ``public.*_candles_1m`` are not touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.research_candles import session_minutes_collector as sc

# Reuse the migration fixture: old kr table + new objects created in a rolled-back tx.
from tests.test_hk6145_session_minutes_migration_db import _run_step, conn  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T1 = datetime(2026, 11, 24, 3, 0, 0, tzinfo=UTC)
T2 = T1 + timedelta(minutes=5)


def _bars(market: str, venue: str, symbol: str):
    import pandas as pd

    labels = (
        ["2026-11-24 10:00", "2026-11-24 10:01"]
        if market == "kr"
        else ["2026-11-24 09:30", "2026-11-24 09:31"]
    )
    frame = pd.DataFrame(
        {
            "datetime": pd.to_datetime(labels),
            "open": [1, 2],
            "high": [2, 3],
            "low": [1, 1],
            "close": [2, 2],
            "volume": [5, 6],
            "value": [10, 12],
        }
    )
    return sc.bars_from_frame(
        frame,
        market=market,  # type: ignore[arg-type]
        symbol=symbol,
        venue=venue,
        observed_before=datetime(2026, 12, 1, tzinfo=UTC),
    ).bars


@pytest.mark.parametrize(
    ("market", "venue", "symbol", "table"),
    [
        ("kr", "KRX", "005930", "research.kr_candles_1m"),
        ("us", "NASD", "NVDA", "research.us_candles_1m"),
    ],
)
async def test_rerun_inserts_zero_and_keeps_first_retrieved_at(
    conn,  # noqa: F811
    market: str,
    venue: str,
    symbol: str,
    table: str,
) -> None:
    await conn.run_sync(_run_step, "upgrade")
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        public_before = {}
        for public in ("public.kr_candles_1m", "public.us_candles_1m"):
            exists = (
                await conn.execute(text("SELECT to_regclass(:name)"), {"name": public})
            ).scalar_one()
            if exists is not None:
                public_before[public] = (
                    await conn.execute(text(f"SELECT count(*) FROM {public}"))
                ).scalar_one()

        bars = _bars(market, venue, symbol)
        assert len(bars) == 2
        writer = sc.SqlMinuteWriter(session, market)  # type: ignore[arg-type]
        assert await writer.insert(bars, batch_id="b1", retrieved_at=T1) == (2, 0)
        assert await writer.insert(bars, batch_id="b2", retrieved_at=T2) == (0, 2)

        rows = (
            await conn.execute(
                text(
                    f"SELECT retrieved_at, batch_id, source FROM {table} ORDER BY time_utc"
                )
            )
        ).all()
        assert [(r.retrieved_at, r.batch_id, r.source) for r in rows] == [
            (T1, "b1", "KIS"),
            (T1, "b1", "KIS"),
        ]
        for public, count in public_before.items():
            after = (
                await conn.execute(text(f"SELECT count(*) FROM {public}"))
            ).scalar_one()
            assert after == count
    finally:
        await session.close()
