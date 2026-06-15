from __future__ import annotations

import datetime as dt
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.services.investor_flow_snapshots.builder import build_investor_flow_snapshots

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "investor_flow"


async def _fake_fetcher(symbol: str, days: int):
    assert days == 3
    fixtures = {
        "900301": [
            {
                "date": "2026-05-12",
                "close": 75000,
                "change_pct": 2.5,
                "volume": 15_118_684,
                "foreign_net": 300,
                "institutional_net": 200,
                "foreign_holding_shares": 2_790_424_635,
                "foreign_holding_rate": 47.73,
            },
            {"date": "2026-05-11", "foreign_net": 100, "institutional_net": -50},
            {"date": "2026-05-08", "foreign_net": -25, "institutional_net": -75},
        ],
        "900302": [
            {"date": "2026-05-12", "foreign_net": -500, "institutional_net": -10},
            {"date": "2026-05-11", "foreign_net": -100, "institutional_net": 40},
            {"date": "2026-05-08", "foreign_net": 50, "institutional_net": 20},
        ],
    }
    return {"symbol": symbol, "data": fixtures[symbol]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_investor_flow_snapshots_derives_streaks_ranks_and_individual():
    collected_at = dt.datetime(2026, 5, 12, 7, 0, tzinfo=dt.UTC)

    result = await build_investor_flow_snapshots(
        symbols=["900301", "900302"],
        days=3,
        collected_at=collected_at,
        fetcher=_fake_fetcher,
        concurrency=2,
    )

    assert result.warnings == ()
    assert len(result.payloads) == 6
    by_key = {(p.symbol, p.snapshot_date): p for p in result.payloads}
    newest = by_key[("900301", dt.date(2026, 5, 12))]
    assert newest.market == "kr"
    assert newest.source == "naver_finance"
    assert newest.collected_at == collected_at
    assert newest.individual_net == -500
    assert newest.close == Decimal("75000")
    assert newest.change_rate == Decimal("2.5")
    assert newest.volume == 15_118_684
    assert newest.foreign_holding_shares == 2_790_424_635
    assert newest.foreign_holding_rate == Decimal("47.73")
    assert newest.foreign_consecutive_buy_days == 2
    assert newest.foreign_consecutive_sell_days is None
    assert newest.institution_consecutive_buy_days == 1
    assert newest.individual_consecutive_sell_days == 2
    assert newest.foreign_net_buy_rank == 1
    assert newest.institution_net_buy_rank == 1

    sell_leader = by_key[("900302", dt.date(2026, 5, 12))]
    assert sell_leader.foreign_consecutive_sell_days == 2
    assert sell_leader.institution_consecutive_sell_days == 1
    assert sell_leader.foreign_net_sell_rank == 1
    assert sell_leader.institution_net_sell_rank == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_investor_flow_snapshots_warns_and_skips_empty_or_invalid_rows():
    async def fake_fetcher(symbol: str, days: int):
        if symbol == "900303":
            return {"symbol": symbol, "data": []}
        return {"symbol": symbol, "data": [{"date": "not-a-date", "foreign_net": 1}]}

    result = await build_investor_flow_snapshots(
        symbols=["900303", "900304"],
        fetcher=fake_fetcher,
    )

    assert result.payloads == []
    assert "900303: no investor-flow rows returned" in result.warnings
    assert "900304: row 0 has invalid date" in result.warnings
    assert "900304: no valid investor-flow rows built" in result.warnings


@pytest.mark.unit
def test_403550_naver_fixture_loads_and_has_expected_shape():
    fixture = json.loads((_FIXTURE_DIR / "403550_naver_sample.json").read_text())
    assert fixture["symbol"] == "403550"
    assert fixture["source"] == "naver_finance"
    payloads = fixture["rows"]
    assert len(payloads) == 10
    assert payloads[0]["date"] == "2026-05-12"
    assert isinstance(payloads[0]["foreign_net"], int)
    assert isinstance(payloads[0]["institutional_net"], int)
