"""ROB-273 — snapshot-backed collector tests.

Each test verifies that the collector emits a well-formed
:class:`SnapshotCollectResult` and never reaches into broker /
order / watch / scheduler write paths.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    CandidateUniverseStubCollector,
    InvestPageStubCollector,
    NaverRemoteDebugStubCollector,
    SymbolStubCollector,
    TossRemoteDebugStubCollector,
)
from app.services.action_report.snapshot_backed.collectors.portfolio import (
    PortfolioSnapshotCollector,
)
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
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
    from app.models.manual_holdings import MarketType

    session = MagicMock()

    class _Row:
        ticker = "005930"
        market_type = MarketType.KR
        quantity = 10
        avg_price = 70_000
        display_name = "삼성전자"
        updated_at = dt.datetime(2026, 5, 19, tzinfo=dt.UTC)

    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[_Row()])
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=result)

    collector = PortfolioSnapshotCollector(session)
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].snapshot_kind == "portfolio"
    assert results[0].source_kind == "auto_trader_mcp"
    assert results[0].payload_json["count"] == 1
    assert results[0].payload_json["holdings"][0]["ticker"] == "005930"


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
        SymbolStubCollector,
        CandidateUniverseStubCollector,
        InvestPageStubCollector,
        NaverRemoteDebugStubCollector,
        TossRemoteDebugStubCollector,
        BrowserProbeStubCollector,
    ],
)
async def test_stubs_return_unavailable(collector_cls: type) -> None:
    collector = collector_cls()
    results = await collector.collect(_request())
    assert len(results) == 1
    assert results[0].freshness_status == "unavailable"
    assert results[0].snapshot_kind == collector.snapshot_kind


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
    )
    target_modules = [
        "app.services.action_report.snapshot_backed.collectors.portfolio",
        "app.services.action_report.snapshot_backed.collectors.journal",
        "app.services.action_report.snapshot_backed.collectors.watch_context",
        "app.services.action_report.snapshot_backed.collectors.market",
        "app.services.action_report.snapshot_backed.collectors.news",
        "app.services.action_report.snapshot_backed.collectors.optional_stubs",
        "app.services.action_report.snapshot_backed.collectors.registry",
        "app.services.action_report.snapshot_backed.generator",
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
