# tests/mcp_server/test_kis_live_tool_descriptions.py
from app.mcp_server.tooling import orders_kis_variants as mod


def _tool_desc(name: str) -> str:
    # descriptions live as string literals in the module source.
    import inspect

    return inspect.getsource(mod)


def test_place_order_desc_mentions_account_truth():
    src = _tool_desc("kis_live_place_order")
    assert "get_holdings" in src
    assert "get_available_capital" in src


def test_reconcile_desc_mentions_local_bookkeeping():
    src = _tool_desc("kis_live_reconcile_orders")
    assert "LOCAL bookkeeping" in src
