"""ROB-265 Plan 5 — NXT advisory-only pilot end-to-end.

Exercises the full ``investment_*`` stack in the locked NXT pilot
shape: ``market='kr'``, ``market_session='nxt'``, ``account_scope='kis_live'``,
``execution_mode='advisory_only'``.

Flow:
1. Ingest a report with one action + one watch item.
2. Approve both items.
3. Activate the watch.
4. Stub Hermes to deliver successfully. Stub market data so the watch
   threshold is crossed.
5. Run ``InvestmentWatchScanner.scan_market("kr")``.
6. Assert: event row persisted with ``delivery_status='delivered'``,
   alert transitioned to ``triggered``, Hermes received exactly one
   call carrying the full immutable trigger snapshot and the locked
   pilot scope.

No broker mutation, no live order submission, no agent-gateway imports.
The advisory-only invariant is enforced at the DB CHECK level (Plan
1) and at the Pydantic validator level (Plan 2) — this test confirms
the full path through Plans 3 and 4 honours it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

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
class _NxtPilotHermesStub:
    """Records every payload sent during the NXT pilot scan."""

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
        return None


@pytest.mark.asyncio
async def test_nxt_advisory_only_end_to_end(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Ingest the NXT pilot report. Both schema (Plan 2) and DB CHECK
    #    (Plan 1) reject this combo unless execution_mode='advisory_only'.
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_nxt_pilot",
            market="kr",
            market_session="nxt",
            account_scope="kis_live",
            execution_mode="advisory_only",
            created_by_profile="pilot-operator",
            title="NXT 종목 자문 파일럿",
            summary="advisory_only — 자동 주문 없음, 와치는 재검토 트리거만",
            risk_summary="NXT 세션은 자문 전용 — 실주문 차단",
            kst_date="2026-05-19",
            generator_version="pilot-v1",
            items=[
                IngestReportItem(
                    client_item_key="nxt-action-1",
                    item_kind="action",
                    symbol="005930",
                    side="buy",
                    intent="buy_review",
                    rationale="자문용 매수 후보 — 실주문 차단",
                ),
                IngestReportItem(
                    client_item_key="nxt-watch-1",
                    item_kind="watch",
                    symbol="005930",
                    intent="trend_recovery_review",
                    rationale="RSI 30 하회 시 재검토",
                    watch_condition=WatchConditionPayload(
                        metric="rsi",
                        operator="below",
                        threshold=Decimal("30"),
                        action_mode="approval_required",
                    ),
                    valid_until=future_datetime(days=14),
                ),
            ],
        )
    )
    await session.commit()

    # Confirm the report persisted with the pilot scope intact.
    assert report.market == "kr"
    assert report.market_session == "nxt"
    assert report.account_scope == "kis_live"
    assert report.execution_mode == "advisory_only"

    # 2. Approve both items.
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    item_by_kind = {item.item_kind: item for item in items}
    action_item = item_by_kind["action"]
    watch_item = item_by_kind["watch"]

    decisions = InvestmentReportDecisionService(session)
    await decisions.record(
        RecordDecisionRequest(
            item_uuid=action_item.item_uuid,
            decision="approve",
            actor="pilot-operator",
        )
    )
    await decisions.record(
        RecordDecisionRequest(
            item_uuid=watch_item.item_uuid,
            decision="approve",
            actor="pilot-operator",
        )
    )

    # 3. Activate the watch — copies the item into an immutable alert
    #    snapshot. Item transitions to 'activated'.
    alert = await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=watch_item.item_uuid, actor="pilot-operator")
    )
    await session.commit()
    alert_id = alert.id
    alert_uuid = alert.alert_uuid

    # 4. Stub the market-data layer to make the watch trigger; stub Hermes
    #    to deliver successfully.
    async def _fake_current_value(**_kwargs) -> float:
        return 25.0  # below 30 → trigger

    monkeypatch.setattr(scanner_module, "is_market_open", lambda _market: True)
    monkeypatch.setattr(scanner_module, "get_current_value", _fake_current_value)

    hermes_stub = _NxtPilotHermesStub()
    scanner = InvestmentWatchScanner(hermes_client=hermes_stub)

    # 5. Run the scan.
    summary = await scanner.scan_market("kr")
    assert summary["triggered"] == 1
    assert summary["notified"] == 1
    assert summary["failed_delivery"] == 0
    assert summary["skipped_delivery"] == 0

    # 6. Verify the Hermes payload preserves the NXT pilot scope and
    #    immutable trigger identity. action_mode='approval_required'
    #    maps to outcome='review_required' per the Plan 4 outcome map.
    assert len(hermes_stub.calls) == 1
    payload = hermes_stub.calls[0]
    assert payload.alert_uuid == alert_uuid
    assert payload.market == "kr"
    assert payload.target_kind == "asset"
    assert payload.symbol == "005930"
    assert payload.metric == "rsi"
    assert payload.operator == "below"
    assert payload.threshold == Decimal("30")
    assert payload.action_mode == "approval_required"
    assert payload.outcome == "review_required"
    assert payload.current_value == Decimal("25.0")
    assert payload.source_report_uuid == report.report_uuid
    assert payload.source_item_uuid == watch_item.item_uuid

    # 7. Verify DB state via a fresh transaction (test session's
    #    identity-map is stale once the scanner committed in its own
    #    session). Event row carries the delivery-tracking columns
    #    (Plan 4 hardening) and the alert transitioned only because
    #    delivery actually succeeded.
    await session.commit()
    delivery_row = await session.execute(
        sa.text(
            "SELECT delivery_status, delivery_attempts, outcome "
            "FROM review.investment_watch_events WHERE alert_id = :alert_id"
        ),
        {"alert_id": alert_id},
    )
    delivery_status, delivery_attempts, outcome = delivery_row.one()
    assert delivery_status == "delivered"
    assert delivery_attempts == 1
    assert outcome == "review_required"

    alert_status = await session.scalar(
        sa.text(
            "SELECT status FROM review.investment_watch_alerts WHERE alert_uuid = :uuid"
        ),
        {"uuid": str(alert_uuid)},
    )
    assert alert_status == "triggered"


@pytest.mark.asyncio
async def test_nxt_with_non_advisory_execution_mode_rejected_at_schema(
    session: AsyncSession,
) -> None:
    """Defense-in-depth check: even before hitting the DB CHECK from Plan 1,
    the Pydantic validator from Plan 2 rejects ``nxt`` + non-advisory.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="advisory_only"):
        IngestReportRequest(
            report_type="kr_nxt_pilot",
            market="kr",
            market_session="nxt",
            account_scope="kis_live",
            execution_mode="mock_preview",  # blocked
            created_by_profile="pilot-operator",
            title="x",
            summary="x",
            kst_date="2026-05-19",
        )
