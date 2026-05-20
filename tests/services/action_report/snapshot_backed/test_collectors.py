"""ROB-273 — snapshot-backed collector tests.

Each test verifies that the collector emits a well-formed
:class:`SnapshotCollectResult` and never reaches into broker /
order / watch / scheduler write paths.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.action_report.snapshot_backed.collectors.candidate_universe import (
    CandidateUniverseSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.invest_page import (
    InvestPageSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.journal import (
    JournalSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.market import (
    MarketEventsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.news import (
    NewsSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.optional_stubs import (
    BrowserProbeStubCollector,
    NaverRemoteDebugStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.action_report.snapshot_backed.collectors.symbol import (
    SymbolSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.watch_context import (
    WatchContextSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest


def _request(market: str = "kr", account_scope: str = "kis_live") -> CollectorRequest:
    return CollectorRequest(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
    )


# ---------------------------------------------------------------------------
# Portfolio collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_portfolio_collector_returns_holdings(monkeypatch: pytest.MonkeyPatch):
    """v1 manual-primary path remains for non-(kr+kis_live) combos.

    ROB-278 reserved the kr+kis_live combo for the new KIS live path
    (see test_portfolio_v2_*). Other combos keep the v1 contract and
    additionally surface the ``primary_source="manual"`` label.
    """
    from app.models.manual_holdings import MarketType

    session = MagicMock()

    class _Row:
        ticker = "AAPL"
        market_type = MarketType.US
        quantity = 10
        avg_price = 150.0
        display_name = "Apple"
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[_Row()])
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=result)

    collector = PortfolioSnapshotCollector(session)
    results = await collector.collect(_request(market="us", account_scope="kis_live"))
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].source_kind == "auto_trader_mcp"
    assert results[0].payload_json["count"] == 1
    assert results[0].payload_json["holdings"][0]["ticker"] == "AAPL"
    # ROB-278 — payload v2 surfaces primary_source even on the v1 path.
    assert results[0].payload_json["primary_source"] == "manual"


@pytest.mark.asyncio
async def test_portfolio_collector_empty_holdings_returns_partial():
    """No matching holdings → result still emitted, status='partial'."""
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[]))
    result = MagicMock(scalars=MagicMock(return_value=scalars))
    session.execute = AsyncMock(return_value=result)

    collector = PortfolioSnapshotCollector(session)
    results = await collector.collect(_request(market="us", account_scope="kis_live"))
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].freshness_status == "partial"
    assert results[0].payload_json["count"] == 0


# ---------------------------------------------------------------------------
# Portfolio v2 — ROB-278 KIS live path for KR + kis_live.
#
# Lockdown policy:
# - user_id missing on kis_live → fail-closed (unavailable, no implicit default).
# - KIS success → primary_source="kis"; manual rows go to reference_holdings.
# - KIS failure → primary_source="none"; manual NEVER promoted to primary.
# - Payload v2 is additive: existing v1 keys (holdings, count, market) preserved.
# - Non-(kr+kis_live) combos preserve v1 manual-primary behaviour.
# ---------------------------------------------------------------------------
def _kr_kis_request(user_id: int | None = None) -> CollectorRequest:
    return CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=user_id,
    )


def _empty_manual_session() -> MagicMock:
    """Session whose execute() returns no manual_holdings rows."""
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[]))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _manual_kr_session(rows: list[Any] | None = None) -> MagicMock:
    from app.models.manual_holdings import MarketType

    class _ManualRow:
        def __init__(
            self,
            ticker: str = "005930",
            quantity: float = 5.0,
            avg_price: float = 70_000,
        ) -> None:
            self.ticker = ticker
            self.market_type = MarketType.KR
            self.quantity = quantity
            self.avg_price = avg_price
            self.display_name = ticker
            self.updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    rows = rows if rows is not None else [_ManualRow()]
    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=rows))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    return session


def _kis_reader_with_holdings() -> MagicMock:
    """KISHomeReader stub returning one KR holding + KRW cash."""
    from app.schemas.invest_home import Account, CashAmounts, Holding
    from app.services.invest_home_service import _SourceFetchResult

    holding_kr = Holding(
        holdingId="kis:kr:005930",
        accountId="kis_account",
        source="kis",
        accountKind="live",
        symbol="005930",
        market="KR",
        assetType="equity",
        assetCategory="kr_stock",
        displayName="삼성전자",
        quantity=10.0,
        averageCost=70_000,
        costBasis=700_000,
        currency="KRW",
        valueNative=750_000,
        valueKrw=750_000,
        pnlKrw=50_000,
        pnlRate=0.0714,
        sellableQuantity=8.0,
        pendingSellQuantity=2.0,
        referenceQuantity=0.0,
    )
    account = Account(
        accountId="kis_account",
        displayName="KIS 실계좌",
        source="kis",
        accountKind="live",
        includedInHome=True,
        valueKrw=750_000,
        costBasisKrw=700_000,
        pnlKrw=50_000,
        pnlRate=0.0714,
        cashBalances=CashAmounts(krw=1_200_000.0, usd=None),
        buyingPower=CashAmounts(krw=1_000_000.0, usd=None),
    )
    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[account], holdings=[holding_kr], warning=None
        )
    )
    return reader


def _kis_reader_failed() -> MagicMock:
    from app.schemas.invest_home import InvestHomeWarning
    from app.services.invest_home_service import _SourceFetchResult

    reader = MagicMock()
    reader.fetch = AsyncMock(
        return_value=_SourceFetchResult(
            accounts=[],
            holdings=[],
            warning=InvestHomeWarning(source="kis", message="connection timeout"),
        )
    )
    return reader


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_missing_user_id_is_fail_closed():
    """ROB-278 — no user_id on kis_live request → unavailable, no implicit default."""
    session = _empty_manual_session()
    reader = MagicMock()
    reader.fetch = AsyncMock(
        side_effect=AssertionError("KIS must not be called without user_id")
    )
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=None))
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].freshness_status == "unavailable"
    assert "user_id" in results[0].errors_json["reason"]
    # primary_source label is present and explicitly "none" (manual NOT promoted).
    assert results[0].payload_json.get("primary_source") == "none"
    reader.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_success_populates_kis_primary():
    """ROB-278 — KIS success: primary_source=kis, KIS holdings primary, manual in reference."""
    session = _manual_kr_session()
    reader = _kis_reader_with_holdings()
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "fresh"
    # v1 keys preserved (additive shape).
    assert "holdings" in payload
    assert "count" in payload
    assert "market" in payload
    # v2 fields.
    assert payload["primary_source"] == "kis"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "005930"
    assert payload["holdings"][0]["source"] == "kis"
    assert payload["holdings"][0]["sellable_quantity"] == 8.0
    assert payload["holdings"][0]["pending_sell_quantity"] == 2.0
    assert payload["cash"]["krw"] == 1_200_000.0
    assert payload["buying_power"]["krw"] == 1_000_000.0
    assert payload["sellable_summary"]["sellable_count"] == 1
    # Manual KR row appears in reference_holdings, NOT in holdings.
    assert payload["reference_holdings"][0]["ticker"] == "005930"
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance.
    assert payload["provenance"]["kis_fetch_status"] == "ok"
    assert payload["provenance"]["account_scope"] == "kis_live"
    reader.fetch.assert_awaited_once_with(user_id=42)


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_failure_does_not_promote_manual():
    """ROB-278 — KIS failure: primary_source=none, manual stays in reference_holdings."""
    session = _manual_kr_session()
    reader = _kis_reader_failed()
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert payload["holdings"] == []
    assert payload["count"] == 0
    # Manual remains visible as reference, never promoted to primary.
    assert len(payload["reference_holdings"]) == 1
    assert payload["reference_holdings"][0]["source"] == "manual"
    # Provenance carries the failure reason.
    assert payload["provenance"]["kis_fetch_status"] == "failed"
    assert "kis" in str(payload["provenance"]["warnings"]).lower()


@pytest.mark.asyncio
async def test_portfolio_v2_kr_kis_live_exception_is_fail_closed():
    """ROB-278 — KISHomeReader raising is treated like 'failed', not crash."""
    session = _manual_kr_session()
    reader = MagicMock()
    reader.fetch = AsyncMock(side_effect=RuntimeError("boom"))
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    results = await collector.collect(_kr_kis_request(user_id=42))
    assert len(results) == 1
    payload = results[0].payload_json
    assert results[0].freshness_status == "unavailable"
    assert payload["primary_source"] == "none"
    assert "boom" in payload["provenance"]["errors"][0]


@pytest.mark.asyncio
async def test_portfolio_v2_crypto_upbit_live_preserves_v1_manual_primary():
    """ROB-278 — non-(kr+kis_live) combos unchanged: manual still primary."""
    from app.models.manual_holdings import MarketType

    class _CryptoRow:
        ticker = "KRW-BTC"
        market_type = MarketType.CRYPTO
        quantity = 0.1
        avg_price = 50_000_000
        display_name = "비트코인"
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[_CryptoRow()]))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )
    # KIS reader MUST NOT be called for upbit_live.
    reader = MagicMock()
    reader.fetch = AsyncMock(
        side_effect=AssertionError("KIS reader called for non-kis_live request")
    )
    collector = PortfolioSnapshotCollector(session, kis_reader=reader)
    req = CollectorRequest(
        market="crypto",
        account_scope="upbit_live",
        symbols=None,
        candidate_limit=None,
        policy_snapshot={},
        user_id=None,
    )
    results = await collector.collect(req)
    payload = results[0].payload_json
    assert results[0].freshness_status == "fresh"
    assert payload["count"] == 1
    assert payload["holdings"][0]["ticker"] == "KRW-BTC"
    # New label is "manual" for non-KIS combos so payload always says where data came from.
    assert payload.get("primary_source") == "manual"
    reader.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Journal collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_journal_collector_returns_active_and_recent():
    session = MagicMock()

    class _ActiveJournal:
        id = 1
        symbol = "005930"
        instrument_type = MagicMock(value="kr_stock")
        side = "buy"
        status = "active"
        entry_price = 70_000
        quantity = 10
        thesis = "thesis"
        strategy = "swing"
        target_price = 80_000
        stop_loss = 65_000
        hold_until = None
        exit_price = None
        exit_reason = None
        pnl_pct = None
        account_type = "live"
        created_at = dt.datetime(2026, 5, 18, tzinfo=dt.UTC)
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    active_scalars = MagicMock(all=MagicMock(return_value=[_ActiveJournal()]))
    active_result = MagicMock(scalars=MagicMock(return_value=active_scalars))
    recent_scalars = MagicMock(all=MagicMock(return_value=[]))
    recent_result = MagicMock(scalars=MagicMock(return_value=recent_scalars))
    session.execute = AsyncMock(side_effect=[active_result, recent_result])

    collector = JournalSnapshotCollector(session)
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].snapshot_kind == "journal"
    assert results[0].payload_json["active_count"] == 1
    assert results[0].payload_json["retrospective_count"] == 0


# ---------------------------------------------------------------------------
# Watch-context collector — MUST NOT call activation paths
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_watch_context_collector_uses_only_read_methods():
    """The collector reads via list_active_alerts and never touches activation."""

    session = MagicMock()
    repo = MagicMock()

    class _Alert:
        alert_uuid = "11111111-1111-1111-1111-111111111111"
        source_report_uuid = "22222222-1111-1111-1111-111111111111"
        source_item_uuid = "33333333-1111-1111-1111-111111111111"
        market = "kr"
        symbol = "005930"
        metric = "price"
        operator = "above"
        threshold = 80_000
        threshold_key = "80000"
        intent = "buy_review"
        action_mode = "notify_only"
        rationale = "rationale"
        valid_until = dt.datetime(2026, 5, 20, tzinfo=dt.UTC)
        status = "active"
        activated_at = dt.datetime(2026, 5, 18, tzinfo=dt.UTC)

    repo.list_active_alerts = AsyncMock(return_value=[_Alert()])
    # Force the test to fail if the collector tries to activate/insert/transition.
    repo.insert_alert = MagicMock(
        side_effect=AssertionError("collector must not insert_alert")
    )
    repo.update_alert_status = MagicMock(
        side_effect=AssertionError("collector must not update_alert_status")
    )

    collector = WatchContextSnapshotCollector(session, repository=repo)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "watch_context"
    assert results[0].payload_json["active_count"] == 1
    repo.list_active_alerts.assert_awaited_once()
    # Mutation methods must not have been called.
    assert not repo.insert_alert.called
    assert not repo.update_alert_status.called


# ---------------------------------------------------------------------------
# Market collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_market_collector_returns_events():
    from app.schemas.market_events import MarketEventsRangeResponse

    session = MagicMock()
    query = MagicMock()
    query.list_for_range = AsyncMock(
        return_value=MarketEventsRangeResponse(
            from_date=dt.date(2026, 5, 19),
            to_date=dt.date(2026, 5, 20),
            count=0,
            events=[],
        )
    )
    collector = MarketEventsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "market"
    assert results[0].payload_json["event_count"] == 0


@pytest.mark.asyncio
async def test_market_collector_query_failure_returns_unavailable():
    session = MagicMock()
    query = MagicMock()
    query.list_for_range = AsyncMock(side_effect=RuntimeError("boom"))
    collector = MarketEventsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"
    assert "boom" in results[0].errors_json["reason"]


# ---------------------------------------------------------------------------
# News collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_news_collector_returns_citations():
    from app.schemas.research_reports import (
        ResearchReportCitation,
        ResearchReportCitationListResponse,
    )

    session = MagicMock()
    query = MagicMock()
    citation = ResearchReportCitation(
        report_uuid="44444444-1111-1111-1111-111111111111",
        title="t",
        source="kr_news",
        symbol_candidates=[],
        published_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        summary_text="s",
    )
    query.find_relevant = AsyncMock(
        return_value=ResearchReportCitationListResponse(count=1, citations=[citation])
    )
    collector = NewsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].snapshot_kind == "news"
    assert results[0].source_kind == "news_ingestor"
    assert results[0].payload_json["count"] == 1


@pytest.mark.asyncio
async def test_news_collector_failure_is_fail_open():
    session = MagicMock()
    query = MagicMock()
    query.find_relevant = AsyncMock(side_effect=RuntimeError("transient"))
    collector = NewsSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collector_cls",
    [
        NaverRemoteDebugStubCollector,
        TossRemoteDebugStubCollector,
        BrowserProbeStubCollector,
    ],
)
async def test_remote_debug_stubs_return_unavailable(collector_cls: type) -> None:
    collector = collector_cls()
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"
    assert results[0].snapshot_kind == collector.snapshot_kind


# ---------------------------------------------------------------------------
# Symbol collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_symbol_collector_returns_unavailable_when_no_symbols():
    session = MagicMock()
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(_request())  # symbols=None
    assert results[0].snapshot_kind == "symbol"
    assert results[0].freshness_status == "unavailable"


@pytest.mark.asyncio
async def test_symbol_collector_returns_results_for_each_symbol():
    from app.services.investment_snapshots.collectors import CollectorRequest

    class _Row:
        def __init__(self, symbol: str, name: str) -> None:
            self.symbol = symbol
            self.name = name
            self.instrument_type = "equity_kr"
            self.exchange = "KRX"
            self.sector = "Tech"
            self.market_cap = 1_000_000.0
            self.is_active = True

    session = MagicMock()
    scalars = MagicMock(all=MagicMock(return_value=[_Row("005930", "삼성전자")]))
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=scalars))
    )

    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930", "000660"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)
    # Two entries: one for resolved 005930, one partial for missing 000660.
    assert len(results) == 2
    assert any(r.symbol == "005930" for r in results)
    assert any(r.freshness_status == "partial" for r in results)


@pytest.mark.asyncio
async def test_symbol_collector_query_failure_is_fail_open():
    from app.services.investment_snapshots.collectors import CollectorRequest

    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("transient"))
    req = CollectorRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        candidate_limit=None,
        policy_snapshot={},
    )
    collector = SymbolSnapshotCollector(session)
    results = await collector.collect(req)
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Candidate-universe collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_candidate_universe_kr_returns_coverage_counts():
    from app.services.invest_screener_snapshots.repository import CoverageCounts

    session = MagicMock()
    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=12,
            stale_count=3,
            last_computed_at=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        )
    )
    collector = CandidateUniverseSnapshotCollector(session, equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="kis_live"))
    assert results[0].payload_json["fresh_count"] == 12
    assert results[0].payload_json["stale_count"] == 3


@pytest.mark.asyncio
async def test_candidate_universe_kr_returns_partial_when_no_rows():
    from app.services.invest_screener_snapshots.repository import CoverageCounts

    session = MagicMock()
    repo = MagicMock()
    repo.coverage = AsyncMock(
        return_value=CoverageCounts(
            market="kr",
            today_trading_date=dt.date(2026, 5, 19),
            fresh_count=0,
            stale_count=0,
            last_computed_at=None,
        )
    )
    collector = CandidateUniverseSnapshotCollector(session, equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="kis_live"))
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_candidate_universe_crypto_queries_crypto_partition():
    session = MagicMock()
    latest_result = MagicMock(
        scalar_one_or_none=MagicMock(return_value=dt.date(2026, 5, 19))
    )
    count_result = MagicMock(scalar_one=MagicMock(return_value=42))
    session.execute = AsyncMock(side_effect=[latest_result, count_result])

    collector = CandidateUniverseSnapshotCollector(session)
    results = await collector.collect(
        _request(market="crypto", account_scope="upbit_live")
    )
    assert results[0].payload_json["fresh_count"] == 42
    assert results[0].payload_json["latest_partition"] == "2026-05-19"


@pytest.mark.asyncio
async def test_candidate_universe_failure_is_fail_open():
    session = MagicMock()
    repo = MagicMock()
    repo.coverage = AsyncMock(side_effect=RuntimeError("boom"))
    collector = CandidateUniverseSnapshotCollector(session, equity_repository=repo)
    results = await collector.collect(_request(market="kr", account_scope="kis_live"))
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Invest-page collector
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invest_page_returns_recent_published_reports():
    session = MagicMock()
    query = MagicMock()

    class _Report:
        report_uuid = "55555555-1111-1111-1111-111111111111"
        report_type = "snapshot_backed_advisory_v1"
        status = "published"
        title = "t"
        published_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)
        snapshot_bundle_uuid = "66666666-1111-1111-1111-111111111111"
        snapshot_freshness_summary = {"overall": "fresh"}

    query.list_reports = AsyncMock(return_value=[_Report()])
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].payload_json["count"] == 1
    assert (
        results[0].payload_json["recent_published_reports"][0][
            "snapshot_freshness_overall"
        ]
        == "fresh"
    )


@pytest.mark.asyncio
async def test_invest_page_returns_partial_when_no_recent_reports():
    session = MagicMock()
    query = MagicMock()
    query.list_reports = AsyncMock(return_value=[])
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "partial"


@pytest.mark.asyncio
async def test_invest_page_failure_is_fail_open():
    session = MagicMock()
    query = MagicMock()
    query.list_reports = AsyncMock(side_effect=RuntimeError("transient"))
    collector = InvestPageSnapshotCollector(session, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_production_registry_covers_all_policy_kinds():
    from app.services.investment_snapshots.policy import INTRADAY_ACTION_REPORT_V1

    registry = production_collector_registry(session=MagicMock())
    registered = registry.list_kinds()
    policy_kinds = {k.snapshot_kind for k in INTRADAY_ACTION_REPORT_V1.kinds}

    missing = policy_kinds - registered
    assert missing == set(), f"policy kinds missing collectors: {missing}"


def test_production_registry_registers_pending_orders():
    """ROB-274 — pending_orders collector is wired into the production registry."""
    registry = production_collector_registry(session=MagicMock())
    assert "pending_orders" in registry.list_kinds()


# ---------------------------------------------------------------------------
# Static-import guard — none of the collector modules pull in known
# mutation paths. If a future contributor wires the trade execution
# service, the broker SDK, or WatchActivationService into a collector
# module's import graph, this assertion fires.
# ---------------------------------------------------------------------------
def test_collector_modules_do_not_import_broker_or_activation_paths():
    import importlib
    import sys

    forbidden_substrings: tuple[str, ...] = (
        "kis_trading_service",
        "investment_reports.watch_activation",
        "alpaca_paper_ledger_service",
        "upbit.client",  # upbit broker client
        # ROB-278 — also forbid explicit broker order-mutation verbs even
        # when shipped under different paths (defence in depth).
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
    )
    target_modules = [
        "app.services.action_report.snapshot_backed.collectors.portfolio",
        "app.services.action_report.snapshot_backed.collectors.journal",
        "app.services.action_report.snapshot_backed.collectors.watch_context",
        "app.services.action_report.snapshot_backed.collectors.market",
        "app.services.action_report.snapshot_backed.collectors.news",
        "app.services.action_report.snapshot_backed.collectors.symbol",
        "app.services.action_report.snapshot_backed.collectors.candidate_universe",
        "app.services.action_report.snapshot_backed.collectors.invest_page",
        "app.services.action_report.snapshot_backed.collectors.optional_stubs",
        "app.services.action_report.snapshot_backed.collectors.pending_orders",
        "app.services.action_report.snapshot_backed.collectors.registry",
        "app.services.action_report.snapshot_backed.generator",
        "app.services.action_report.snapshot_backed.symbol_derivation",
    ]

    for name in target_modules:
        importlib.import_module(name)
        module = sys.modules[name]
        source = open(module.__file__, encoding="utf-8").read()  # type: ignore[arg-type]
        for forbidden in forbidden_substrings:
            assert forbidden not in source, (
                f"{name} unexpectedly references {forbidden!r} — "
                "collectors must remain read-only"
            )
