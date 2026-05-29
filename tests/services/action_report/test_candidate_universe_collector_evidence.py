import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.models.invest_crypto_screener_snapshot import InvestCryptoScreenerSnapshot
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
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
            market="us",
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
    # ROB-359 Scope E — per-candidate lineage so a new-buy item is self-describing.
    assert top["source_preset"] == "crypto_momentum"
    assert top["data_state"] == "fresh"  # usefulness "useful" → fresh
    # crypto_momentum is an auto_trader-original preset, not a Toss-parity one.
    assert top["toss_parity_status"] == "not_toss_parity"


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


@pytest.mark.asyncio
async def test_cap_surfaced_when_universe_exceeds_limit(db_session):
    """ROB-352 Slice C — universe larger than the limit is flagged, not silent."""
    repo = _FakeEquityRepository()  # 3 rows, fresh_count=3
    collector = CandidateUniverseSnapshotCollector(db_session, equity_repository=repo)
    results = await collector.collect(
        CollectorRequest(
            market="us",
            account_scope=None,
            symbols=[],
            candidate_limit=2,
            policy_snapshot={},
        )
    )
    payload = results[0].payload_json
    assert payload["universe_count"] == 3
    assert payload["capped"] is True
    assert results[0].coverage_json["universe_count"] == 3
    assert results[0].coverage_json["capped"] is True


@pytest.mark.asyncio
async def test_cap_not_flagged_when_universe_within_limit(db_session):
    """ROB-352 Slice C — universe <= limit → capped is False."""
    repo = _FakeEquityRepository()  # 3 rows
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
    assert payload["universe_count"] == 3
    assert payload["capped"] is False
    assert results[0].coverage_json["capped"] is False


@pytest.mark.asyncio
async def test_kr_collector_sources_consecutive_gainers_preset(db_session):
    """ROB-363 — KR candidate source is consecutive_gainers (full Toss parity),
    not top_gainers, when the preset returns rows. Per-candidate data_state and
    toss_parity_status reflect the real preset."""
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM invest_screener_snapshots"))
    await db_session.commit()

    today = dt.date(2026, 5, 29)
    db_session.add(
        InvestScreenerSnapshot(
            market="kr",
            symbol="005930",
            snapshot_date=today,
            latest_close=Decimal("70000"),
            change_rate=Decimal("2.0"),
            week_change_rate=Decimal("8.0"),
            consecutive_up_days=6,
            closes_window=[1, 2, 3, 4, 5],
            source="kis",
        )
    )
    await db_session.commit()

    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(
            market="kr", account_scope=None, symbols=[], policy_snapshot={}
        )
    )
    payload = results[0].payload_json
    syms = [c["symbol"] for c in payload["candidates"]]
    assert "005930" in syms
    top = next(c for c in payload["candidates"] if c["symbol"] == "005930")
    assert top["source_preset"] == "consecutive_gainers"
    assert top["toss_parity_status"] == "full"
    assert top["data_state"] in {"fresh", "stale"}

    # Cleanup: remove test rows so subsequent runs don't see qualifying KR data.
    await db_session.execute(text("DELETE FROM invest_screener_snapshots"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_kr_collector_falls_back_to_top_gainers_when_no_preset_rows(
    db_session, monkeypatch
):
    """ROB-363 — when KR Toss-parity presets yield no rows, the collector falls
    back to the top_gainers momentum ranking (not_toss_parity), deterministically
    (loader stubbed to None, so this does not depend on DB contents)."""
    import app.services.invest_view_model.screener_service as ss

    async def _no_rows(session, *, market, limit):
        return None

    monkeypatch.setattr(ss, "load_consecutive_gainers_from_snapshots", _no_rows)

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
    payload = results[0].payload_json
    assert payload["preset"] == "top_gainers"
    top = payload["candidates"][0]
    assert top["source_preset"] == "top_gainers"
    assert top["toss_parity_status"] == "not_toss_parity"


@pytest.mark.asyncio
async def test_kr_preset_pool_wider_than_limit_is_capped(db_session, monkeypatch):
    """ROB-363 — the KR preset pool is gathered wider than candidate_limit, then
    sliced; universe_count reflects the full evaluated pool and capped is True.
    Loader stubbed so the assertion does not depend on DB contents."""
    import app.services.invest_view_model.screener_service as ss

    async def _three_fresh_rows(session, *, market, limit):
        return [
            {
                "symbol": sym,
                "name": sym,
                "source": "kis",
                "change_rate": rate,
                "close": 1000,
                "consecutive_up_days": 6,
                "volume": 1,
                "_screener_snapshot_state": "fresh",
            }
            for sym, rate in [("000660", 9.0), ("005930", 8.0), ("035720", 7.0)]
        ]

    monkeypatch.setattr(
        ss, "load_consecutive_gainers_from_snapshots", _three_fresh_rows
    )

    collector = CandidateUniverseSnapshotCollector(db_session)
    results = await collector.collect(
        CollectorRequest(
            market="kr",
            account_scope=None,
            symbols=[],
            candidate_limit=2,
            policy_snapshot={},
        )
    )
    payload = results[0].payload_json
    assert payload["universe_count"] == 3
    assert payload["capped"] is True
    assert len(payload["candidates"]) == 2
    assert results[0].coverage_json["capped"] is True
