import datetime as dt

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _snap(symbol, *, close, vol, change, d):
    return InvestScreenerSnapshot(
        market="us",
        symbol=symbol,
        snapshot_date=d,
        latest_close=close,
        change_rate=change,
        week_change_rate=0,
        closes_window=[],
        source="yahoo",
        daily_volume=vol,
    )


@pytest.mark.asyncio
async def test_us_candidates_carry_quality_flags_and_priority(db_session):
    today = dt.date(2026, 6, 9)
    db_session.add_all(
        [
            _snap("GOOD", close=150.0, vol=20_000_000, change=3.0, d=today),
            _snap("PENNY", close=2.0, vol=100_000, change=3.0, d=today),
            USSymbolUniverse(
                symbol="GOOD", exchange="NASDAQ", is_active=True, is_common_stock=True
            ),
            USSymbolUniverse(
                symbol="PENNY", exchange="NYSE", is_active=True, is_common_stock=True
            ),
        ]
    )
    await db_session.flush()
    collector = CandidateUniverseSnapshotCollector(db_session)
    req = CollectorRequest(
        market="us",
        account_scope="kis_live",
        candidate_limit=5,
        symbols=None,
        policy_snapshot={},
    )
    results = await collector.collect(req)
    cands = {c["symbol"]: c for c in results[0].payload_json["candidates"]}
    assert "priority_score" in cands["GOOD"]
    assert "penny" in cands["PENNY"]["quality_flags"]
    assert "illiquid" in cands["PENNY"]["quality_flags"]
    # priority: GOOD (liquid) ranks above PENNY
    assert cands["GOOD"]["candidate_rank"] < cands["PENNY"]["candidate_rank"]
    # pool/display transparency (ROB-346 §3.6)
    assert results[0].payload_json["pool_size"] >= len(cands)


@pytest.mark.asyncio
async def test_us_unknown_common_stock_flagged(db_session):
    # Liquid, non-penny US symbol with NO us_symbol_universe row → is_common_stock
    # is None → "common_stock_unknown" (data_gap-grade), never silently rejected.
    today = dt.date(2026, 6, 9)
    db_session.add(_snap("NOUNIV", close=150.0, vol=20_000_000, change=3.0, d=today))
    await db_session.flush()
    collector = CandidateUniverseSnapshotCollector(db_session)
    req = CollectorRequest(
        market="us",
        account_scope="kis_live",
        candidate_limit=5,
        symbols=None,
        policy_snapshot={},
    )
    results = await collector.collect(req)
    cands = {c["symbol"]: c for c in results[0].payload_json["candidates"]}
    assert "common_stock_unknown" in cands["NOUNIV"]["quality_flags"]


@pytest.mark.asyncio
async def test_kr_candidates_have_no_quality_gate(db_session):
    # ROB-346 is US-only: the KR path must not gain quality_flags (no regression).
    today = dt.date(2026, 6, 9)
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="005930",
            snapshot_date=today,
            latest_close=3.0,  # would be "penny" if US gate applied
            change_rate=3.0,
            week_change_rate=0,
            closes_window=[],
            source="kis",
            daily_volume=100,
        )
    )
    await db_session.flush()
    collector = CandidateUniverseSnapshotCollector(db_session)
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        candidate_limit=5,
        symbols=None,
        policy_snapshot={},
    )
    results = await collector.collect(req)
    for cand in results[0].payload_json["candidates"]:
        assert "quality_flags" not in cand  # US-only gate: KR untouched
