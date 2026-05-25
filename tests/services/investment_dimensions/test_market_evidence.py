import datetime as dt
from decimal import Decimal

import pytest

from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)
from app.services.investment_dimensions.market_evidence import build_market_evidence


@pytest.mark.asyncio
async def test_build_market_evidence_bundle(db_session):
    from sqlalchemy import text

    await db_session.execute(
        text("DELETE FROM invest_screener_snapshots WHERE market = 'us'")
    )
    repo = InvestScreenerSnapshotsRepository(db_session)
    base = {"market": "us", "snapshot_date": dt.date(2026, 5, 23), "source": "yahoo"}
    await repo.upsert(
        SnapshotUpsert(
            symbol="AAA",
            latest_close=Decimal("10"),
            change_rate=Decimal("5.0"),
            closes_window=[10],
            consecutive_up_days=3,
            **base,
        )
    )
    await repo.upsert(
        SnapshotUpsert(
            symbol="BBB",
            latest_close=Decimal("10"),
            change_rate=Decimal("-2.0"),
            closes_window=[10],
            **base,
        )
    )
    await db_session.commit()

    bundle = await build_market_evidence(repo, market="us", held={"AAA"})
    assert bundle["market"] == "us"
    assert bundle["breadth"]["advancers"] >= 1
    assert bundle["breadth"]["decliners"] >= 1
    assert bundle["top_movers"][0]["symbol"] == "AAA"  # highest change_rate
    assert bundle["top_movers"][0]["is_held"] is True
    assert "freshness" in bundle and "partition_date" in bundle["freshness"]
    assert isinstance(bundle["data_health"]["fresh_count"], int)
