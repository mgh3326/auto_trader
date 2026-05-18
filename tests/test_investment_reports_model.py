"""ROB-265 — Investment-report ORM + DB-level CHECK constraint tests.

Constraint exercises go through ``assert_integrity_error`` and the
shared async session fixture from ``tests/_investment_reports_helpers``
so the per-test boilerplate stays small.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)
from tests._investment_reports_helpers import (
    assert_integrity_error,
    future_datetime,
)

# ``session`` fixture is provided by the
# ``tests._investment_reports_helpers`` pytest plugin (registered in
# ``tests/conftest.py``), so it doesn't need to be imported here.


# ---------------------------------------------------------------------------
# Payload builders (local to this file — schemas-layer builders live in P2)
# ---------------------------------------------------------------------------
def _base_payload(**overrides) -> dict:
    payload: dict = {
        "report_uuid": uuid.uuid4(),
        "idempotency_key": f"key-{uuid.uuid4()}",
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "테스트 리포트",
        "summary": "요약",
        "status": "draft",
    }
    payload.update(overrides)
    return payload


def _base_item_payload(report_id: int, **overrides) -> dict:
    payload: dict = {
        "report_id": report_id,
        "item_uuid": uuid.uuid4(),
        "idempotency_key": f"item-{uuid.uuid4()}",
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "target_kind": "asset",
        "priority": 10,
        "rationale": "정규장 확인 후 수동 승인 후보",
    }
    payload.update(overrides)
    return payload


def _base_alert_payload(
    report_uuid: uuid.UUID, item_uuid: uuid.UUID, **overrides
) -> dict:
    payload: dict = {
        "alert_uuid": uuid.uuid4(),
        "idempotency_key": f"alert-{uuid.uuid4()}",
        "source_report_uuid": report_uuid,
        "source_item_uuid": item_uuid,
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "metric": "price",
        "operator": "below",
        "threshold": 70000,
        "threshold_key": "70000",
        "intent": "buy_review",
        "action_mode": "notify_only",
        "rationale": "저점 매수 후보 모니터링",
        "valid_until": future_datetime(),
    }
    payload.update(overrides)
    return payload


def _base_event_snapshot(**overrides) -> dict:
    """Required immutable trigger-identity columns on investment_watch_events.

    Watch events MUST carry these even after the source alert is deleted
    (Plan 1 patch — preserves audit identity).
    """
    payload: dict = {
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "metric": "price",
        "operator": "below",
        "threshold": 70000,
        "threshold_key": "70000",
        "intent": "buy_review",
        "action_mode": "notify_only",
    }
    payload.update(overrides)
    return payload


async def _make_report(session: AsyncSession, **overrides) -> InvestmentReport:
    row = InvestmentReport(**_base_payload(**overrides))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# InvestmentReport
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_round_trip_insert(session: AsyncSession) -> None:
    row = InvestmentReport(**_base_payload())
    session.add(row)
    await session.commit()

    result = await session.execute(
        sa.select(InvestmentReport).where(InvestmentReport.id == row.id)
    )
    fetched = result.scalar_one()
    assert fetched.market == "kr"
    assert fetched.execution_mode == "mock_preview"
    assert fetched.market_snapshot == {}
    assert fetched.report_metadata == {}


@pytest.mark.asyncio
async def test_idempotency_key_is_unique(session: AsyncSession) -> None:
    key = f"dup-{uuid.uuid4()}"
    session.add(InvestmentReport(**_base_payload(idempotency_key=key)))
    await session.commit()
    await assert_integrity_error(
        session, InvestmentReport(**_base_payload(idempotency_key=key))
    )


@pytest.mark.asyncio
async def test_advisory_only_invariant_blocks_live_with_mock_preview(
    session: AsyncSession,
) -> None:
    """kis_live account scope MUST pair with execution_mode='advisory_only'."""
    await assert_integrity_error(
        session,
        InvestmentReport(
            **_base_payload(account_scope="kis_live", execution_mode="mock_preview")
        ),
    )


@pytest.mark.asyncio
async def test_advisory_only_invariant_allows_live_with_advisory_only(
    session: AsyncSession,
) -> None:
    row = InvestmentReport(
        **_base_payload(account_scope="kis_live", execution_mode="advisory_only")
    )
    session.add(row)
    await session.commit()
    assert row.id is not None


@pytest.mark.asyncio
async def test_nxt_session_requires_advisory_only(session: AsyncSession) -> None:
    await assert_integrity_error(
        session,
        InvestmentReport(
            **_base_payload(market_session="nxt", execution_mode="mock_preview")
        ),
    )


@pytest.mark.asyncio
async def test_status_check_rejects_unknown_value(session: AsyncSession) -> None:
    await assert_integrity_error(
        session, InvestmentReport(**_base_payload(status="bogus"))
    )


# ---------------------------------------------------------------------------
# InvestmentReportItem
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_item_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    assert item.status == "proposed"
    assert item.target_kind == "asset"
    assert item.trigger_checklist == []


@pytest.mark.asyncio
async def test_item_kind_check(session: AsyncSession) -> None:
    report = await _make_report(session)
    await assert_integrity_error(
        session,
        InvestmentReportItem(**_base_item_payload(report.id, item_kind="bogus")),
    )


@pytest.mark.asyncio
async def test_watch_item_requires_condition(session: AsyncSession) -> None:
    """Missing watch_condition for item_kind='watch' → violation.

    ``valid_until`` is supplied so the failure is unambiguously the
    watch_has_condition CHECK, not the watch_has_expiry CHECK.
    """
    report = await _make_report(session)
    await assert_integrity_error(
        session,
        InvestmentReportItem(
            **_base_item_payload(
                report.id,
                item_kind="watch",
                side=None,
                valid_until=future_datetime(),
            )
        ),
    )


@pytest.mark.asyncio
async def test_watch_item_requires_valid_until(session: AsyncSession) -> None:
    """Watch items are time-bounded; missing valid_until → violation."""
    report = await _make_report(session)
    await assert_integrity_error(
        session,
        InvestmentReportItem(
            **_base_item_payload(
                report.id,
                item_kind="watch",
                side=None,
                intent="trend_recovery_review",
                watch_condition={
                    "metric": "rsi",
                    "operator": "below",
                    "threshold": 30,
                    "target_kind": "asset",
                },
            )
        ),
    )


@pytest.mark.asyncio
async def test_watch_item_with_condition_inserts(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(
        **_base_item_payload(
            report.id,
            item_kind="watch",
            side=None,
            intent="trend_recovery_review",
            watch_condition={
                "metric": "rsi",
                "operator": "below",
                "threshold": 30,
                "target_kind": "asset",
            },
            valid_until=future_datetime(),
        )
    )
    session.add(item)
    await session.commit()
    assert item.watch_condition["metric"] == "rsi"


@pytest.mark.asyncio
async def test_target_kind_check_rejects_unknown(session: AsyncSession) -> None:
    report = await _make_report(session)
    await assert_integrity_error(
        session,
        InvestmentReportItem(**_base_item_payload(report.id, target_kind="commodity")),
    )


@pytest.mark.asyncio
async def test_cascade_delete_from_report(session: AsyncSession) -> None:
    report = await _make_report(session)
    session.add(InvestmentReportItem(**_base_item_payload(report.id)))
    session.add(InvestmentReportItem(**_base_item_payload(report.id)))
    await session.commit()

    await session.delete(report)
    await session.commit()

    remaining = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentReportItem)
    )
    assert remaining == 0


# ---------------------------------------------------------------------------
# InvestmentReportItemDecision
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_decision_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    decision = InvestmentReportItemDecision(
        item_id=item.id,
        decision_uuid=uuid.uuid4(),
        idempotency_key=f"dec-{uuid.uuid4()}",
        decision="approve",
        actor="operator-test",
    )
    session.add(decision)
    await session.commit()

    fetched = await session.scalar(
        sa.select(InvestmentReportItemDecision).where(
            InvestmentReportItemDecision.id == decision.id
        )
    )
    assert fetched.decision == "approve"


@pytest.mark.asyncio
async def test_decision_check_rejects_unknown(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    await assert_integrity_error(
        session,
        InvestmentReportItemDecision(
            item_id=item.id,
            decision_uuid=uuid.uuid4(),
            idempotency_key=f"dec-{uuid.uuid4()}",
            decision="unknown-verb",
            actor="operator-test",
        ),
    )


@pytest.mark.asyncio
async def test_multiple_decisions_per_item_allowed(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    for verb in ("defer", "approve"):
        session.add(
            InvestmentReportItemDecision(
                item_id=item.id,
                decision_uuid=uuid.uuid4(),
                idempotency_key=f"dec-{uuid.uuid4()}",
                decision=verb,
                actor="operator-test",
            )
        )
    await session.commit()

    total = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentReportItemDecision)
    )
    assert total == 2


# ---------------------------------------------------------------------------
# InvestmentWatchAlert
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_alert_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    alert = InvestmentWatchAlert(
        **_base_alert_payload(report.report_uuid, item.item_uuid)
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)
    assert alert.status == "active"
    assert alert.target_kind == "asset"


@pytest.mark.asyncio
async def test_active_alert_requires_valid_until(session: AsyncSession) -> None:
    """investment_watch_alerts.valid_until is NOT NULL — Plan 1 patch."""
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    await assert_integrity_error(
        session,
        InvestmentWatchAlert(
            **_base_alert_payload(report.report_uuid, item.item_uuid, valid_until=None)
        ),
    )


@pytest.mark.asyncio
async def test_alert_action_mode_check(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    await assert_integrity_error(
        session,
        InvestmentWatchAlert(
            **_base_alert_payload(
                report.report_uuid,
                item.item_uuid,
                action_mode="auto_execute",
            )
        ),
    )


@pytest.mark.asyncio
async def test_alert_target_kind_index_allowed(session: AsyncSession) -> None:
    """Scanner asset/index/fx dimensions must survive."""
    report = await _make_report(session)
    item = InvestmentReportItem(
        **_base_item_payload(report.id, target_kind="index", symbol="KOSPI")
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)

    alert = InvestmentWatchAlert(
        **_base_alert_payload(
            report.report_uuid,
            item.item_uuid,
            target_kind="index",
            symbol="KOSPI",
            metric="price",
        )
    )
    session.add(alert)
    await session.commit()
    assert alert.target_kind == "index"


# ---------------------------------------------------------------------------
# InvestmentWatchEvent
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_event_round_trip(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)
    alert = InvestmentWatchAlert(
        **_base_alert_payload(report.report_uuid, item.item_uuid)
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)

    event = InvestmentWatchEvent(
        event_uuid=uuid.uuid4(),
        idempotency_key=f"{alert.alert_uuid}:2026-05-18:70000",
        alert_id=alert.id,
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        current_value=69500,
        outcome="notified",
        correlation_id=str(uuid.uuid4()),
        kst_date="2026-05-18",
        **_base_event_snapshot(),
    )
    session.add(event)
    await session.commit()
    assert event.id is not None


@pytest.mark.asyncio
async def test_event_outcome_check(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    await assert_integrity_error(
        session,
        InvestmentWatchEvent(
            event_uuid=uuid.uuid4(),
            idempotency_key=f"x-{uuid.uuid4()}",
            source_report_uuid=report.report_uuid,
            source_item_uuid=item.item_uuid,
            outcome="auto_executed",  # not in allowed set
            correlation_id=str(uuid.uuid4()),
            kst_date="2026-05-18",
            **_base_event_snapshot(),
        ),
    )


@pytest.mark.asyncio
async def test_event_idempotency_dedup(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    key = f"dup-event-{uuid.uuid4()}"
    base: dict = {
        "source_report_uuid": report.report_uuid,
        "source_item_uuid": item.item_uuid,
        "outcome": "notified",
        "correlation_id": str(uuid.uuid4()),
        "kst_date": "2026-05-18",
        **_base_event_snapshot(),
    }

    session.add(
        InvestmentWatchEvent(event_uuid=uuid.uuid4(), idempotency_key=key, **base)
    )
    await session.commit()
    await assert_integrity_error(
        session,
        InvestmentWatchEvent(event_uuid=uuid.uuid4(), idempotency_key=key, **base),
    )


@pytest.mark.asyncio
async def test_event_survives_alert_deletion(session: AsyncSession) -> None:
    report = await _make_report(session)
    item = InvestmentReportItem(**_base_item_payload(report.id))
    session.add(item)
    await session.commit()
    await session.refresh(item)

    alert = InvestmentWatchAlert(
        **_base_alert_payload(report.report_uuid, item.item_uuid)
    )
    session.add(alert)
    await session.commit()
    await session.refresh(alert)

    event = InvestmentWatchEvent(
        event_uuid=uuid.uuid4(),
        idempotency_key=f"keep-{uuid.uuid4()}",
        alert_id=alert.id,
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        outcome="notified",
        correlation_id=str(uuid.uuid4()),
        kst_date="2026-05-18",
        **_base_event_snapshot(),
    )
    session.add(event)
    await session.commit()

    await session.delete(alert)
    await session.commit()

    # SET NULL fires at the DB level. SQLAlchemy's identity map still has
    # the stale alert_id, so refresh the event row from disk explicitly.
    await session.refresh(event)
    assert event.alert_id is None

    # Plan 1 patch — trigger identity must survive alert deletion.
    assert event.market == "kr"
    assert event.target_kind == "asset"
    assert event.symbol == "005930"
    assert event.metric == "price"
    assert event.operator == "below"
    assert float(event.threshold) == 70000
    assert event.threshold_key == "70000"
    assert event.intent == "buy_review"
    assert event.action_mode == "notify_only"
    assert event.source_report_uuid == report.report_uuid
    assert event.source_item_uuid == item.item_uuid
