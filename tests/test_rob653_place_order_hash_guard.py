# tests/test_rob653_place_order_hash_guard.py
from unittest.mock import AsyncMock

import pytest

import app.mcp_server.tooling.order_execution as oe
from app.core.config import settings


@pytest.fixture
def _stub_pricing(monkeypatch):
    # Keep the flow offline: fixed price, no balance block, no real send.
    async def _price(symbol, market_type):
        return 70000.0

    monkeypatch.setattr(oe, "_get_current_price_for_order", _price)
    monkeypatch.setattr(
        oe, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )


@pytest.mark.asyncio
async def test_dry_run_emits_approval_hash(monkeypatch, _stub_pricing):
    res = await oe._place_order_impl(
        symbol="005930",
        side="buy",
        market="KR",
        order_type="limit",
        quantity=10,
        price=70000,
        dry_run=True,
        thesis="t",
        strategy="t",
    )
    assert res["success"] is True and res["dry_run"] is True
    assert res["approval_hash"].startswith("p6a1.")
    assert "approval_expires_at" in res
    assert res["idempotency_key"].startswith("tossp6-")


@pytest.mark.asyncio
async def test_required_mode_blocks_without_hash(monkeypatch, _stub_pricing):
    monkeypatch.setattr(settings, "order_approval_hash_mode", "required")
    res = await oe._place_order_impl(
        symbol="005930",
        side="buy",
        market="KR",
        order_type="limit",
        quantity=10,
        price=70000,
        dry_run=False,
        thesis="t",
        strategy="t",
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_required"


@pytest.mark.asyncio
async def test_required_mode_does_not_block_mock_path(monkeypatch, _stub_pricing):
    # ROB-659: required-mode fail-close is scoped to LIVE (not is_mock). Mock
    # scalping / automation callers that can't mint a hash must NOT be blocked,
    # otherwise flipping ORDER_APPROVAL_HASH_MODE=required breaks internal loops.
    monkeypatch.setattr(settings, "order_approval_hash_mode", "required")
    sentinel = {"success": True, "sent": True}
    monkeypatch.setattr(oe, "_execute_and_record", AsyncMock(return_value=sentinel))
    res = await oe._place_order_impl(
        symbol="005930",
        side="buy",
        market="KR",
        order_type="limit",
        quantity=10,
        price=70000,
        dry_run=False,
        thesis="t",
        strategy="t",
        is_mock=True,
    )
    # Gate passed through to execution; no approval_hash_required rejection.
    assert res is sentinel
    assert res.get("error_code") != "approval_hash_required"


@pytest.mark.asyncio
async def test_required_mode_still_blocks_live_path(monkeypatch, _stub_pricing):
    # The live counterpart of the mock exemption above stays fail-closed.
    monkeypatch.setattr(settings, "order_approval_hash_mode", "required")
    monkeypatch.setattr(oe, "_execute_and_record", AsyncMock(return_value={"x": 1}))
    res = await oe._place_order_impl(
        symbol="005930",
        side="buy",
        market="KR",
        order_type="limit",
        quantity=10,
        price=70000,
        dry_run=False,
        thesis="t",
        strategy="t",
        is_mock=False,
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_required"


@pytest.mark.asyncio
async def test_mismatched_hash_fails_closed_with_diff(monkeypatch, _stub_pricing):
    monkeypatch.setattr(settings, "order_approval_hash_mode", "required")
    preview = await oe._place_order_impl(
        symbol="005930",
        side="buy",
        market="KR",
        order_type="limit",
        quantity=10,
        price=70000,
        dry_run=True,
        thesis="t",
        strategy="t",
    )
    token = preview["approval_hash"]
    # place a DIFFERENT quantity with the old token
    res = await oe._place_order_impl(
        symbol="005930",
        side="buy",
        market="KR",
        order_type="limit",
        quantity=11,
        price=70000,
        dry_run=False,
        thesis="t",
        strategy="t",
        approval_hash=token,
    )
    assert res["success"] is False
    assert res["error_code"] == "approval_hash_mismatch"
    assert "diff" in res
