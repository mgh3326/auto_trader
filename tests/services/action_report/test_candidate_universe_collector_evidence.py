import datetime as dt
from decimal import Decimal

import pytest

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot
from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


@pytest.mark.asyncio
async def test_crypto_collector_emits_candidate_evidence(db_session):
    # Clean up crypto snapshots before test to avoid contamination
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM invest_crypto_screener_snapshots"))
    await db_session.commit()

    db_session.add(
        InvestCryptoScreenerSnapshot(
            symbol="KRW-XRP", snapshot_date=dt.date(2026, 5, 23), name="리플",
            latest_close=Decimal("3000"), change_rate=Decimal("8.0"),
            trade_amount_24h=Decimal("500000000"), source="tvscreener_upbit",
        )
    )
    await db_session.commit()

    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(market="crypto", account_scope=None, symbols=[], policy_snapshot={})
    )
    payload = results[0].payload_json
    assert payload["candidates"], "expected candidate evidence rows"
    top = payload["candidates"][0]
    assert top["symbol"] == "KRW-XRP"
    assert top["score"] == 9.0
    assert top["reasons"] == ["단기 상승 모멘텀 후보"]
    assert payload["source_coverage"] == {"tvscreener_upbit": 1}
    assert payload["usefulness"] == "useful"


@pytest.mark.asyncio
async def test_crypto_collector_empty_sets_structured_missing_data(db_session):
    # Clean up crypto snapshots before test
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM invest_crypto_screener_snapshots"))
    await db_session.commit()

    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(market="crypto", account_scope=None, symbols=[], policy_snapshot={})
    )
    payload = results[0].payload_json
    assert payload["candidates"] == []
    assert payload["usefulness"] == "empty"
    assert payload["missing_data"]["confidence_impact"] == "cap 20"
    assert "암호화폐" in payload["missing_data"]["what"]
