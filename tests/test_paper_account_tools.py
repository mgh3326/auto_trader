"""Tests for paper trading account management MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.paper_account_registration import _serialize_account
from app.models.paper_trading import PaperAccount
from tests._mcp_tooling_support import build_tools as _build_tools


def build_tools():
    return _build_tools(profile=McpProfile.DB_PAPER)


@pytest.mark.asyncio
async def test_only_the_account_read_tool_is_registered() -> None:
    """MCP surface audit 2026-09-03: create/reset/delete were all class D.

    Zero calls in 90 days and zero prompt/runbook/code references, so the three
    mutating simulator-account tools were removed and db-paper's simulator
    surface is exactly the read.
    """
    tools = build_tools()
    assert "list_paper_accounts" in tools
    for retired in (
        "create_paper_account",
        "reset_paper_account",
        "delete_paper_account",
    ):
        assert retired not in tools, f"{retired} is still registered"


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
    assert out["initial_capital"] == pytest.approx(100_000_000.0)
    assert out["cash_krw"] == pytest.approx(95_000_000.0)
    assert out["cash_usd"] == pytest.approx(0.0)
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
    assert out["total_evaluated_krw"] == pytest.approx(98_500_000.0)
    assert out["total_pnl_pct"] == pytest.approx(-1.5)


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
    assert first["total_evaluated_krw"] == pytest.approx(98_500_000.0)
    assert first["total_pnl_pct"] == pytest.approx(-1.5)
    second = result["accounts"][1]
    assert second["id"] == 2
    assert second["cash_usd"] == pytest.approx(5000.0)


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
