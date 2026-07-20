from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_discovery_tool_descriptions_guide_selection_and_snapshot_freshness():
    from app.mcp_server.tooling.analysis_registration import register_analysis_tools

    class _FakeMCP:
        def __init__(self):
            self.descriptions = {}

        def tool(self, *, name, description, **kw):
            def _d(fn):
                self.descriptions[name] = description
                return fn

            return _d

    mcp = _FakeMCP()
    register_analysis_tools(mcp)
    snapshot_desc = mcp.descriptions["screen_stocks_snapshot"].lower()
    assert "consensus" in snapshot_desc
    assert "cache" in snapshot_desc or "cached" in snapshot_desc
    assert "screen_stocks" in snapshot_desc
    assert "get_top_stocks" in snapshot_desc
    assert "get_momentum_candidates" in snapshot_desc
    assert "priceLabel".lower() in snapshot_desc
    assert "changepctlabel" in snapshot_desc
    assert "rsi" in snapshot_desc
    assert "one session" in snapshot_desc
    assert "get_quote" in snapshot_desc
    assert "analyze_stock_batch" in snapshot_desc

    screen_desc = mcp.descriptions["screen_stocks"].lower()
    assert "krx_session_expired" in screen_desc
    assert "screen_stocks_snapshot" in screen_desc

    assert "screen_stocks_snapshot" in mcp.descriptions["get_top_stocks"].lower()
    assert (
        "screen_stocks_snapshot" in mcp.descriptions["get_momentum_candidates"].lower()
    )
