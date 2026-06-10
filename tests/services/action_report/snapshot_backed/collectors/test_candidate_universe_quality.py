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
async def test_us_candidate_pool_size_and_displayed_count_are_exposed(db_session):
    today = dt.date(2026, 6, 9)
    rows = []
    for idx in range(55):
        symbol = f"POOL{idx:02d}"
        rows.extend(
            [
                _snap(
                    symbol,
                    close=100.0 + idx,
                    vol=20_000_000 + idx,
                    change=3.0 + idx / 100,
                    d=today,
                ),
                USSymbolUniverse(
                    symbol=symbol,
                    exchange="NASDAQ",
                    is_active=True,
                    is_common_stock=True,
                ),
            ]
        )
    db_session.add_all(rows)
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
    payload = results[0].payload_json

    # US path evaluates a wide pool of max(limit*5, 50), not the entire DB partition.
    assert payload["pool_size"] == 50
    assert payload["displayed_count"] == 5
    assert len(payload["candidates"]) == 5
    assert payload["candidate_limit"] == 5
    assert payload["capped"] is True


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


# KR no-regression (US-only quality gate) is covered deterministically in
# tests/services/action_report/test_candidate_universe_collector_evidence.py
# (test_kr_equity_path_has_no_us_quality_gate) using the fake equity repo, which
# avoids the real-DB KR preset path that was CI-flaky here.
