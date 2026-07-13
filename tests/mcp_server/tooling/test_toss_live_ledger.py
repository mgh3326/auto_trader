from __future__ import annotations

import contextlib
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.execution_ledger import ExecutionLedger
from app.models.review import TossLiveOrderLedger
from app.services.toss_live_order_ledger_service import TossLiveOrderLedgerService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
pytestmark.append(pytest.mark.usefixtures("toss_ledger_cleanup_lock"))


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session, toss_ledger_cleanup_lock):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.execute(delete(ExecutionLedger))
    await db_session.commit()
    yield


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session):
    from app.mcp_server.tooling import toss_live_ledger

    # Create a mock that when called twice returns db_session
    # async with _order_session_factory()() as db:
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = db_session
    mock_cm.__aexit__.return_value = None

    def factory_call():
        return mock_cm

    with patch.object(
        toss_live_ledger, "_order_session_factory", return_value=factory_call
    ):
        yield


async def _accepted(db_session, *, side: str = "buy", market: str = "us"):
    is_kr = market == "kr"
    suffix = side if market == "us" else f"{market}-{side}"
    return await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market=market,
        symbol="034020" if is_kr else "AAPL",
        side=side,
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("3") if is_kr else Decimal("2"),
        price=Decimal("85000") if is_kr else Decimal("190"),
        order_amount=None,
        currency="KRW" if is_kr else "USD",
        client_order_id=f"cid-{suffix}",
        broker_order_id=f"ord-{suffix}",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t" if side == "buy" else None,
        strategy="s" if side == "buy" else None,
        exit_reason="trim" if side == "sell" else None,
    )


async def _seed_toss_resting_proposal(
    db_session, *, broker_order_id: str, correlation_id: str
):
    from datetime import UTC, datetime

    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="buy",
        order_type="limit",
        proposer="projection-test",
        rungs=[RungInput(0, "buy", Decimal("2"), Decimal("190"), None)],
    )
    now = datetime.now(UTC)
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(group.proposal_id, 0, new_state=state)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=broker_order_id,
        correlation_id=correlation_id,
        idempotency_key=f"idem-{broker_order_id}",
        approval_hash_digest=f"digest-{broker_order_id}",
        now=now,
    )
    await db_session.commit()
    return group.proposal_id


async def _proposal_rung(db_session, proposal_id):
    from app.services.order_proposals import OrderProposalsService

    _, rungs = await OrderProposalsService(db_session).get_proposal(proposal_id)
    await db_session.refresh(rungs[0])
    return rungs[0]


async def _proposal_accepted_row(db_session, *, suffix: str):
    unique = uuid4().hex
    broker_order_id = f"ord-proposal-{suffix}-{unique}"
    correlation_id = f"corr-proposal-{suffix}-{unique}"
    proposal_id = await _seed_toss_resting_proposal(
        db_session,
        broker_order_id=broker_order_id,
        correlation_id=correlation_id,
    )
    row = await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190"),
        order_amount=None,
        currency="USD",
        client_order_id=f"cid-proposal-{suffix}-{unique}",
        broker_order_id=broker_order_id,
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="projection test",
        strategy="projection test",
        correlation_id=correlation_id,
    )
    return proposal_id, row


def _toss_evidence(*, verdict: str, local_status: str, filled_qty: str):
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    return TossFillEvidence(
        verdict=verdict,
        local_status=local_status,
        broker_status=local_status.upper(),
        filled_qty=Decimal(filled_qty),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": local_status.upper()},
        reason=verdict,
    )


def _projection_booking_patches(mod):
    return (
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=555)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_id": 77}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock(return_value=None)),
        patch.object(
            mod,
            "upsert_toss_execution_fill",
            new=AsyncMock(return_value=("inserted", 858)),
        ),
        patch.object(mod, "_notify_toss_fill", new=AsyncMock(return_value=False)),
        patch.object(mod, "_close_journals_on_sell", new=AsyncMock(return_value=None)),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
    )


async def _reconcile_with_evidence(mod, row, evidence):
    source = AsyncMock()
    source.evidence_for.return_value = evidence
    with ExitStack() as stack:
        for booking_patch in _projection_booking_patches(mod):
            stack.enter_context(booking_patch)
        return await mod._reconcile_one_toss_row(
            row,
            dry_run=False,
            evidence_source=source,
        )


