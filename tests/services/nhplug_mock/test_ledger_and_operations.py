"""DB-backed ledger + operations tests against an offline fake NH mock broker.

Covers the evidence-first ledger (hard rule 5), the double gate (hard rule 4)
at the operations layer, limit-only refusal, and the "empty array != no open
orders" rule end to end through reconcile.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.nhplug_mock_order_ledger import NHPlugMockOrderLedger
from app.services.brokers.nhplug.order_evidence import OrderAck
from app.services.nhplug_mock import operations
from app.services.nhplug_mock.ledger_service import (
    NHPlugMockLedgerError,
    NHPlugMockLedgerService,
    ReconcileUpdate,
)
from tests.services.nhplug_mock._fake_broker import MOCK_ACCOUNT, FakeNHMockBroker

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CREDENTIALS = operations.NHPlugMockCredentials(
    app_key="test-key", app_secret="test-secret", account_no=MOCK_ACCOUNT
)


def _order_date() -> str:
    # A unique 8-digit date key isolates each test's rows in the shared table.
    return f"3{uuid.uuid4().int % 10_000_000:07d}"


async def _tokens() -> str:
    return "unit-test-token"


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHPLUG_MOCK_ENABLED", "true")


def _factory(broker: FakeNHMockBroker):
    async def build():
        return await operations.open_verified_client(
            CREDENTIALS, token_provider=_tokens, transport=broker.transport
        )

    return build


async def _rows(
    ledger: NHPlugMockLedgerService, date: str
) -> list[NHPlugMockOrderLedger]:
    return list(await ledger.list_for_date(date))


# --- ledger service --------------------------------------------------------


async def test_ledger_rejects_accepted_without_broker_number(db_session) -> None:
    ledger = NHPlugMockLedgerService(db_session)
    row = await ledger.record_submitting(
        order_date=_order_date(),
        operation_kind="place",
        symbol="005930",
        side="buy",
        quantity=1,
        price=50000,
    )
    assert (row.broker, row.account_mode, row.venue, row.order_type) == (
        "nhplug",
        "nhplug_mock",
        "KRX",
        "limit",
    )
    with pytest.raises(NHPlugMockLedgerError):
        await ledger.record_ack(row.id, OrderAck("accepted", None, "00000", None))


async def test_fill_and_terminal_states_need_verified_evidence(db_session) -> None:
    ledger = NHPlugMockLedgerService(db_session)
    row = await ledger.record_submitting(
        order_date=_order_date(),
        operation_kind="place",
        symbol="005930",
        side="buy",
        quantity=1,
        price=50000,
    )
    await ledger.record_ack(row.id, OrderAck("accepted", "1000123", "00000", None))
    with pytest.raises(NHPlugMockLedgerError, match="evidence"):
        await ledger.apply_reconcile(
            row.id, ReconcileUpdate(reconcile_state="unknown", status="filled")
        )
    with pytest.raises(NHPlugMockLedgerError, match="evidence"):
        await ledger.apply_reconcile(
            row.id,
            ReconcileUpdate(reconcile_state="verified", status="filled", evidence=None),
        )
    with pytest.raises(NHPlugMockLedgerError, match="evidence"):
        await ledger.apply_reconcile(
            row.id, ReconcileUpdate(reconcile_state="unknown", filled_qty=1)
        )
    filled = await ledger.apply_reconcile(
        row.id,
        ReconcileUpdate(
            reconcile_state="verified",
            status="filled",
            filled_qty=1,
            avg_fill_price=Decimal(50000),
            evidence={"order_no": "1000123"},
        ),
    )
    assert filled.status == "filled" and filled.filled_qty == 1
    with pytest.raises(NHPlugMockLedgerError, match="immutable"):
        await ledger.apply_reconcile(
            row.id, ReconcileUpdate(reconcile_state="unknown", status="open")
        )
    with pytest.raises(NHPlugMockLedgerError, match="immutable"):
        await ledger.apply_reconcile(row.id, ReconcileUpdate(reconcile_state="unknown"))


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("order_type", "market"),
        ("venue", "NXT"),
        ("account_mode", "kis_mock"),
        ("broker", "kiwoom"),
        ("symbol", "AAPL"),
        ("status", "sent"),
    ),
)
async def test_table_checks_pin_the_stage2_scope(
    db_session, column: str, value: Any
) -> None:
    row = NHPlugMockOrderLedger(
        client_request_id=uuid.uuid4(),
        order_date=_order_date(),
        operation_kind="place",
        symbol="005930",
        side="buy",
        quantity=Decimal(1),
        price=Decimal(50000),
        status="submitting",
    )
    setattr(row, column, value)
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_table_check_requires_broker_number_for_accepted(db_session) -> None:
    db_session.add(
        NHPlugMockOrderLedger(
            client_request_id=uuid.uuid4(),
            order_date=_order_date(),
            operation_kind="place",
            symbol="005930",
            side="buy",
            quantity=Decimal(1),
            price=Decimal(50000),
            status="filled",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# --- operations: gates and limit-only ---------------------------------------


async def test_dry_run_place_is_offline_and_writes_nothing(db_session, armed) -> None:
    broker = FakeNHMockBroker()
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    result = await operations.place_limit_order(
        client_factory=_factory(broker),
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        order_date=date,
    )
    assert result["dry_run"] is True and result["dispatch_started"] is False
    assert broker.requests == []
    assert await _rows(ledger, date) == []


@pytest.mark.parametrize("confirm", (False, None, 1, "true"))
async def test_unconfirmed_send_is_refused_before_any_network(
    db_session, armed, confirm: Any
) -> None:
    broker = FakeNHMockBroker()
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    result = await operations.place_limit_order(
        client_factory=_factory(broker),
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=confirm,
        order_date=date,
    )
    assert result["error_code"] == "confirm_required"
    assert broker.requests == []
    assert await _rows(ledger, date) == []


@pytest.mark.parametrize(
    ("order_type", "price", "code"),
    (
        ("market", 50000, "limit_orders_only"),
        ("MARKET", None, "limit_orders_only"),
        ("best_limit", 50000, "limit_orders_only"),
        ("stop_limit", 50000, "limit_orders_only"),
        ("limit", None, "limit_price_required"),
    ),
)
async def test_market_and_non_limit_orders_are_refused_clearly(
    db_session, armed, order_type: str, price: Any, code: str
) -> None:
    broker = FakeNHMockBroker()
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    result = await operations.place_limit_order(
        client_factory=_factory(broker),
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=price,
        order_type=order_type,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert result["success"] is False
    assert result["error_code"] == code
    assert "market" in result["error"].lower()
    assert broker.requests == []
    assert await _rows(ledger, date) == []


async def test_gate_off_refuses_after_confirm_without_network(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NHPLUG_MOCK_ENABLED", raising=False)
    broker = FakeNHMockBroker()
    ledger = NHPlugMockLedgerService(db_session)
    result = await operations.place_limit_order(
        client_factory=_factory(broker),
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=_order_date(),
    )
    assert result["error_code"] == "account_verification_failed"
    assert broker.requests == []


async def test_no_ledger_row_means_no_order(db_session, armed) -> None:
    broker = FakeNHMockBroker()

    class BrokenLedger(NHPlugMockLedgerService):
        async def record_submitting(self, **kwargs: Any):  # type: ignore[override]
            raise RuntimeError("db down")

    result = await operations.place_limit_order(
        client_factory=_factory(broker),
        ledger=BrokenLedger(db_session),
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=_order_date(),
    )
    assert result["error_code"] == "ledger_unavailable"
    assert broker.order_paths() == []


async def test_uncertain_dispatch_is_recorded_then_bound_by_reconcile(
    db_session, armed
) -> None:
    broker = FakeNHMockBroker(timeout_after_create=True)
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    result = await operations.place_limit_order(
        client_factory=_factory(broker),
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert result["status"] == "acceptance_uncertain"
    assert result["retry_allowed"] is False and result["reconcile_required"] is True
    [row] = await _rows(ledger, date)
    assert row.status == "acceptance_uncertain" and row.requires_manual_review

    open_orders = await operations.get_open_orders(
        await _factory(broker)(), order_date=date, ledger=ledger
    )
    # The broker shows the order: positive evidence wins over the ledger gap.
    assert open_orders["open_orders_state"] == "present"
    assert "ledger_has_order_with_unknown_broker_number" in open_orders["reasons"]

    reconciled = await operations.reconcile_orders(
        await _factory(broker)(), ledger, order_date=date, dry_run=False
    )
    assert reconciled["results"][0]["status"] == "accepted"
    await db_session.refresh(row)
    assert row.status == "accepted" and row.broker_order_id == str(
        broker.orders[0].order_no
    )


# --- end-to-end round trip (the smoke procedure, offline) -------------------


async def test_offline_round_trip_place_query_modify_cancel_reconcile(
    db_session, armed
) -> None:
    broker = FakeNHMockBroker()
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    factory = _factory(broker)
    client = await factory()

    before = await operations.get_open_orders(client, order_date=date, ledger=ledger)
    assert before["open_orders_state"] == "none_confirmed"

    placed = await operations.place_limit_order(
        client_factory=factory,
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert placed["status"] == "accepted"
    first = placed["broker_order_id"]

    after_place = await operations.get_open_orders(
        client, order_date=date, ledger=ledger
    )
    assert after_place["open_orders_state"] == "present"
    assert [o["order_no"] for o in after_place["open_orders"]] == [first]

    modified = await operations.modify_limit_order(
        client_factory=factory,
        ledger=ledger,
        order_id=first,
        symbol="005930",
        new_price=49500,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert modified["status"] == "accepted" and modified["full_quantity"] is True
    second = modified["broker_order_id"]

    cancelled = await operations.cancel_order(
        client_factory=factory,
        ledger=ledger,
        order_id=second,
        symbol="005930",
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert cancelled["status"] == "accepted"
    assert cancelled["original_verified_by"] == "broker_listing"

    reconciled = await operations.reconcile_orders(
        client, ledger, order_date=date, dry_run=False
    )
    assert reconciled["success"] is True and reconciled["unresolved"] == 0
    statuses = {
        (row.operation_kind, row.status, row.reconcile_state)
        for row in await _rows(ledger, date)
    }
    assert statuses == {
        ("place", "modified", "verified"),
        ("modify", "cancelled", "verified"),
        ("cancel", "confirmed", "verified"),
    }

    final = await operations.get_open_orders(client, order_date=date, ledger=ledger)
    assert final["open_orders_state"] == "none_confirmed"
    assert all(source["complete"] for source in final["sources"])
    assert "CUSTOMER_NAME_MUST_NOT_LEAK" not in str(final)
    assert MOCK_ACCOUNT not in str(final) + str(reconciled) + str(placed)


async def test_evidence_first_fill_is_booked_only_by_reconcile(
    db_session, armed
) -> None:
    broker = FakeNHMockBroker(fill_on_place=True)
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    placed = await operations.place_limit_order(
        client_factory=_factory(broker),
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    [row] = await _rows(ledger, date)
    assert placed["status"] == "accepted"
    assert row.status == "accepted" and row.filled_qty is None
    await operations.reconcile_orders(
        await _factory(broker)(), ledger, order_date=date, dry_run=False
    )
    await db_session.refresh(row)
    assert row.status == "filled" and row.filled_qty == 1
    assert row.avg_fill_price == Decimal(50000)
    assert row.evidence["order_no"] == placed["broker_order_id"]


# --- "empty array != no open orders" end to end ----------------------------


async def test_kt00009_style_empty_open_scope_never_closes_a_resting_order(
    db_session, armed
) -> None:
    broker = FakeNHMockBroker(open_scope_always_empty=True)
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    factory = _factory(broker)
    placed = await operations.place_limit_order(
        client_factory=factory,
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    client = await factory()
    result = await operations.get_open_orders(client, order_date=date, ledger=ledger)
    assert result["open_orders_state"] == "present"
    assert "open_order_sources_disagree" in result["reasons"]

    reconciled = await operations.reconcile_orders(
        client, ledger, order_date=date, dry_run=False
    )
    assert reconciled["results"][0]["reconcile_state"] == "source_disagreement"
    [row] = await _rows(ledger, date)
    assert row.status == "accepted"
    assert row.broker_order_id == placed["broker_order_id"]


@pytest.mark.parametrize("knob", ("open_scope_error", "all_scope_error"))
async def test_error_shaped_listing_reports_unknown_and_closes_nothing(
    db_session, armed, knob: str
) -> None:
    broker = FakeNHMockBroker()
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    factory = _factory(broker)
    await operations.place_limit_order(
        client_factory=factory,
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    broker.orders[0].open_qty = 0
    broker.orders[0].cancelled = 1  # cancelled at the broker behind our back
    setattr(broker, knob, True)
    client = await factory()
    result = await operations.get_open_orders(client, order_date=date, ledger=ledger)
    assert result["success"] is False
    assert result["open_orders_state"] == "unknown"
    reconciled = await operations.reconcile_orders(
        client, ledger, order_date=date, dry_run=False
    )
    assert reconciled["success"] is False
    [row] = await _rows(ledger, date)
    assert row.status == "accepted"
    assert row.reconcile_state == "unknown"


async def test_modify_refuses_without_a_complete_listing(db_session, armed) -> None:
    broker = FakeNHMockBroker(all_scope_error=True)
    ledger = NHPlugMockLedgerService(db_session)
    result = await operations.modify_limit_order(
        client_factory=_factory(broker),
        ledger=ledger,
        order_id="1000101",
        symbol="005930",
        new_price=49500,
        dry_run=False,
        confirm=True,
        order_date=_order_date(),
    )
    assert result["error_code"] == "original_order_unverified"
    assert broker.order_paths() == []


async def test_cancel_falls_back_to_own_live_ledger_row_only(db_session, armed) -> None:
    broker = FakeNHMockBroker()
    ledger = NHPlugMockLedgerService(db_session)
    date = _order_date()
    factory = _factory(broker)
    placed = await operations.place_limit_order(
        client_factory=factory,
        ledger=ledger,
        side="buy",
        symbol="005930",
        quantity=1,
        price=50000,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    broker.all_scope_error = True
    unknown_order = await operations.cancel_order(
        client_factory=factory,
        ledger=ledger,
        order_id="999999",
        symbol="005930",
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert unknown_order["error_code"] == "original_order_unverified"
    partial = await operations.cancel_order(
        client_factory=factory,
        ledger=ledger,
        order_id=placed["broker_order_id"],
        symbol="005930",
        cancel_quantity=1,
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert partial["error_code"] == "original_order_unverified"
    full = await operations.cancel_order(
        client_factory=factory,
        ledger=ledger,
        order_id=placed["broker_order_id"],
        symbol="005930",
        dry_run=False,
        confirm=True,
        order_date=date,
    )
    assert full["status"] == "accepted"
    assert full["original_verified_by"] == "ledger_row"


async def test_positions_read_and_history(db_session, armed) -> None:
    broker = FakeNHMockBroker()
    client = await _factory(broker)()
    positions = await operations.get_positions(client)
    assert positions["positions_state"] == "present"
    assert positions["positions"][0]["quantity"] == "3"
    assert positions["cash"]["orderable_krw"] == 9_950_000
    assert MOCK_ACCOUNT not in str(positions)
    history = await operations.get_order_history(
        client, order_date=_order_date(), scope="all"
    )
    assert history["success"] is True and history["orders"] == []
