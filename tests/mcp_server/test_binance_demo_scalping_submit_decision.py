from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

import app.mcp_server.tooling.binance_demo_scalping_handler as mod


@pytest.mark.asyncio
async def test_dry_run_returns_plan_no_order() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="funding flip", dry_run=True
    )
    assert result["status"] == "planned"
    assert result["dry_run"] is True
    assert result["symbol"] == "XRPUSDT"
    assert result["side"] == "BUY"
    assert result["session_tag"] == "llm"
    assert "rationale" in result


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_symbol() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="BTCUSDT", side="BUY", rationale="x", dry_run=True
    )
    assert result["status"] == "rejected"
    assert "symbol" in result["error"].lower()


@pytest.mark.asyncio
async def test_rejects_empty_rationale() -> None:
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="  ", dry_run=True
    )
    assert result["status"] == "rejected"
    assert "rationale" in result["error"].lower()


@pytest.mark.asyncio
async def test_confirm_executes_monitored_with_llm_tag() -> None:
    fake_result = type(
        "R",
        (),
        {
            "status": "reconciled",
            "open_client_order_id": "rob307-x",
            "close_client_order_id": "rob307-y",
            "exit_reason": "take_profit",
            "to_evidence_dict": lambda self: {"status": "reconciled"},
        },
    )()
    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return fake_result

    with patch.object(
        mod, "_execute_confirmed_round_trip", AsyncMock(side_effect=fake_run)
    ):
        result = await mod.binance_demo_scalping_submit_decision(
            symbol="SOLUSDT",
            side="SELL",
            rationale="OI surge fade",
            dry_run=False,
            confirm=True,
        )
    assert result["status"] == "reconciled"
    assert captured["session_tag"] == "llm"
    assert captured["signal_snapshot"]["rationale"] == "OI surge fade"
    assert captured["signal_snapshot"]["source"] == "llm"
    assert captured["symbol"] == "SOLUSDT"
    assert captured["side"] == "SELL"


@pytest.mark.asyncio
async def test_confirm_required_for_real_order() -> None:
    # dry_run False but confirm False → still a plan, no execution.
    result = await mod.binance_demo_scalping_submit_decision(
        symbol="XRPUSDT", side="BUY", rationale="x", dry_run=False, confirm=False
    )
    assert result["status"] == "planned"
    assert result["dry_run"] is True
