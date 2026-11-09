"""Tests for the typed KIS order tool variants (kis_live_* / kis_mock_*).

Verifies:
- kis_mock_* tools fail closed when KIS mock config is missing.
- kis_mock_* tools pass is_mock=True to underlying impls.
- kis_mock_* tools reject account_mode='kis_live' argument.
- kis_live_* tools pass is_mock=False to underlying impls.
- kis_live_* tools reject account_mode='kis_mock' argument.
"""

from __future__ import annotations

import asyncio
import datetime
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

import app.mcp_server.tooling.orders_kis_variants as orders_kis_variants
from app.mcp_server.tooling import (
    kis_mock_ledger,
    order_execution,
    orders_history,
    orders_modify_cancel,
    orders_registration,
)
from app.mcp_server.tooling.orders_kis_variants import (
    register_kis_live_order_tools,
    register_kis_mock_order_tools,
)
from app.services.brokers import edge_client
from app.services.brokers.toss.dto import TossWarningInfo
from app.services.kis_mock_runner import singleton
from app.services.order_send_intent_service import DuplicateOrderIntent
from tests._mcp_tooling_support import DummyMCP
from tests.services.mock_integration.test_kis_coordination_adapter import (
    _domestic_client,
    _execute_kwargs,
)


def _build_live_mcp() -> DummyMCP:
    mcp = DummyMCP()
    register_kis_live_order_tools(cast(Any, mcp))
    return mcp


def _build_mock_mcp() -> DummyMCP:
    mcp = DummyMCP()
    register_kis_mock_order_tools(cast(Any, mcp))
    return mcp


# ---------------------------------------------------------------------------
# Helper: patch validate_kis_mock_config to simulate missing config
# ---------------------------------------------------------------------------


def _patch_mock_config_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing: list[str] | None = None,
) -> None:
    names = missing or ["KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY"]
    monkeypatch.setattr(
        orders_kis_variants,
        "validate_kis_mock_config",
        lambda: names,
    )


def _patch_mock_config_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orders_kis_variants,
        "validate_kis_mock_config",
        lambda: [],
    )


# ===========================================================================
# kis_mock_place_order
# ===========================================================================


class TestKisMockPlaceOrder:
    @pytest.mark.asyncio
    async def test_fails_closed_when_config_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_missing(
            monkeypatch, ["KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY"]
        )
        result = await mcp.tools["kis_mock_place_order"](
            symbol="005930", side="buy", price=50000.0, dry_run=False, confirm=True
        )
        assert result["success"] is False
        assert result["account_mode"] == "kis_mock"
        assert "KIS_MOCK_ENABLED" in result["error"]
        assert "KIS_MOCK_APP_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_passes_is_mock_true_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        captured: dict[str, Any] = {}

        async def fake_place_order_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "dry_run": True}

        monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_order_impl)

        await mcp.tools["kis_mock_place_order"](
            symbol="005930", side="buy", price=50000.0, dry_run=False, confirm=True
        )
        assert captured.get("is_mock") is True

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_live_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        result = await mcp.tools["kis_mock_place_order"](
            symbol="005930",
            side="buy",
            price=50000.0,
            account_mode="kis_live",
            dry_run=False,
            confirm=True,
        )
        assert result["success"] is False
        assert "kis_mock" in result["error"]
        assert "account_mode" in result["error"]

    @pytest.mark.asyncio
    async def test_accepts_matching_account_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        captured: dict[str, Any] = {}

        async def fake_place_order_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "dry_run": True}

        monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_order_impl)

        result = await mcp.tools["kis_mock_place_order"](
            symbol="005930",
            side="buy",
            price=50000.0,
            account_mode="kis_mock",
            dry_run=False,
            confirm=True,
        )
        assert result.get("success") is True


# ===========================================================================
# kis_mock_cancel_order
# ===========================================================================


class TestKisMockCancelOrder:
    @pytest.mark.asyncio
    async def test_fails_closed_when_config_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_missing(monkeypatch, ["KIS_MOCK_ENABLED"])

        result = await mcp.tools["kis_mock_cancel_order"](
            order_id="ORD-001", dry_run=False, confirm=True
        )
        assert result["success"] is False
        assert result["account_mode"] == "kis_mock"
        assert "KIS_MOCK_ENABLED" in result["error"]

    @pytest.mark.asyncio
    async def test_passes_is_mock_true_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        captured: dict[str, Any] = {}

        async def fake_cancel_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "order_id": kwargs.get("order_id")}

        monkeypatch.setattr(orders_kis_variants, "cancel_order_impl", fake_cancel_impl)

        await mcp.tools["kis_mock_cancel_order"](
            order_id="ORD-001", dry_run=False, confirm=True
        )
        assert captured.get("is_mock") is True

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_live_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        result = await mcp.tools["kis_mock_cancel_order"](
            order_id="ORD-001", account_mode="kis_live", dry_run=False, confirm=True
        )
        assert result["success"] is False
        assert "kis_mock" in result["error"]


