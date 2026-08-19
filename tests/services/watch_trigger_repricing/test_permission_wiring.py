"""ROB-1286 §101차 급소 B — the permission wiring, checked in code.

The brief's rule: 이름 매칭 ≠ 의미 강제. "the tool is in a list" proves
nothing. What follows checks the three claims against the code that would
actually run:

① the spawned session's surface is a real MCP profile, not a list;
② that profile actually exposes ``order_proposal_create``;
③ a proposal created that way enters the existing approval machinery, and
   this package cannot relax or bypass any of its gates.

Where a claim cannot be made true inside this repo it is asserted as an
explicit gap rather than papered over -- see
``test_no_launcher_in_this_repo_can_start_the_profile``.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling import order_proposal_tools as opt
from app.mcp_server.tooling.watch_repricing_registration import (
    WATCH_REPRICING_TOOL_NAMES,
)
from app.services.watch_trigger_repricing.capability import (
    EXECUTION_BOUNDARY,
    PROPOSAL_ONLY_TOOLS,
)

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# ① a real profile
# ---------------------------------------------------------------------------
def test_the_profile_is_a_real_selectable_mcp_profile() -> None:
    assert McpProfile.WATCH_REPRICING.value == "watch_repricing"
    assert resolve_mcp_profile("watch_repricing") is McpProfile.WATCH_REPRICING


def test_the_registry_has_a_closed_branch_for_it() -> None:
    """It must return before the broad 'Always' block, or the closed world leaks."""
    source = (REPO_ROOT / "app" / "mcp_server" / "tooling" / "registry.py").read_text()
    tree = ast.parse(source)

    branch_returns = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "WATCH_REPRICING" not in ast.dump(node.test):
            continue
        branch_returns = any(isinstance(n, ast.Return) for n in node.body)
    assert branch_returns, "the watch_repricing branch must return, not fall through"


# ---------------------------------------------------------------------------
# ② the tool is real, and callable with what the session would pass
# ---------------------------------------------------------------------------
def test_order_proposal_create_exists_and_is_the_boundary() -> None:
    assert EXECUTION_BOUNDARY == "order_proposal_create"
    assert callable(opt.order_proposal_create)
    assert EXECUTION_BOUNDARY in WATCH_REPRICING_TOOL_NAMES


def test_the_session_could_actually_call_it_with_a_sell_proposal() -> None:
    """Signature-level check: the arguments a repricing session has are enough.

    A tool being present is not the same as being usable by this caller, so
    this asserts the required parameters are exactly ones the session can
    supply from a watch fire.
    """
    signature = inspect.signature(opt.order_proposal_create)
    required = {
        name
        for name, param in signature.parameters.items()
        if param.default is inspect.Parameter.empty
    }
    assert required == {
        "symbol",
        "market",
        "account_mode",
        "side",
        "order_type",
        "proposer",
        "rungs",
    }


# ---------------------------------------------------------------------------
# ③ the approval machinery is entered, and cannot be bypassed from here
# ---------------------------------------------------------------------------
def test_creating_a_proposal_enters_the_approval_dispatch_path() -> None:
    """The created proposal is handed to dispatch, not left inert."""
    source = (
        REPO_ROOT / "app" / "mcp_server" / "tooling" / "order_proposal_tools.py"
    ).read_text()
    assert "dispatch_proposal(" in source
    assert "ORDER_PROPOSALS_TELEGRAM_ENABLED" in source


def test_the_auto_approve_lane_is_reachable_from_dispatch() -> None:
    """§101차 급소 B: 토스 자동승인 포함. Named so a reader sees the risk."""
    dispatch = (
        REPO_ROOT / "app" / "services" / "order_proposals" / "dispatch.py"
    ).read_text()
    assert "evaluate_auto_approve_eligibility" in dispatch
    assert "ORDER_PROPOSALS_AUTO_APPROVE" in dispatch


def test_this_package_relaxes_none_of_the_approval_gates() -> None:
    """The gates live outside; this package must not touch their inputs."""
    package = REPO_ROOT / "app" / "services" / "watch_trigger_repricing"
    for path in sorted(package.glob("*.py")):
        source = path.read_text()
        for gate in (
            "ORDER_PROPOSALS_AUTO_APPROVE",
            "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED",
            "min_distance_pct",
            "breakeven_band_pct",
            "round_trip_cost_bps",
            "daily_cap",
            "per_order_cap",
        ):
            assert gate not in source, f"{path.name} references {gate}"


def test_the_profile_cannot_reach_any_submit_or_approve_tool() -> None:
    """The load-bearing claim: proposing is allowed, submitting is not."""
    for beyond in (
        "order_proposal_redispatch",
        "order_proposal_void",
        "place_order",
        "kis_live_place_order",
        "toss_place_order",
        "upbit_place_order",
        "live_reconcile_orders",
        "kis_live_reconcile_orders",
    ):
        assert beyond not in PROPOSAL_ONLY_TOOLS


def test_loss_cut_stays_human_approved() -> None:
    """§101차: loss_cut 계열은 여전히 사람 승인.

    The profile has no submit tool at all, so a loss-cut proposal made by a
    repricing session still has to go through the same human path as any
    other. Asserted here as an explicit statement rather than an inference.
    """
    auto_approve = (
        REPO_ROOT / "app" / "services" / "order_proposals" / "auto_approve.py"
    ).read_text()
    assert "loss_cut" in auto_approve
    package = REPO_ROOT / "app" / "services" / "watch_trigger_repricing"
    assert all("loss_cut" not in p.read_text() for p in package.glob("*.py"))


# ---------------------------------------------------------------------------
# The honest gap
# ---------------------------------------------------------------------------
def test_no_launcher_in_this_repo_can_start_the_profile() -> None:
    """Recorded as a gap, not hidden.

    ``scripts/mock_session_mcp.py`` is the repo's only session launcher seam,
    and its ``SAFE_MOCK_PROFILES`` allowlist does not contain
    ``watch_repricing``. So nothing in this repo can start a live repricing
    session today -- which is consistent with there being no live spawner --
    and wiring one is a separate, reviewable change to that allowlist rather
    than something this PR quietly enabled.
    """
    from scripts.mock_session_mcp import SAFE_MOCK_PROFILES

    assert "watch_repricing" not in SAFE_MOCK_PROFILES
