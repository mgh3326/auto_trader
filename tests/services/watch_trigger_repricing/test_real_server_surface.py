"""ROB-1290 r3 follow-up — the boundary is the server, so test the server.

r3's measurement: an in-process guard cannot close the approval boundary.
Capability tokens, ``@final`` and in-process allowlists all fall to a
subclass, an injected judge or a swapped module global, because the raw
callable never stops being reachable inside the interpreter.

The boundary that does hold is the process boundary -- the set of tools an
MCP server hands out. So the proof has to be made against a *built server*,
not a registration stand-in:

* ``tools/list`` returns exactly the proposal-only allowlist -- closed
  equality, not containment (:data:`PROPOSAL_ONLY_TOOLS`);
* a ``tools/call`` naming a broker order tool comes back
  ``isError=True  "Unknown tool"`` **from the server**. The client is not
  filtering: it sends a name it was never given and the server refuses it.

``tests/.../test_live_spawner_contract.py`` already pins registration
against a ``_Registry`` stand-in. A stand-in cannot answer either question
above -- it has no ``tools/list`` and no call path -- which is exactly the
gap these tests close.
"""

from __future__ import annotations

import pathlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import NotFoundError

from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
    LIVE_RECONCILE_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_us_variants import KIWOOM_MOCK_US_TOOL_NAMES
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.orders_toss_variants import TOSS_LIVE_ORDER_TOOL_NAMES
from app.mcp_server.tooling.paper_execution_registration import (
    PAPER_EXECUTION_TOOL_NAMES,
)
from app.mcp_server.tooling.watch_repricing_registration import (
    WATCH_REPRICING_TOOL_NAMES,
    assert_provisioned_surface,
    build_watch_repricing_server,
    provisioned_tool_names,
)
from app.services.watch_trigger_repricing.capability import (
    EXECUTION_BOUNDARY,
    PROPOSAL_ONLY_TOOLS,
    CapabilityBoundaryViolation,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# The submit-family names the raw round trip is run against one by one. Every
# order registry in the repo is swept in
# ``test_no_order_registry_name_can_be_called`` below; this shorter list is
# the one that gets the full client -> server -> raw-payload treatment.
SUBMIT_TOOLS = (
    "place_order",
    "kis_live_place_order",
    "kis_mock_place_order",
    "toss_place_order",
    "kiwoom_mock_place_order",
    "kiwoom_mock_us_place_order",
    "paper_execution_submit_order",
)

# Every name any order registry registers, so a tool added to one of them
# next quarter is swept without this file being edited.
ORDER_REGISTRY_NAMES = frozenset(
    set(ORDER_TOOL_NAMES)
    | set(KIS_LIVE_ORDER_TOOL_NAMES)
    | set(KIS_MOCK_ORDER_TOOL_NAMES)
    | set(LIVE_RECONCILE_TOOL_NAMES)
    | set(KIWOOM_MOCK_TOOL_NAMES)
    | set(KIWOOM_MOCK_US_TOOL_NAMES)
    | set(TOSS_LIVE_ORDER_TOOL_NAMES)
    | set(PAPER_EXECUTION_TOOL_NAMES)
)


def _error_text(result: object) -> str:
    """The text the server put in a ``CallToolResult``."""
    blocks = getattr(result, "content", None) or []
    return "\n".join(str(getattr(block, "text", "")) for block in blocks)


# ---------------------------------------------------------------------------
# ALLOWLIST_CLOSED — what a built server serves, compared with ==
# ---------------------------------------------------------------------------
async def test_a_real_server_serves_exactly_the_allowlist() -> None:
    """Not a subset, not a superset -- asked of a real FastMCP, not a stub."""
    server = build_watch_repricing_server()

    served = await provisioned_tool_names(server)

    assert served == PROPOSAL_ONLY_TOOLS
    assert served == WATCH_REPRICING_TOOL_NAMES
    assert len(served) == len(PROPOSAL_ONLY_TOOLS)


async def test_the_attestation_helper_accepts_that_server() -> None:
    server = build_watch_repricing_server()

    assert await assert_provisioned_surface(server) == PROPOSAL_ONLY_TOOLS


async def test_a_client_is_offered_exactly_the_allowlist() -> None:
    """The same equality, over the wire this time: a real ``tools/list``."""
    server = build_watch_repricing_server()

    async with Client(server) as client:
        offered = frozenset(tool.name for tool in await client.list_tools())

    assert offered == PROPOSAL_ONLY_TOOLS


# ---------------------------------------------------------------------------
# The comparison is not vacuous — drift in either direction is refused
# ---------------------------------------------------------------------------
async def test_a_widened_server_is_refused() -> None:
    """One broker tool added to the built server, and attestation fails."""
    server = build_watch_repricing_server()

    @server.tool(name="toss_place_order")
    def _leaked() -> str:  # pragma: no cover - never called
        return "should not exist"

    with pytest.raises(CapabilityBoundaryViolation) as exc:
        await assert_provisioned_surface(server)
    assert "toss_place_order" in str(exc.value)


async def test_a_narrowed_server_is_refused() -> None:
    """Dropping the boundary is the other failure: a session that can only read."""
    server = build_watch_repricing_server()
    server.local_provider.remove_tool(EXECUTION_BOUNDARY)

    with pytest.raises(CapabilityBoundaryViolation) as exc:
        await assert_provisioned_surface(server)
    assert EXECUTION_BOUNDARY in str(exc.value)


# ---------------------------------------------------------------------------
# DENIED_AT_SERVER — the call is refused, not merely absent from a list
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("denied", SUBMIT_TOOLS)
async def test_an_order_tool_call_is_refused_by_the_server(denied: str) -> None:
    """Raw ``tools/call`` payload, straight from the server.

    ``call_tool_mcp`` returns the server's ``CallToolResult`` instead of
    raising, so the assertion is made against the response the server
    produced rather than against a client-side exception. The client asks
    for a name that was never in the ``tools/list`` it received -- nothing on
    the client side could have decided the answer.
    """
    server = build_watch_repricing_server()

    async with Client(server) as client:
        offered = frozenset(tool.name for tool in await client.list_tools())
        assert denied not in offered
        result = await client.call_tool_mcp(denied, {})

    assert result.isError is True
    assert "Unknown tool" in _error_text(result)
    assert denied in _error_text(result)


@pytest.mark.parametrize("denied", SUBMIT_TOOLS)
async def test_the_servers_own_call_path_refuses_with_no_client_involved(
    denied: str,
) -> None:
    """``FastMCP.call_tool`` is what the ``tools/call`` handler dispatches to.

    Calling it directly removes the client and its transport from the loop
    entirely, so what is left is the server's own refusal.
    """
    server = build_watch_repricing_server()
    # Interlock: if the name did resolve, ``call_tool`` would *run* the broker
    # tool rather than refuse it. Prove it cannot resolve before calling it.
    assert await server.get_tool(denied) is None

    with pytest.raises(NotFoundError) as exc:
        await server.call_tool(denied, {})
    assert denied in str(exc.value)


async def test_no_order_registry_name_can_be_called() -> None:
    """Sweep every name the repo's order registries define, not a curated few."""
    server = build_watch_repricing_server()
    # A registry rename that emptied this set would make the sweep vacuous.
    assert len(ORDER_REGISTRY_NAMES) >= 30
    assert ORDER_REGISTRY_NAMES & PROPOSAL_ONLY_TOOLS == frozenset()

    for name in sorted(ORDER_REGISTRY_NAMES):
        assert await server.get_tool(name) is None, name
        with pytest.raises(NotFoundError):
            await server.call_tool(name, {})


# ---------------------------------------------------------------------------
# Negative control — the refusal is name-scoped, not "everything fails"
# ---------------------------------------------------------------------------
async def test_an_allowed_tool_fails_the_other_way_on_the_same_path() -> None:
    """Same client, same call, allowed name: argument validation, not absence.

    Without this the denial above would also pass on a server that answers
    every call with an error.
    """
    server = build_watch_repricing_server()

    async with Client(server) as client:
        result = await client.call_tool_mcp(EXECUTION_BOUNDARY, {})

    assert result.isError is True
    text = _error_text(result)
    assert "Unknown tool" not in text
    assert "validation error" in text.lower()


async def test_resolution_succeeds_for_the_boundary_and_fails_for_a_broker() -> None:
    server = build_watch_repricing_server()

    assert await server.get_tool(EXECUTION_BOUNDARY) is not None
    assert await server.get_tool("toss_place_order") is None


# ---------------------------------------------------------------------------
# Provisioning is not arming
# ---------------------------------------------------------------------------
async def test_provisioning_reaches_for_no_transport() -> None:
    """A built server is inert until something serves it, and nothing here does.

    ``scripts/mock_session_mcp.py``'s ``SAFE_MOCK_PROFILES`` still excludes
    this profile (``test_permission_wiring.py``), so no launcher in the repo
    can start it. This adds the other half: the provisioning helper itself
    never opens a transport.
    """
    source = (
        REPO_ROOT / "app" / "mcp_server" / "tooling" / "watch_repricing_registration.py"
    ).read_text()

    for token in (
        ".run(",
        "run_async",
        "run_http_async",
        "run_stdio_async",
        "http_app",
    ):
        assert token not in source, token
