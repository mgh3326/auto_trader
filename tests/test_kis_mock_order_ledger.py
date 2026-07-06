"""Tests for KIS mock order ledger (ROB-37).

Covers:
- ORM model columns/constraints
- _save_kis_mock_order_ledger helper
- buy/sell execution writes ledger and skips live journal/fill paths
- fail-closed config behaviour
- live-path unchanged regression
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import OrderSendIntent


@pytest_asyncio.fixture
async def clean_kis_live_order_send_intents(db_session):
    """Clear KIS live send reservations around tests that exercise live send.

    The production path commits review.order_send_intents through its own
    session before broker send. Local shared test_db keeps those rows across
    pytest invocations, while the idempotency key is deterministic for the same
    canonical order and trading day.
    """

    async def _delete_intents() -> None:
        await db_session.execute(delete(OrderSendIntent))
        await db_session.commit()

    await _delete_intents()
    yield
    await _delete_intents()


# ---------------------------------------------------------------------------
# Task 1: model shape
# ---------------------------------------------------------------------------


def test_model_columns_and_constraints():
    from app.models.review import KISMockOrderLedger

    cols = {c.name for c in KISMockOrderLedger.__table__.columns}
    assert {
        "id",
        "trade_date",
        "symbol",
        "instrument_type",
        "side",
        "order_type",
        "quantity",
        "price",
        "amount",
        "fee",
        "currency",
        "order_no",
        "order_time",
        "krx_fwdg_ord_orgno",
        "account_mode",
        "broker",
        "status",
        "response_code",
        "response_message",
        "raw_response",
        "reason",
        "thesis",
        "strategy",
        "notes",
        "created_at",
        # ROB-102 additive columns
        "lifecycle_state",
        "holdings_baseline_qty",
        "reconcile_attempts",
        "reconciled_at",
        "last_reconcile_detail",
    } <= cols
    assert KISMockOrderLedger.__table__.schema == "review"
    # Naming convention: ck_%(table_name)s_%(constraint_name)s
    constraint_names = {c.name for c in KISMockOrderLedger.__table__.constraints}
    assert "uq_kis_mock_ledger_order_no" in constraint_names
    assert any(
        "kis_mock_ledger_account_mode_kis_mock" in (n or "") for n in constraint_names
    )
    assert any("kis_mock_ledger_broker_kis" in (n or "") for n in constraint_names)
    assert any("kis_mock_ledger_status_allowed" in (n or "") for n in constraint_names)
    assert any(
        "kis_mock_ledger_lifecycle_state_allowed" in (n or "") for n in constraint_names
    )


# ---------------------------------------------------------------------------
# Task 3: helper insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_helper_inserts_row(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    captured: dict = {}

    class FakeResult:
        inserted_primary_key = (123,)

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def execute(self, stmt):
            captured["stmt"] = stmt
            return FakeResult()

        async def commit(self):
            pass

    def fake_factory():
        return lambda: FakeDB()

    monkeypatch.setattr(kis_mock_ledger, "_order_session_factory", fake_factory)

    ledger_id = await kis_mock_ledger._save_kis_mock_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=10,
        price=70000,
        amount=700000,
        currency="KRW",
        order_no="0001234567",
        order_time="091500",
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message="정상처리",
        raw_response={"rt_cd": "0", "output": {"ODNO": "0001234567"}},
        reason="t",
        thesis=None,
        strategy=None,
        notes=None,
    )
    assert ledger_id == 123


# ---------------------------------------------------------------------------
# Task 4: buy — writes ledger, skips live paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kis_mock_buy_writes_ledger_and_skips_live(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger, order_execution, order_journal
    from tests._mcp_tooling_support import build_tools

    # Allow KIS mock config through
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda *_, **__: [],
    )

    monkeypatch.setattr(
        order_execution,
        "_execute_order",
        AsyncMock(
            return_value={
                "odno": "0001234567",
                "ord_tmd": "091500",
                "msg": "정상처리",
                "rt_cd": "0",
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_fetch_current_price",
        AsyncMock(return_value=70000.0),
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "side": "buy",
                "order_type": "limit",
                "price": 70000.0,
                "quantity": 10,
                "estimated_value": 700000.0,
                "fee": 0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        order_execution,
        "_record_order_history",
        AsyncMock(),
    )

    save_ledger = AsyncMock(return_value=42)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save_ledger)

    save_fill = AsyncMock()
    create_journal = AsyncMock()
    close_journals = AsyncMock()
    link_journal = AsyncMock()
    monkeypatch.setattr(order_journal, "_save_order_fill", save_fill)
    monkeypatch.setattr(order_journal, "_create_trade_journal_for_buy", create_journal)
    monkeypatch.setattr(order_journal, "_close_journals_on_sell", close_journals)
    monkeypatch.setattr(order_journal, "_link_journal_to_fill", link_journal)

    tools = build_tools()
    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=10,
        price=70000.0,
        dry_run=False,
        account_mode="kis_mock",
    )

    assert result["success"] is True, result
    assert result["account_mode"] == "kis_mock"
    assert result["ledger_id"] == 42
    assert result["order_no"] == "0001234567"
    assert result["order_time"] == "091500"
    save_ledger.assert_awaited_once()
    save_fill.assert_not_awaited()
    create_journal.assert_not_awaited()
    close_journals.assert_not_awaited()
    link_journal.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 4: sell — writes ledger, does NOT close live journals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kis_mock_sell_writes_ledger_and_does_not_close_journals(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger, order_execution, order_journal
    from tests._mcp_tooling_support import build_tools

    # Allow KIS mock config through
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda *_, **__: [],
    )

    monkeypatch.setattr(
        order_execution,
        "_execute_order",
        AsyncMock(
            return_value={
                "odno": "0009999999",
                "ord_tmd": "103000",
                "msg": "정상처리",
                "rt_cd": "0",
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_fetch_current_price",
        AsyncMock(return_value=70000.0),
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "side": "sell",
                "order_type": "limit",
                "price": 70000.0,
                "quantity": 5,
                "estimated_value": 350000.0,
                "fee": 0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        order_execution,
        "_record_order_history",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.order_validation._validate_sell_side",
        AsyncMock(return_value=(5, 70000.0, None)),
    )
    monkeypatch.setattr(
        order_execution,
        "_validate_sell_side",
        AsyncMock(return_value=(5, 70000.0, None)),
    )

    save_ledger = AsyncMock(return_value=99)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save_ledger)

    save_fill = AsyncMock()
    create_journal = AsyncMock()
    close_journals = AsyncMock()
    link_journal = AsyncMock()
    monkeypatch.setattr(order_journal, "_save_order_fill", save_fill)
    monkeypatch.setattr(order_journal, "_create_trade_journal_for_buy", create_journal)
    monkeypatch.setattr(order_journal, "_close_journals_on_sell", close_journals)
    monkeypatch.setattr(order_journal, "_link_journal_to_fill", link_journal)

    tools = build_tools()
    result = await tools["place_order"](
        symbol="005930",
        side="sell",
        order_type="limit",
        quantity=5,
        price=70000.0,
        dry_run=False,
        account_mode="kis_mock",
    )

    assert result["success"] is True, result
    assert result["account_mode"] == "kis_mock"
    assert result["ledger_id"] == 99
    save_ledger.assert_awaited_once()
    save_fill.assert_not_awaited()
    create_journal.assert_not_awaited()
    close_journals.assert_not_awaited()
    link_journal.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 5: fail-closed — missing config blocks broker before call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kis_mock_missing_config_fails_closed_before_broker(monkeypatch):
    from app.mcp_server.tooling import order_execution
    from tests._mcp_tooling_support import build_tools

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda *_, **__: ["KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY"],
    )
    sentinel = AsyncMock(side_effect=AssertionError("must not call broker"))
    monkeypatch.setattr(order_execution, "_execute_order", sentinel)

    tools = build_tools()
    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=1,
        price=70000.0,
        dry_run=False,
        account_mode="kis_mock",
    )
    assert result["success"] is False
    assert "KIS_MOCK_ENABLED" in result["error"]
    assert result["account_mode"] == "kis_mock"
    sentinel.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 5: kis_mock buy does NOT require thesis/strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kis_mock_buy_does_not_require_thesis_strategy(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger, order_execution, order_journal
    from tests._mcp_tooling_support import build_tools

    # Allow KIS mock config through
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda *_, **__: [],
    )

    monkeypatch.setattr(
        order_execution,
        "_execute_order",
        AsyncMock(
            return_value={
                "odno": "0001111111",
                "ord_tmd": "100000",
                "msg": "정상처리",
                "rt_cd": "0",
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_fetch_current_price",
        AsyncMock(return_value=70000.0),
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "side": "buy",
                "order_type": "limit",
                "price": 70000.0,
                "quantity": 3,
                "estimated_value": 210000.0,
                "fee": 0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        order_execution,
        "_record_order_history",
        AsyncMock(),
    )

    save_ledger = AsyncMock(return_value=7)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save_ledger)
    monkeypatch.setattr(order_journal, "_save_order_fill", AsyncMock())

    tools = build_tools()
    # No thesis, no strategy — should succeed for kis_mock
    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=3,
        price=70000.0,
        dry_run=False,
        account_mode="kis_mock",
    )
    assert result["success"] is True, result
    assert result["ledger_id"] == 7
    save_ledger.assert_awaited_once()


# ---------------------------------------------------------------------------
# Task 5: kis_live path unchanged — still calls _save_order_fill, not ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_kis_live_order_send_intents")
async def test_kis_live_kr_path_records_to_live_ledger_not_save_fill(monkeypatch):
    # ROB-395: KR live no longer books a fill/journal at send. It records the
    # order accepted-only to review.kis_live_order_ledger; _save_order_fill must
    # NOT run, and the response carries fill_recorded:false. (US-unchanged is
    # covered separately by test_order_execution_live_routing.)
    from app.mcp_server.tooling import (
        kis_live_ledger,
        kis_mock_ledger,
        order_execution,
        order_journal,
    )
    from tests._mcp_tooling_support import build_tools

    monkeypatch.setattr(
        order_execution,
        "_execute_order",
        AsyncMock(
            return_value={
                "odno": "9990000001",
                "ord_tmd": "090000",
                "msg": "정상처리",
                "rt_cd": "0",
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_fetch_current_price",
        AsyncMock(return_value=70000.0),
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "side": "buy",
                "order_type": "limit",
                "price": 70000.0,
                "quantity": 1,
                "estimated_value": 70000.0,
                "fee": 0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        order_execution,
        "_record_order_history",
        AsyncMock(),
    )

    save_fill = AsyncMock(return_value=55)
    create_journal = AsyncMock(
        return_value={
            "journal_created": True,
            "journal_id": 10,
            "journal_status": "active",
        }
    )
    link_journal = AsyncMock()
    # order_execution imports these names directly, so patch on order_execution too
    monkeypatch.setattr(order_execution, "_save_order_fill", save_fill)
    monkeypatch.setattr(order_journal, "_save_order_fill", save_fill)
    monkeypatch.setattr(
        order_execution, "_create_trade_journal_for_buy", create_journal
    )
    monkeypatch.setattr(order_journal, "_create_trade_journal_for_buy", create_journal)
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", link_journal)
    monkeypatch.setattr(order_journal, "_link_journal_to_fill", link_journal)

    save_ledger = AsyncMock(return_value=None)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save_ledger)

    # ROB-395: KR live writes to the live ledger instead of booking a fill.
    save_live_ledger = AsyncMock(return_value=77)
    monkeypatch.setattr(
        kis_live_ledger, "_save_kis_live_order_ledger", save_live_ledger
    )

    tools = build_tools()
    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=1,
        price=70000.0,
        dry_run=False,
        account_mode="kis_live",
        thesis="t",
        strategy="s",
    )

    assert result["success"] is True, result
    # accepted-only: no fill/journal booked at send
    save_fill.assert_not_awaited()
    create_journal.assert_not_awaited()
    # recorded to the live ledger; mock ledger untouched
    save_live_ledger.assert_awaited_once()
    save_ledger.assert_not_awaited()
    assert result["fill_recorded"] is False
    assert result["broker_status"] == "accepted"


# ---------------------------------------------------------------------------
# ROB-102: lifecycle mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_to_lifecycle_state_mapping():
    """ROB-102: existing 3-value `status` maps to ROB-100 lifecycle states."""
    from app.mcp_server.tooling.kis_mock_ledger import _status_to_lifecycle_state

    assert _status_to_lifecycle_state("accepted") == "accepted"
    assert _status_to_lifecycle_state("rejected") == "failed"
    assert _status_to_lifecycle_state("unknown") == "anomaly"
    assert _status_to_lifecycle_state(None) == "anomaly"
    assert _status_to_lifecycle_state("garbage") == "anomaly"


@pytest.mark.asyncio
async def test_save_helper_persists_lifecycle_state(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    captured: dict = {}

    class FakeResult:
        inserted_primary_key = (321,)

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def execute(self, stmt):
            captured["stmt"] = stmt
            return FakeResult()

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: FakeDB()
    )

    new_id = await kis_mock_ledger._save_kis_mock_order_ledger(
        symbol="005930",
        instrument_type="equity_kr",
        side="buy",
        order_type="limit",
        quantity=10,
        price=1000,
        amount=10000,
        currency="KRW",
        order_no="MOCK-1",
        order_time=None,
        krx_fwdg_ord_orgno=None,
        status="accepted",
        response_code="0",
        response_message=None,
        raw_response={"rt_cd": "0"},
        reason=None,
        thesis=None,
        strategy=None,
        notes=None,
        lifecycle_state="accepted",
    )
    assert new_id == 321
    # Verify that lifecycle_state is in the insert values
    params = captured["stmt"].compile().params
    assert params["lifecycle_state"] == "accepted"


# ---------------------------------------------------------------------------
# ROB-255: KIS mock DB shadow pending helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_pending_orders_are_formatted_from_lifecycle_rows(monkeypatch):
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace

    from app.mcp_server.tooling import kis_mock_ledger

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class FakeSvc:
        def __init__(self, db):
            self.db = db

        async def list_open_orders(self, **kwargs):
            assert kwargs["symbol"] == "005930"
            assert kwargs["instrument_type"] == "equity_kr"
            return [
                SimpleNamespace(
                    id=123,
                    trade_date=datetime(2026, 5, 14, 9, 1, tzinfo=UTC),
                    symbol="005930",
                    instrument_type="equity_kr",
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("2"),
                    price=Decimal("70000"),
                    amount=Decimal("140000"),
                    currency="KRW",
                    order_no="MOCK-255",
                    lifecycle_state="accepted",
                )
            ]

    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: lambda: FakeDB()
    )
    monkeypatch.setattr(kis_mock_ledger, "KISMockLifecycleService", FakeSvc)

    rows = await kis_mock_ledger._list_kis_mock_shadow_pending_orders(
        normalized_symbol="005930", market_type="equity_kr"
    )

    assert rows == [
        {
            "order_id": "MOCK-255",
            "ledger_id": 123,
            "symbol": "005930",
            "market": "kr",
            "instrument_type": "equity_kr",
            "side": "buy",
            "order_type": "limit",
            "status": "pending",
            "lifecycle_state": "accepted",
            "ordered_qty": 2.0,
            "remaining_qty": 2.0,
            "filled_qty": 0.0,
            "ordered_price": 70000.0,
            "amount": 140000.0,
            "currency": "KRW",
            "ordered_at": "2026-05-14T09:01:00+00:00",
            "created_at": "2026-05-14T09:01:00+00:00",
            "source": "kis_mock_ledger_shadow",
            "confidence": "db_shadow_pending",
            "warning": kis_mock_ledger.KIS_MOCK_SHADOW_PENDING_WARNING,
        }
    ]


@pytest.mark.asyncio
async def test_shadow_exposure_unknown_on_db_error(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger

    async def boom(**kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(kis_mock_ledger, "_list_kis_mock_shadow_pending_orders", boom)

    result = await kis_mock_ledger._get_kis_mock_shadow_exposure(
        normalized_symbol="005930", market_type="equity_kr"
    )

    assert result["confidence"] == "unknown"
    assert result["buy_reserved_amount"] == 0.0
    assert result["sell_reserved_quantity"] == 0.0
    assert "db unavailable" in result["error"]


# ---------------------------------------------------------------------------
# ROB-730: place-time provenance spine — mint correlation_id + emit forecast
# ---------------------------------------------------------------------------


def _mock_exec_result(rt_cd: str = "0", odno: str = "0001234567") -> dict:
    return {"odno": odno, "ord_tmd": "091500", "msg": "정상처리", "rt_cd": rt_cd}


def _mock_preview() -> dict:
    return {"price": 70000, "quantity": 10, "estimated_value": 700000}


@pytest.mark.asyncio
async def test_record_mints_correlation_id_and_publishes_forecast(monkeypatch):
    """ROB-730: the tool path passes no correlation_id, so the mock record path
    mints a deterministic namespaced id, stores it on the ledger row, and emits a
    place-time forecast for an accepted buy with a target — mirroring kis_live."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    pub = AsyncMock(return_value="fc-1")
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=_mock_exec_result(),
        reason="t",
        thesis=None,
        strategy=None,
        notes=None,
        target_price=80000.0,
        min_hold_days=5,
    )

    cid = result["correlation_id"]
    assert cid is not None
    assert cid.startswith("live:kis_mock:")
    # stored on the ledger row
    assert save.await_args.kwargs["correlation_id"] == cid
    # forecast published for the accepted buy, tagged for mock provenance
    pub.assert_awaited_once()
    assert pub.await_args.kwargs["correlation_id"] == cid
    assert pub.await_args.kwargs["session_label"] == "kis_mock_place"
    assert pub.await_args.kwargs["created_by"] == "auto_place_mock"
    assert pub.await_args.kwargs["target_price"] == 80000.0


