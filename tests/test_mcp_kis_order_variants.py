"""Tests for the typed KIS order tool variants (kis_live_* / kis_mock_*).

Verifies:
- kis_mock_* tools fail closed when KIS mock config is missing.
- kis_mock_* tools pass is_mock=True to underlying impls.
- kis_mock_* tools reject account_mode='kis_live' argument.
- kis_live_* tools pass is_mock=False to underlying impls.
- kis_live_* tools reject account_mode='kis_mock' argument.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import app.mcp_server.tooling.orders_kis_variants as orders_kis_variants
from app.mcp_server.tooling import order_execution, orders_history
from app.mcp_server.tooling.orders_kis_variants import (
    register_kis_live_order_tools,
    register_kis_mock_order_tools,
)
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
