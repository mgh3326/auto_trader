from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.schemas.analysis_snapshot_bundle import (
    ANALYSIS_SECTION_NAMES,
    AnalysisBundleCreateRequest,
)
from app.services.action_report.common.canonicalize import canonical_payload_hash
from app.services.analysis_snapshot_bundle import (
    AnalysisBundleCaptureService,
    AnalysisInputFrozenCollector,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectorRegistry,
    SnapshotCollectResult,
)
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

NOW = dt.datetime(2026, 7, 12, 3, 0, tzinfo=dt.UTC)


class FakeCollector:
    def __init__(
        self, snapshot_kind: str, results: list[SnapshotCollectResult]
    ) -> None:
        self.snapshot_kind = snapshot_kind
        self.collect = AsyncMock(return_value=results)


def _result(kind: str, *, symbol: str | None = None) -> SnapshotCollectResult:
    payload = {"kind": kind}
    if kind == "symbol":
        payload = {"quote": {"status": "ok", "price": 70_000}}
    return SnapshotCollectResult(
        snapshot_kind=kind,
        market="kr",
        account_scope="kis_live",
        symbol=symbol,
        source_kind="manual",
        payload_json=payload,
        source_timestamps_json={"provider": NOW.isoformat()},
        coverage_json={"covered": True},
        as_of=NOW,
    )


@pytest.fixture
def capture_request() -> AnalysisBundleCreateRequest:
    return AnalysisBundleCreateRequest(
        market="kr",
        account_scope="kis_live",
        symbols=["005930"],
        user_id=7,
        requested_by="claude_code",
    )


@pytest.fixture
def portfolio() -> FakeCollector:
    return FakeCollector("portfolio", [_result("portfolio")])


@pytest.fixture
def symbol() -> FakeCollector:
    return FakeCollector("symbol", [_result("symbol", symbol="005930")])


@pytest.fixture
def market() -> FakeCollector:
    return FakeCollector("market", [_result("market")])


@pytest.fixture
def investor_flow() -> FakeCollector:
    return FakeCollector("investor_flow", [_result("investor_flow", symbol="005930")])


@pytest.fixture
def analysis_fn() -> AsyncMock:
    return AsyncMock(return_value={"005930": {"rsi": 52.1}})


@pytest.fixture
def decision_history_fn() -> AsyncMock:
    return AsyncMock(return_value={"recent": []})


@pytest_asyncio.fixture
async def repo(db_session) -> InvestmentSnapshotsRepository:
    return InvestmentSnapshotsRepository(db_session)


@pytest_asyncio.fixture
async def service(
    db_session,
    portfolio,
    symbol,
    market,
    investor_flow,
    analysis_fn,
    decision_history_fn,
) -> AnalysisBundleCaptureService:
    registry = SnapshotCollectorRegistry()
    for collector in (portfolio, symbol, market, investor_flow):
        registry.register(collector)
    return AnalysisBundleCaptureService(
        db_session,
        collectors=registry,
        analysis_fn=analysis_fn,
        decision_history_fn=decision_history_fn,
        clock=lambda: NOW,
    )


@pytest_asyncio.fixture
async def bundle(repo):
    async def load(bundle_id):
        found = await repo.get_bundle_by_uuid(bundle_id)
        assert found is not None
        return found

    return load


@pytest_asyncio.fixture
async def load_frozen_document(repo):
    async def load(bundle_id):
        bundle = await repo.get_bundle_by_uuid(bundle_id)
        assert bundle is not None
        pairs = await repo.list_bundle_items_with_snapshots(bundle.id)
        assert len(pairs) == 1
        return pairs[0][1].payload_json

    return load


@pytest.mark.asyncio
async def test_capture_persists_one_frozen_snapshot_with_all_sections(
    service, capture_request, repo, bundle, portfolio, symbol, market
):
    response = await service.capture(capture_request)
    stored_bundle = await bundle(response.bundle_id)
    pairs = await repo.list_bundle_items_with_snapshots(stored_bundle.id)
    assert len(pairs) == 1
    snapshot = pairs[0][1]
    assert snapshot.snapshot_kind == "llm_input_frozen"
    assert snapshot.canonical_payload_hash == canonical_payload_hash(
        snapshot.payload_json
    )
    assert set(snapshot.payload_json["sections"]) == set(ANALYSIS_SECTION_NAMES)
    portfolio.collect.assert_awaited_once()
    symbol.collect.assert_awaited_once()
    market.collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_stores_provider_error_without_retry(
    service, capture_request, investor_flow, load_frozen_document
):
    investor_flow.collect.side_effect = RuntimeError("provider off")
    response = await service.capture(capture_request)
    document = await load_frozen_document(response.bundle_id)
    section = document["sections"]["investor_flow"]
    assert section["status"] == "unavailable"
    assert section["error"] == "RuntimeError: provider off"
    investor_flow.collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_preserves_all_collector_results_when_one_is_unavailable(
    service, capture_request, investor_flow, load_frozen_document
):
    fresh = _result("investor_flow", symbol="005930")
    unavailable = SnapshotCollectResult(
        snapshot_kind="investor_flow",
        market="kr",
        account_scope="kis_live",
        symbol="000660",
        source_kind="manual",
        payload_json={"foreign_net": None},
        source_timestamps_json={"provider": NOW.isoformat()},
        coverage_json={"covered": False},
        errors_json={"provider": "RuntimeError: provider off"},
        as_of=NOW,
        freshness_status="unavailable",
    )
    investor_flow.collect.return_value = [fresh, unavailable]

    response = await service.capture(capture_request)
    document = await load_frozen_document(response.bundle_id)
    section = document["sections"]["investor_flow"]

    assert section["status"] == "unavailable"
    assert section["error"] == "RuntimeError: provider off"
    assert section["data"] == [
        {
            "payload_json": fresh.payload_json,
            "errors_json": fresh.errors_json,
            "coverage_json": fresh.coverage_json,
            "source_timestamps_json": fresh.source_timestamps_json,
            "freshness_status": fresh.freshness_status,
            "symbol": fresh.symbol,
        },
        {
            "payload_json": unavailable.payload_json,
            "errors_json": unavailable.errors_json,
            "coverage_json": unavailable.coverage_json,
            "source_timestamps_json": unavailable.source_timestamps_json,
            "freshness_status": unavailable.freshness_status,
            "symbol": unavailable.symbol,
        },
    ]
    investor_flow.collect.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_uses_full_analysis_and_separate_decision_history(
    service, capture_request, analysis_fn, decision_history_fn
):
    await service.capture(capture_request)
    analysis_fn.assert_awaited_once_with(
        ["005930"],
        market="kr",
        include_peers=False,
        quick=False,
        include_position=False,
        refresh=False,
    )
    decision_history_fn.assert_awaited_once_with("005930", "kr", "kis_live")