# ===========================================================================
# kis_mock_modify_order
# ===========================================================================


class TestKisMockModifyOrder:
    @pytest.mark.asyncio
    async def test_fails_closed_when_config_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_missing(
            monkeypatch, ["KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY"]
        )

        result = await mcp.tools["kis_mock_modify_order"](
            order_id="ORD-001",
            symbol="005930",
            new_price=51000.0,
            dry_run=False,
            confirm=True,
        )
        assert result["success"] is False
        assert result["account_mode"] == "kis_mock"

    @pytest.mark.asyncio
    async def test_passes_is_mock_true_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        captured: dict[str, Any] = {}

        async def fake_modify_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "dry_run": True}

        monkeypatch.setattr(orders_kis_variants, "modify_order_impl", fake_modify_impl)

        await mcp.tools["kis_mock_modify_order"](
            order_id="ORD-001",
            symbol="005930",
            new_price=51000.0,
            dry_run=False,
            confirm=True,
        )
        assert captured.get("is_mock") is True

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_live_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        result = await mcp.tools["kis_mock_modify_order"](
            order_id="ORD-001",
            symbol="005930",
            new_price=51000.0,
            account_mode="kis_live",
            dry_run=False,
            confirm=True,
        )
        assert result["success"] is False
        assert "kis_mock" in result["error"]


# ===========================================================================
# kis_mock_get_order_history
# ===========================================================================


class TestKisMockGetOrderHistory:
    @pytest.mark.asyncio
    async def test_fails_closed_when_config_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_missing(monkeypatch, ["KIS_MOCK_ENABLED"])

        result = await mcp.tools["kis_mock_get_order_history"](symbol="005930")
        assert result["success"] is False
        assert result["account_mode"] == "kis_mock"

    @pytest.mark.asyncio
    async def test_passes_is_mock_true_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        captured: dict[str, Any] = {}

        async def fake_get_history_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "orders": []}

        monkeypatch.setattr(
            orders_history, "get_order_history_impl", fake_get_history_impl
        )

        await mcp.tools["kis_mock_get_order_history"](symbol="005930")
        assert captured.get("is_mock") is True

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_live_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        result = await mcp.tools["kis_mock_get_order_history"](
            symbol="005930", account_mode="kis_live"
        )
        assert result["success"] is False
        assert "kis_mock" in result["error"]


# ===========================================================================
# kis_live_place_order
# ===========================================================================


