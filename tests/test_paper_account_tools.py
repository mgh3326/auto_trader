"""Tests for paper trading account management MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.mcp_server.tooling.paper_account_registration import _serialize_account
from app.models.paper_trading import PaperAccount
from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_paper_account_tools_registered() -> None:
    """All 4 paper account management tools must be registered."""
    tools = build_tools()
    assert "create_paper_account" in tools
    assert "list_paper_accounts" in tools
    assert "reset_paper_account" in tools
    assert "delete_paper_account" in tools


def _make_account(**overrides) -> PaperAccount:
    defaults = {
        "id": 1,
        "name": "default",
        "initial_capital": Decimal("100000000"),
        "cash_krw": Decimal("95000000"),
        "cash_usd": Decimal("0"),
        "description": None,
        "strategy_name": None,
        "is_active": True,
        "created_at": datetime(2026, 4, 13, 10, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 4, 13, 10, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return PaperAccount(**defaults)


def test_serialize_account_basic_fields() -> None:
    acc = _make_account()
    out = _serialize_account(acc)
    assert out["id"] == 1
    assert out["name"] == "default"
    assert out["initial_capital"] == 100_000_000.0
    assert out["cash_krw"] == 95_000_000.0
    assert out["cash_usd"] == 0.0
    assert out["strategy_name"] is None
    assert out["created_at"] == "2026-04-13T10:00:00+00:00"
    # Summary fields absent when not provided
    assert "positions_count" not in out
    assert "total_evaluated_krw" not in out
    assert "total_pnl_pct" not in out


def test_serialize_account_with_summary() -> None:
    acc = _make_account()
    out = _serialize_account(
        acc,
        positions_count=3,
        total_evaluated=Decimal("98500000"),
        total_pnl_pct=Decimal("-1.50"),
    )
    assert out["positions_count"] == 3
    assert out["total_evaluated_krw"] == 98_500_000.0
    assert out["total_pnl_pct"] == -1.5


def test_serialize_account_none_totals_become_null() -> None:
    acc = _make_account()
    out = _serialize_account(
        acc,
        positions_count=0,
        total_evaluated=None,
        total_pnl_pct=None,
    )
    assert out["positions_count"] == 0
    assert out["total_evaluated_krw"] is None
    assert out["total_pnl_pct"] is None


class _SessionCtx:
    """Async-context wrapper that yields a pre-made mock db."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _patch_session(monkeypatch, db) -> None:
    """Make _session_factory()() yield our mock db."""
    factory = MagicMock()
    factory.return_value = _SessionCtx(db)
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_account_registration._session_factory",
        lambda: factory,
    )


@pytest.mark.asyncio
async def test_create_paper_account_success(monkeypatch) -> None:
    db = AsyncMock()
    db.add = MagicMock()

    async def _refresh(instance):
        instance.id = 42
        instance.created_at = instance.created_at or None
        instance.updated_at = instance.updated_at or None

    db.refresh = AsyncMock(side_effect=_refresh)
    _patch_session(monkeypatch, db)

    tools = build_tools()
    result = await tools["create_paper_account"](
        name="bot-1",
        initial_capital=50_000_000,
        description="test",
    )

    assert result["success"] is True
    assert result["account"]["id"] == 42
    assert result["account"]["name"] == "bot-1"
    assert result["account"]["initial_capital"] == 50_000_000.0
    assert result["account"]["cash_krw"] == 50_000_000.0
    assert result["account"]["description"] == "test"


@pytest.mark.asyncio
async def test_create_paper_account_with_strategy_name(monkeypatch) -> None:
    db = AsyncMock()
    db.add = MagicMock()

    async def _refresh(instance):
        instance.id = 55
        instance.created_at = None
        instance.updated_at = None

    db.refresh = AsyncMock(side_effect=_refresh)
    _patch_session(monkeypatch, db)

    tools = build_tools()
    result = await tools["create_paper_account"](
        name="momentum-bot",
        strategy_name="momentum",
    )

    assert result["success"] is True
    assert result["account"]["strategy_name"] == "momentum"


@pytest.mark.asyncio
async def test_list_paper_accounts_with_strategy_filter(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc = _make_account(id=1, name="momentum-bot", strategy_name="momentum")

    async def _list(is_active, strategy_name):
        assert strategy_name == "momentum"
        return [acc]

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.list_accounts = AsyncMock(side_effect=_list)
        svc.get_portfolio_summary = AsyncMock(
            return_value={
                "total_invested": Decimal("0"),
                "total_evaluated": Decimal("100000000"),
                "total_pnl": Decimal("0"),
                "total_pnl_pct": Decimal("0.00"),
                "cash_krw": acc.cash_krw,
                "cash_usd": acc.cash_usd,
                "positions_count": 0,
            }
        )

        tools = build_tools()
        result = await tools["list_paper_accounts"](strategy_name="momentum")

    assert result["success"] is True
    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["strategy_name"] == "momentum"


@pytest.mark.asyncio
async def test_create_paper_account_duplicate_name(monkeypatch) -> None:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("unique")))
    _patch_session(monkeypatch, db)

    tools = build_tools()
    result = await tools["create_paper_account"](name="dup")

    assert result["success"] is False
    assert (
        "already exists" in result["error"].lower()
        or "duplicate" in result["error"].lower()
    )


