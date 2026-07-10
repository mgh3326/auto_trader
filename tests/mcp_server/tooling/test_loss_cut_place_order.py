from unittest.mock import patch

import pytest

from app.mcp_server.tooling import order_execution as oe
from app.mcp_server.tooling import order_validation as ov


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_loss_cut_returns_all_violations_single_response():
    with patch.object(ov, "get_caller_agent_id", return_value="nobody"):
        resp = await oe._place_order_impl(
            symbol="KRW-DOT",
            side="buy",
            market="crypto",
            order_type="market",
            price=1244.0,
            quantity=10,
            dry_run=True,
            exit_intent="loss_cut",
            retrospective_id=None,
        )
    assert resp["success"] is False
    assert resp["error"] == "loss_cut_preconditions_failed"
    assert isinstance(resp["violations"], list) and len(resp["violations"]) >= 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loss_cut_and_defensive_trim_mutually_exclusive():
    resp = await oe._place_order_impl(
        symbol="KRW-DOT",
        side="sell",
        market="crypto",
        order_type="limit",
        price=1244.0,
        quantity=10,
        dry_run=True,
        exit_intent="loss_cut",
        defensive_trim=True,
        approval_issue_id="ROB-800",
    )
    assert resp["success"] is False
    assert "mutually exclusive" in resp["error"].lower()
