"""Tests for paper trading account management MCP tools."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

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


from datetime import datetime, timezone

from app.mcp_server.tooling.paper_account_registration import _serialize_account


def _make_account(**overrides) -> PaperAccount:
    defaults = dict(
        id=1,
        name="default",
        initial_capital=Decimal("100000000"),
        cash_krw=Decimal("95000000"),
        cash_usd=Decimal("0"),
        description=None,
        strategy_name=None,
        is_active=True,
        created_at=datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc),
    )
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
