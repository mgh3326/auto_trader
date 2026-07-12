"""ROB-842 G5: a caller/order-derived (synthetic) snapshot cannot be trusted
market evidence for a production submit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.services.alpaca_paper_market_evidence import (
    MarketEvidenceError,
    load_market_evidence,
)

pytestmark = [pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean():
    stmt = delete(MarketQuoteSnapshot).where(MarketQuoteSnapshot.symbol == "AAPL")
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.commit()


async def _seed(raw_payload) -> int:
    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market="us",
            symbol="AAPL",
            source="yahoo",
            snapshot_at=datetime.now(UTC),
            price=Decimal("150"),
            raw_payload=raw_payload,
        )
        db.add(row)
        await db.commit()
        return row.id


@pytest.mark.parametrize(
    "raw",
    [
        {"synthetic": True},
        {"provenance": "smoke"},
        {"provenance": "operator_synthetic"},
        {"provenance": "order_derived"},
    ],
)
async def test_synthetic_snapshot_rejected_as_evidence(raw):
    sid = await _seed(raw)
    async with AsyncSessionLocal() as db:
        with pytest.raises(MarketEvidenceError) as exc:
            await load_market_evidence(
                db,
                sid,
                execution_symbol="AAPL",
                asset_class="us_equity",
                now=datetime.now(UTC),
                max_age=timedelta(minutes=5),
            )
    assert exc.value.code == "synthetic_snapshot"


async def test_genuine_snapshot_accepted_as_evidence():
    # A real (unmarked / real-payload) snapshot is accepted.
    sid = await _seed({"source_api": "yahoo", "regularMarketPrice": 150})
    async with AsyncSessionLocal() as db:
        evidence = await load_market_evidence(
            db,
            sid,
            execution_symbol="AAPL",
            asset_class="us_equity",
            now=datetime.now(UTC),
            max_age=timedelta(minutes=5),
        )
    assert evidence.price == Decimal("150")
    assert evidence.market_data_source == "yahoo"
