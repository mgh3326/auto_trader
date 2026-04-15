"""Tests for execution comment formatter MCP tool."""

import pytest

from app.mcp_server.tooling.execution_comment import format_execution_comment


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strategy_stage():
    result = await format_execution_comment(
        stage="strategy",
        symbol="AAPL",
        side="buy",
        thesis="Strong earnings momentum",
    )
    assert "## 실행 기록" in result
    assert "| symbol | AAPL |" in result
    assert "| side | buy |" in result
    assert "| thesis | Strong earnings momentum |" in result
    assert "order_id" not in result
    assert "fill_status" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_stage():
    result = await format_execution_comment(
        stage="dry_run",
        symbol="TSLA",
        side="buy",
        qty=5.0,
        price=250.0,
        currency="$",
    )
    assert "| symbol | TSLA |" in result
    assert "| price | $250.0 |" in result
    assert "| mode | dry_run |" in result
    assert "order_id" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_stage():
    result = await format_execution_comment(
        stage="live",
        symbol="AAPL",
        side="buy",
        qty=10.0,
        price=185.50,
        mode="live",
        order_id="KIS-20260415-001",
        journal_id=42,
        fill_status="pending",
        currency="$",
        account_type="kis_auto",
    )
    assert "| order_id | KIS-20260415-001 |" in result
    assert "| fill_status | pending |" in result
    assert "| price | $185.5 |" in result
    assert "| account_type | kis_auto |" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_stage_with_fee():
    result = await format_execution_comment(
        stage="fill",
        symbol="AAPL",
        side="buy",
        qty=10.0,
        price=185.50,
        mode="live",
        order_id="KIS-20260415-001",
        journal_id=42,
        fill_status="filled",
        filled_qty=10.0,
        fee=1.5,
        currency="$",
    )
    assert "| fill_status | filled |" in result
    assert "| filled_qty | 10.0 |" in result
    assert "| fee | $1.5 |" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_follow_up_stage():
    result = await format_execution_comment(
        stage="follow_up",
        symbol="AAPL",
        journal_id=42,
        next_action="hold_until 2026-05-15",
        market_context="SPY up 1.2%, sector bullish",
    )
    assert "| symbol | AAPL |" in result
    assert "| journal_id | 42 |" in result
    assert "| next_action | hold_until 2026-05-15 |" in result
    assert "| market_context | SPY up 1.2%, sector bullish |" in result
    assert "side" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_optional_fields_omitted():
    result = await format_execution_comment(
        stage="live",
        symbol="AAPL",
        side="buy",
        qty=10.0,
        price=185.50,
        mode="live",
        fill_status="pending",
    )
    assert "order_id" not in result
    assert "journal_id" not in result
    assert "account_type" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_stage():
    result = await format_execution_comment(
        stage="invalid",
        symbol="AAPL",
    )
    assert "Error: invalid stage" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_currency_prepended_to_price():
    result = await format_execution_comment(
        stage="live",
        symbol="삼성전자",
        side="buy",
        qty=10.0,
        price=72000,
        mode="live",
        fill_status="pending",
        currency="₩",
    )
    assert "| price | ₩72000 |" in result