class TestKisLivePlaceOrder:
    @pytest.mark.asyncio
    async def test_passes_is_mock_false_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_live_mcp()

        captured: dict[str, Any] = {}

        async def fake_place_order_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "dry_run": True}

        monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_order_impl)

        await mcp.tools["kis_live_place_order"](
            symbol="005930", side="buy", price=50000.0
        )
        assert captured.get("is_mock") is False

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_mock_argument(self) -> None:
        mcp = _build_live_mcp()

        result = await mcp.tools["kis_live_place_order"](
            symbol="005930", side="buy", price=50000.0, account_mode="kis_mock"
        )
        assert result["success"] is False
        assert "kis_live" in result["error"]
        assert "account_mode" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"venue": "nxt"},
            {"venue": "krx"},
            {"venue": "unified"},
            {"order_validity": "gtc"},
            {"order_validity": "예약"},
            {"reserved_time": "093000"},
        ],
    )
    async def test_nxt_venue_tif_reserved_are_gated_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
    ) -> None:
        # ROB-463: NXT venue / TIF / 예약주문 require operator confirmation of the
        # exact KIS wire codes — until then they MUST fail closed (no live order),
        # even in dry_run.
        mcp = _build_live_mcp()

        called = {"placed": False}

        async def fake_place_order_impl(**_kwargs: Any) -> dict[str, Any]:
            called["placed"] = True
            return {"success": True}

        monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_order_impl)

        result = await mcp.tools["kis_live_place_order"](
            symbol="005930", side="buy", price=50000.0, dry_run=True, **kwargs
        )

        assert result["success"] is False
        assert result["error"] == "venue_tif_pending_operator_confirmation"
        assert "ROB-463" in result.get("linear", "") + result.get("reason", "")
        # The order path must NOT have been reached.
        assert called["placed"] is False

    @pytest.mark.asyncio
    async def test_default_and_auto_day_venue_proceed_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # venue=None/"auto" + order_validity=None/"day" is the existing behaviour
        # (auto-routing, day order) and must still reach the order path.
        mcp = _build_live_mcp()

        captured: dict[str, Any] = {}

        async def fake_place_order_impl(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"success": True, "dry_run": True}

        monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_order_impl)

        result = await mcp.tools["kis_live_place_order"](
            symbol="005930",
            side="buy",
            price=50000.0,
            venue="auto",
            order_validity="day",
        )
        assert result["success"] is True
        assert captured.get("is_mock") is False

    @pytest.mark.asyncio
    async def test_dry_run_includes_active_toss_warnings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_live_mcp()

        class _WarningsClient:
            async def warnings(self, symbol: str):
                assert symbol == "005930"
                return [
                    TossWarningInfo(
                        warning_type="OVERHEATED",
                        exchange="KRX",
                        start_date="2026-06-12",
                        end_date=None,
                    )
                ]

            async def aclose(self) -> None:
                pass

        async def fake_place_order_impl(**kwargs: Any) -> dict[str, Any]:
            assert kwargs["dry_run"] is True
            return {"success": True, "dry_run": True}

        monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_order_impl)
        monkeypatch.setattr(
            orders_kis_variants.TossReadClient,
            "from_settings",
            lambda: _WarningsClient(),
        )

        result = await mcp.tools["kis_live_place_order"](
            symbol="005930", side="buy", price=50000.0, dry_run=True
        )

        assert result["success"] is True
        assert result["warnings"][0]["warning_type"] == "OVERHEATED"

    @pytest.mark.asyncio
    async def test_live_buy_blocks_active_liquidation_trading_before_kis_post(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_live_mcp()
        called = {"placed": False}

        class _WarningsClient:
            async def warnings(self, symbol: str):
                assert symbol == "005930"
                return [
                    TossWarningInfo(
                        warning_type="LIQUIDATION_TRADING",
                        exchange="KRX",
                        start_date="2026-06-12",
                        end_date=None,
                    )
                ]

            async def aclose(self) -> None:
                pass

        async def fake_place_order_impl(**kwargs: Any) -> dict[str, Any]:
            called["placed"] = True
            return {"success": True, "dry_run": kwargs["dry_run"]}

        monkeypatch.setattr(order_execution, "_place_order_impl", fake_place_order_impl)
        monkeypatch.setattr(
            orders_kis_variants.TossReadClient,
            "from_settings",
            lambda: _WarningsClient(),
        )

        result = await mcp.tools["kis_live_place_order"](
            symbol="005930", side="buy", price=50000.0, dry_run=False
        )

        assert result["success"] is False
        assert result["mutation_sent"] is False
        assert "LIQUIDATION_TRADING" in result["error"]
        assert called["placed"] is False


# ===========================================================================
# kis_live_cancel_order
# ===========================================================================


class TestKisLiveCancelOrder:
    @pytest.mark.asyncio
    async def test_passes_is_mock_false_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_live_mcp()

        captured: dict[str, Any] = {}

        async def fake_cancel_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "order_id": kwargs.get("order_id")}

        monkeypatch.setattr(orders_kis_variants, "cancel_order_impl", fake_cancel_impl)

        await mcp.tools["kis_live_cancel_order"](order_id="ORD-001")
        assert captured.get("is_mock") is False

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_mock_argument(self) -> None:
        mcp = _build_live_mcp()

        result = await mcp.tools["kis_live_cancel_order"](
            order_id="ORD-001", account_mode="kis_mock"
        )
        assert result["success"] is False
        assert "kis_live" in result["error"]


# ===========================================================================
# kis_live_modify_order
# ===========================================================================


class TestKisLiveModifyOrder:
    @pytest.mark.asyncio
    async def test_passes_is_mock_false_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_live_mcp()

        captured: dict[str, Any] = {}

        async def fake_modify_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "dry_run": True}

        monkeypatch.setattr(orders_kis_variants, "modify_order_impl", fake_modify_impl)

        await mcp.tools["kis_live_modify_order"](
            order_id="ORD-001", symbol="005930", new_price=51000.0
        )
        assert captured.get("is_mock") is False

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_mock_argument(self) -> None:
        mcp = _build_live_mcp()

        result = await mcp.tools["kis_live_modify_order"](
            order_id="ORD-001",
            symbol="005930",
            new_price=51000.0,
            account_mode="kis_mock",
        )
        assert result["success"] is False
        assert "kis_live" in result["error"]


# ===========================================================================
# kis_live_get_order_history
# ===========================================================================


class TestKisLiveGetOrderHistory:
    @pytest.mark.asyncio
    async def test_passes_is_mock_false_to_impl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_live_mcp()

        captured: dict[str, Any] = {}

        async def fake_get_history_impl(**kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0)
            captured.update(kwargs)
            return {"success": True, "orders": []}

        monkeypatch.setattr(
            orders_history, "get_order_history_impl", fake_get_history_impl
        )

        await mcp.tools["kis_live_get_order_history"](symbol="005930")
        assert captured.get("is_mock") is False

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_mock_argument(self) -> None:
        mcp = _build_live_mcp()

        result = await mcp.tools["kis_live_get_order_history"](
            symbol="005930", account_mode="kis_mock"
        )
        assert result["success"] is False
        assert "kis_live" in result["error"]


_real_execute_order = order_execution._execute_order


def _mock_ledger_order(**overrides: Any) -> dict[str, Any]:
    return {
        "ledger_id": 1,
        "symbol": "005930",
        "side": "buy",
        "quantity": 2.0,
        "remaining_quantity": 2.0,
        "price": 70000.0,
        "krx_fwdg_ord_orgno": "00950",
        "instrument_type": "equity_kr",
        "lifecycle_state": "accepted",
        "edge_command_id": None,
        **overrides,
    }


