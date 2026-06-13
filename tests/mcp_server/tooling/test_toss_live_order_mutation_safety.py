"""ROB-545 — Toss live order mutation-safety regressions.

These exercise the *real* ledger path (no fake on record_toss_place_order /
record_send) so the idempotency + UNIQUE behaviour and the error-response
order_id preservation are validated end-to-end before
TOSS_LIVE_ORDER_MUTATIONS_ENABLED is flipped.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

import app.mcp_server.tooling.orders_toss_variants as otv
from app.models.review import TossLiveOrderLedger

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    await db_session.execute(delete(TossLiveOrderLedger))
    await db_session.commit()
    yield


@pytest.fixture(autouse=True)
def _patch_session_factory(db_session):
    """Route every ledger session through the test db_session (real ledger)."""
    from app.mcp_server.tooling import toss_live_ledger

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = db_session
    mock_cm.__aexit__.return_value = None

    with patch.object(
        toss_live_ledger, "_order_session_factory", return_value=lambda: mock_cm
    ):
        yield


@pytest.fixture(autouse=True)
def _enable_toss(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])


class _FakeTossClient:
    """Minimal Toss client: US symbol skips the warnings guard, no pending
    orders, and place_order returns a controllable sequence of order ids."""

    def __init__(
        self,
        order_ids=None,
        place_error: Exception | None = None,
        mutate_error: Exception | None = None,
    ):
        self._order_ids = iter(order_ids or [])
        self._place_error = place_error
        self._mutate_error = mutate_error
        self.place_calls: list[dict] = []

    async def aclose(self):
        return None

    async def list_orders(self, *, status, symbol=None, cursor=None, **kwargs):
        return SimpleNamespace(orders=[], next_cursor=None, has_next=False)

    async def get_order(self, order_id):
        from decimal import Decimal

        return SimpleNamespace(
            order_id=order_id,
            symbol="AAPL",
            side="buy",
            order_type="limit",
            time_in_force="DAY",
            price=Decimal("150.0"),
            quantity=Decimal("10"),
            order_amount=None,
            currency="USD",
        )

    async def modify_order(self, order_id, payload):
        if self._mutate_error is not None:
            raise self._mutate_error
        return SimpleNamespace(order_id=next(self._order_ids))

    async def cancel_order(self, order_id):
        if self._mutate_error is not None:
            raise self._mutate_error
        return SimpleNamespace(order_id=next(self._order_ids))

    async def place_order(self, payload):
        self.place_calls.append(payload)
        if self._place_error is not None:
            raise self._place_error
        return SimpleNamespace(
            order_id=next(self._order_ids),
            client_order_id=payload.get("clientOrderId"),
        )


def _bind_client(monkeypatch, client: _FakeTossClient) -> None:
    monkeypatch.setattr(otv.TossReadClient, "from_settings", lambda *a, **k: client)


async def _place(**overrides):
    kwargs = {
        "symbol": "AAPL",
        "side": "buy",
        "order_type": "limit",
        "quantity": "1",
        "price": "190",
        "order_amount": None,
        "market": "us",
        "time_in_force": "DAY",
        "dry_run": False,
        "confirm": True,
        "confirm_high_value_order": False,
        "reason": "ROB-545 test",
        "exit_reason": None,
        "thesis": None,
        "strategy": None,
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": None,
        "indicators_snapshot": None,
        "report_item_uuid": None,
        "account_mode": "toss_live",
        "account_type": None,
        "client_order_id_override": "rob545cid0001",
    }
    kwargs.update(overrides)
    return await otv._toss_place_order_impl(**kwargs)


async def test_idempotent_retry_replays_single_ledger_row(db_session, monkeypatch):
    # Toss is idempotent: same clientOrderId -> same orderId on retry.
    _bind_client(monkeypatch, _FakeTossClient(order_ids=["ord-1", "ord-1"]))

    first = await _place()
    second = await _place()

    assert first["success"] is True
    assert second["success"] is True
    assert first["order_id"] == "ord-1"
    assert second["order_id"] == "ord-1"
    assert first["ledger_id"] == second["ledger_id"]

    rows = (await db_session.execute(select(TossLiveOrderLedger))).scalars().all()
    assert len(rows) == 1


async def test_idempotency_anomaly_surfaces_order_id_for_cancel(
    db_session, monkeypatch
):
    # Genuine anomaly: same clientOrderId, Toss returns a *different* orderId.
    _bind_client(monkeypatch, _FakeTossClient(order_ids=["ord-1", "ord-2"]))

    first = await _place()
    second = await _place()

    assert first["success"] is True and first["order_id"] == "ord-1"
    # The retry must fail (anomaly) yet still surface the new live order id so
    # the smoke's finally-cancel can cancel the duplicate.
    assert second["success"] is False
    assert second["order_id"] == "ord-2"
    assert second["client_order_id"] is not None

    # Only the first order is recorded; the anomalous duplicate is NOT silently
    # written under a conflicting client_order_id.
    rows = (await db_session.execute(select(TossLiveOrderLedger))).scalars().all()
    assert len(rows) == 1


async def test_place_broker_error_response_includes_client_order_id(monkeypatch):
    _bind_client(
        monkeypatch,
        _FakeTossClient(place_error=TimeoutError("broker accepted but timed out")),
    )

    res = await _place()

    assert res["success"] is False
    assert res["mutation_sent"] is True
    # The clientOrderId we sent must be in the response so a retry can reuse the
    # same idempotency key instead of minting a new one.
    assert res["client_order_id"] == "rob545cid0001"


async def test_place_post_success_db_failure_preserves_order_id(monkeypatch):
    _bind_client(monkeypatch, _FakeTossClient(order_ids=["ord-1"]))

    async def _boom(**kwargs):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(otv, "record_toss_place_order", _boom)

    res = await _place()

    assert res["success"] is False
    assert res["mutation_sent"] is True
    # POST succeeded -> the broker order id must not be lost when the DB write
    # fails, otherwise the accepted order can never be reconciled or cancelled.
    assert res["order_id"] == "ord-1"
    assert res["client_order_id"] is not None


async def test_modify_broker_error_includes_original_order_id(monkeypatch):
    _bind_client(
        monkeypatch,
        _FakeTossClient(mutate_error=TimeoutError("modify accepted but timed out")),
    )

    res = await otv.toss_modify_order(
        order_id="orig-ord-123",
        new_price="155.0",
        market="us",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert res["mutation_sent"] is True
    assert res["original_order_id"] == "orig-ord-123"


async def test_modify_post_success_db_failure_preserves_replacement_id(monkeypatch):
    _bind_client(monkeypatch, _FakeTossClient(order_ids=["mod-ord-456"]))

    async def _boom(**kwargs):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(otv, "record_toss_replacement_order", _boom)

    res = await otv.toss_modify_order(
        order_id="orig-ord-123",
        new_price="155.0",
        market="us",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert res["mutation_sent"] is True
    assert res["original_order_id"] == "orig-ord-123"
    # modify succeeded at the broker -> the new replacement order id must survive.
    assert res["replacement_order_id"] == "mod-ord-456"


async def test_cancel_broker_error_includes_original_order_id(monkeypatch):
    _bind_client(
        monkeypatch,
        _FakeTossClient(mutate_error=TimeoutError("cancel accepted but timed out")),
    )

    res = await otv.toss_cancel_order(
        order_id="orig-ord-123",
        dry_run=False,
        confirm=True,
        account_mode="toss_live",
    )

    assert res["success"] is False
    assert res["mutation_sent"] is True
    assert res["original_order_id"] == "orig-ord-123"
