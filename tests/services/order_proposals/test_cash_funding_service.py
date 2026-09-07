"""§S177 proposal binding and durable cumulative-cap wiring."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_proposal_tools
from app.services.order_proposals import service as service_module
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.repository import OrderProposalRepository
from app.services.order_proposals.service import (
    OrderProposalsService,
    RungInput,
    batch_member_block_reason,
)

_NOW = datetime(2026, 9, 7, 3, tzinfo=UTC)


def _cash_create_kwargs(**overrides):
    values = {
        "symbol": "SGOV",
        "market": "equity_us",
        "account_mode": "kis_live",
        "broker_account_id": "test-cash-account",
        "side": "sell",
        "order_type": "limit",
        "proposer": "cash-funding-test",
        "thesis": "Fund an already-planned USD purchase.",
        "exit_intent": "cash_funding",
        "rungs": [RungInput(0, "sell", Decimal("2"), Decimal("100"), None)],
        "source_asof": {
            "cash_funding": {
                "funding_target": {
                    "market": "equity_us",
                    "required": "100",
                    "plan_ref": "planned-buy-001",
                }
            }
        },
        "now": _NOW,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_cash_funding_create_requires_fresh_insufficient_same_account(
    db_session, monkeypatch
):
    observed: dict[str, object] = {}

    async def insufficient(session, **kwargs):
        assert session is db_session
        observed.update(kwargs)
        return {"status": "insufficient", "shortfall": "200"}

    monkeypatch.setattr(service_module, "build_create_advisory", insufficient)
    group = await OrderProposalsService(db_session).create_proposal(
        **_cash_create_kwargs()
    )

    assert observed == {
        "account_mode": "kis_live",
        "broker_account_id": "test-cash-account",
        "currency": "USD",
        "now": _NOW,
    }
    assert group.exit_reason is None
    assert group.retrospective_id is None
    assert group.approval_issue_id is None
    assert group.source_asof is not None
    assert group.source_asof["cash_funding"] == {
        "funding_target": {
            "market": "equity_us",
            "required": "100",
            "plan_ref": "planned-buy-001",
        },
        "measured_shortfall": "200",
        "currency": "USD",
    }


@pytest.mark.asyncio
async def test_cash_funding_create_fails_closed_without_target(db_session, monkeypatch):
    reader = AsyncMock(return_value={"status": "insufficient", "shortfall": "200"})
    monkeypatch.setattr(service_module, "build_create_advisory", reader)

    with pytest.raises(OrderProposalError, match="cash_funding_funding_target_missing"):
        await OrderProposalsService(db_session).create_proposal(
            **_cash_create_kwargs(source_asof={"cash_funding": {}})
        )

    reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_create_carries_load_bearing_cash_target_in_source_asof(monkeypatch):
    proposal_id = uuid.uuid4()
    group = SimpleNamespace(
        proposal_id=proposal_id,
        lifecycle_state="proposed",
        action="place",
        target_broker_order_id=None,
        valid_until=None,
    )
    rung = SimpleNamespace(
        rung_index=0,
        side="sell",
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
        notional=None,
        state="pending_approval",
        broker_order_id=None,
        correlation_id=None,
    )
    session = SimpleNamespace(commit=AsyncMock())

    @contextlib.asynccontextmanager
    async def session_factory():
        yield session

    service = SimpleNamespace(
        create_proposal=AsyncMock(return_value=group),
        get_proposal=AsyncMock(return_value=(group, [rung])),
    )
    monkeypatch.setattr(order_proposal_tools, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        order_proposal_tools, "OrderProposalsService", lambda _session: service
    )
    monkeypatch.setattr(
        order_proposal_tools,
        "_link_funding_provenance_fail_open",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        order_proposal_tools,
        "_complete_committed_proposal_create",
        AsyncMock(return_value={"success": True}),
    )

    target = {"market": "equity_us", "required": "100", "plan_ref": "p-1"}
    result = await order_proposal_tools.order_proposal_create(
        symbol="SGOV",
        market="equity_us",
        account_mode="kis_live",
        side="sell",
        order_type="limit",
        proposer="cash-funding-test",
        exit_intent="cash_funding",
        funding_target=target,
        rungs=[
            {
                "rung_index": 0,
                "side": "sell",
                "quantity": "2",
                "limit_price": "100",
            }
        ],
    )

    assert result == {"success": True}
    assert service.create_proposal.await_args.kwargs["source_asof"] == {
        "cash_funding": {"funding_target": target}
    }


@pytest.mark.asyncio
async def test_cash_funding_repository_sum_uses_shared_rows_and_sell_intent_only():
    repo = object.__new__(OrderProposalRepository)
    rows = [
        ({}, 0, Decimal("2"), Decimal("100"), "sell", "SGOV", "cash_funding"),
        ({}, 1, Decimal("3"), Decimal("100"), "buy", "SGOV", "cash_funding"),
        ({}, 2, Decimal("4"), Decimal("100"), "sell", "SGOV", None),
    ]
    fetch = AsyncMock(return_value=rows)
    repo._auto_approved_rung_rows = fetch  # type: ignore[method-assign]

    total = await repo.auto_approved_cash_funding_notional_between(
        account_mode="kis_live",
        market="equity_us",
        broker_account_id="test-cash-account",
        start=_NOW,
        end=_NOW,
    )

    assert total == Decimal("200")
    fetch.assert_awaited_once_with(
        account_mode="kis_live",
        market="equity_us",
        broker_account_id="test-cash-account",
        start=_NOW,
        end=_NOW,
    )


@pytest.mark.asyncio
async def test_cash_funding_service_reuses_parking_kst_window_and_lock_key():
    class Repo:
        def __init__(self) -> None:
            self.locks: list[str] = []
            self.parking_kwargs: dict[str, object] | None = None
            self.cash_kwargs: dict[str, object] | None = None

        async def acquire_auto_approve_lock(self, key: str) -> None:
            self.locks.append(key)

        async def auto_approved_parking_notional_between(self, **kwargs):
            self.parking_kwargs = kwargs
            return Decimal("0")

        async def auto_approved_cash_funding_notional_between(self, **kwargs):
            self.cash_kwargs = kwargs
            return Decimal("0")

    repo = Repo()
    service = object.__new__(OrderProposalsService)
    service._repo = repo
    group = SimpleNamespace(
        symbol="SGOV",
        account_mode="kis_live",
        market="equity_us",
        broker_account_id="test-cash-account",
    )

    assert await service.auto_approved_parking_notional(group, now=_NOW) == Decimal("0")
    assert await service.auto_approved_cash_funding_notional(
        group, now=_NOW
    ) == Decimal("0")

    assert repo.locks[0] == repo.locks[1]
    assert repo.parking_kwargs is not None and repo.cash_kwargs is not None
    assert repo.parking_kwargs["start"] == repo.cash_kwargs["start"]
    assert repo.parking_kwargs["end"] == repo.cash_kwargs["end"]


def test_cash_funding_is_excluded_from_approval_batches():
    group = SimpleNamespace(
        superseded_by_proposal_id=None,
        lifecycle_state="proposed",
        exit_intent="cash_funding",
    )

    assert batch_member_block_reason(group, [], now=_NOW) == "cash_funding_excluded"
