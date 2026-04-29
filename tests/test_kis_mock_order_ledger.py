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
        "_check_daily_order_limit",
        AsyncMock(return_value=True),
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
        "_check_daily_order_limit",
        AsyncMock(return_value=True),
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
        "_check_daily_order_limit",
        AsyncMock(return_value=True),
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
async def test_kis_live_path_unchanged_calls_save_order_fill(monkeypatch):
    from app.mcp_server.tooling import kis_mock_ledger, order_execution, order_journal
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
        "_check_daily_order_limit",
        AsyncMock(return_value=True),
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
    save_fill.assert_awaited_once()
    save_ledger.assert_not_awaited()