@pytest.mark.asyncio
async def test_list_paper_accounts_returns_enriched(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc1 = _make_account(id=1, name="default")
    acc2 = _make_account(
        id=2, name="us-bot", cash_krw=Decimal("0"), cash_usd=Decimal("5000")
    )

    async def _list(is_active, strategy_name=None):
        assert is_active is True
        return [acc1, acc2]

    summaries = {
        1: {
            "total_invested": Decimal("0"),
            "total_evaluated": Decimal("98500000"),
            "total_pnl": Decimal("-1500000"),
            "total_pnl_pct": Decimal("-1.50"),
            "cash_krw": acc1.cash_krw,
            "cash_usd": acc1.cash_usd,
            "positions_count": 3,
        },
        2: {
            "total_invested": Decimal("0"),
            "total_evaluated": Decimal("5100"),
            "total_pnl": Decimal("100"),
            "total_pnl_pct": Decimal("2.00"),
            "cash_krw": acc2.cash_krw,
            "cash_usd": acc2.cash_usd,
            "positions_count": 1,
        },
    }

    async def _summary(account_id):
        return summaries[account_id]

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.list_accounts = AsyncMock(side_effect=_list)
        svc.get_portfolio_summary = AsyncMock(side_effect=_summary)

        tools = build_tools()
        result = await tools["list_paper_accounts"]()

    assert result["success"] is True
    assert len(result["accounts"]) == 2
    first = result["accounts"][0]
    assert first["id"] == 1
    assert first["positions_count"] == 3
    assert first["total_evaluated_krw"] == 98_500_000.0
    assert first["total_pnl_pct"] == -1.5
    second = result["accounts"][1]
    assert second["id"] == 2
    assert second["cash_usd"] == 5000.0


@pytest.mark.asyncio
async def test_list_paper_accounts_is_active_false(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    captured: dict[str, object] = {}

    async def _list(is_active, strategy_name=None):
        captured["is_active"] = is_active
        return []

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.list_accounts = AsyncMock(side_effect=_list)

        tools = build_tools()
        result = await tools["list_paper_accounts"](is_active=False)

    assert captured["is_active"] is False
    assert result == {"success": True, "accounts": []}


@pytest.mark.asyncio
async def test_reset_paper_account_success(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc = _make_account(id=7, name="reset-me")
    reset_acc = _make_account(
        id=7, name="reset-me", cash_krw=Decimal("100000000"), cash_usd=Decimal("0")
    )

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=acc)
        svc.reset_account = AsyncMock(return_value=reset_acc)

        tools = build_tools()
        result = await tools["reset_paper_account"](name="reset-me")

    svc.reset_account.assert_awaited_once_with(7)
    assert result["success"] is True
    assert result["account"]["id"] == 7
    assert result["account"]["cash_krw"] == 100_000_000.0


@pytest.mark.asyncio
async def test_reset_paper_account_missing(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=None)

        tools = build_tools()
        result = await tools["reset_paper_account"](name="ghost")

    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_delete_paper_account_success(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    acc = _make_account(id=9, name="goodbye")

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=acc)
        svc.delete_account = AsyncMock(return_value=True)

        tools = build_tools()
        result = await tools["delete_paper_account"](name="goodbye")

    svc.delete_account.assert_awaited_once_with(9)
    assert result == {"success": True, "deleted": True, "name": "goodbye", "id": 9}


@pytest.mark.asyncio
async def test_delete_paper_account_missing(monkeypatch) -> None:
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.get_account_by_name = AsyncMock(return_value=None)

        tools = build_tools()
        result = await tools["delete_paper_account"](name="ghost")

    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_paper_account_full_flow(monkeypatch) -> None:
    """create → list → reset → delete all succeed against a mocked service."""
    db = AsyncMock()
    _patch_session(monkeypatch, db)

    created = _make_account(id=101, name="flow")
    after_reset = _make_account(id=101, name="flow")

    with patch(
        "app.mcp_server.tooling.paper_account_registration.PaperTradingService"
    ) as svc_cls:
        svc = svc_cls.return_value
        svc.create_account = AsyncMock(return_value=created)
        svc.list_accounts = AsyncMock(return_value=[created])
        svc.get_portfolio_summary = AsyncMock(
            return_value={
                "total_invested": Decimal("0"),
                "total_evaluated": Decimal("100000000"),
                "total_pnl": Decimal("0"),
                "total_pnl_pct": Decimal("0.00"),
                "cash_krw": created.cash_krw,
                "cash_usd": created.cash_usd,
                "positions_count": 0,
            }
        )
        svc.get_account_by_name = AsyncMock(return_value=created)
        svc.reset_account = AsyncMock(return_value=after_reset)
        svc.delete_account = AsyncMock(return_value=True)

        tools = build_tools()

        create_result = await tools["create_paper_account"](name="flow")
        assert create_result["success"] is True

        list_result = await tools["list_paper_accounts"](is_active=True)
        assert list_result["success"] is True
        assert list_result["accounts"][0]["id"] == 101

        reset_result = await tools["reset_paper_account"](name="flow")
        assert reset_result["success"] is True

        delete_result = await tools["delete_paper_account"](name="flow")
        assert delete_result == {
            "success": True,
            "deleted": True,
            "name": "flow",
            "id": 101,
        }