@pytest.mark.asyncio
async def test_record_preserves_explicit_correlation_id(monkeypatch):
    """ROB-730: an explicit correlation_id (ROB-402 scalping entry/exit pairing)
    must be preserved, never overwritten by a freshly minted one."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    pub = AsyncMock(return_value="fc-1")
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=_mock_exec_result(),
        reason="t",
        thesis=None,
        strategy=None,
        notes=None,
        correlation_id="scalp-pair-1",
    )

    assert result["correlation_id"] == "scalp-pair-1"
    assert save.await_args.kwargs["correlation_id"] == "scalp-pair-1"
    assert pub.await_args.kwargs["correlation_id"] == "scalp-pair-1"


@pytest.mark.asyncio
async def test_record_rejected_order_mints_but_does_not_publish(monkeypatch):
    """ROB-730: a rejected order still gets a correlation_id (spine), but no
    place-time forecast is emitted (mirrors live: publish only when accepted)."""
    from app.mcp_server.tooling import kis_mock_ledger

    save = AsyncMock(return_value=5)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save)
    pub = AsyncMock(return_value=None)
    monkeypatch.setattr(kis_mock_ledger, "publish_place_time_forecast", pub)

    result = await kis_mock_ledger._record_kis_mock_order(
        normalized_symbol="005930",
        market_type="equity_kr",
        side="buy",
        order_type="limit",
        dry_run_result=_mock_preview(),
        execution_result=_mock_exec_result(rt_cd="40", odno=""),
        reason="t",
        thesis=None,
        strategy=None,
        notes=None,
        target_price=80000.0,
    )

    assert result["status"] == "rejected"
    assert result["correlation_id"] is not None
    assert save.await_args.kwargs["correlation_id"] == result["correlation_id"]
    pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_place_order_impl_threads_correlation_id(db_session, monkeypatch):
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from app.mcp_server.tooling import order_execution
    from app.models.review import KISMockOrderLedger

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda *_, **__: [],
    )
    monkeypatch.setattr(
        order_execution,
        "_execute_order",
        AsyncMock(
            return_value={
                "odno": "0001234567",
                "ord_tmd": "091500",
                "msg": "정상처리",
                "rt_cd": "0",
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_fetch_current_price",
        AsyncMock(return_value=55000.0),
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "symbol": "005930",
                "side": "buy",
                "order_type": "limit",
                "price": 55000.0,
                "quantity": 10,
                "estimated_value": 550000.0,
                "fee": 0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        order_execution,
        "_record_order_history",
        AsyncMock(),
    )

    result = await order_execution._place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=10,
        price=55000,
        dry_run=False,
        reason="rob402-test",
        is_mock=True,
        correlation_id="corr-rob402",
    )
    assert result["success"] is True, result
    row = (
        await db_session.execute(
            select(KISMockOrderLedger).where(
                KISMockOrderLedger.correlation_id == "corr-rob402"
            )
        )
    ).scalar_one_or_none()
    assert row is not None
