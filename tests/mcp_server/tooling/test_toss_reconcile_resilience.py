from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import TossLiveOrderLedger
from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossErrorEnvelope,
    TossRateLimitError,
)
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session):
    from app.mcp_server.tooling import toss_live_ledger

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = db_session
    mock_cm.__aexit__.return_value = None
    with patch.object(
        toss_live_ledger, "_order_session_factory", return_value=lambda: mock_cm
    ):
        yield


async def _accepted(db_session):
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="kr",
        symbol="034020",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("3"),
        price=Decimal("85000"),
        order_amount=None,
        currency="KRW",
        client_order_id="cid-kr-buy",
        broker_order_id="ord-kr-buy",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t",
        strategy="s",
    )


def _toss_err(code: str, status: int):
    return TossApiResponseError(
        TossErrorEnvelope(request_id="ray", code=code, message="boom", data=None),
        status_code=status,
    )


@pytest.mark.parametrize(
    "exc",
    [
        TossRateLimitError(
            TossErrorEnvelope(request_id="r", code="rate-limit-exceeded", message="x", data=None),
            status_code=429,
        ),
        _toss_err("internal-error", 500),
        _toss_err("maintenance", 503),
        _toss_err("expired-token", 401),
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("refused"),
    ],
)
async def test_transient_error_leaves_row_retryable_not_anomaly(db_session, exc):
    from app.mcp_server.tooling import toss_live_ledger as mod

    row = await _accepted(db_session)
    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=exc)):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"deferred": 1}
    entry = out["reconciled"][0]
    assert entry["verdict"] == "deferred"
    assert entry["retryable"] is True

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"  # still selected by list_open next pass
    assert refreshed.requires_manual_review is False
    assert refreshed.manual_review_reason is None
    assert refreshed.last_reconcile_error is not None  # error recorded for observability


async def test_404_order_not_found_still_marks_anomaly(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    row = await _accepted(db_session)
    exc = _toss_err("order-not-found", 404)
    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=exc)):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"anomaly": 1}
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "anomaly"
    assert refreshed.requires_manual_review is True


async def test_impl_uses_batch_source_and_echoes_window(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)

    fake_source = AsyncMock()
    fake_source.evidence_for = AsyncMock(
        return_value=TossFillEvidence(
            verdict="pending",
            local_status="pending",
            broker_status="PENDING",
            filled_qty=Decimal("0"),
            avg_price=None,
            commission=None,
            tax=None,
            fee_total=Decimal("0"),
            settlement_date=None,
            raw_order={"status": "PENDING"},
            reason="pending",
        )
    )
    fake_source.aclose = AsyncMock()
    fake_source.single_fetch_count = 0
    fake_source.closed_pages_capped = False
    fake_source.window_from = "2026-07-01"
    fake_source.window_to = "2026-07-03"

    with patch.object(
        mod.TossBatchEvidenceSource, "build", new=AsyncMock(return_value=fake_source)
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["success"] is True
    assert out["counts"] == {"pending": 1}
    # evidence came from the batch source, not a per-row adapter
    fake_source.evidence_for.assert_awaited_once()
    fake_source.aclose.assert_awaited_once()
    assert out["window"]["from"] == "2026-07-01"
    assert out["window"]["closed_pages_capped"] is False


async def test_impl_batch_build_failure_falls_back_per_row(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    await _accepted(db_session)

    pending = TossFillEvidence(
        verdict="pending", local_status="pending", broker_status="PENDING",
        filled_qty=Decimal("0"), avg_price=None, commission=None, tax=None,
        fee_total=Decimal("0"), settlement_date=None,
        raw_order={"status": "PENDING"}, reason="pending",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=pending)

    with (
        patch.object(
            mod.TossBatchEvidenceSource,
            "build",
            new=AsyncMock(side_effect=RuntimeError("toss disabled in test")),
        ),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"pending": 1}  # per-row fallback still reconciles
