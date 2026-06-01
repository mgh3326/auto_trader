"""ROB-337 Slice 2 — watch validity review service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_reports import (
    ActivateWatchRequest,
    IngestReportRequest,
    RecordDecisionRequest,
)
from app.services.hermes_client import HermesDeliveryResult, ReviewTriggerPayload
from app.services.investment_reports import watch_validity_review as review_module
from app.services.investment_reports.decisions import InvestmentReportDecisionService
from app.services.investment_reports.ingestion import InvestmentReportIngestionService
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_activation import WatchActivationService
from app.services.investment_reports.watch_validity_review import (
    WatchValidityReviewService,
)
from tests._investment_reports_helpers import future_datetime


@dataclass
class _StubHermes:
    calls: list[ReviewTriggerPayload] = field(default_factory=list)
    delivery: HermesDeliveryResult = field(
        default_factory=lambda: HermesDeliveryResult(status="success", http_status=200)
    )

    async def send_review_trigger(
        self, payload: ReviewTriggerPayload
    ) -> HermesDeliveryResult:
        self.calls.append(payload)
        return self.delivery

    async def close(self) -> None:
        pass


async def _seed_active_alert(session: AsyncSession, *, recommendation: dict) -> Any:
    """Ingest -> approve -> activate one KR watch, then set its
    watch_recommendation. Returns the activated alert."""
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            market_session="regular",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="test",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                {
                    "client_item_key": "watch-1",
                    "item_kind": "watch",
                    "symbol": "005930",
                    "intent": "trend_recovery_review",
                    "rationale": "r",
                    "watch_condition": {
                        "metric": "price",
                        "operator": "below",
                        "threshold": 100,
                    },
                    "valid_until": future_datetime().isoformat(),
                }
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    item = items[0]
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(item_uuid=item.item_uuid, decision="approve", actor="op")
    )
    alert = await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="op")
    )
    await repo.update_item_watch_recommendation(item.id, recommendation)
    await session.commit()
    return alert


def _rec(entry: str = "100", inval: str = "80") -> dict:
    return {
        "watch_reason": "r",
        "data_state": "ok",
        "reference_price": "110",
        "entry_review_below_price": entry,
        "suggested_limit_price_range": {"low": entry, "high": entry},
        "max_chase_price": entry,
        "invalidation": {"kind": "price_below", "price": inval},
        "review_cadence": "daily",
        "source_evidence": {"lookback_days": 20},
        "policy_version": "v1",
        "computed_at": "2026-06-01T00:00:00+00:00",
    }


@pytest.fixture
def _stub_md(monkeypatch):
    async def fake_current_value(**_kwargs):
        return 90.0  # <= entry 100 -> review_now, > invalidation 80

    async def fake_ohlcv(*_a, **_k):
        return []  # recompute -> data_gap; stored present so classification uses stored

    monkeypatch.setattr(review_module, "get_current_value", fake_current_value)
    monkeypatch.setattr(review_module.market_data_service, "get_ohlcv", fake_ohlcv)


@pytest.mark.asyncio
async def test_dry_run_no_writes_no_notify(session: AsyncSession, _stub_md) -> None:
    alert = await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()

    @asynccontextmanager
    async def fake_factory():
        yield session

    svc = WatchValidityReviewService(hermes_client=hermes, session_factory=fake_factory)
    summary = await svc.review_market("kr", dry_run=True)
    assert summary["verdict_counts"].get("review_now") == 1
    assert hermes.calls == []
    # alert_metadata unchanged (no last_review)
    repo = InvestmentReportsRepository(session)
    reloaded = await repo.get_alert_by_idempotency_key(alert.idempotency_key)
    assert "last_review" not in (reloaded.alert_metadata or {})


@pytest.mark.asyncio
async def test_run_notifies_actionable_and_records_last_review(
    session: AsyncSession, _stub_md
) -> None:
    alert = await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()

    @asynccontextmanager
    async def fake_factory():
        yield session

    svc = WatchValidityReviewService(hermes_client=hermes, session_factory=fake_factory)
    summary = await svc.review_market("kr", dry_run=False)
    assert summary["notified"] == 1
    assert len(hermes.calls) == 1
    assert hermes.calls[0].scanner_snapshot["validity_verdict"] == "review_now"
    assert hermes.calls[0].outcome == "review_required"
    repo = InvestmentReportsRepository(session)
    reloaded = await repo.get_alert_by_idempotency_key(alert.idempotency_key)
    assert reloaded.alert_metadata["last_review"]["verdict"] == "review_now"
    assert reloaded.status == "active"  # no-mutation: status untouched


@pytest.mark.asyncio
async def test_throttle_suppresses_same_verdict_same_day(
    session: AsyncSession, _stub_md
) -> None:
    await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()

    @asynccontextmanager
    async def fake_factory():
        yield session

    svc = WatchValidityReviewService(hermes_client=hermes, session_factory=fake_factory)
    await svc.review_market("kr", dry_run=False)
    await svc.review_market("kr", dry_run=False)
    assert len(hermes.calls) == 1  # second run is material-unchanged -> no re-notify


@pytest.mark.asyncio
async def test_keep_is_not_notified(session: AsyncSession, monkeypatch) -> None:
    async def fake_cv(**_k):
        return 200.0  # well above entry -> keep

    async def fake_ohlcv(*_a, **_k):
        return []

    monkeypatch.setattr(review_module, "get_current_value", fake_cv)
    monkeypatch.setattr(review_module.market_data_service, "get_ohlcv", fake_ohlcv)
    await _seed_active_alert(session, recommendation=_rec())
    hermes = _StubHermes()

    @asynccontextmanager
    async def fake_factory():
        yield session

    svc = WatchValidityReviewService(hermes_client=hermes, session_factory=fake_factory)
    summary = await svc.review_market("kr", dry_run=False)
    assert summary["verdict_counts"].get("keep") == 1
    assert hermes.calls == []
