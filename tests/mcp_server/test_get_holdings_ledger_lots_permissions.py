"""Task #963 (operator ruling: option B) — no permission surface change.

The ledger lots block rides on ``get_holdings``, so the kr lane allowlist and
the robin harness argv list need no edit. The #678 harness denial of
``kis_live_get_order_history`` is unchanged. ROB-1436 retired the live-*
profiles (live sessions run on DEFAULT), so the live.yaml manifest checks that
used to live here are gone with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    HARNESS_DENIED_TOOL_BASIS,
    HARNESS_DENIED_TOOLS,
)
from tests.mcp_server._registration_recorder import RegistrationRecorder

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]
DENIED = "kis_live_get_order_history"


def test_get_holdings_is_on_the_kr_lane_allowlist() -> None:
    lines = (REPO_ROOT / "config/mcp_lane_allowlists/kr.txt").read_text().splitlines()
    tools = {line.split("\t")[0] for line in lines if line and not line.startswith("#")}
    assert "get_holdings" in tools
    assert not {t for t in tools if "ledger_lots" in t or "kis_lots" in t}
    assert DENIED not in tools


def test_default_registered_get_holdings_accepts_the_opt_in_flag() -> None:
    import inspect

    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=McpProfile.DEFAULT)
    parameters = inspect.signature(recorder.tools["get_holdings"]).parameters
    assert parameters["include_ledger_lots"].default is False


def test_678_kis_live_get_order_history_stays_harness_denied() -> None:
    assert DENIED in HARNESS_DENIED_TOOLS
    assert HARNESS_DENIED_TOOL_BASIS == "live_session_harness_denied_678"
    for lane in ("kr", "us", "crypto"):
        lines = (
            (REPO_ROOT / f"config/mcp_lane_allowlists/{lane}.txt")
            .read_text()
            .splitlines()
        )
        tools = {line.split("\t")[0] for line in lines if line and line[0] != "#"}
        assert DENIED not in tools, f"{lane}: #678 denial must hold"
