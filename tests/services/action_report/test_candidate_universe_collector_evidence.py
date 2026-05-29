import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot
from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.invest_screener_snapshots.repository import CoverageCounts
from app.services.investment_snapshots.collectors import CollectorRequest


@dataclass
class _EquityRow:
    symbol: str
    change_rate: Decimal
    latest_close: Decimal = Decimal("1000")
    source: str = "kis"
    daily_volume: int = 100_000
    consecutive_up_days: int | None = None


class _FakeEquityRepository:
    def __init__(self) -> None:
        self.rows = [
            _EquityRow(symbol="000660", change_rate=Decimal("9.0")),
            _EquityRow(symbol="005930", change_rate=Decimal("8.0")),
            _EquityRow(symbol="035720", change_rate=Decimal("7.0")),
        ]
        self.requested_limits: list[int] = []

    async def coverage(
        self, *, market: str, today_trading_date: dt.date
    ) -> CoverageCounts:
        return CoverageCounts(
            market=market,
            today_trading_date=today_trading_date,
            fresh_count=len(self.rows),
            stale_count=0,
            last_computed_at=None,
        )

    async def list_top_candidates(
        self, *, market: str, limit: int = 10
    ) -> list[_EquityRow]:
        self.requested_limits.append(limit)
        return self.rows[:limit]


@pytest.mark.asyncio
async def test_equity_collector_respects_candidate_limit(db_session):
    repo = _FakeEquityRepository()
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)

    results = await collector.collect(
        CollectorRequest(
            market="kr",
            account_scope=None,
            symbols=[],
            candidate_limit=2,
            policy_snapshot={},
        )
    )

    assert repo.requested_limits == [2]
    payload = results[0].payload_json
    assert payload["candidate_limit"] == 2
    assert [candidate["symbol"] for candidate in payload["candidates"]] == [
        "000660",
        "005930",
    ]
    assert results[0].coverage_json["candidate_count"] == 2


@pytest.mark.asyncio
async def test_crypto_collector_emits_candidate_evidence(db_session):
    # Clean up crypto snapshots before test to avoid contamination
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM invest_crypto_screener_snapshots"))
    await db_session.commit()

    db_session.add(
        InvestCryptoScreenerSnapshot(
            symbol="KRW-XRP",
            snapshot_date=dt.date(2026, 5, 23),
            name="리플",
            latest_close=Decimal("3000"),
            change_rate=Decimal("8.0"),
            trade_amount_24h=Decimal("500000000"),
            source="tvscreener_upbit",
        )
    )
    await db_session.commit()

    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(
            market="crypto", account_scope=None, symbols=[], policy_snapshot={}
        )
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
        CollectorRequest(
            market="crypto", account_scope=None, symbols=[], policy_snapshot={}
        )
    )
    payload = results[0].payload_json
    assert payload["candidates"] == []
    assert payload["usefulness"] == "empty"
    assert payload["missing_data"]["confidence_impact"] == "cap 20"
    assert "암호화폐" in payload["missing_data"]["what"]


@pytest.mark.asyncio
async def test_equity_collector_dedupes_symbol_format_variants(db_session):
    """ROB-352 Slice C — BRK.B / BRK-B collapse to one candidate; ranks contiguous."""

    class _DupRepo(_FakeEquityRepository):
        def __init__(self) -> None:
            super().__init__()
            self.rows = [
                _EquityRow(symbol="BRK.B", change_rate=Decimal("9.0")),
                _EquityRow(symbol="BRK-B", change_rate=Decimal("8.5")),
                _EquityRow(symbol="AAPL", change_rate=Decimal("8.0")),
            ]

    repo = _DupRepo()
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)
    results = await collector.collect(
        CollectorRequest(
            market="us",
            account_scope=None,
            symbols=[],
            candidate_limit=10,
            policy_snapshot={},
        )
    )
    payload = results[0].payload_json
    symbols = [c["symbol"] for c in payload["candidates"]]
    assert symbols == ["BRK.B", "AAPL"]
    assert payload["candidates"][0]["candidate_rank"] == 1
    assert payload["candidates"][1]["candidate_rank"] == 2
    assert results[0].coverage_json["candidate_count"] == 2
