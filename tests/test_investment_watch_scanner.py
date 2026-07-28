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
    CreateInvestmentWatchRequest,
    IngestReportItem,
    IngestReportRequest,
    RecordDecisionRequest,
    WatchConditionClause,
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
from app.services.investment_reports.watch_create import DirectWatchCreateService
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

    # Plan 4 hardening — event row carries delivery status / timestamp /
    # attempt counter so a future operator UI can show what actually
    # reached Hermes.
    delivery_row = await session.execute(
        sa.text(
            "SELECT delivery_status, delivered_at, delivery_attempts, "
            "delivery_reason "
            "FROM review.investment_watch_events "
            "WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert.id},
    )
    delivery_status, delivered_at, delivery_attempts, delivery_reason = (
        delivery_row.one()
    )
    assert delivery_status == "delivered"
    assert delivered_at is not None
    assert delivery_attempts == 1
    assert delivery_reason is None


@pytest.mark.asyncio
async def test_scan_direct_watch_uses_null_source_links(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert, _ = await DirectWatchCreateService(session).create(
        CreateInvestmentWatchRequest(
            created_by="test",
            market="kr",
            symbol="005930",
            intent="trend_recovery_review",
            rationale="direct watch without a report",
            watch_condition=WatchConditionPayload(
                metric="rsi",
                operator="below",
                threshold=Decimal("30"),
            ),
            valid_until=future_datetime(days=30),
        )
    )
    await session.commit()

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    summary = await InvestmentWatchScanner(hermes_client=stub).scan_market("kr")

    assert summary["triggered"] == 1
    assert len(stub.calls) == 1
    payload = stub.calls[0]
    assert payload.alert_uuid == alert.alert_uuid
    assert payload.source_report_uuid is None
    assert payload.source_item_uuid is None
    assert payload.invest_links is not None
    assert payload.invest_links.report_path is None
    assert payload.invest_links.stock_path == "/invest/stocks/kr/005930"


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
async def test_scan_market_hermes_failure_does_not_consume_alert(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 4 hardening — failed Hermes delivery leaves alert active.

    The event row is still persisted (auditable, retryable) but the
    alert.status stays 'active' so the next scan loop will re-attempt
    delivery against the existing event row.
    """
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

    # Event row persisted, alert NOT transitioned.
    await session.commit()
    status_value = await session.scalar(
        sa.text(
            "SELECT status FROM review.investment_watch_alerts WHERE alert_uuid = :uuid"
        ),
        {"uuid": str(alert.alert_uuid)},
    )
    assert status_value == "active"
    delivery_row = await session.execute(
        sa.text(
            "SELECT delivery_status, delivery_reason, delivered_at, delivery_attempts "
            "FROM review.investment_watch_events WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert.id},
    )
    delivery_status, delivery_reason, delivered_at, delivery_attempts = (
        delivery_row.one()
    )
    assert delivery_status == "failed"
    assert delivery_reason == "http_500"
    assert delivered_at is None
    assert delivery_attempts == 1


@pytest.mark.asyncio
async def test_scan_market_hermes_skipped_does_not_consume_alert(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 4 hardening — disabled Hermes (skipped) keeps the alert active.

    Useful for dev/test runs where HERMES_ENABLED=False — the scanner
    still writes audit history of what would have fired, but does not
    silently consume a real watch.
    """
    alert = await _seed_active_kr_alert(session)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient(delivery=HermesDeliveryResult(status="skipped"))
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 1
    assert summary["notified"] == 0
    assert summary["skipped_delivery"] == 1

    await session.commit()
    status_value = await session.scalar(
        sa.text(
            "SELECT status FROM review.investment_watch_alerts WHERE alert_uuid = :uuid"
        ),
        {"uuid": str(alert.alert_uuid)},
    )
    assert status_value == "active"
    delivery_status = await session.scalar(
        sa.text(
            "SELECT delivery_status FROM review.investment_watch_events "
            "WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert.id},
    )
    assert delivery_status == "skipped"


@pytest.mark.asyncio
async def test_re_fire_after_failed_delivery_retries_and_consumes_on_success(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 4 hardening — outbox-shaped retry on the next scan iteration.

    Scan 1: Hermes 500 → event.delivery_status='failed', alert stays active.
    Scan 2: Hermes 200 → existing event row updated to 'delivered',
    delivery_attempts increments to 2, alert finally transitions to
    'triggered'. No duplicate event row is inserted.
    """
    alert = await _seed_active_kr_alert(session)
    # Capture identity columns before crossing async-session boundaries —
    # the scanner uses its own session and the test session's identity
    # map gets stale once the scanner commits.
    alert_id = alert.id
    alert_uuid_str = str(alert.alert_uuid)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    failing = _StubHermesClient(
        delivery=HermesDeliveryResult(
            status="failed", http_status=500, reason="http_500"
        )
    )
    scanner_fail = InvestmentWatchScanner(hermes_client=failing)
    summary_first = await scanner_fail.scan_market("kr")
    assert summary_first["triggered"] == 1
    assert summary_first["failed_delivery"] == 1

    # Scan 2: same alert, same conditions, Hermes now succeeds. The test
    # session is not touched between scans so its identity-map state
    # doesn't interfere with the scanner's own AsyncSessionLocal.
    succeeding = _StubHermesClient(
        delivery=HermesDeliveryResult(status="success", http_status=200)
    )
    scanner_ok = InvestmentWatchScanner(hermes_client=succeeding)
    summary_second = await scanner_ok.scan_market("kr")

    # Idempotency collision → re-attempt against the existing row, not a
    # new insert. summary['triggered'] counts only NEW event rows, so the
    # retry-on-existing case shows up as ``duplicates`` but the delivery
    # itself succeeded and notified increments.
    assert summary_second["duplicates"] == 1
    assert summary_second["notified"] == 1
    assert len(succeeding.calls) == 1
    assert len(failing.calls) == 1  # didn't grow

    await session.commit()
    # Single event row, now delivered + 2 attempts; alert transitioned.
    event_row = await session.execute(
        sa.text(
            "SELECT delivery_status, delivery_attempts, delivered_at "
            "FROM review.investment_watch_events WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert_id},
    )
    delivery_status, delivery_attempts, delivered_at = event_row.one()
    assert delivery_status == "delivered"
    assert delivery_attempts == 2
    assert delivered_at is not None

    event_count = await session.scalar(
        sa.text(
            "SELECT COUNT(*) FROM review.investment_watch_events "
            "WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert_id},
    )
    assert event_count == 1

    final_status = await session.scalar(
        sa.text(
            "SELECT status FROM review.investment_watch_alerts WHERE alert_uuid = :uuid"
        ),
        {"uuid": alert_uuid_str},
    )
    assert final_status == "triggered"


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


@pytest.mark.asyncio
async def test_scan_market_triggers_on_zone_inside(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:

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
                IngestReportItem(
                    client_item_key="watch-zone",
                    item_kind="watch",
                    symbol="005930",
                    intent="buy_review",
                    rationale="zone",
                    watch_condition=WatchConditionPayload(
                        conditions=[
                            WatchConditionClause(
                                metric="price",
                                op="between",
                                low=Decimal("50000"),
                                high=Decimal("55000"),
                            )
                        ]
                    ),
                    valid_until=future_datetime(days=30),
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    item = (await repo.list_items_for_report(report.id))[0]
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(item_uuid=item.item_uuid, decision="approve", actor="op")
    )
    await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="op")
    )
    await session.commit()

    async def _price_inside(**_kwargs) -> float:
        return 52000.0  # inside [50000, 55000] → triggered

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _price_inside)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 1
    assert len(stub.calls) == 1
    payload = stub.calls[0]
    assert payload.operator == "between"
    assert payload.threshold == Decimal("50000")
    assert payload.threshold_high == Decimal("55000")


@pytest.mark.asyncio
async def test_scan_calls_auto_execute_for_auto_execute_mock(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert = await _seed_active_kr_alert(session, action_mode="auto_execute_mock")

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0  # below 30 → triggered

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    captured: list = []

    async def _fake_maybe_auto_execute(db, *, alert, correlation_id, kst_date, **kw):
        outcome_before_execution = await db.scalar(
            sa.text(
                "SELECT outcome FROM review.investment_watch_events "
                "WHERE correlation_id = :correlation_id"
            ),
            {"correlation_id": correlation_id},
        )
        captured.append(
            {
                "symbol": alert.symbol,
                "cid": correlation_id,
                "outcome_before_execution": outcome_before_execution,
            }
        )
        return {"executed": False, "skipped": "stubbed"}

    monkeypatch.setattr(scanner_module, "maybe_auto_execute", _fake_maybe_auto_execute)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    summary = await scanner.scan_market("kr")

    assert summary["triggered"] == 1, summary
    assert len(captured) == 1, captured
    assert captured[0]["symbol"] == "005930"
    assert captured[0]["outcome_before_execution"] == "pending"

    event_outcome = await session.scalar(
        sa.text(
            "SELECT outcome FROM review.investment_watch_events "
            "WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert.id},
    )
    assert event_outcome == "failed"
    assert stub.calls[0].outcome == "failed"


@pytest.mark.asyncio
async def test_scan_records_executed_only_after_positive_execution_evidence(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert = await _seed_active_kr_alert(
        session,
        action_mode="auto_execute_mock",
        client_item_key="watch-executed-evidence",
    )

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    outcomes_seen_by_executor: list[str | None] = []

    async def _fake_maybe_auto_execute(db, *, correlation_id, **_kwargs):
        outcomes_seen_by_executor.append(
            await db.scalar(
                sa.text(
                    "SELECT outcome FROM review.investment_watch_events "
                    "WHERE correlation_id = :correlation_id"
                ),
                {"correlation_id": correlation_id},
            )
        )
        return {"executed": True, "correlation_id": correlation_id}

    monkeypatch.setattr(scanner_module, "maybe_auto_execute", _fake_maybe_auto_execute)

    stub = _StubHermesClient()
    summary = await InvestmentWatchScanner(hermes_client=stub).scan_market("kr")

    assert summary["triggered"] == 1
    assert outcomes_seen_by_executor == ["pending"]
    event_outcome = await session.scalar(
        sa.text(
            "SELECT outcome FROM review.investment_watch_events "
            "WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert.id},
    )
    assert event_outcome == "executed"
    assert stub.calls[0].outcome == "executed"


# --- ROB-500 Tests ---


def _recommendation_fixture() -> dict:
    return {
        "watch_reason": "r",
        "data_state": "ok",
        "reference_price": "110",
        "entry_review_below_price": "100",
        "suggested_limit_price_range": {"low": "95", "high": "100"},
        "max_chase_price": "102",
        "invalidation": {"kind": "price_below", "price": "80"},
        "review_cadence": "daily",
        "source_evidence": {"lookback_days": 20},
        "policy_version": "v1",
        "computed_at": "2026-06-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_trigger_payload_carries_links_guidance_and_price_guidance(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-500 — 발화 페이로드에 invest_links + 액션 가이드 + 가격 가이드."""
    alert = await _seed_active_kr_alert(session)
    alert.max_action = {
        "side": "buy",
        "quantity": "1",
        "amount_krw": "980000",
        "limit_price": "975000",
        "ladder_level": "1",
    }
    alert.trigger_checklist = ["quote spread ok", "thesis still valid"]
    repo = InvestmentReportsRepository(session)
    item = await repo.get_item_by_uuid(alert.source_item_uuid)
    assert item is not None
    await repo.update_item_watch_recommendation(item.id, _recommendation_fixture())
    await session.commit()

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0  # rsi below 30 → triggered

    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    await scanner.scan_market("kr")

    assert len(stub.calls) == 1
    payload = stub.calls[0]

    assert payload.invest_links is not None
    assert payload.invest_links.report_path == (
        f"/invest/reports/{alert.source_report_uuid}"
    )
    assert payload.invest_links.stock_path == "/invest/stocks/kr/005930"
    assert payload.invest_links.event_anchor == (
        f"/invest/reports/{alert.source_report_uuid}#watch-event-{payload.event_uuid}"
    )
    assert payload.invest_links.alert_anchor == (
        f"/invest/reports/{alert.source_report_uuid}#watch-alert-{alert.alert_uuid}"
    )

    assert payload.operator_action_guidance is not None
    assert payload.operator_action_guidance.requires_operator_review is False
    assert payload.operator_action_guidance.order_behavior == "none"

    assert payload.price_guidance is not None
    assert payload.price_guidance.entry_review_below_price == Decimal("100")
    assert payload.price_guidance.suggested_limit_price_range.low == Decimal("95")
    assert payload.price_guidance.suggested_limit_price_range.high == Decimal("100")
    assert payload.price_guidance.max_chase_price == Decimal("102")
    assert payload.price_guidance.invalidation.kind == "price_below"

    assert payload.planned_action is not None
    assert payload.planned_action.side == "buy"
    assert payload.planned_action.qty == Decimal("1")
    assert payload.planned_action.amount_krw == Decimal("980000")
    assert payload.planned_action.limit_price_hint == Decimal("975000")
    assert payload.planned_action.ladder_level == "1"
    assert payload.trigger_checklist == ["quote spread ok", "thesis still valid"]


@pytest.mark.asyncio
async def test_trigger_payload_price_guidance_none_without_recommendation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-500 — watch_recommendation 없는 watch는 가이드 추론 금지(None)."""
    await _seed_active_kr_alert(session)
    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    scanner = InvestmentWatchScanner(hermes_client=stub)
    await scanner.scan_market("kr")

    assert len(stub.calls) == 1
    assert stub.calls[0].price_guidance is None
    assert stub.calls[0].invest_links is not None  # 링크는 항상 채움


# ---------------------------------------------------------------------------
# ROB-1110 — per-alert isolation.
#
# The scan loop writes to the DB while still iterating. Before the fix the
# alerts were live ORM rows owned by that same session, so the first
# ``rollback()`` (the routine same-day idempotency-collision retry path)
# expired the identity map and every later ``alert.conditions`` read raised
# ``MissingGreenlet`` — taking out the whole remainder of the cycle.
# Live ``notify_only`` alerts share this exact loop, so these cover the live
# notify lane too.
# ---------------------------------------------------------------------------


_ROB1110_SYMBOLS = ("005930", "000660", "035420", "051910")


async def _seed_kr_alert_fleet(
    session: AsyncSession,
    *,
    action_mode: str = "notify_only",
    symbols: tuple[str, ...] = _ROB1110_SYMBOLS,
) -> list[Any]:
    """Seed several independently-activated active KR alerts."""
    alerts = []
    for index, symbol in enumerate(symbols):
        alerts.append(
            await _seed_active_kr_alert(
                session,
                action_mode=action_mode,
                symbol=symbol,
                # A distinct report per alert: report ingestion is idempotent
                # per (report_type, kst_date), and the helper activates
                # ``items[0]``, so same-day seeds would all resolve to the
                # very same watch item.
                kst_date=f"2026-05-{18 + index:02d}",
                client_item_key=f"rob1110-watch-{index}",
            )
        )
    return alerts


@pytest.mark.asyncio
async def test_multiple_triggers_in_one_cycle_all_emit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-1110 — a cycle with many simultaneous triggers emits all of them."""
    await _seed_kr_alert_fleet(session)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0  # below 30 → every alert triggers

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    stub = _StubHermesClient()
    summary = await InvestmentWatchScanner(hermes_client=stub).scan_market("kr")

    assert summary["alerts_seen"] == len(_ROB1110_SYMBOLS)
    assert summary["triggered"] == len(_ROB1110_SYMBOLS)
    assert summary["notified"] == len(_ROB1110_SYMBOLS)
    assert summary["failed_lookups"] == 0
    assert summary["failed_alerts"] == 0
    assert summary["not_evaluated"] == 0
    assert {call.symbol for call in stub.calls} == set(_ROB1110_SYMBOLS)


@pytest.mark.asyncio
async def test_rollback_on_first_alert_does_not_kill_the_rest(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-1110 regression — the ``MissingGreenlet`` cascade.

    Scan 1 emits an event row per alert but Hermes is disabled, so every
    alert stays ``active``. Scan 2 therefore hits the same-day
    ``IntegrityError`` → ``db.rollback()`` path on the *first* alert, which
    expires the ORM identity map. Before the fix the second alert's
    ``alert.conditions`` read became a lazy refresh and raised
    ``MissingGreenlet``, so scan 2 reported ``duplicates=1`` and silently
    booked the other three as ``failed_lookups``. All of them must be
    evaluated.
    """
    await _seed_kr_alert_fleet(session)

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    # Scan 1 — Hermes disabled → events written, alerts remain 'active'.
    skipped = _StubHermesClient(
        delivery=HermesDeliveryResult(
            status="skipped", http_status=None, reason="hermes_disabled"
        )
    )
    first = await InvestmentWatchScanner(hermes_client=skipped).scan_market("kr")
    assert first["triggered"] == len(_ROB1110_SYMBOLS)
    assert first["skipped_delivery"] == len(_ROB1110_SYMBOLS)

    # Scan 2 — every alert now collides on idempotency_key and rolls back.
    retry = _StubHermesClient()
    second = await InvestmentWatchScanner(hermes_client=retry).scan_market("kr")

    assert second["alerts_seen"] == len(_ROB1110_SYMBOLS)
    # Rolled-back insert → retry against the existing row, for *every* alert.
    assert second["duplicates"] == len(_ROB1110_SYMBOLS)
    assert second["notified"] == len(_ROB1110_SYMBOLS)
    assert second["failed_lookups"] == 0, second
    assert second["failed_alerts"] == 0, second
    assert second["not_evaluated"] == 0, second
    assert {call.symbol for call in retry.calls} == set(_ROB1110_SYMBOLS)


@pytest.mark.asyncio
async def test_failing_alert_is_isolated_recorded_and_cycle_completes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-1110 — one alert blowing up must not cost the others, and the
    failure must be visible in the summary rather than swallowed."""
    await _seed_kr_alert_fleet(session)
    poisoned = _ROB1110_SYMBOLS[1]

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    @dataclass
    class _ExplodingHermesClient(_StubHermesClient):
        async def send_review_trigger(
            self, payload: ReviewTriggerPayload
        ) -> HermesDeliveryResult:
            if payload.symbol == poisoned:
                raise RuntimeError("hermes transport exploded")
            return await super().send_review_trigger(payload)

    stub = _ExplodingHermesClient()
    summary = await InvestmentWatchScanner(hermes_client=stub).scan_market("kr")

    assert summary["alerts_seen"] == len(_ROB1110_SYMBOLS)
    assert summary["failed_alerts"] == 1
    assert summary["not_evaluated"] == 0
    # The other three completed end-to-end.
    assert summary["notified"] == len(_ROB1110_SYMBOLS) - 1
    assert {call.symbol for call in stub.calls} == set(_ROB1110_SYMBOLS) - {poisoned}

    # The failure is recorded, not swallowed.
    failures = [d for d in summary["details"] if d.get("status") == "alert_failed"]
    assert len(failures) == 1
    assert failures[0]["symbol"] == poisoned
    assert "hermes transport exploded" in failures[0]["error"]

    # The failed alert keeps its retryable state: event row still pending and
    # the alert is still active, so the next cycle picks it back up.
    await session.commit()
    row = await session.execute(
        sa.text(
            "SELECT e.delivery_status, a.status "
            "FROM review.investment_watch_events e "
            "JOIN review.investment_watch_alerts a ON a.id = e.alert_id "
            "WHERE e.symbol = :symbol"
        ),
        {"symbol": poisoned},
    )
    delivery_status, alert_status = row.one()
    assert delivery_status == "pending"
    assert alert_status == "active"


@pytest.mark.asyncio
async def test_db_error_in_auto_execute_does_not_poison_later_alerts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROB-1110 / ROB-1109 shape — an aborted transaction must not cascade.

    ``maybe_auto_execute`` failing on a missing table (the original ROB-1109
    observation) leaves the transaction in an aborted state. Without the
    rollback the alert's own delivery bookkeeping — and every commit after
    it — fails too. The alert that broke still has to record its delivery,
    and the alerts behind it must be untouched.
    """
    await _seed_kr_alert_fleet(session, action_mode="auto_execute_mock")
    poisoned = _ROB1110_SYMBOLS[0]

    async def _fake_current_value(**_kwargs) -> float:
        return 25.0

    async def _exploding_auto_execute(db, *, alert, **_kwargs):
        if alert.symbol == poisoned:
            # A real aborted-transaction failure, not a bare raise.
            await db.execute(sa.text("SELECT * FROM review.__rob1110_missing__"))
        return {"executed": False, "skipped": "test"}

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)
    monkeypatch.setattr(scanner_module, "maybe_auto_execute", _exploding_auto_execute)

    stub = _StubHermesClient()
    summary = await InvestmentWatchScanner(hermes_client=stub).scan_market("kr")

    assert summary["alerts_seen"] == len(_ROB1110_SYMBOLS)
    assert summary["triggered"] == len(_ROB1110_SYMBOLS)
    # Including the alert whose auto-execute blew up: the event row was
    # already committed, so its delivery is still booked.
    assert summary["notified"] == len(_ROB1110_SYMBOLS)
    assert summary["failed_alerts"] == 0, summary
    assert summary["not_evaluated"] == 0, summary
    assert {call.symbol for call in stub.calls} == set(_ROB1110_SYMBOLS)

    await session.commit()
    delivered = await session.scalar(
        sa.text(
            "SELECT COUNT(*) FROM review.investment_watch_events "
            "WHERE symbol = ANY(:symbols) AND delivery_status = 'delivered'"
        ),
        {"symbols": list(_ROB1110_SYMBOLS)},
    )
    assert delivered == len(_ROB1110_SYMBOLS)
