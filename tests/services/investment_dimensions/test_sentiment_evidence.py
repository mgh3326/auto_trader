import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.services.investment_dimensions.sentiment_evidence import (
    build_sentiment_evidence,
)
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)


async def _clear(db_session, *symbols: str) -> None:
    if symbols:
        await db_session.execute(
            sa.delete(InvestorFlowSnapshot).where(
                InvestorFlowSnapshot.symbol.in_(symbols)
            )
        )
    await db_session.commit()


def _flow(symbol, *, snapshot_date, foreign_net, double_buy=False):
    return InvestorFlowSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=snapshot_date,
        foreign_net=foreign_net,
        institution_net=5000,
        double_buy=double_buy,
        double_sell=False,
        foreign_consecutive_buy_days=3,
        institution_consecutive_buy_days=2,
        source="naver_finance",
    )


@pytest.mark.asyncio
async def test_build_sentiment_evidence_kr_covered(db_session):
    symbol = f"TSD{uuid.uuid4().hex[:8].upper()}"
    missing_symbol = f"TSM{uuid.uuid4().hex[:8].upper()}"
    await _clear(db_session, symbol, missing_symbol)
    db_session.add(
        _flow(
            symbol,
            snapshot_date=dt.date(2026, 5, 23),
            foreign_net=120000,
            double_buy=True,
        )
    )
    await db_session.commit()

    bundle = await build_sentiment_evidence(
        InvestorFlowSnapshotsRepository(db_session),
        market="kr",
        symbols={symbol, missing_symbol},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["market"] == "kr"
    assert bundle["data_health"] == {"requested": 2, "covered": 1}
    assert bundle["covered_count"] == 1
    row = bundle["per_symbol"][0]
    assert row["symbol"] == symbol
    assert row["foreign_net"] == 120000
    assert row["double_buy"] is True
    assert row["foreign_consecutive_buy_days"] == 3
    assert bundle["freshness"]["status"] in {"fresh", "stale"}
    assert bundle["freshness"]["latest_snapshot_date"] == "2026-05-23"


@pytest.mark.asyncio
async def test_build_sentiment_evidence_non_kr_is_unavailable(db_session):
    bundle = await build_sentiment_evidence(
        InvestorFlowSnapshotsRepository(db_session),
        market="us",
        symbols={"AAPL"},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["market"] == "us"
    assert bundle["per_symbol"] == []
    assert bundle["covered_count"] == 0
    assert bundle["freshness"]["status"] == "unavailable"
    assert bundle["data_health"] == {"requested": 1, "covered": 0}


@pytest.mark.asyncio
async def test_build_sentiment_evidence_empty_kr_is_unavailable(db_session):
    symbol = f"TSE{uuid.uuid4().hex[:8].upper()}"
    await _clear(db_session, symbol)
    bundle = await build_sentiment_evidence(
        InvestorFlowSnapshotsRepository(db_session),
        market="kr",
        symbols={symbol},
        now=dt.datetime(2026, 5, 24, tzinfo=dt.UTC),
    )
    assert bundle["covered_count"] == 0
    assert bundle["freshness"]["status"] == "unavailable"
