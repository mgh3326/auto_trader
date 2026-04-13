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
