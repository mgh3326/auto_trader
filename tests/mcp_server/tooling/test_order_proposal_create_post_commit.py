from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.mcp_server.tooling import order_proposal_tools
from app.monitoring.trade_notifier import notifier as notifier_module
from app.services.order_proposals.errors import OrderProposalError

PROPOSAL_ID = uuid.UUID("a1111111-1111-4111-8111-111111111111")


def _create_kwargs() -> dict:
    return {
        "symbol": "005930",
        "market": "equity_kr",
        "account_mode": "kis_live",
        "side": "buy",
        "order_type": "limit",
        "proposer": "post-commit-probe",
        "rungs": [
            {
                "rung_index": 0,
                "side": "buy",
                "quantity": "1",
                "limit_price": "70000",
            }
        ],
    }


def _committed_group() -> SimpleNamespace:
    return SimpleNamespace(
        proposal_id=PROPOSAL_ID,
        lifecycle_state="proposed",
        action="place",
        target_broker_order_id=None,
        valid_until=None,
    )


def _saved_rung() -> SimpleNamespace:
    return SimpleNamespace(
        rung_index=0,
        side="buy",
        quantity=1,
        limit_price=70000,
        notional=None,
        state="pending_approval",
        broker_order_id=None,
        correlation_id=None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("primary_failure", "enabled", "allowlist"),
    [
        ("telegram_disabled", False, "chat"),
        ("telegram_allowlist_empty", True, ""),
        ("telegram_transport_error", True, "chat"),
        ("approval_card_dispatch_failed", True, "chat"),
    ],
)
async def test_committed_create_survives_dispatch_and_failure_recorder_errors(
    monkeypatch,
    primary_failure: str,
    enabled: bool,
    allowlist: str,
) -> None:
    session = SimpleNamespace(commit=AsyncMock())

    @contextlib.asynccontextmanager
    async def session_factory():
        yield session

    service = SimpleNamespace(
        create_proposal=AsyncMock(return_value=_committed_group()),
        get_proposal=AsyncMock(return_value=(_committed_group(), [_saved_rung()])),
    )
    monkeypatch.setattr(order_proposal_tools, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        order_proposal_tools, "OrderProposalsService", lambda _session: service
    )
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_ENABLED", enabled)
    monkeypatch.setattr(
        settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", allowlist
    )
    dispatch = AsyncMock(side_effect=OrderProposalError(primary_failure))
    monkeypatch.setattr(order_proposal_tools, "dispatch_proposal", dispatch)
    monkeypatch.setattr(
        notifier_module, "get_trade_notifier", lambda: SimpleNamespace()
    )
    recorder = AsyncMock(side_effect=RuntimeError("attempt_ledger_unavailable"))
    monkeypatch.setattr(
        order_proposal_tools, "record_approval_dispatch_failure", recorder
    )

    result = await order_proposal_tools.order_proposal_create(**_create_kwargs())

    assert result["success"] is True
    assert result["proposal_id"] == str(PROPOSAL_ID)
    assert result["approval_dispatch"] == {
        "state": "failed",
        "message_id": None,
        "status_code": None,
        "error_code": None,
        "error_classification": None,
        "payload_chars": 0,
        "failure_code": "approval_dispatch_ledger_error",
        "ok": False,
    }
    session.commit.assert_awaited_once()
    service.create_proposal.assert_awaited_once()
    recorder.assert_awaited_once()
    if enabled and allowlist:
        dispatch.assert_awaited_once()
    else:
        dispatch.assert_not_awaited()

    # A caller that retries only create failures observes durable success and
    # therefore does not create a duplicate proposal.
    if not result["success"]:
        await order_proposal_tools.order_proposal_create(**_create_kwargs())
    service.create_proposal.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pre_commit_create_error_remains_caller_visible(monkeypatch) -> None:
    session = SimpleNamespace(commit=AsyncMock())

    @contextlib.asynccontextmanager
    async def session_factory():
        yield session

    service = SimpleNamespace(
        create_proposal=AsyncMock(
            side_effect=OrderProposalError("proposal_validation_failed")
        ),
    )
    recorder = AsyncMock()
    monkeypatch.setattr(order_proposal_tools, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        order_proposal_tools, "OrderProposalsService", lambda _session: service
    )
    monkeypatch.setattr(
        order_proposal_tools, "record_approval_dispatch_failure", recorder
    )

    result = await order_proposal_tools.order_proposal_create(**_create_kwargs())

    assert result == {"success": False, "error": "proposal_validation_failed"}
    session.commit.assert_not_awaited()
    recorder.assert_not_awaited()
