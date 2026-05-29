"""ROB-265 Plan 2 — Ingestion service tests."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_reports import (
    IngestReportItem,
    IngestReportRequest,
    WatchConditionPayload,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from tests._investment_reports_helpers import future_datetime


def _base_request(**overrides) -> IngestReportRequest:
    payload: dict = {
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "테스트",
        "summary": "요약",
        "kst_date": "2026-05-18",
        "generator_version": "v1",
    }
    payload.update(overrides)
    return IngestReportRequest(**payload)


def _action_item(client_item_key: str = "action-1", **overrides) -> IngestReportItem:
    kwargs: dict = {
        "client_item_key": client_item_key,
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "rationale": "r",
    }
    kwargs.update(overrides)
    return IngestReportItem(**kwargs)


def _watch_item(client_item_key: str = "watch-1", **overrides) -> IngestReportItem:
    kwargs: dict = {
        "client_item_key": client_item_key,
        "item_kind": "watch",
        "symbol": "000660",
        "intent": "trend_recovery_review",
        "rationale": "r",
        "watch_condition": WatchConditionPayload(
            metric="rsi", operator="below", threshold=30
        ),
        "valid_until": future_datetime(),
    }
    kwargs.update(overrides)
    return IngestReportItem(**kwargs)


@pytest.mark.asyncio
async def test_ingest_creates_report_with_items(session: AsyncSession) -> None:
    service = InvestmentReportIngestionService(session)
    request = _base_request(items=[_action_item(), _watch_item()])
    report = await service.ingest(request)
    assert report.id is not None
    assert report.report_uuid is not None

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 2
    kinds = {it.item_kind for it in items}
    assert kinds == {"action", "watch"}


@pytest.mark.asyncio
async def test_ingest_is_idempotent_on_same_key(session: AsyncSession) -> None:
    service = InvestmentReportIngestionService(session)
    request = _base_request(items=[_action_item()])
    first = await service.ingest(request)
    second = await service.ingest(request)

    assert first.report_uuid == second.report_uuid
    assert first.id == second.id

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(first.id)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_different_kst_date_creates_distinct_report(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)
    a = await service.ingest(_base_request(kst_date="2026-05-18"))
    b = await service.ingest(_base_request(kst_date="2026-05-19"))
    assert a.id != b.id
    assert a.report_uuid != b.report_uuid


@pytest.mark.asyncio
async def test_different_account_scope_creates_distinct_report(
    session: AsyncSession,
) -> None:
    """Plan 2 hardening #4: kis_mock vs kis_live for the same date must NOT collide."""
    service = InvestmentReportIngestionService(session)
    mock = await service.ingest(
        _base_request(account_scope="kis_mock", execution_mode="mock_preview")
    )
    live = await service.ingest(
        _base_request(account_scope="kis_live", execution_mode="advisory_only")
    )
    assert mock.id != live.id
    assert mock.report_uuid != live.report_uuid


@pytest.mark.asyncio
async def test_watch_condition_stored_as_jsonb(session: AsyncSession) -> None:
    service = InvestmentReportIngestionService(session)
    request = _base_request(items=[_watch_item()])
    report = await service.ingest(request)
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert items[0].watch_condition is not None
    assert items[0].watch_condition["metric"] == "rsi"
    # threshold_key defaulted from threshold via WatchConditionPayload validator
    assert items[0].watch_condition["threshold_key"] == "30"


@pytest.mark.asyncio
async def test_ingest_advisory_only_invariant_at_schema(
    session: AsyncSession,
) -> None:
    """kis_live + mock_preview is rejected before hitting the DB."""
    with pytest.raises(Exception) as exc_info:
        _base_request(account_scope="kis_live", execution_mode="mock_preview")
    assert "advisory_only" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ingest_kis_live_advisory_only_succeeds(
    session: AsyncSession,
) -> None:
    service = InvestmentReportIngestionService(session)
    report = await service.ingest(
        _base_request(account_scope="kis_live", execution_mode="advisory_only")
    )
    assert report.account_scope == "kis_live"
    assert report.execution_mode == "advisory_only"


@pytest.mark.asyncio
async def test_item_idempotency_key_is_deterministic(
    session: AsyncSession,
) -> None:
    """Re-ingest with the same payload doesn't duplicate items."""
    service = InvestmentReportIngestionService(session)
    request = _base_request(items=[_watch_item()])
    first = await service.ingest(request)
    repo = InvestmentReportsRepository(session)
    first_items = await repo.list_items_for_report(first.id)
    assert len(first_items) == 1
    first_item_idempotency = first_items[0].idempotency_key

    second = await service.ingest(request)
    assert second.id == first.id
    second_items = await repo.list_items_for_report(second.id)
    assert len(second_items) == 1
    assert second_items[0].idempotency_key == first_item_idempotency


