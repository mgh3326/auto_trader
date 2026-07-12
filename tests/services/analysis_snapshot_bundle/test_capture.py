from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.schemas.analysis_snapshot_bundle import (
    ANALYSIS_SECTION_NAMES,
    AnalysisBundleCreateRequest,
)
from app.services.action_report.common.canonicalize import canonical_payload_hash
from app.services.analysis_snapshot_bundle import AnalysisBundleCaptureService
from app.services.investment_snapshots.collectors import (
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
    service, capture_request, repo, bundle
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
