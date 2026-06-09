"""ROB-265 Plan 2 — Decisions service tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItem
from app.schemas.investment_reports import (
    IngestReportItem,
    IngestReportRequest,
    RecordDecisionRequest,
)
from app.services.investment_reports.decisions import (
    InvestmentReportDecisionService,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository


async def _seed_report_with_one_action_item(
    session: AsyncSession,
    *,
    kst_date: str = "2026-05-18",
    client_item_key: str = "action-1",
    symbol: str = "005930",
) -> InvestmentReportItem:
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
            kst_date=kst_date,
            items=[
                IngestReportItem(
                    client_item_key=client_item_key,
                    item_kind="action",
                    symbol=symbol,
                    side="buy",
                    intent="buy_review",
                    rationale="r",
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    return items[0]


@pytest.mark.asyncio
async def test_approve_records_decision_and_transitions_item(
    session: AsyncSession,
) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    decision = await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    assert decision.decision == "approve"
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed is not None
    assert refreshed.status == "approved"


@pytest.mark.asyncio
async def test_deny_transitions_item_to_denied(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="deny", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "denied"


@pytest.mark.asyncio
async def test_defer_transitions_item_to_deferred(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="defer", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "deferred"


@pytest.mark.asyncio
async def test_skip_leaves_item_status_unchanged(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="skip", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "proposed"


@pytest.mark.asyncio
async def test_partial_approve_transitions_to_approved_with_snapshot(
    session: AsyncSession,
) -> None:
    item = await _seed_report_with_one_action_item(session)
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid,
            decision="partial_approve",
            actor="operator-test",
            approved_payload_snapshot={"max_notional_krw": 100000},
        )
    )
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "approved"
    decisions = await repo.list_decisions_for_item(item.id)
    assert decisions[0].approved_payload_snapshot == {"max_notional_krw": 100000}


@pytest.mark.asyncio
async def test_record_is_idempotent_per_default_key(session: AsyncSession) -> None:
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    first = await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    second = await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    assert first.id == second.id
    repo = InvestmentReportsRepository(session)
    decisions = await repo.list_decisions_for_item(item.id)
    assert len(decisions) == 1


@pytest.mark.asyncio
async def test_multiple_decisions_per_item_persist(session: AsyncSession) -> None:
    """defer → approve produces two rows; final status reflects latest verb."""
    item = await _seed_report_with_one_action_item(session)
    service = InvestmentReportDecisionService(session)
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="defer", actor="operator-test"
        )
    )
    await service.record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="approve", actor="operator-test"
        )
    )
    repo = InvestmentReportsRepository(session)
    decisions = await repo.list_decisions_for_item(item.id)
    assert {d.decision for d in decisions} == {"defer", "approve"}
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "approved"


@pytest.mark.asyncio
async def test_unknown_item_uuid_raises(session: AsyncSession) -> None:
    service = InvestmentReportDecisionService(session)
    with pytest.raises(ValueError):
        await service.record(
            RecordDecisionRequest(
                item_uuid=uuid.uuid4(),
                decision="approve",
                actor="operator-test",
            )
        )


@pytest.mark.asyncio
async def test_caller_supplied_idempotency_key_cross_item_collision_rejected(
    session: AsyncSession,
) -> None:
    """Plan 2 hardening #1: a caller-supplied idempotency_key that collides
    with an existing decision on a DIFFERENT item must raise, not silently
    return the wrong row.
    """
    item_a = await _seed_report_with_one_action_item(
        session, client_item_key="a-1", symbol="005930"
    )
    item_b = await _seed_report_with_one_action_item(
        session, kst_date="2026-05-19", client_item_key="b-1", symbol="000660"
    )
    shared_key = f"shared-key-{uuid.uuid4()}"
    service = InvestmentReportDecisionService(session)

    await service.record(
        RecordDecisionRequest(
            item_uuid=item_a.item_uuid,
            decision="approve",
            actor="operator",
            idempotency_key=shared_key,
        )
    )
    with pytest.raises(ValueError, match="already used for a different item"):
        await service.record(
            RecordDecisionRequest(
                item_uuid=item_b.item_uuid,
                decision="approve",
                actor="operator",
                idempotency_key=shared_key,
            )
        )


# ---------------------------------------------------------------------------
# ROB-455 PR2 — order-lifecycle verbs: cancel + reprice
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_records_verb_and_projects_item_to_denied(
    session: AsyncSession,
) -> None:
    item = await _seed_report_with_one_action_item(session)
    decision = await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="cancel", actor="operator-test"
        )
    )
    # The verb is first-class in the decision audit row...
    assert decision.decision == "cancel"
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    # ...while item.status reuses the 'denied' projection (no new status value).
    assert refreshed is not None
    assert refreshed.status == "denied"


@pytest.mark.asyncio
async def test_reprice_requires_payload_then_projects_to_approved(
    session: AsyncSession,
) -> None:
    item = await _seed_report_with_one_action_item(session)
    # reprice carries the new levels — rejected at the schema layer without them
    # (mirrors partial_approve's approved_payload_snapshot requirement).
    with pytest.raises(Exception) as exc:
        RecordDecisionRequest(
            item_uuid=item.item_uuid, decision="reprice", actor="operator-test"
        )
    assert "approved_payload_snapshot" in str(exc.value)

    decision = await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=item.item_uuid,
            decision="reprice",
            actor="operator-test",
            approved_payload_snapshot={"suggested_limit_price": 70000},
        )
    )
    assert decision.decision == "reprice"
    assert decision.approved_payload_snapshot == {"suggested_limit_price": 70000}
    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed is not None
    assert refreshed.status == "approved"