async def test_proposal_projection_partial_fill_and_duplicate_evidence_are_idempotent(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod

    proposal_id, row = await _proposal_accepted_row(db_session, suffix="partial-fill")

    partial = await _reconcile_with_evidence(
        mod,
        row,
        _toss_evidence(verdict="partial", local_status="partial", filled_qty="0.5"),
    )
    rung = await _proposal_rung(db_session, proposal_id)
    assert partial["proposal_rung"] == {
        "converged": True,
        "proposal_rung_state": "partially_filled",
    }
    assert rung.state == "partially_filled"
    assert rung.filled_qty == Decimal("0.5")

    await db_session.refresh(row)
    filled = await _reconcile_with_evidence(
        mod,
        row,
        _toss_evidence(verdict="filled", local_status="filled", filled_qty="2"),
    )
    rung = await _proposal_rung(db_session, proposal_id)
    assert filled["proposal_rung"]["proposal_rung_state"] == "filled"
    assert rung.state == "filled"
    assert rung.filled_qty == Decimal("2")

    await db_session.refresh(row)
    duplicate = await _reconcile_with_evidence(
        mod,
        row,
        _toss_evidence(verdict="filled", local_status="filled", filled_qty="2"),
    )
    rung = await _proposal_rung(db_session, proposal_id)
    assert duplicate["action"] == "noop_already_booked"
    assert "proposal_rung" not in duplicate
    assert rung.state == "filled"
    assert rung.filled_qty == Decimal("2")


async def test_proposal_projection_cancel_preserves_existing_partial_quantity(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod

    proposal_id, row = await _proposal_accepted_row(db_session, suffix="partial-cancel")
    await _reconcile_with_evidence(
        mod,
        row,
        _toss_evidence(verdict="partial", local_status="partial", filled_qty="0.5"),
    )

    await db_session.refresh(row)
    cancelled = await _reconcile_with_evidence(
        mod,
        row,
        _toss_evidence(verdict="none", local_status="cancelled", filled_qty="0"),
    )
    rung = await _proposal_rung(db_session, proposal_id)

    assert cancelled["proposal_rung"]["proposal_rung_state"] == "cancelled"
    assert rung.state == "cancelled"
    assert rung.filled_qty == Decimal("0.5")


async def test_proposal_projection_dry_run_is_read_only(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    proposal_id, row = await _proposal_accepted_row(db_session, suffix="dry-run")
    source = AsyncMock()
    source.evidence_for.return_value = _toss_evidence(
        verdict="filled", local_status="filled", filled_qty="2"
    )

    outcome = await mod._reconcile_one_toss_row(
        row,
        dry_run=True,
        evidence_source=source,
    )
    rung = await _proposal_rung(db_session, proposal_id)

    assert outcome["action"] == "would_book"
    assert "proposal_rung" not in outcome
    assert rung.state == "resting"
    assert rung.filled_qty is None


async def test_toss_projection_never_matches_another_broker_by_order_id(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput

    shared_broker_order_id = f"cross-broker-{uuid4().hex}"
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="collision-test",
        rungs=[RungInput(0, "buy", Decimal("2"), Decimal("190"), None)],
    )
    for state in ("revalidating", "approved", "submitting"):
        await service.transition_rung(group.proposal_id, 0, new_state=state)
    await service.record_resting(
        group.proposal_id,
        0,
        broker_order_id=shared_broker_order_id,
        correlation_id=f"kis-correlation-{uuid4().hex}",
        idempotency_key=f"kis-idem-{uuid4().hex}",
        approval_hash_digest="kis-digest",
        now=datetime.now(UTC),
    )
    row = await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="place",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("190"),
        order_amount=None,
        currency="USD",
        client_order_id=f"toss-collision-{uuid4().hex}",
        broker_order_id=shared_broker_order_id,
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        correlation_id=f"toss-correlation-{uuid4().hex}",
    )

    outcome = await _reconcile_with_evidence(
        mod,
        row,
        _toss_evidence(verdict="filled", local_status="filled", filled_qty="2"),
    )
    rung = await _proposal_rung(db_session, group.proposal_id)

    assert "proposal_rung" not in outcome
    assert rung.state == "resting"
    assert rung.filled_qty is None


async def test_proposal_projects_confirmed_fill_before_booking_failure(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    proposal_id, row = await _proposal_accepted_row(
        db_session, suffix="booking-failure"
    )
    source = AsyncMock()
    source.evidence_for.return_value = _toss_evidence(
        verdict="filled", local_status="filled", filled_qty="2"
    )

    with (
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(
            mod,
            "_save_order_fill",
            new=AsyncMock(side_effect=RuntimeError("booking unavailable")),
        ),
    ):
        with pytest.raises(RuntimeError, match="booking unavailable"):
            await mod._reconcile_one_toss_row(
                row,
                dry_run=False,
                evidence_source=source,
            )

    rung = await _proposal_rung(db_session, proposal_id)
    assert rung.state == "filled"
    assert rung.filled_qty == Decimal("2")


async def test_terminal_ledger_projection_failure_is_retried_by_sweep(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.order_proposals import OrderProposalsService

    proposal_id, row = await _proposal_accepted_row(db_session, suffix="retry-sweep")
    evidence = _toss_evidence(verdict="filled", local_status="filled", filled_qty="2")

    with patch.object(
        OrderProposalsService,
        "record_fill_evidence",
        new=AsyncMock(side_effect=RuntimeError("projection unavailable")),
    ):
        failed = await _reconcile_with_evidence(mod, row, evidence)

    await db_session.refresh(row)
    rung = await _proposal_rung(db_session, proposal_id)
    assert row.status == "filled"
    assert failed["proposal_rung"] == {
        "converged": False,
        "error": "projection unavailable",
    }
    assert rung.state == "resting"

    with (
        patch.object(
            mod.TossLiveOrderLedgerService,
            "reopen_anomalies_for_reconcile",
            new=AsyncMock(
                return_value={
                    "rows": [],
                    "dry_run": False,
                    "reopened": 0,
                    "candidates": 0,
                }
            ),
        ),
        patch.object(
            mod.TossLiveOrderLedgerService,
            "list_open",
            new=AsyncMock(return_value=[]),
        ),
    ):
        repaired = await mod.toss_reconcile_orders_impl(dry_run=False)

    rung = await _proposal_rung(db_session, proposal_id)
    assert repaired["proposal_projection_repair"] == {
        "candidates": 1,
        "converged": 1,
        "failed": 0,
    }
    assert rung.state == "filled"
    assert rung.filled_qty == Decimal("2")


@pytest.mark.parametrize("terminal_status", ["filled", "cancelled"])
async def test_toss_loss_cut_proposal_approval_submit_and_reconcile_e2e(
    db_session,
    monkeypatch,
    terminal_status,
):
    from app.core.config import settings
    from app.mcp_server.caller_identity import get_caller_agent_id
    from app.mcp_server.tooling import order_validation as validation
    from app.mcp_server.tooling import orders_toss_variants as toss_orders
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.order_proposals import OrderProposalsService
    from app.services.order_proposals.service import RungInput
    from app.services.order_proposals.telegram_callback import handle_callback_update
    from app.services.trade_journal.trade_retrospective_service import (
        build_retrospective_pending,
    )

    unique = uuid4().hex
    broker_order_id = f"toss-loss-cut-{terminal_status}-{unique}"
    now = datetime.now(UTC)
    submit_agent_id = f"proposal-submit-{unique}"

    async def fake_retrospective(session, retrospective_id):
        return type(
            "Retro",
            (),
            {
                "id": 42,
                "symbol": "AAPL",
                "trigger_type": "stop_loss",
                "created_at": now,
                "lesson": "손절 기준을 늦추지 않는다",
            },
        )()

    monkeypatch.setattr(
        "app.services.order_proposals.service.get_retrospective_by_id",
        fake_retrospective,
    )
    service = OrderProposalsService(db_session)
    group = await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="toss_live",
        side="sell",
        order_type="limit",
        proposer="e2e-test",
        rungs=[RungInput(0, "sell", Decimal("2"), Decimal("99"), None)],
        exit_intent="loss_cut",
        exit_reason="stop_loss",
        retrospective_id=42,
        approval_issue_id=None,
        now=now,
    )
    nonce = f"nonce-{unique}"
    await service.set_approval_nonce(group.proposal_id, nonce)
    await db_session.commit()

    class _Client:
        def __init__(self):
            self.placed_payloads: list[dict] = []

        async def aclose(self):
            return None

        async def warnings(self, symbol):
            return []

        async def holdings(self, *, symbol=None):
            holding = type(
                "Holding",
                (),
                {"symbol": symbol or "AAPL", "average_purchase_price": Decimal("200")},
            )()
            return type("Holdings", (), {"items": [holding], "raw_overview": {}})()

        async def prices(self, symbols):
            price = type(
                "Price",
                (),
                {"symbol": symbols[0], "last_price": Decimal("100"), "currency": "USD"},
            )()
            return [price]

        async def list_orders(self, *, status, symbol=None, **kwargs):
            return type(
                "Orders", (), {"orders": [], "next_cursor": None, "has_next": False}
            )()

        async def place_order(self, payload):
            self.placed_payloads.append(payload)
            return type(
                "Placed",
                (),
                {
                    "order_id": broker_order_id,
                    "client_order_id": payload["clientOrderId"],
                },
            )()

    class _Notifier:
        def __init__(self):
            self.answered = []
            self.edited = []

        async def answer_callback(self, callback_query_id, text=None):
            self.answered.append((callback_query_id, text))
            return True

        async def edit_message(self, chat_id, message_id, text, reply_markup=None):
            self.edited.append((chat_id, message_id, text, reply_markup))
            return True

    @contextlib.asynccontextmanager
    async def service_factory():
        yield db_session

    client = _Client()
    observed_submit_identities: list[str | None] = []

    retro = type(
        "Retro",
        (),
        {
            "id": 42,
            "symbol": "AAPL",
            "trigger_type": "stop_loss",
            "created_at": now,
            "lesson": "손절 기준을 늦추지 않는다",
        },
    )()
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR", "42")
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_SUBMIT_AGENT_ID", submit_agent_id)
    monkeypatch.setattr(settings, "LOSS_CUT_ALLOWED_AGENT_IDS", [submit_agent_id])
    monkeypatch.setattr(toss_orders, "validate_toss_api_config", lambda: [])
    monkeypatch.setattr(toss_orders.TossReadClient, "from_settings", lambda: client)
    monkeypatch.setattr(toss_orders.settings, "toss_api_enabled", True)
    monkeypatch.setattr(toss_orders.settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(toss_orders.settings, "toss_approval_hash_mode", "off")
    monkeypatch.setattr(
        toss_orders,
        "_preview_cost_context",
        AsyncMock(
            return_value={
                "estimated_value": "198",
                "estimated_value_currency": "USD",
                "fee": "0",
                "fee_currency": "USD",
                "fx_cost_full_conversion": "0",
                "fx_cost_full_conversion_currency": "KRW",
                "estimated_costs": {},
            }
        ),
    )
    monkeypatch.setattr(
        toss_orders, "_nxt_preflight_context", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        toss_orders,
        "_invalidate_sellable_after_sell_mutation",
        AsyncMock(return_value=None),
    )

    async def loss_cut_retro_lookup(retrospective_id):
        observed_submit_identities.append(get_caller_agent_id())
        return retro

    monkeypatch.setattr(
        validation,
        "_get_retrospective_by_id_for_loss_cut",
        loss_cut_retro_lookup,
    )
    monkeypatch.setattr(validation, "_loss_cut_max_slip_value", lambda: 0.02)
    monkeypatch.setattr(
        mod, "publish_place_time_forecast", AsyncMock(return_value=None)
    )

    notifier = _Notifier()
    first_callback = await handle_callback_update(
        {
            "callback_query": {
                "id": f"callback-{unique}",
                "from": {"id": 777},
                "message": {"chat": {"id": 42}, "message_id": 555},
                "data": f"op:{str(group.proposal_id)[:8]}:{nonce}",
            }
        },
        now=now,
        service_factory=service_factory,
        notifier=notifier,
    )

    assert first_callback["reason"] == "loss_cut_confirmation_required"
    assert client.placed_payloads == []
    confirmation_data = notifier.edited[-1][3]["inline_keyboard"][0][0]["callback_data"]
    second_callback = await handle_callback_update(
        {
            "callback_query": {
                "id": f"confirmation-{unique}",
                "from": {"id": 777},
                "message": {"chat": {"id": 42}, "message_id": 555},
                "data": confirmation_data,
            }
        },
        now=now + timedelta(seconds=30),
        service_factory=service_factory,
        notifier=notifier,
    )

    assert second_callback["reason"] == "approved"
    assert second_callback["results"] == ["submitted_resting"]
    assert observed_submit_identities == [submit_agent_id] * 4
    assert len(client.placed_payloads) == 1
    row = (
        await db_session.execute(
            select(TossLiveOrderLedger).where(
                TossLiveOrderLedger.broker_order_id == broker_order_id
            )
        )
    ).scalar_one()
    assert (row.exit_intent, row.retrospective_id, row.approval_issue_id) == (
        "loss_cut",
        42,
        None,
    )

    partial = _toss_evidence(
        verdict="partial", local_status="partial", filled_qty="0.5"
    )
    partial_result = await _reconcile_with_evidence(mod, row, partial)
    assert partial_result.get("proposal_rung") == {
        "converged": True,
        "proposal_rung_state": "partially_filled",
    }
    await db_session.refresh(row)
    if terminal_status == "filled":
        terminal = _toss_evidence(
            verdict="filled", local_status="filled", filled_qty="2"
        )
    else:
        terminal = _toss_evidence(
            verdict="none", local_status="cancelled", filled_qty="0"
        )
    terminal_result = await _reconcile_with_evidence(mod, row, terminal)
    assert terminal_result.get("proposal_rung") == {
        "converged": True,
        "proposal_rung_state": terminal_status,
    }

    due = await build_retrospective_pending(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        account_mode="toss_live",
        include_cancelled=True,
    )
    assert any(
        item["suggested_correlation_id"] == f"toss_live:{broker_order_id}"
        for item in due["pending"]
    )


async def test_reconcile_filled_buy_books_once(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "_save_order_fill", new=AsyncMock(return_value=101)
        ) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out1 = await mod._reconcile_one_toss_row(row, dry_run=False)
        row2 = await db_session.get(TossLiveOrderLedger, row.id)
        db_session.expunge(row2)
        out2 = await mod._reconcile_one_toss_row(row2, dry_run=False)

    assert out1["action"] == "booked"
    assert out2["action"] == "noop_already_booked"
    assert m_fill.await_count == 1
    assert m_fill.await_args.kwargs["fee"] == 0.06
    assert m_journal.await_count == 1


async def test_reconcile_filled_kr_buy_books_with_equity_kr_instrument_type(db_session):
    """ROB-631: KR equity fills must book with InstrumentType.equity_kr.

    The reconcile path previously hardcoded the invalid literal ``"equity"`` for
    KR rows, which is not an ``InstrumentType`` member, so the buy-journal create
    raised ``ValueError: 'equity' is not a valid InstrumentType`` and the row was
    parked as anomaly/requires_manual_review instead of being booked.
    """
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence
    from app.models.trading import InstrumentType

    row = await _accepted(db_session, side="buy", market="kr")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("3"),
        avg_price=Decimal("85000"),
        commission=Decimal("100"),
        tax=Decimal("50"),
        fee_total=Decimal("150"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "_save_order_fill", new=AsyncMock(return_value=101)
        ) as m_fill,
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    # The instrument type fed to both the trade-fill insert and the buy journal
    # must be a valid InstrumentType member for KR equities, not the bare "equity".
    assert m_fill.await_args.kwargs["instrument_type"] == "equity_kr"
    assert m_journal.await_args.kwargs["market_type"] == "equity_kr"
    # Bind the contract to the real consequence: InstrumentType(...) must not raise.
    assert (
        InstrumentType(m_journal.await_args.kwargs["market_type"])
        is InstrumentType.equity_kr
    )


async def test_reconcile_filled_kr_sell_books_with_equity_kr_instrument_type(
    db_session,
):
    """ROB-631: KR sell fills also pass instrument type to _save_order_fill.

    On the sell path the bad "equity" literal was swallowed by _save_order_fill's
    try/except, silently dropping the trade row. The fill must carry "equity_kr".
    """
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="sell", market="kr")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("3"),
        avg_price=Decimal("90000"),
        commission=Decimal("100"),
        tax=Decimal("50"),
        fee_total=Decimal("150"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 5.0,
        "realized_pnl_basis": "journal_entry",
        "total_pnl_krw": 15000.0,
    }

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "_save_order_fill", new=AsyncMock(return_value=303)
        ) as m_fill,
        patch.object(
            mod, "_close_journals_on_sell", new=AsyncMock(return_value=close_result)
        ),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert m_fill.await_args.kwargs["instrument_type"] == "equity_kr"


async def test_reconcile_cancelled_partial_books_delta_and_terminal(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="partial",
        local_status="cancelled",
        broker_status="CANCELED",
        filled_qty=Decimal("0.5"),
        avg_price=Decimal("190.5"),
        commission=Decimal("0.02"),
        tax=Decimal("0"),
        fee_total=Decimal("0.02"),
        settlement_date=None,
        raw_order={"status": "CANCELED"},
        reason="partial cancelled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=303)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 404}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "cancelled"
    assert refreshed.filled_qty == Decimal("0.5")


async def test_reconcile_pending_is_noop(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
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

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "noop_pending"
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"


async def test_reconcile_impl_lists_only_toss_rows(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod

    await _accepted(db_session)

    with patch.object(
        mod,
        "_reconcile_one_toss_row",
        new=AsyncMock(return_value={"verdict": "pending", "action": "noop_pending"}),
    ):
        out = await mod.toss_reconcile_orders_impl(dry_run=True)

    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["counts"] == {"pending": 1}


async def test_rejected_replacement_reopens_original_order(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    original = await _accepted(db_session)
    replacement = await TossLiveOrderLedgerService(db_session).record_send(
        operation_kind="modify",
        market="us",
        symbol="AAPL",
        side="buy",
        order_type="limit",
        time_in_force="DAY",
        quantity=Decimal("2"),
        price=Decimal("191"),
        order_amount=None,
        currency="USD",
        client_order_id="cid-rejected-replacement",
        broker_order_id="ord-rejected-replacement",
        original_order_id=original.broker_order_id,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
    )
    original.replaced_by_order_id = replacement.broker_order_id
    await db_session.commit()
    db_session.expunge(replacement)

    evidence = TossFillEvidence(
        verdict="pending",
        local_status="replace_rejected",
        broker_status="REPLACE_REJECTED",
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=None,
        tax=None,
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "REPLACE_REJECTED"},
        reason="replace rejected",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()):
        out = await mod._reconcile_one_toss_row(replacement, dry_run=False)

    assert out["action"] == "noop_pending"
    refreshed_original = await db_session.get(TossLiveOrderLedger, original.id)
    refreshed_replacement = await db_session.get(TossLiveOrderLedger, replacement.id)
    assert refreshed_original.status == "accepted"
    assert refreshed_original.replaced_by_order_id is None
    assert refreshed_replacement.status == "replace_rejected"


async def test_reconcile_impl_reports_manual_review_on_error_without_mutating_dry_run(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    row = await _accepted(db_session)
    err = TossApiResponseError(
        TossErrorEnvelope(
            request_id="ray-dry",
            code="non-json-response",
            message="<html>Forbidden dry-run</html>",
            data=None,
        ),
        status_code=403,
    )

    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=err)):
        out = await mod.toss_reconcile_orders_impl(dry_run=True)

    assert out["counts"] == {"anomaly": 1}
    assert out["reconciled"][0]["requires_manual_review"] is True
    assert out["reconciled"][0]["manual_review_reason"].startswith(
        "reconcile failed; operator must verify Toss order detail"
    )
    assert out["reconciled"][0]["error_details"] == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-dry",
        "message": "<html>Forbidden dry-run</html>",
        "data": None,
    }

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "accepted"
    assert refreshed.requires_manual_review is False
    assert refreshed.last_reconcile_error is None


async def test_reconcile_impl_marks_manual_review_on_error_when_not_dry_run(
    db_session,
):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope

    row = await _accepted(db_session)
    err = TossApiResponseError(
        TossErrorEnvelope(
            request_id="ray-apply",
            code="non-json-response",
            message="<html>Forbidden apply</html>",
            data=None,
        ),
        status_code=403,
    )

    with patch.object(mod, "_reconcile_one_toss_row", new=AsyncMock(side_effect=err)):
        out = await mod.toss_reconcile_orders_impl(dry_run=False)

    assert out["counts"] == {"anomaly": 1}
    assert out["reconciled"][0]["action"] == "requires_manual_review"
    assert out["reconciled"][0]["requires_manual_review"] is True

    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.status == "anomaly"
    assert refreshed.requires_manual_review is True
    assert refreshed.manual_review_reason.startswith(
        "reconcile failed; operator must verify Toss order detail"
    )
    assert refreshed.last_reconcile_error == {
        "type": "TossApiResponseError",
        "status_code": 403,
        "code": "non-json-response",
        "request_id": "ray-apply",
        "message": "<html>Forbidden apply</html>",
        "data": None,
    }


async def test_toss_us_buy_reconcile_captures_buy_fx_rate(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="buy")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("100"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    rate=Decimal("1389.33"),
                    fx_rate_source="reconcile_spot",
                    fx_pnl_accuracy="approximate",
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["buy_fx_rate"] == pytest.approx(1389.33)
    assert out["fx_rate_source"] == "reconcile_spot"
    assert m_journal.await_args.kwargs["buy_fx_rate"] == 1389.33


async def test_toss_us_sell_reconcile_surfaces_fx_pnl(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="sell")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("130"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 30.0,
        "realized_pnl_basis": "journal_entry",
        "buy_fx_rate": 1389.33,
        "sell_fx_rate": 1503.19,
        "fx_pnl_krw": 22772.0,
        "security_pnl_usd": 60.0,
        "security_pnl_krw": 90191.4,
        "total_pnl_krw": 112963.4,
        "fx_rate_source": "reconcile_spot",
        "fx_pnl_accuracy": "approximate",
        "fx_unavailable_journal_ids": [],
    }

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    rate=Decimal("1503.19"),
                    fx_rate_source="reconcile_spot",
                    fx_pnl_accuracy="approximate",
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod, "_close_journals_on_sell", new=AsyncMock(return_value=close_result)
        ),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["fx_pnl_krw"] == 22772.0
    assert out["total_pnl_krw"] == 112963.4
    assert out["fx_rate_source"] == "reconcile_spot"
    assert out["fx_pnl_accuracy"] == "approximate"


async def test_toss_us_sell_reconcile_persists_zero_fx_values(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.fx_pnl import FxRateCapture
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="sell")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("100"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )
    close_result = {
        "journals_closed": 1,
        "journals_kept": 0,
        "closed_ids": [77],
        "total_pnl_pct": 0.0,
        "realized_pnl_basis": "journal_entry",
        "buy_fx_rate": 1500.0,
        "sell_fx_rate": 1500.0,
        "fx_pnl_krw": 0.0,
        "security_pnl_usd": 0.0,
        "security_pnl_krw": 0.0,
        "total_pnl_krw": 0.0,
        "fx_rate_source": "reconcile_spot",
        "fx_pnl_accuracy": "approximate",
        "fx_unavailable_journal_ids": [],
    }

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod,
            "capture_reconcile_spot_fx",
            new=AsyncMock(
                return_value=FxRateCapture(
                    rate=Decimal("1500"),
                    fx_rate_source="reconcile_spot",
                    fx_pnl_accuracy="approximate",
                )
            ),
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod, "_close_journals_on_sell", new=AsyncMock(return_value=close_result)
        ),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["fx_pnl_krw"] == 0.0
    refreshed = await db_session.get(TossLiveOrderLedger, row.id)
    assert refreshed.fx_pnl_krw == Decimal("0.0000")
    assert refreshed.security_pnl_usd == Decimal("0.0000")
    assert refreshed.total_pnl_krw == Decimal("0.0000")


async def test_reconcile_booked_fill_notifies_when_enabled(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is True
    notifier.notify_fill.assert_awaited_once()
    order = notifier.notify_fill.await_args.args[0]
    assert order.account == "toss"
    assert order.market_type == "us"
    assert order.currency == "USD"
    assert order.filled_qty == 2
    assert notifier.notify_fill.await_args.kwargs["enrichment"] is None
    assert notifier.notify_fill.await_args.kwargs["detail_url"].endswith(
        "/invest/stocks/us/AAPL"
    )


async def test_reconcile_booked_fill_skips_notify_when_gate_disabled(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_not_awaited()


async def test_reconcile_booked_fill_skips_notify_below_threshold(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(notify_fill=AsyncMock(return_value=True))
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "is_fill_notifiable", return_value=False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_not_awaited()


async def test_reconcile_booked_fill_notification_failure_is_fail_open(db_session):
    from types import SimpleNamespace

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session)
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("2"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    notifier = SimpleNamespace(
        notify_fill=AsyncMock(side_effect=RuntimeError("discord down"))
    )
    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", True),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(
            mod, "capture_reconcile_spot_fx", new=AsyncMock(return_value=None)
        ),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
        patch.object(mod, "get_trade_notifier", return_value=notifier),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["fill_notified"] is False
    notifier.notify_fill.assert_awaited_once()


async def test_reconcile_buy_journal_backfills_correlation_id(db_session):
    """ROB-714: reconcile-time buy journal must carry the ledger row's
    correlation_id. Drives the REAL _reconcile_one_toss_row (KR path)."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await TossLiveOrderLedgerService(db_session).record_send(
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
        client_order_id="cid-corr-kr",
        broker_order_id="ord-corr-kr",
        original_order_id=None,
        status="accepted",
        broker_status=None,
        response_code="0",
        response_message=None,
        raw_response={},
        thesis="t",
        strategy="s",
        correlation_id="live:toss_live:reconcileKR",
    )
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("3"),
        avg_price=Decimal("85100"),
        commission=Decimal("0"),
        tax=Decimal("0"),
        fee_total=Decimal("0"),
        settlement_date=None,
        raw_order={"status": "FILLED"},
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ) as m_journal,
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        await mod._reconcile_one_toss_row(row, dry_run=False)

    m_journal.assert_awaited_once()
    assert m_journal.await_args.kwargs["correlation_id"] == "live:toss_live:reconcileKR"


async def test_reconcile_books_toss_fill_into_execution_ledger(db_session):
    from app.mcp_server.tooling import toss_live_ledger as mod
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence

    row = await _accepted(db_session, side="buy", market="kr")
    evidence = TossFillEvidence(
        verdict="filled",
        local_status="filled",
        broker_status="FILLED",
        filled_qty=Decimal("3"),
        avg_price=Decimal("85000"),
        commission=Decimal("100"),
        tax=Decimal("50"),
        fee_total=Decimal("150"),
        settlement_date=None,
        raw_order={
            "orderId": row.broker_order_id,
            "orderedAt": "2026-07-07T00:30:00Z",
            "status": "FILLED",
            "execution": {"filledQuantity": "3", "averageFilledPrice": "85000"},
        },
        reason="filled",
    )

    class _Adapter:
        fetch_evidence = AsyncMock(return_value=evidence)

    with (
        patch.object(mod.settings, "toss_fill_notify_enabled", False),
        patch.object(mod, "TossEvidenceAdapter", return_value=_Adapter()),
        patch.object(mod, "_save_order_fill", new=AsyncMock(return_value=101)),
        patch.object(
            mod,
            "_create_trade_journal_for_buy",
            new=AsyncMock(return_value={"journal_created": True, "journal_id": 202}),
        ),
        patch.object(mod, "_link_journal_to_fill", new=AsyncMock()),
    ):
        out = await mod._reconcile_one_toss_row(row, dry_run=False)

    assert out["action"] == "booked"
    assert out["execution_ledger"]["status"] == "inserted"
    assert out["execution_ledger"]["id"] > 0


def test_toss_execution_ledger_fill_seq_changes_by_delta():
    from app.mcp_server.tooling.toss_live_evidence import TossFillEvidence
    from app.services.toss_execution_ledger import build_toss_execution_ledger_upsert

    row = type(
        "Row",
        (),
        {
            "id": 1,
            "market": "us",
            "symbol": "AAPL",
            "side": "buy",
            "broker_order_id": "ord-partial",
            "currency": "USD",
            "correlation_id": "corr-1",
        },
    )()
    evidence = TossFillEvidence(
        verdict="partial",
        local_status="partial",
        broker_status="PARTIAL_FILLED",
        filled_qty=Decimal("1.5"),
        avg_price=Decimal("191.25"),
        commission=Decimal("0.05"),
        tax=Decimal("0.01"),
        fee_total=Decimal("0.06"),
        settlement_date=None,
        raw_order={"orderedAt": "2026-07-07T00:30:00Z", "execution": {}},
        reason="partial",
    )

    first = build_toss_execution_ledger_upsert(
        row,
        evidence,
        previous_filled_qty=Decimal("0"),
        delta=Decimal("1"),
        avg_price=Decimal("191.25"),
    )
    second = build_toss_execution_ledger_upsert(
        row,
        evidence,
        previous_filled_qty=Decimal("1"),
        delta=Decimal("0.5"),
        avg_price=Decimal("191.25"),
    )

    assert first.broker == "toss"
    assert first.account_mode == "live"
    assert first.source == "reconciler"
    assert first.venue == "toss_us"
    assert first.filled_qty == Decimal("1")
    assert second.filled_qty == Decimal("0.5")
    assert first.fill_seq != second.fill_seq
