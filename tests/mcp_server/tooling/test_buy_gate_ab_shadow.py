from __future__ import annotations

from pathlib import Path
from typing import Any

from app.mcp_server.tooling.analysis_readonly_registration import (
    ANALYSIS_READONLY_TOOL_NAMES,
)
from app.mcp_server.tooling.buy_gate_ab_shadow import (
    evaluate_buy_gate_ab_shadow_impl,
)
from app.mcp_server.tooling.buy_gate_ab_shadow_registration import (
    register_buy_gate_ab_shadow_tools,
)
from app.mcp_server.tooling.route_request_lanes import (
    MUTATION_TOOLS,
    READ_ONLY_ADVISORY_TOOLS,
)
from app.services.buy_gate_ab_shadow.epoch import COLLECTION_EPOCH
from app.services.buy_gate_ab_shadow.spec import PINNED_SPEC_SHA256, spec_sha256


def _candidate() -> dict[str, Any]:
    return {
        "symbol": "005930",
        "market": "kr",
        "current_price": "70000",
        "support_strength": "moderate",
        "support_distance_pct": "4",
        "rsi": "40",
        "honest_upside_pct": "45",
        "other_gate_bits": {
            "liquid_midcap": True,
            "concentration": True,
            "overhang": True,
        },
    }


def test_impl_returns_shadow_forecasts_without_writing_or_promoting() -> None:
    result = evaluate_buy_gate_ab_shadow_impl(
        [_candidate()],
        evaluation_as_of="2026-08-31T00:30:00+00:00",
        created_by="orch-mock",
    )
    assert result["success"] is True
    assert result["promote"] is False
    assert result["live_gate_impact"] is False
    assert result["do_not_use_for_policy_change"] is True
    assert result["spec_sha256"] == spec_sha256() == PINNED_SPEC_SHA256
    assert result["collection_epoch"] == COLLECTION_EPOCH.as_dict()
    assert result["counts"]["b_only"] == 1
    assert len(result["shadow_buy_forecasts"]) == 2
    assert result["candidates"][0]["cohort"] == "b_only"


def test_blank_created_by_fails_closed() -> None:
    result = evaluate_buy_gate_ab_shadow_impl(
        [_candidate()],
        evaluation_as_of="2026-08-31T00:30:00+00:00",
        created_by=" ",
    )
    assert result["success"] is False
    assert result["promote"] is False
    assert result["live_gate_impact"] is False


def test_registration_is_observation_only() -> None:
    class _FakeMCP:
        def __init__(self) -> None:
            self.descriptions: dict[str, str] = {}

        def tool(self, *, name: str, description: str, **_: Any) -> Any:
            def decorate(function: Any) -> Any:
                self.descriptions[name] = description
                return function

            return decorate

    mcp = _FakeMCP()
    register_buy_gate_ab_shadow_tools(mcp)  # type: ignore[arg-type]
    description = mcp.descriptions["evaluate_buy_gate_ab_shadow"].lower()
    assert "observation-only" in description
    assert "never creates a proposal" in description
    assert "policy change" in description
    assert "evaluate_buy_gate_ab_shadow" in ANALYSIS_READONLY_TOOL_NAMES
    assert "evaluate_buy_gate_ab_shadow" in READ_ONLY_ADVISORY_TOOLS
    assert "evaluate_buy_gate_ab_shadow" not in MUTATION_TOOLS


def test_tool_source_does_not_call_forecast_save() -> None:
    source = Path(
        __import__(
            "app.mcp_server.tooling.buy_gate_ab_shadow", fromlist=["dummy"]
        ).__file__
        or ""
    ).read_text(encoding="utf-8")
    assert "forecast_save(" not in source
    assert "save_forecast(" not in source