@pytest.mark.asyncio
async def test_duplicate_natural_key_items_with_distinct_client_keys(
    session: AsyncSession,
) -> None:
    """Plan 2 hardening #2: two items with identical natural fields but
    distinct client_item_keys must produce two distinct rows.
    """
    service = InvestmentReportIngestionService(session)
    request = _base_request(
        items=[
            # Same symbol, side, intent — distinguished only by client_item_key.
            _action_item(client_item_key="buy-005930-tranche-1"),
            _action_item(client_item_key="buy-005930-tranche-2"),
        ]
    )
    report = await service.ingest(request)
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 2
    keys = {it.idempotency_key for it in items}
    assert len(keys) == 2  # distinct idempotency keys


@pytest.mark.asyncio
async def test_multiple_risk_items_without_symbol_no_longer_collide(
    session: AsyncSession,
) -> None:
    """Plan 2 hardening #2: pre-fix, multiple risk items would collide on
    (kind=risk, symbol=None, side=None, intent=risk_review) — fixed by
    client_item_key.
    """
    service = InvestmentReportIngestionService(session)

    def _risk(key: str, rationale: str) -> IngestReportItem:
        return IngestReportItem(
            client_item_key=key,
            item_kind="risk",
            intent="risk_review",
            rationale=rationale,
        )

    request = _base_request(
        items=[
            _risk("risk-fx", "FX 변동성 확대"),
            _risk("risk-credit", "신용 스프레드 확대"),
        ]
    )
    report = await service.ingest(request)
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 2
    assert {it.idempotency_key for it in items} != {None}


@pytest.mark.asyncio
async def test_overwrite_replaces_items_and_keeps_uuid(session: AsyncSession) -> None:
    """ROB-352 — overwrite=True replaces items in place, report_uuid stable."""
    service = InvestmentReportIngestionService(session)
    first = await service.ingest(_base_request(title="v1", items=[_action_item("a1")]))
    repo = InvestmentReportsRepository(session)
    assert len(await repo.list_items_for_report(first.id)) == 1

    second = await service.ingest(
        _base_request(
            title="v2",
            items=[_action_item("a1"), _action_item("a2", symbol="000660")],
        ),
        overwrite=True,
        overwrite_reason="restated",
    )
    assert second.report_uuid == first.report_uuid
    assert second.id == first.id
    assert second.title == "v2"
    assert second.report_metadata.get("overwrite_reason") == "restated"

    items = await repo.list_items_for_report(first.id)
    assert len(items) == 2


@pytest.mark.asyncio
async def test_default_reuse_does_not_replace_items(session: AsyncSession) -> None:
    """ROB-352 — without overwrite, a second ingest leaves the stored row intact."""
    service = InvestmentReportIngestionService(session)
    first = await service.ingest(_base_request(title="v1", items=[_action_item("a1")]))
    second = await service.ingest(
        _base_request(
            title="v2-ignored", items=[_action_item("a1"), _action_item("a2")]
        )
    )
    assert second.id == first.id
    assert second.title == "v1"  # stored row unchanged

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(first.id)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_overwrite_blocked_when_item_has_operator_decision(
    session: AsyncSession,
) -> None:
    """ROB-352 — overwrite must not clobber operator decision audit.

    investment_report_item_decisions.item_id is ON DELETE CASCADE, so a
    delete+reinsert would erase the audit trail. Refuse instead.
    """
    from app.services.investment_reports.ingestion import (
        ReportOverwriteBlockedError,
    )

    service = InvestmentReportIngestionService(session)
    first = await service.ingest(_base_request(items=[_action_item("a1")]))
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(first.id)
    await repo.insert_decision(
        item_id=items[0].id,
        idempotency_key="dec:1",
        decision="approve",
        actor="operator",
    )

    with pytest.raises(ReportOverwriteBlockedError):
        await service.ingest(
            _base_request(title="v2", items=[_action_item("a1")]),
            overwrite=True,
            overwrite_reason="redo",
        )

    # Audit + items left intact (no partial mutation).
    still = await repo.list_items_for_report(first.id)
    assert len(still) == 1
    assert len(await repo.list_decisions_for_items([items[0].id])) == 1


@pytest.mark.asyncio
async def test_cited_snapshot_uuids_round_trip(session: AsyncSession) -> None:
    """ROB-352 Slice B — cited_snapshot_uuids persists and reads back."""
    import uuid as _uuid

    u1, u2 = _uuid.uuid4(), _uuid.uuid4()
    service = InvestmentReportIngestionService(session)
    report = await service.ingest(
        _base_request(items=[_action_item("a1", cited_snapshot_uuids=[u1, u2])])
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    assert items[0].cited_snapshot_uuids == [u1, u2]
