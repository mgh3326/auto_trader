from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_screen_stocks_snapshot_description_notes_consensus_cache():
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
    desc = mcp.descriptions["screen_stocks_snapshot"].lower()
    assert "consensus" in desc
    assert "cache" in desc or "cached" in desc
