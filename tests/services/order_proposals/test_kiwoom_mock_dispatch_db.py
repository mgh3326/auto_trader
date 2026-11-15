"""hk 4732 (3797 T-P5) — a kiwoom_mock proposal dispatches through one boundary.

CI only (``integration``): the pytest-owned throwaway database is the only
target. The broker is a stub; the place-path persistence (send reservation,
order ledger, attribution signal) is the **real** service, so the assertions
read real rows. Every row this test creates is removed in ``finally``.

Proves, end to end: regular-session submit → exactly one POST → rung
``resting`` → one accepted ledger row → one committed signal row with the
proposal's correlation id; a second dispatch sends nothing; outside 09:00-15:30
KST nothing is sent at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from app.core.db import AsyncSessionLocal, engine
from app.models.review import (
    KiwoomMockOrderLedger,
    KiwoomMockSignalLedger,
    OrderSendIntent,
)
from app.services.order_proposals.approval_window import (
    ApprovalWindowCode,
    evaluate_approval_window,
)
from app.services.order_proposals.revalidation import revalidate_and_submit
from app.services.order_proposals.service import OrderProposalsService, RungInput
from tests._run_owned_database import validate_run_owned_database_url

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# 2026-11-16 (Monday) 10:00 KST — inside the KRX regular session.
FROZEN_NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
# 2026-11-16 16:00 KST — KRX closed, and the NXT after-market must not open it.
AFTER_HOURS = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)
ACK = {"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"}


class _StubOrderClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def factory(self):
        stub = self

        class _OrderClient:
            def __init__(self, _client):
                pass

            async def place_buy_order(self, **kwargs):
                stub.posts.append({"method": "buy", **kwargs})
                return ACK

            async def place_sell_order(self, **kwargs):
                stub.posts.append({"method": "sell", **kwargs})
                return ACK

        return _OrderClient


async def _no_claim(**_kwargs):
    return None


async def _no_release(**_kwargs):
    return None


def _wire(monkeypatch) -> _StubOrderClient:
    from app.services import kiwoom_mock_place

    stub = _StubOrderClient()

    async def fake_preflight(**_kwargs):
        from app.services.brokers.kiwoom.order_preflight import (
            PREFLIGHT_OK,
            PreflightResult,
        )

        return PreflightResult(
            ok=True, error_code=PREFLIGHT_OK, checks=[], estimated_evidence={}
        )

    monkeypatch.setattr(kiwoom_mock_place, "mock_config_error", lambda: None)
    monkeypatch.setattr(kiwoom_mock_place, "new_kiwoom_mock_client", lambda: object())
    monkeypatch.setattr(kiwoom_mock_place, "run_kiwoom_mock_preflight", fake_preflight)
    monkeypatch.setattr(kiwoom_mock_place, "KiwoomDomesticOrderClient", stub.factory())
    monkeypatch.setattr(
        kiwoom_mock_place, "KiwoomDomesticAccountClient", lambda _client: object()
    )
    monkeypatch.setattr(kiwoom_mock_place, "utc_now", lambda: FROZEN_NOW)
    return stub


async def _cleanup(*, client_order_id: str | None, correlation_id: str | None) -> None:
    async with AsyncSessionLocal() as db:
        if client_order_id is not None:
            await db.execute(
                delete(KiwoomMockOrderLedger).where(
                    KiwoomMockOrderLedger.client_order_id == client_order_id
                )
            )
            await db.execute(
                delete(OrderSendIntent).where(
                    OrderSendIntent.account_scope == "kiwoom_mock",
                    OrderSendIntent.idempotency_key == client_order_id,
                )
            )
        if correlation_id is not None:
            await db.execute(
                delete(KiwoomMockSignalLedger).where(
                    KiwoomMockSignalLedger.correlation_id == correlation_id
                )
            )
        await db.commit()


async def _create(db_session, *, price: int, strategy: str) -> Any:
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="005930",
        market="equity_kr",
        account_mode="kiwoom_mock",
        side="buy",
        order_type="limit",
        proposer="db-test",
        strategy=strategy,
        rungs=[RungInput(0, "buy", Decimal("3"), Decimal(price), None)],
        valid_until=FROZEN_NOW + timedelta(days=1),
        now=FROZEN_NOW,
    )
    await db_session.commit()
    return service, group


async def _dispatch(service, proposal_id, *, now):
    decision = await evaluate_approval_window(
        (await service.get_proposal(proposal_id))[0], now=now
    )
    return await revalidate_and_submit(
        service=service,
        proposal_id=proposal_id,
        now=now,
        expected_policy_stamp=decision.policy_stamp,
        window_evaluator=evaluate_approval_window,
        now_fn=lambda: now,
        buying_power_claimer=_no_claim,
        buying_power_releaser=_no_release,
    )


async def test_regular_session_submit_sends_once_and_records_both_rows(
    db_session, monkeypatch
):
    stub = _wire(monkeypatch)
    price = 100000 + uuid.uuid4().int % 800000
    strategy = f"db-test-{uuid.uuid4().hex[:12]}"
    service, group = await _create(db_session, price=price, strategy=strategy)
    client_order_id: str | None = None
    correlation_id: str | None = None
    try:
        outcomes = await _dispatch(service, group.proposal_id, now=FROZEN_NOW)

        assert len(outcomes) == 1
        assert outcomes[0].result == "submitted_resting"
        assert len(stub.posts) == 1
        assert stub.posts[0]["method"] == "buy"
        assert (stub.posts[0]["quantity"], stub.posts[0]["price"]) == (3, price)

        _group, rungs = await service.get_proposal(group.proposal_id)
        rung = rungs[0]
        assert rung.state == "resting"
        assert rung.broker_order_id == "0000111222"
        assert rung.correlation_id is not None
        correlation_id = rung.correlation_id
        assert rung.idempotency_key is not None
        client_order_id = rung.idempotency_key

        async with AsyncSessionLocal() as db:
            ledger = (
                await db.execute(
                    select(KiwoomMockOrderLedger).where(
                        KiwoomMockOrderLedger.client_order_id == client_order_id
                    )
                )
            ).scalar_one()
            assert (ledger.status, ledger.broker_order_id) == (
                "accepted",
                "0000111222",
            )
            assert ledger.account_mode == "kiwoom_mock"
            signal = (
                await db.execute(
                    select(KiwoomMockSignalLedger).where(
                        KiwoomMockSignalLedger.correlation_id == correlation_id
                    )
                )
            ).scalar_one()
            assert signal.strategy == strategy
            assert signal.symbol == "005930"
            assert signal.outcome_state == "submitted"

        # A second dispatch finds no pending rung: no second POST, no second row.
        again = await _dispatch(service, group.proposal_id, now=FROZEN_NOW)
        assert [outcome.result for outcome in again] == []
        assert len(stub.posts) == 1
        async with AsyncSessionLocal() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(KiwoomMockOrderLedger)
                    .where(KiwoomMockOrderLedger.client_order_id == client_order_id)
                )
            ).scalar_one()
        assert count == 1
    finally:
        await _cleanup(client_order_id=client_order_id, correlation_id=correlation_id)


async def test_outside_the_regular_session_nothing_is_sent(db_session, monkeypatch):
    stub = _wire(monkeypatch)
    price = 100000 + uuid.uuid4().int % 800000
    strategy = f"db-test-{uuid.uuid4().hex[:12]}"
    service, group = await _create(db_session, price=price, strategy=strategy)
    try:
        outcomes = await _dispatch(service, group.proposal_id, now=AFTER_HOURS)

        assert len(outcomes) == 1
        assert outcomes[0].result == "defer_session_closed"
        assert outcomes[0].detail["approval_window"]["code"] == (
            ApprovalWindowCode.DEFER_SESSION_CLOSED.value
        )
        assert stub.posts == []

        _group, rungs = await service.get_proposal(group.proposal_id)
        assert rungs[0].state == "pending_approval"
        assert rungs[0].broker_order_id is None
        # Not even the attribution row exists: the refusal was before the place
        # path was entered at all.
        async with AsyncSessionLocal() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(KiwoomMockSignalLedger)
                    .where(KiwoomMockSignalLedger.strategy == strategy)
                )
            ).scalar_one()
        assert count == 0
    finally:
        await _cleanup(client_order_id=None, correlation_id=None)
