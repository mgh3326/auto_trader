from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.mirror_counterfactual_tools import (
    kis_mock_mirror_execute_report,
)

MIRROR_COUNTERFACTUAL_TOOL_NAMES: set[str] = {"kis_mock_mirror_execute_report"}


def register_mirror_counterfactual_tools(mcp: Any) -> None:
    _ = mcp.tool(
        name="kis_mock_mirror_execute_report",
        description=(
            "ROB-734: execute a report's original analysis plan as KIS mock "
            "mirror counterfactual orders. dry_run=True previews only. "
            "dry_run=False mutates only account_mode='kis_mock', never live. "
            "Uses original report item sizing, not operator-approved trims. "
            "Returns caveats because KIS mock fills omit queue/liquidity/slippage."
        ),
    )(kis_mock_mirror_execute_report)
