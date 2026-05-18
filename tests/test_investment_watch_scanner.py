"""ROB-265 Plan 4 — InvestmentWatchScanner end-to-end tests.

Seeds an active ``investment_watch_alert`` via the Plan 2 services,
monkey-patches the market-data layer to control the trigger condition,
and stubs Hermes delivery to capture payloads. Asserts both DB state
(event row + alert status transition) and Hermes calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs import investment_watch_scanner as scanner_module
from app.jobs.investment_watch_scanner import InvestmentWatchScanner
from app.schemas.investment_reports import (
    ActivateWatchRequest,
    IngestReportItem,
    IngestReportRequest,
    RecordDecisionRequest,
    WatchConditionPayload,
)
from app.services.hermes_client import HermesDeliveryResult, ReviewTriggerPayload
from app.services.investment_reports.decisions import (
    InvestmentReportDecisionService,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_activation import WatchActivationService
from tests._investment_reports_helpers import future_datetime


@dataclass
class _StubHermesClient:
    """Records every ``send_review_trigger`` call. Configurable delivery."""

    calls: list[ReviewTriggerPayload] = field(default_factory=list)
    delivery: HermesDeliveryResult = field(
        default_factory=lambda: HermesDeliveryResult(status="success", http_status=200)
    )
    closed: bool = False

    async def send_review_trigger(
        self, payload: ReviewTriggerPayload
    ) -> HermesDeliveryResult:
        self.calls.append(payload)
        return self.delivery

    async def close(self) -> None:
        self.closed = True


async def _seed_active_kr_alert(
    session: AsyncSession,
    *,
    action_mode: str = "notify_only",
    metric: str = "rsi",
    operator: str = "below",
    threshold: Decimal = Decimal("30"),
    symbol: str = "005930",
    market: str = "kr",
    kst_date: str = "2026-05-18",
    client_item_key: str = "watch-1",
) -> Any:
    """Ingest report → approve watch item → activate. Returns the alert row."""
    ingest = InvestmentReportIngestionService(session)
    market_session = "regular" if market == "kr" else None
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market=market,
            market_session=market_session,
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="test",
            title="t",
            summary="s",
            kst_date=kst_date,
            items=[
                IngestReportItem(
                    client_item_key=client_item_key,
                    item_kind="watch",
                    symbol=symbol,
                    intent="trend_recovery_review",
                    rationale="r",
                    watch_condition=WatchConditionPayload(
                        metric=metric,
                        operator=operator,
                        threshold=threshold,
                        action_mode=action_mode,
                    ),
                    valid_until=future_datetime(days=30),
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    watch_item = items[0]
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=watch_item.item_uuid, decision="approve", actor="op"
        )
    )
    alert = await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=watch_item.item_uuid, actor="op")
    )
    await session.commit()
    return alert


@pytest.mark.asyncio
async def test_scan_market_no_alerts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")
    assert summary["alerts_seen"] == 0
    assert summary["triggered"] == 0
    assert stub.calls == []


@pytest.mark.asyncio
async def test_scan_market_not_triggered_when_threshold_not_crossed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RSI = 50, threshold below 30 → not triggered, no event, no Hermes call."""
    await _seed_active_kr_alert(session)

    async def _fake_current_value(**_kwargs) -> float:
        return 50.0  # operator='below', threshold=30 → 50 is NOT below 30

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["alerts_seen"] == 1
    assert summary["triggered"] == 0
    assert summary["notified"] == 0
    assert stub.calls == []


@pytest.mark.asyncio
async def test_scan_market_triggered_notify_only_emits_event_and_hermes_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert = await _seed_active_kr_alert(session, action_mode="notify_only")
    alert_uuid = alert.alert_uuid

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0  # below 30 → triggered

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 1
    assert summary["notified"] == 1
    assert summary["duplicates"] == 0

    # Event was persisted with the full immutable snapshot.
    assert len(stub.calls) == 1
    payload = stub.calls[0]
    assert payload.alert_uuid == alert_uuid
    assert payload.market == "kr"
    assert payload.target_kind == "asset"
    assert payload.metric == "rsi"
    assert payload.operator == "below"
    assert payload.threshold == Decimal("30")
    assert payload.action_mode == "notify_only"
    assert payload.outcome == "notified"
    assert payload.current_value == Decimal("25.0")
    assert payload.correlation_id  # non-empty hex
    assert payload.scanner_snapshot["metric"] == "rsi"

    # Alert was transitioned to 'triggered'. The scanner used its own
    # session — use raw SQL on a fresh transaction to bypass the test
    # session's identity-map cache of the pre-trigger row.
    await session.commit()
    status_value = await session.scalar(
        sa.text(
            "SELECT status FROM review.investment_watch_alerts WHERE alert_uuid = :uuid"
        ),
        {"uuid": str(alert.alert_uuid)},
    )
    assert status_value == "triggered"


@pytest.mark.asyncio
async def test_scan_market_approval_required_outcome(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_active_kr_alert(session, action_mode="approval_required")

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    await scanner.scan_market("kr")

    assert len(stub.calls) == 1
    assert stub.calls[0].outcome == "review_required"


@pytest.mark.asyncio
async def test_scan_market_preview_only_outcome(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_active_kr_alert(session, action_mode="preview_only")

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    await scanner.scan_market("kr")

    assert len(stub.calls) == 1
    assert stub.calls[0].outcome == "preview_attached"


@pytest.mark.asyncio
async def test_scan_market_skips_closed_market_except_fx(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asset alert on a closed market is skipped; event count stays at 0."""
    await _seed_active_kr_alert(session)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: False)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 0
    assert summary["skipped_closed"] == 1
    assert stub.calls == []


@pytest.mark.asyncio
async def test_scan_market_hermes_failure_keeps_event_row(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery failure does not roll back the event row."""
    alert = await _seed_active_kr_alert(session)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient(
        delivery=HermesDeliveryResult(
            status="failed", http_status=500, reason="http_500"
        )
    )
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 1
    assert summary["notified"] == 0
    assert summary["failed_delivery"] == 1

    # Event row still persisted; alert still transitioned. Use raw SQL
    # on a fresh transaction to bypass the test session's identity map.
    await session.commit()
    status_value = await session.scalar(
        sa.text(
            "SELECT status FROM review.investment_watch_alerts WHERE alert_uuid = :uuid"
        ),
        {"uuid": str(alert.alert_uuid)},
    )
    assert status_value == "triggered"
    event_count = await session.scalar(
        sa.text(
            "SELECT COUNT(*) FROM review.investment_watch_events WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert.id},
    )
    assert event_count == 1


@pytest.mark.asyncio
async def test_scan_market_re_fire_same_day_is_idempotent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If somehow the alert is re-listed (e.g. a manual revert to active)
    the same-day threshold cross does not duplicate the event row.
    """
    alert = await _seed_active_kr_alert(session)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary_first = await scanner.scan_market("kr")
    assert summary_first["triggered"] == 1

    # Manually revert the alert to 'active' so a second scan can re-list it.
    repo = InvestmentReportsRepository(session)
    await repo.update_alert_status(alert.id, "active")
    await session.commit()

    summary_second = await scanner.scan_market("kr")
    # Idempotency_key collision → event insert is rolled back.
    assert summary_second["triggered"] == 0
    assert summary_second["duplicates"] == 1
    # Only ONE Hermes call across both scans.
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_close_closes_hermes_client(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    await scanner.close()
    assert stub.closed is True
