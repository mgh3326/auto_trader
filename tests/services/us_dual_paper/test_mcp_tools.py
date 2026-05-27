import pytest

from app.mcp_server.tooling.us_dual_paper import (
    US_DUAL_PAPER_TOOL_NAMES,
    us_dual_paper_capability_matrix,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_capability_matrix_tool_returns_both_scopes():
    result = await us_dual_paper_capability_matrix()
    assert set(result["matrix"]) == {"kis_mock", "alpaca_paper"}
    assert result["submit_enabled"] is False


@pytest.mark.unit
def test_tool_names_pinned():
    assert "us_dual_paper_capability_matrix" in US_DUAL_PAPER_TOOL_NAMES
    assert "us_dual_paper_account_states" in US_DUAL_PAPER_TOOL_NAMES
