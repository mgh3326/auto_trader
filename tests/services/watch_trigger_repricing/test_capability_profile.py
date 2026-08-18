"""ROB-1286 r2 / BLOCKER-4 — the boundary as a capability, not a sentence.

r1 put ``"order_proposal_create"`` in a string field on the spawn request.
Nothing read it. The MCP profile a live spawner would most plausibly reuse
(``tradingcodex_execution``) registers ``place_order``,
``kis_live_place_order``, ``toss_place_order`` and their cancel variants,
so the session would have held a direct broker mutation surface while its
request still *said* proposal-only.

These tests check the profile against the **real registry constants**, not
against a copy: the assertion is that the tool set this package grants and
the tool sets the repo actually registers as order mutations are disjoint.
If someone adds a broker tool to any of those registries and it somehow
lands in this allowlist, the intersection stops being empty and this fails.
"""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.order_proposal_tools import ORDER_PROPOSAL_TOOL_NAMES
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import TOSS_LIVE_ORDER_TOOL_NAMES
from app.mcp_server.tooling.tradingcodex_execution_registration import (
    KIWOOM_MOCK_EXECUTION_TOOL_NAMES,
    TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES,
    TRADINGCODEX_EXECUTION_TOOL_NAMES,
)
from app.services.watch_trigger_repricing.capability import (
    EXECUTION_BOUNDARY,
    PROPOSAL_ONLY_PROFILE,
    PROPOSAL_ONLY_TOOLS,
    CapabilityBoundaryViolation,
    CapabilityProfile,
    assert_proposal_only,
)
from app.services.watch_trigger_repricing.claims import InMemoryClaimStore
from app.services.watch_trigger_repricing.orchestrator import run_repricing_tick
from app.services.watch_trigger_repricing.spawn import SpawnRequest

from .conftest import INCIDENT_TICK, make_event

pytestmark = pytest.mark.unit

# Every tool set in the repo that can mutate an order at a broker.
MUTATION_TOOL_NAMES = (
    ORDER_TOOL_NAMES
    | KIS_LIVE_ORDER_TOOL_NAMES
    | KIS_MOCK_ORDER_TOOL_NAMES
    | TOSS_LIVE_ORDER_TOOL_NAMES
    | KIWOOM_MOCK_EXECUTION_TOOL_NAMES
)


# ---------------------------------------------------------------------------
# 1. The profile itself
# ---------------------------------------------------------------------------
def test_profile_grants_no_order_mutation_tool() -> None:
    """Checked against the live registries, not a hand-copied list."""
    overlap = PROPOSAL_ONLY_TOOLS & MUTATION_TOOL_NAMES
    assert overlap == set(), f"proposal-only profile grants order tools: {overlap}"


def test_profile_grants_nothing_the_execution_profile_denies() -> None:
    """Cross-checked against the repo's own privileged-profile deny set.

    Not a subset assertion: the profile also grants plain market-data reads
    (``get_quote`` and friends) that ``tradingcodex_execution`` does not
    itself register, and requiring a subset would say something false. What
    must hold is the deny direction -- nothing this profile grants appears
    in the set that profile explicitly refuses.
    """
    denied = PROPOSAL_ONLY_TOOLS & TRADINGCODEX_EXECUTION_FORBIDDEN_TOOL_NAMES
    assert denied == set(), f"profile grants explicitly-denied tools: {denied}"


def test_profile_shares_only_non_mutating_tools_with_the_execution_profile() -> None:
    """Where the two overlap, the overlap must contain no order tool."""
    overlap = PROPOSAL_ONLY_TOOLS & TRADINGCODEX_EXECUTION_TOOL_NAMES
    assert overlap, "the two profiles are unrelated; the cross-check is vacuous"
    assert overlap & MUTATION_TOOL_NAMES == set()


def test_profile_excludes_the_dangerous_proposal_tools() -> None:
    """order_proposals is not wholesale-safe; three of its tools are not."""
    for tool in (
        "order_proposal_redispatch",
        "order_proposal_void",
        "support_reserve_net_consume",
    ):
        assert tool in ORDER_PROPOSAL_TOOL_NAMES, f"{tool} vanished from the registry"
        assert tool not in PROPOSAL_ONLY_TOOLS


