from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.market_valuation_snapshots.normalized_market_cap import (
    load_normalized_kr_market_caps,
)

_SYMBOL = "976101"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(
        delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == _SYMBOL)
    )
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(
        delete(MarketValuationSnapshot).where(MarketValuationSnapshot.symbol == _SYMBOL)
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_uses_latest_naver_raw_krw_value_and_excludes_other_sources(db_session):
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol=_SYMBOL,
                snapshot_date=dt.date(2026, 7, 19),
                source="naver_finance",
                market_cap=Decimal("59000000000000"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol=_SYMBOL,
                snapshot_date=dt.date(2026, 7, 20),
                source="naver_finance",
                market_cap=Decimal("60300000000000"),
            ),
            # A newer non-Naver row must not shadow the normalized KRW source.
            MarketValuationSnapshot(
                market="kr",
                symbol=_SYMBOL,
                snapshot_date=dt.date(2026, 7, 21),
                source="toss_openapi",
                market_cap=Decimal("39943178204"),
            ),
        ]
    )
    await db_session.commit()

    result = await load_normalized_kr_market_caps(db_session, [_SYMBOL])

    assert result[_SYMBOL].value == Decimal("60300000000000")
    assert result[_SYMBOL].snapshot_date == dt.date(2026, 7, 20)
    assert result[_SYMBOL].source == "naver_finance"