@pytest.fixture
def order_path(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    events: list[str] = []
    reservations: dict[tuple[str, str], tuple[int, str | None]] = {}
    next_row_id = 0

    @asynccontextmanager
    async def session():
        yield object()

    class MemoryIntents:
        def __init__(self, db: object) -> None:
            pass

        async def reserve(
            self,
            *,
            account_scope: str,
            idempotency_key: str,
            side: str | None = None,
            **kwargs: Any,
        ) -> int:
            nonlocal next_row_id
            key = (account_scope, idempotency_key)
            if key in reservations:
                raise DuplicateOrderIntent
            next_row_id += 1
            reservations[key] = (next_row_id, side)
            events.append(f"reserve:{idempotency_key}")
            return next_row_id

        async def release_if_matches(
            self,
            *,
            account_scope: str,
            idempotency_key: str,
            row_id: int,
            side: str | None,
        ) -> int:
            key = (account_scope, idempotency_key)
            if reservations.get(key) != (row_id, side):
                return 0
            del reservations[key]
            return 1

    async def signal(*args: Any, **kwargs: Any) -> None:
        events.append("signal_committed")

    async def send(**kwargs: Any) -> dict[str, Any]:
        events.append("broker_send")
        return {"odno": "0000117058", "rt_cd": "0", "msg1": "ok"}

    monkeypatch.setattr(order_execution, "_order_session_factory", lambda: session)
    monkeypatch.setattr(order_execution, "OrderSendIntentService", MemoryIntents)
    monkeypatch.setattr(order_execution, "record_signal", signal)
    monkeypatch.setattr(order_execution, "mark_signal_outcome", AsyncMock())
    monkeypatch.setattr(order_execution, "_record_order_history", AsyncMock())
    monkeypatch.setattr(order_execution, "_execute_order", send)
    monkeypatch.setattr(
        kis_mock_ledger, "_fetch_kis_mock_baseline_qty", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        kis_mock_ledger,
        "_record_kis_mock_order",
        AsyncMock(return_value={"success": True, "order_no": "0000117058"}),
    )
    monkeypatch.setattr(order_execution, "get_kis_mock_edge_url", lambda: None)
    singleton.set_kis_mock_coordination_route_provider(None)
    return {"events": events, "reservations": reservations}


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_kis_mock_same_idempotency_key_is_reserved_before_single_send(
    order_path: dict[str, Any], market_type: str
) -> None:
    kwargs = _execute_kwargs(
        market_type=market_type, mirror_cohort=None, idempotency_key="client-key"
    )
    first = await order_execution._execute_and_record(**kwargs)
    second = await order_execution._execute_and_record(**kwargs)

    assert first["success"] is True
    assert second["success"] is False
    assert order_path["events"].count("broker_send") == 1
    assert (
        order_path["events"].index("signal_committed")
        < order_path["events"].index("reserve:client-key")
        < order_path["events"].index("broker_send")
    )
    assert ("kis_mock", "client-key") in order_path["reservations"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("market", "symbol"), [("kr", "005930"), ("us", "AAPL")])
@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("explicit_key", [False, True])
async def test_kis_mock_identical_orders_reserve_only_explicit_keys(
    order_path: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    market: str,
    symbol: str,
    mirror: bool,
    explicit_key: bool,
) -> None:
    monkeypatch.setattr(
        order_execution, "_fetch_current_price", AsyncMock(return_value=100.0)
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={"price": 100.0, "quantity": 1.0, "estimated_value": 100.0}
        ),
    )
    monkeypatch.setattr(
        order_execution, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        order_execution,
        "evaluate_sector_concentration",
        AsyncMock(return_value={"verdict": "ok"}),
    )
    kwargs = {
        "symbol": symbol,
        "side": "buy",
        "market": market,
        "price": 100.0,
        "quantity": 1.0,
        "dry_run": False,
        "is_mock": True,
        "strategy": "test_strategy",
        "mirror_cohort": "mock_counterfactual" if mirror else None,
        "client_order_id": "client-key" if explicit_key else None,
    }

    first = await order_execution._place_order_impl(**kwargs, correlation_id="item-1")
    second = await order_execution._place_order_impl(**kwargs, correlation_id="item-2")

    assert first["success"] is True
    assert second["success"] is not explicit_key
    assert order_path["events"].count("broker_send") == (1 if explicit_key else 2)
    if explicit_key and not mirror:
        assert "미러" not in second["error"]
    if mirror:
        assert ("kis_mock", "item-1") in order_path["reservations"]
        assert (
            ("kis_mock", "item-2") in order_path["reservations"]
        ) is not explicit_key
    assert (("kis_mock", "client-key") in order_path["reservations"]) is explicit_key