def test_profile_contains_the_boundary() -> None:
    assert EXECUTION_BOUNDARY == "order_proposal_create"
    assert EXECUTION_BOUNDARY in PROPOSAL_ONLY_TOOLS
    assert PROPOSAL_ONLY_PROFILE.tools == PROPOSAL_ONLY_TOOLS


# ---------------------------------------------------------------------------
# 2. Enforcement -- a widened profile is refused, not merely wrong
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "smuggled",
    sorted(MUTATION_TOOL_NAMES),
)
def test_every_order_tool_is_refused_by_the_allowlist(smuggled: str) -> None:
    """Adding *any* registered order tool to a profile raises."""
    widened = CapabilityProfile(
        name="widened",
        tools=PROPOSAL_ONLY_TOOLS | {smuggled},
    )

    with pytest.raises(CapabilityBoundaryViolation) as excinfo:
        assert_proposal_only(widened)
    assert smuggled in str(excinfo.value)


def test_an_unknown_future_tool_is_refused_too() -> None:
    """Allowlist, not deny-list: a tool invented tomorrow is refused today."""
    with pytest.raises(CapabilityBoundaryViolation):
        assert_proposal_only(
            CapabilityProfile(
                name="future",
                tools=PROPOSAL_ONLY_TOOLS | {"some_broker_tool_added_next_quarter"},
            )
        )


def test_a_profile_without_the_boundary_is_refused() -> None:
    """A session that cannot propose would eat the fire and emit nothing."""
    with pytest.raises(CapabilityBoundaryViolation) as excinfo:
        assert_proposal_only(
            CapabilityProfile(
                name="toothless",
                tools=PROPOSAL_ONLY_TOOLS - {EXECUTION_BOUNDARY},
            )
        )
    assert EXECUTION_BOUNDARY in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. The enforcement point is request construction
# ---------------------------------------------------------------------------
def test_spawn_request_rejects_a_widened_profile() -> None:
    """No spawner ever sees an over-privileged request: it cannot be built."""
    with pytest.raises(CapabilityBoundaryViolation):
        SpawnRequest(
            event_uuid="evt-1",
            symbol="005930",
            market="kr",
            kst_date="2026-08-18",
            label="opa-watch-005930-0906",
            capability_profile=CapabilityProfile(
                name="smuggled",
                tools=PROPOSAL_ONLY_TOOLS | {sorted(MUTATION_TOOL_NAMES)[0]},
            ),
        )


def test_spawn_request_rejects_a_widened_execution_boundary() -> None:
    with pytest.raises(ValueError, match="execution_boundary"):
        SpawnRequest(
            event_uuid="evt-1",
            symbol="005930",
            market="kr",
            kst_date="2026-08-18",
            label="opa-watch-005930-0906",
            execution_boundary="order_proposal_approve",
        )


def test_spawn_request_defaults_to_the_proposal_only_profile() -> None:
    request = SpawnRequest(
        event_uuid="evt-1",
        symbol="005930",
        market="kr",
        kst_date="2026-08-18",
        label="opa-watch-005930-0906",
    )

    assert request.capability_profile is PROPOSAL_ONLY_PROFILE
    assert request.execution_boundary == EXECUTION_BOUNDARY
    assert request.capability_profile.tools & MUTATION_TOOL_NAMES == set()


def test_every_request_the_orchestrator_builds_carries_the_profile() -> None:
    """The tick has no code path that constructs an unprofiled request."""
    from app.services.watch_trigger_repricing.spawn import DrySessionSpawner

    spawner = DrySessionSpawner()
    run_repricing_tick(
        [
            make_event(event_uuid="evt-a", symbol="005930"),
            make_event(event_uuid="evt-b", symbol="039200"),
        ],
        store=InMemoryClaimStore(),
        now=INCIDENT_TICK,
        spawner=spawner,
    )

    assert len(spawner.requests) == 2
    for request in spawner.requests:
        assert request.capability_profile is PROPOSAL_ONLY_PROFILE
        assert request.capability_profile.tools & MUTATION_TOOL_NAMES == set()


def test_the_tick_report_names_the_profile() -> None:
    """An operator reading the tick output can see which profile was granted."""
    result = run_repricing_tick(
        [make_event(event_uuid="evt-1")],
        store=InMemoryClaimStore(),
        now=INCIDENT_TICK,
    )

    payload = result.as_dict()["spawned"][0]
    assert payload["capabilityProfile"] == "rob1286-proposal-only"
    assert payload["executionBoundary"] == EXECUTION_BOUNDARY
