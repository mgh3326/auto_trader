import pytest

from app.mcp_server.tooling import order_proposal_tools as opt


@pytest.mark.asyncio
async def test_create_then_get_then_list():
    created = await opt.order_proposal_create(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="operator:sess-x",
        thesis="t",
        strategy="ladder",
        rungs=[
            {
                "rung_index": 0,
                "side": "buy",
                "quantity": "10",
                "limit_price": "2226000",
                "notional": None,
            }
        ],
    )
    assert created["success"] is True
    pid = created["proposal_id"]

    got = await opt.order_proposal_get(proposal_id=pid)
    assert got["success"] is True
    assert got["proposal"]["symbol"] == "000660"
    assert len(got["rungs"]) == 1

    listed = await opt.order_proposal_list(limit=10, symbol="000660")
    assert listed["success"] is True
    assert any(p["proposal_id"] == pid for p in listed["proposals"])


@pytest.mark.asyncio
async def test_create_rejects_empty_rungs():
    res = await opt.order_proposal_create(
        symbol="A",
        market="equity_kr",
        account_mode="kis_live",
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[],
    )
    assert res["success"] is False
    assert "rung" in res["error"].lower()


@pytest.mark.unit
def test_tools_registered_and_names_exported():
    from fastmcp import FastMCP

    mcp = FastMCP(name="t", on_duplicate="error")
    opt.register_order_proposal_tools(mcp)
    assert opt.ORDER_PROPOSAL_TOOL_NAMES == {
        "order_proposal_create",
        "order_proposal_get",
        "order_proposal_list",
    }