@pytest.mark.asyncio
async def test_second_capture_returns_new_bundle_uuid(service, capture_request):
    first = await service.capture(capture_request)
    second = await service.capture(capture_request)
    assert first.bundle_id != second.bundle_id


@pytest.mark.asyncio
async def test_two_capture_service_instances_with_fixed_clock_create_unique_bundles(
    db_session,
    capture_request,
    portfolio,
    symbol,
    market,
    investor_flow,
    analysis_fn,
    decision_history_fn,
):
    registry = SnapshotCollectorRegistry()
    for collector in (portfolio, symbol, market, investor_flow):
        registry.register(collector)

    first_service = AnalysisBundleCaptureService(
        db_session,
        collectors=registry,
        analysis_fn=analysis_fn,
        decision_history_fn=decision_history_fn,
        clock=lambda: NOW,
    )
    second_service = AnalysisBundleCaptureService(
        db_session,
        collectors=registry,
        analysis_fn=analysis_fn,
        decision_history_fn=decision_history_fn,
        clock=lambda: NOW,
    )

    first = await first_service.capture(capture_request)
    second = await second_service.capture(capture_request)

    assert first.bundle_id != second.bundle_id
    assert first.content_hash == second.content_hash


@pytest.mark.asyncio
async def test_frozen_collector_never_overlaps_shared_session_reads():
    active = 0
    max_active = 0

    async def tracked(result):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return result

    class TrackingCollector:
        def __init__(self, kind: str) -> None:
            self.snapshot_kind = kind

        async def collect(self, request):
            return await tracked([_result(self.snapshot_kind)])

    registry = SnapshotCollectorRegistry()
    for kind in ("portfolio", "symbol", "market", "investor_flow"):
        registry.register(TrackingCollector(kind))

    async def analysis(*args, **kwargs):
        return await tracked({"005930": {"rsi": 52.1}})

    async def decision(*args):
        return await tracked({"recent": []})

    collector = AnalysisInputFrozenCollector(
        registry,
        analysis_fn=analysis,
        decision_history_fn=decision,
        clock=lambda: NOW,
    )
    await collector.collect(
        CollectorRequest(
            market="kr",
            account_scope="kis_live",
            symbols=["005930"],
            policy_snapshot={},
        )
    )

    assert max_active == 1


@pytest.mark.asyncio
async def test_sections_stamp_completion_time_and_preserve_timestamp_provenance():
    ticks = iter(NOW + dt.timedelta(seconds=offset) for offset in range(20))
    registry = SnapshotCollectorRegistry()
    for kind in ("portfolio", "symbol", "market"):
        registry.register(FakeCollector(kind, [_result(kind)]))
    unavailable_flow = _result("investor_flow")
    unavailable_flow.freshness_status = "unavailable"
    unavailable_flow.errors_json = {"provider": "provider off"}
    registry.register(FakeCollector("investor_flow", [unavailable_flow]))

    collector = AnalysisInputFrozenCollector(
        registry,
        analysis_fn=AsyncMock(return_value={"005930": {"rsi": 52.1}}),
        decision_history_fn=AsyncMock(return_value={"recent": []}),
        clock=lambda: next(ticks),
    )
    [result] = await collector.collect(
        CollectorRequest(
            market="kr",
            account_scope="kis_live",
            symbols=["005930"],
            policy_snapshot={},
        )
    )
    sections = result.payload_json["sections"]
    completion_times = [
        dt.datetime.fromisoformat(sections[name]["collected_at"])
        for name in ANALYSIS_SECTION_NAMES
    ]

    assert completion_times == sorted(completion_times)
    assert len(set(completion_times)) == len(ANALYSIS_SECTION_NAMES)
    assert sections["portfolio"]["source"]["source_timestamps_json"] == [
        {"provider": NOW.isoformat()}
    ]
    assert dt.datetime.fromisoformat(sections["investor_flow"]["as_of"]) == NOW
    for name in ("indicators_support_resistance", "decision_history"):
        assert sections[name]["as_of"] == sections[name]["collected_at"]
        assert sections[name]["source"]["as_of_provenance"] == (
            "collection_completion_fallback: provider/domain as_of absent"
        )
