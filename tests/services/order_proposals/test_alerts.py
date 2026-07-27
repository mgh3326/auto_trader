from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.order_proposals import alerts


@contextlib.asynccontextmanager
async def _session_factory():
    yield SimpleNamespace(commit=AsyncMock())


def _service(record: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        get_proposal=AsyncMock(
            return_value=(
                SimpleNamespace(
                    symbol="052690",
                    side="sell",
                    approval_dispatch_attempt_id=uuid.UUID(
                        "a1111111-1111-4111-8111-111111111111"
                    ),
                ),
                [],
            )
        ),
        record_approval_dispatch_alert=record,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocked_dispatch_alert_sends_required_discord_fields(
    monkeypatch,
) -> None:
    proposal_id = uuid.UUID("b2222222-2222-4222-8222-222222222222")
    record = AsyncMock()
    service = _service(record)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts, "OrderProposalsService", lambda _session: service)
    monkeypatch.setattr(alerts, "send_discord_embed_single", sender)
    monkeypatch.setattr(
        settings,
        "discord_webhook_alerts",
        "https://discord.example/ops-alerts",
    )

    result = await alerts.send_approval_dispatch_alert(
        proposal_id,
        dispatch_state="blocked",
        dispatch_failure_code="CALENDAR_UNKNOWN/nxt_capability_stale",
        now=datetime(2026, 7, 27, 8, 16, tzinfo=UTC),
        service_factory=_session_factory,
    )

    assert result.state == "sent"
    assert result.recorded is True
    sender.assert_awaited_once()
    embed = sender.await_args.kwargs["embed"]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["proposal_id"] == str(proposal_id)
    assert fields["symbol"] == "052690"
    assert fields["side"] == "sell"
    assert fields["failure_code"] == "CALENDAR_UNKNOWN/nxt_capability_stale"
    assert "order_proposal_redispatch(dry_run=true)" in fields["operator_action"]
    assert "정규장" in fields["operator_action"]
    record.assert_awaited_once()
    assert record.await_args.kwargs["state"] == "sent"
    assert record.await_args.kwargs["alert_failure_code"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alert_transport_failure_is_logged_and_durably_recorded(
    monkeypatch,
    caplog,
) -> None:
    proposal_id = uuid.UUID("c3333333-3333-4333-8333-333333333333")
    record = AsyncMock()
    service = _service(record)
    monkeypatch.setattr(alerts, "OrderProposalsService", lambda _session: service)
    monkeypatch.setattr(
        alerts,
        "send_discord_embed_single",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        settings,
        "discord_webhook_alerts",
        "https://discord.example/ops-alerts",
    )

    result = await alerts.send_approval_dispatch_alert(
        proposal_id,
        dispatch_state="failed",
        dispatch_failure_code="approval_card_dispatch_failed",
        now=datetime(2026, 7, 27, 8, 17, tzinfo=UTC),
        service_factory=_session_factory,
    )

    assert result.state == "failed"
    assert result.failure_code == "discord_delivery_failed"
    assert "order_proposals.approval_dispatch_alert_failed" in caplog.text
    record.assert_awaited_once()
    assert record.await_args.kwargs["state"] == "failed"
    assert record.await_args.kwargs["alert_failure_code"] == "discord_delivery_failed"