@pytest.fixture
def kr_edge_path(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    commands: list[str] = []

    async def edge_send(*, command: Any, **kwargs: Any) -> dict[str, Any]:
        commands.append(command.command_id)
        return {"rt_cd": "0", "odno": "0000117058"}

    monkeypatch.setattr(order_execution, "_execute_order", _real_execute_order)
    monkeypatch.setattr(order_execution, "execute_kis_mock_command", edge_send)
    monkeypatch.setattr(
        order_execution, "get_kis_mock_edge_url", lambda: "http://127.0.0.1:1"
    )
    monkeypatch.setattr(
        order_execution, "get_kr_security_type", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        order_execution, "_fetch_current_price", AsyncMock(return_value=70000.0)
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={"price": 70000.0, "quantity": 1.0, "estimated_value": 70000.0}
        ),
    )
    monkeypatch.setattr(
        order_execution, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        order_execution,
        "evaluate_sector_concentration",
        AsyncMock(return_value={"verdict": "ok"}),
    )
    return commands


async def _place_kr_mock_edge(
    correlation_id: str, *, client_order_id: str | None = None, price: float = 70000.0
) -> dict[str, Any]:
    return await order_execution._place_order_impl(
        symbol="005930",
        side="buy",
        market="kr",
        price=price,
        quantity=1.0,
        dry_run=False,
        is_mock=True,
        strategy="test_strategy",
        correlation_id=correlation_id,
        client_order_id=client_order_id,
    )


@pytest.mark.asyncio
async def test_kis_mock_kr_edge_distinct_correlations_use_distinct_commands(
    kr_edge_path: list[str],
) -> None:
    first = await _place_kr_mock_edge("item-1")
    second = await _place_kr_mock_edge("item-2")

    assert first["success"] is True
    assert second["success"] is True
    assert len(kr_edge_path) == 2
    assert kr_edge_path[0] != kr_edge_path[1]


@pytest.mark.asyncio
async def test_kis_mock_kr_edge_retry_same_correlation_reuses_command_id(
    kr_edge_path: list[str],
) -> None:
    first = await _place_kr_mock_edge("item-1")
    retry = await _place_kr_mock_edge("item-1")

    assert first["success"] is True
    assert retry["success"] is True
    assert len(kr_edge_path) == 2
    assert kr_edge_path[0] == kr_edge_path[1]


