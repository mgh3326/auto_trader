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
from typing import Any, cast

import pytest

import app.mcp_server.tooling.orders_kis_variants as orders_kis_variants
from app.mcp_server.tooling import order_execution, orders_history
from app.mcp_server.tooling.orders_kis_variants import (
    register_kis_live_order_tools,
    register_kis_mock_order_tools,
)
from app.services.brokers.toss.dto import TossWarningInfo
from tests._mcp_tooling_support import DummyMCP


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
            symbol="005930", side="buy", price=50000.0
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
            symbol="005930", side="buy", price=50000.0
        )
        assert captured.get("is_mock") is True

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_live_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        result = await mcp.tools["kis_mock_place_order"](
            symbol="005930", side="buy", price=50000.0, account_mode="kis_live"
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
            symbol="005930", side="buy", price=50000.0, account_mode="kis_mock"
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

        result = await mcp.tools["kis_mock_cancel_order"](order_id="ORD-001")
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

        await mcp.tools["kis_mock_cancel_order"](order_id="ORD-001")
        assert captured.get("is_mock") is True

    @pytest.mark.asyncio
    async def test_rejects_account_mode_kis_live_argument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp = _build_mock_mcp()
        _patch_mock_config_ok(monkeypatch)

        result = await mcp.tools["kis_mock_cancel_order"](
            order_id="ORD-001", account_mode="kis_live"
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
            order_id="ORD-001", symbol="005930", new_price=51000.0
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
            order_id="ORD-001", symbol="005930", new_price=51000.0
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