@pytest.mark.asyncio
async def test_kis_mock_kr_edge_retry_after_day_change_reuses_command_id(
    kr_edge_path: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(
        [
            datetime.datetime(2026, 11, 9, tzinfo=datetime.UTC),
            datetime.datetime(2026, 11, 10, tzinfo=datetime.UTC),
        ]
    )
    monkeypatch.setattr(order_execution, "now_kst", lambda: next(times))

    first = await _place_kr_mock_edge("item-1")
    retry = await _place_kr_mock_edge("item-1")

    assert first["success"] is True
    assert retry["success"] is True
    assert len(kr_edge_path) == 2
    assert kr_edge_path[0] == kr_edge_path[1]


@pytest.mark.asyncio
async def test_kis_mock_kr_edge_same_correlation_different_content_changes_command_id(
    kr_edge_path: list[str],
) -> None:
    first = await _place_kr_mock_edge("item-1", price=70000.0)
    second = await _place_kr_mock_edge("item-1", price=70100.0)

    assert first["success"] is True
    assert second["success"] is True
    assert len(kr_edge_path) == 2
    assert kr_edge_path[0] != kr_edge_path[1]


@pytest.mark.asyncio
async def test_kis_mock_kr_edge_explicit_key_remains_command_id(
    kr_edge_path: list[str],
) -> None:
    first = await _place_kr_mock_edge("item-1", client_order_id="client-key")
    second = await _place_kr_mock_edge("item-2", client_order_id="client-key")

    assert first["success"] is True
    assert second["success"] is False
    assert kr_edge_path == ["client-key"]


@pytest.mark.asyncio
async def test_kis_mock_mirror_keeps_correlation_and_idempotency_reservations(
    order_path: dict[str, Any],
) -> None:
    kwargs = _execute_kwargs(idempotency_key="client-key", correlation_id="mirror-key")
    first = await order_execution._execute_and_record(**kwargs)
    second = await order_execution._execute_and_record(
        **{**kwargs, "idempotency_key": "other-key"}
    )

    assert first["success"] is True
    assert second["success"] is False
    assert order_path["events"].count("broker_send") == 1
    assert ("kis_mock", "client-key") in order_path["reservations"]
    assert ("kis_mock", "mirror-key") in order_path["reservations"]


@pytest.mark.asyncio
async def test_mirror_duplicate_client_key_releases_only_its_new_correlation(
    order_path: dict[str, Any],
) -> None:
    order_path["reservations"][("kis_mock", "client-key")] = (999, "buy")
    result = await order_execution._execute_and_record(
        **_execute_kwargs(idempotency_key="client-key", correlation_id="mirror-key")
    )
    assert result["success"] is False
    assert order_path["events"].count("broker_send") == 0
    assert order_path["reservations"] == {("kis_mock", "client-key"): (999, "buy")}


@pytest.mark.asyncio
async def test_kis_mock_kr_timeout_keeps_idempotency_reservation_and_never_resends(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timeout(**kwargs: Any) -> None:
        order_path["events"].append("broker_send")
        raise httpx.ReadTimeout("test timeout")

    monkeypatch.setattr(order_execution, "_execute_order", timeout)
    kwargs = _execute_kwargs(mirror_cohort=None, idempotency_key="timeout-key")
    with pytest.raises(order_execution.OrderSendOutcomeUnknown):
        await order_execution._execute_and_record(**kwargs)
    second = await order_execution._execute_and_record(**kwargs)

    assert second["success"] is False
    assert order_path["events"].count("broker_send") == 1
    assert ("kis_mock", "timeout-key") in order_path["reservations"]


@pytest.mark.asyncio
async def test_kis_mock_kr_without_provider_reaches_broker(
    order_path: dict[str, Any],
) -> None:
    result = await order_execution._execute_and_record(
        **_execute_kwargs(mirror_cohort=None)
    )
    assert result["success"] is True
    assert order_path["events"].count("broker_send") == 1


@pytest.mark.asyncio
async def test_kis_mock_place_reserves_before_real_domestic_client_wire(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, parent = _domestic_client()

    async def wire(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        assert ("kis_mock", "client-key") in order_path["reservations"]
        await kwargs["pre_send_hook"]()
        parent.requests.append({"method": method, "url": url, **kwargs})
        return {"rt_cd": "0", "output": {"ODNO": "0000117058"}}

    monkeypatch.setattr(order_execution, "_execute_order", _real_execute_order)
    monkeypatch.setattr(order_execution, "_create_kis_client", lambda **kwargs: client)
    monkeypatch.setattr(
        order_execution, "get_kr_security_type", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(parent, "_request_with_rate_limit", wire)

    result = await order_execution._execute_and_record(
        **_execute_kwargs(mirror_cohort=None, idempotency_key="client-key")
    )
    assert result["success"] is True
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_kis_mock_attribution_required_before_reservation_or_send(
    order_path: dict[str, Any],
) -> None:
    result = await order_execution._execute_and_record(
        **_execute_kwargs(
            strategy=None,
            correlation_id=None,
            mirror_cohort=None,
            idempotency_key="client-key",
        )
    )
    assert result.get("error_code") == "attribution_required"
    assert order_path["events"] == []


@pytest.mark.asyncio
async def test_kis_mock_signal_failure_blocks_before_reservation_or_send(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        order_execution,
        "record_signal",
        AsyncMock(side_effect=RuntimeError("test failure")),
    )
    result = await order_execution._execute_and_record(
        **_execute_kwargs(mirror_cohort=None, idempotency_key="client-key")
    )
    assert result["error_code"] == "signal_record_unavailable"
    assert order_path["events"] == []


@pytest.mark.asyncio
async def test_kis_mock_domestic_place_needs_no_writer_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def no_lease(**kwargs: Any):
        raise AssertionError("unexpected writer lease")
        yield

    monkeypatch.setattr(singleton, "enforce_kis_mock_mutation_writer", no_lease)
    client, parent = _domestic_client()
    await client.order_korea_stock("005930", "buy", 1, 70000, True)
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_kis_mock_cancel_without_capability_reaches_domestic_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order()
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(kis_mock_ledger, "mark_kis_mock_order_cancelled", AsyncMock())
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    monkeypatch.setattr(edge_client, "get_kis_mock_edge_url", lambda: None)

    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is True
    assert result["broker_cancel_confirmed"] is True
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_kis_mock_cancel_unknown_remainder_refuses_before_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(remaining_quantity=None)
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    monkeypatch.setattr(edge_client, "get_kis_mock_edge_url", lambda: None)

    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is False
    assert parent.requests == []


@pytest.mark.asyncio
async def test_kis_mock_cancel_unknown_remainder_cannot_use_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(remaining_quantity=None, edge_command_id="edge-1")
    send = AsyncMock()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        edge_client, "get_kis_mock_edge_url", lambda: "http://127.0.0.1:9999"
    )
    monkeypatch.setattr(edge_client, "cancel_kis_mock_command", send)
    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_kis_mock_cancel_terminal_ledger_row_never_resends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(lifecycle_state="cancelled")
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is False
    assert parent.requests == []


@pytest.mark.asyncio
async def test_kis_mock_modify_without_capability_reaches_domestic_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order()
    client, parent = _domestic_client()
    original_request = parent._request_with_rate_limit

    async def modify_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await original_request(*args, **kwargs)
        return {"rt_cd": "0", "output": {"ODNO": "0001002"}}

    monkeypatch.setattr(parent, "_request_with_rate_limit", modify_response)
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(kis_mock_ledger, "update_kis_mock_order_terms", AsyncMock())
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_kr_security_type_or_none", AsyncMock(return_value=None)
    )
    result = await orders_modify_cancel._modify_kis_mock_domestic(
        "0000117058", "005930", "equity_kr", 70000, 1, False
    )
    assert result["success"] is True
    assert len(parent.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", [4, 5])
@pytest.mark.parametrize("new_quantity", [None, 3])
async def test_kis_mock_partial_fill_modify_then_cancel_preserves_remainder(
    monkeypatch: pytest.MonkeyPatch, filled: int, new_quantity: int | None
) -> None:
    row = SimpleNamespace(
        order_no="original",
        quantity=Decimal("10"),
        price=Decimal("70000"),
        lifecycle_state="fill",
        last_reconcile_detail={"attributed_fill_qty": str(filled)},
    )

    async def resolve(order_no: str) -> dict[str, Any] | None:
        if order_no != row.order_no:
            return None
        _, remaining, _ = kis_mock_ledger._derive_shadow_fill(row, float(row.quantity))
        return _mock_ledger_order(
            quantity=float(row.quantity),
            remaining_quantity=remaining,
            price=float(row.price),
            lifecycle_state=row.lifecycle_state,
        )

    session = SimpleNamespace(get=AsyncMock(return_value=row), commit=AsyncMock())

    @asynccontextmanager
    async def session_factory():
        yield session

    client = SimpleNamespace(
        modify_korea_order=AsyncMock(return_value={"odno": "modified"}),
        cancel_korea_order=AsyncMock(return_value={}),
    )
    monkeypatch.setattr(kis_mock_ledger, "resolve_mock_order_for_cancel", resolve)
    monkeypatch.setattr(
        kis_mock_ledger, "_order_session_factory", lambda: session_factory
    )
    monkeypatch.setattr(kis_mock_ledger, "mark_kis_mock_order_cancelled", AsyncMock())
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_kr_security_type_or_none", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(edge_client, "get_kis_mock_edge_url", lambda: None)

    modified = await orders_modify_cancel._modify_kis_mock_domestic(
        "original", "005930", "equity_kr", 71000, new_quantity, False
    )
    expected_remaining = new_quantity if new_quantity is not None else 10 - filled
    assert modified["success"] is True
    assert client.modify_korea_order.await_args.args[2] == expected_remaining
    assert row.quantity == Decimal(filled + expected_remaining)

    cancelled = await orders_modify_cancel._cancel_kis_mock_domestic(
        "modified", "005930"
    )
    assert cancelled["success"] is True
    assert client.cancel_korea_order.await_args.kwargs["quantity"] == expected_remaining


@pytest.mark.asyncio
async def test_kis_mock_modify_unknown_remainder_refuses_before_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(remaining_quantity=None)
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    result = await orders_modify_cancel._modify_kis_mock_domestic(
        "0000117058", "005930", "equity_kr", 70000, 1, False
    )
    assert result["success"] is False
    assert parent.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ordered_quantity", [None, float("nan"), 1.0])
async def test_kis_mock_modify_inconsistent_ledger_quantity_refuses_before_broker(
    monkeypatch: pytest.MonkeyPatch, ordered_quantity: float | None
) -> None:
    row = _mock_ledger_order(quantity=ordered_quantity, remaining_quantity=2.0)
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    result = await orders_modify_cancel._modify_kis_mock_domestic(
        "0000117058", "005930", "equity_kr", 70000, 1, False
    )
    assert result["success"] is False
    assert parent.requests == []


@pytest.mark.asyncio
async def test_kis_mock_wire_pins_client_and_host_before_token_and_each_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, parent = _domestic_client()
    await client.order_korea_stock("005930", "buy", 1, 70000, True)
    hook = parent.requests[0]["pre_send_hook"]
    assert hook is not None
    parent._settings_view.kis_base_url = "https://openapi.koreainvestment.com:9443"
    with pytest.raises(singleton.KISMockSendBoundaryRejected):
        await hook()
    with pytest.raises(singleton.KISMockSendBoundaryRejected):
        await client.order_korea_stock("005930", "buy", 1, 70000, True)
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_mirror_proven_not_sent_releases_both_exact_reservations(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(**kwargs: Any) -> None:
        raise order_execution.PreSendFreshnessError(("expired",))

    monkeypatch.setattr(order_execution, "_execute_order", blocked)
    result = await order_execution._execute_and_record(
        **_execute_kwargs(idempotency_key="client-key", correlation_id="mirror-key")
    )
    assert result["pre_send_blocked"] is True
    assert order_path["reservations"] == {}


@pytest.mark.asyncio
async def test_mock_pre_send_release_does_not_delete_replacement_row(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def replaced(**kwargs: Any) -> None:
        order_path["reservations"][("kis_mock", "client-key")] = (999, "buy")
        raise order_execution.PreSendFreshnessError(("expired",))

    monkeypatch.setattr(order_execution, "_execute_order", replaced)
    result = await order_execution._execute_and_record(
        **_execute_kwargs(mirror_cohort=None, idempotency_key="client-key")
    )
    assert result["pre_send_blocked"] is True
    assert order_path["reservations"][("kis_mock", "client-key")] == (999, "buy")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "delegate"),
    [
        (
            "kis_mock_place_order",
            {"symbol": "005930", "side": "buy", "price": 70000},
            "_place_order_variant",
        ),
        (
            "kis_mock_cancel_order",
            {"order_id": "ORDER-1", "market": "kr"},
            "_cancel_order_variant",
        ),
        (
            "kis_mock_modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
            "_modify_order_variant",
        ),
    ],
)
@pytest.mark.parametrize(
    ("dry_run", "confirm"), [(True, False), (True, True), (False, False)]
)
async def test_dedicated_mock_mutations_refuse_without_both_gates(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    delegate: str,
    dry_run: bool,
    confirm: bool,
) -> None:
    mcp = DummyMCP()
    orders_kis_variants.register_kis_mock_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(orders_kis_variants, delegate, call)
    result = await mcp.tools[name](**args, dry_run=dry_run, confirm=confirm)
    assert result["success"] is False
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "delegate"),
    [
        (
            "kis_mock_place_order",
            {"symbol": "005930", "side": "buy", "price": 70000},
            "_place_order_variant",
        ),
        (
            "kis_mock_cancel_order",
            {"order_id": "ORDER-1", "market": "kr"},
            "_cancel_order_variant",
        ),
        (
            "kis_mock_modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
            "_modify_order_variant",
        ),
    ],
)
async def test_dedicated_mock_mutations_delegate_only_after_both_gates(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any], delegate: str
) -> None:
    mcp = DummyMCP()
    orders_kis_variants.register_kis_mock_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(orders_kis_variants, delegate, call)
    result = await mcp.tools[name](**args, dry_run=False, confirm=True)
    assert result["success"] is True
    call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "delegate", "recommended"),
    [
        (
            "place_order",
            {"symbol": "005930", "side": "buy", "price": 70000},
            "_place_order_impl",
            "kis_mock_place_order",
        ),
        (
            "cancel_order",
            {"order_id": "ORDER-1", "market": "kr"},
            "cancel_order_impl",
            "kis_mock_cancel_order",
        ),
        (
            "modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
            "modify_order_impl",
            "kis_mock_modify_order",
        ),
    ],
)
async def test_generic_kr_mock_mutations_redirect_before_downstream(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    delegate: str,
    recommended: str,
) -> None:
    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    owner = order_execution if delegate == "_place_order_impl" else orders_registration
    monkeypatch.setattr(owner, delegate, call)
    monkeypatch.setattr(orders_registration, "validate_kis_mock_config", lambda: [])
    result = await mcp.tools[name](**args, account_mode="kis_mock")
    assert result["error_code"] == "kis_mock_use_dedicated_tool"
    assert recommended in result["error"]
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("kis_mock_place_order", {"symbol": "005930", "side": "buy", "price": 70000}),
        ("kis_mock_cancel_order", {"order_id": "ORDER-1", "market": "kr"}),
        (
            "kis_mock_modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
        ),
    ],
)
async def test_dedicated_mock_config_gate_still_refuses_before_io(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any]
) -> None:
    mcp = DummyMCP()
    orders_kis_variants.register_kis_mock_order_tools(mcp)
    monkeypatch.setattr(
        orders_kis_variants, "validate_kis_mock_config", lambda: ["KIS_MOCK_ENABLED"]
    )
    place = AsyncMock()
    cancel = AsyncMock()
    modify = AsyncMock()
    monkeypatch.setattr(order_execution, "_place_order_impl", place)
    monkeypatch.setattr(orders_kis_variants, "cancel_order_impl", cancel)
    monkeypatch.setattr(orders_kis_variants, "modify_order_impl", modify)
    result = await mcp.tools[name](**args, dry_run=False, confirm=True)
    assert result["success"] is False
    assert "KIS_MOCK_ENABLED" in result["error"]
    place.assert_not_awaited()
    cancel.assert_not_awaited()
    modify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
@pytest.mark.parametrize(
    ("name", "args", "delegate"),
    [
        (
            "place_order",
            {"symbol": "AAPL", "side": "buy", "price": 120},
            "_place_order_impl",
        ),
        (
            "cancel_order",
            {"order_id": "ORDER-1", "symbol": "AAPL", "market": "us"},
            "cancel_order_impl",
        ),
        (
            "modify_order",
            {"order_id": "ORDER-1", "symbol": "AAPL", "market": "us", "new_price": 120},
            "modify_order_impl",
        ),
    ],
)
async def test_generic_us_mock_and_live_still_delegate(
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
    name: str,
    args: dict[str, Any],
    delegate: str,
) -> None:
    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    owner = order_execution if delegate == "_place_order_impl" else orders_registration
    monkeypatch.setattr(owner, delegate, call)
    monkeypatch.setattr(orders_registration, "validate_kis_mock_config", lambda: [])
    result = await mcp.tools[name](**args, account_mode=account_mode)
    assert result["success"] is True
    assert call.await_args.kwargs["is_mock"] is (account_mode == "kis_mock")
