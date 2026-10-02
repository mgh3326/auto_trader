"""#1244 — the paper-execution route contract and its fail-closed guards.

The h3-crypto-paper profile (#1171) has no proposal tool by design, so the
proposal-led buy/sell contract was degraded on every H3 run and the runner's
bootstrap check halted. route_request now takes an explicit
``execution_surface`` chosen at registration; only that profile selects the
paper simulator, and only crypto buy/sell move to ``paper-execution-v1``.

Mutants are counted from ``route_request_lanes.py`` ON DISK: every operand of
the three fail-closed conjunctions (``_paper_route``'s return, the paper
contract's ``execution_ready`` and the paper-tool allowance in
``build_route_plan``). Each mutant compiles a copy of the module with ONE
operand forced to ``True`` and runs the scenario only that operand protects;
the real module satisfies the invariant, the mutant must fail it by assertion
(not by crashing). An operand without a declared invariant fails
``test_every_fail_closed_operand_has_a_mutant``.

Invariant sentences (one per mutant):
- SURFACE: a proposal-led registration never reports the paper contract.
- PURPOSE: purpose=account_cleanup keeps the cleanup contract on the paper
  surface.
- LANE: discovery and market_brief never report the paper contract.
- MARKET: kr and us never report the paper contract, even on the paper surface.
- MISSING: the paper contract is degraded when a required paper tool is not
  registered.
- FOREIGN: the paper contract is degraded when any proposal, live or mock order
  tool is registered beside it.
- ALLOW_PAPER: a proposal-led route never allows the paper order tools.
- ALLOW_READY: a degraded paper contract never allows the paper order tools.
- NO_FOREIGN: the paper route never allows or sequences a proposal, live or
  mock order tool, even when one is registered.

The NO_FOREIGN mutant replaces the single on-disk ``paper_excluded``
assignment in ``build_route_plan`` with an empty set.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

from app.mcp_server.tooling import route_request_lanes as lanes
from app.mcp_server.tooling.h3_crypto_paper_registration import (
    H3_CRYPTO_PAPER_TOOL_NAMES,
)
from app.mcp_server.tooling.route_request_registration import (
    PAPER_SURFACE_DESCRIPTION,
    register_route_request_tools,
)
from tests._mcp_tooling_support import DummyMCP

pytestmark = pytest.mark.unit

SOURCE = Path(lanes.__file__)
REPO_ROOT = Path(__file__).resolve().parents[2]
H3 = set(H3_CRYPTO_PAPER_TOOL_NAMES)
PAPER = lanes.ROUTE_SURFACE_PAPER_SIMULATOR
PROPOSAL = lanes.ROUTE_SURFACE_PROPOSAL_LED
STAMP = {"version": "v", "content_hash": "h"}
THRESHOLDS = {"thresholds": {}}
# DEFAULT-like surface: the proposal tool and the paper tools both registered.
DEFAULT_LIKE = H3 | {"order_proposal_create"}


def _plan(
    module: Any,
    intent: str,
    market: str,
    registered: set[str] | None,
    *,
    surface: str = PAPER,
    purpose: str | None = None,
) -> dict[str, Any]:
    if registered is None:
        return module.build_registry_unavailable_plan(
            intent,
            market,
            verdict_thresholds=THRESHOLDS,
            policy_version=STAMP,
            purpose=purpose,
            execution_surface=surface,
        )
    return module.build_route_plan(
        intent,
        market,
        registered_tools=set(registered),
        verdict_thresholds=THRESHOLDS,
        policy_version=STAMP,
        purpose=purpose,
        execution_surface=surface,
    )


def _version(out: dict[str, Any]) -> str:
    return out["route_contract"]["version"]


# --- invariants (each returns normally on the real module) -----------------


def invariant_surface(module: Any) -> None:
    out = _plan(module, "buy_analysis", "crypto", DEFAULT_LIKE, surface=PROPOSAL)
    assert _version(out) == "proposal-led-v1"


def invariant_purpose(module: Any) -> None:
    out = _plan(module, "profit_taking", "crypto", None, purpose="account_cleanup")
    assert _version(out) == "cleanup-reduce-only-v1"
    assert out["hard_constraints"] == list(lanes.ACCOUNT_CLEANUP_HARD_CONSTRAINTS)


def invariant_lane(module: Any) -> None:
    for intent in ("discovery", "market_brief"):
        out = _plan(module, intent, "crypto", H3)
        assert _version(out) == "proposal-led-v1", intent


def invariant_market(module: Any) -> None:
    for market in ("kr", "us"):
        out = _plan(module, "buy_analysis", market, H3)
        assert _version(out) == "proposal-led-v1", market
        assert out["degraded"] is True


def invariant_missing(module: Any) -> None:
    out = _plan(module, "buy_analysis", "crypto", H3 - {"paper_reconcile_orders"})
    assert _version(out) == "paper-execution-v1"
    assert out["success"] is False and out["degraded"] is True
    assert out["route_contract"]["missing_required_tools"] == ["paper_reconcile_orders"]


def invariant_foreign(module: Any) -> None:
    out = _plan(module, "profit_taking", "crypto", H3 | {"kis_live_place_order"})
    assert _version(out) == "paper-execution-v1"
    assert out["success"] is False and out["degraded"] is True
    assert out["route_contract"]["foreign_execution_tools"] == ["kis_live_place_order"]


def invariant_allow_paper(module: Any) -> None:
    out = _plan(module, "buy_analysis", "crypto", DEFAULT_LIKE, surface=PROPOSAL)
    assert out["success"] is True
    assert not lanes.PAPER_EXECUTION_TOOLS & set(out["allowed_tools"])
    assert lanes.PAPER_EXECUTION_TOOLS <= set(out["blocked_actions"])


def invariant_allow_ready(module: Any) -> None:
    out = _plan(module, "buy_analysis", "crypto", H3 | {"order_proposal_create"})
    assert out["route_contract"]["execution_ready"] is False
    assert not lanes.PAPER_EXECUTION_TOOLS & set(out["allowed_tools"])
    assert lanes.PAPER_EXECUTION_TOOLS <= set(out["blocked_actions"])


def invariant_no_foreign(module: Any) -> None:
    out = _plan(module, "buy_analysis", "crypto", H3 | {"order_proposal_create"})
    named = set(out["allowed_tools"]) | {
        s["tool"] for s in out["standard_tool_sequence"]
    }
    assert "order_proposal_create" not in named
    assert "order_proposal_create" in out["route_contract"]["foreign_execution_tools"]


INVARIANTS = {
    "SURFACE": invariant_surface,
    "PURPOSE": invariant_purpose,
    "LANE": invariant_lane,
    "MARKET": invariant_market,
    "MISSING": invariant_missing,
    "FOREIGN": invariant_foreign,
    "ALLOW_PAPER": invariant_allow_paper,
    "ALLOW_READY": invariant_allow_ready,
    "NO_FOREIGN": invariant_no_foreign,
}

# (function, unparsed operand) -> invariant sentence key
DECLARED_OPERANDS: dict[tuple[str, str], str] = {
    ("_paper_route", "execution_surface == ROUTE_SURFACE_PAPER_SIMULATOR"): "SURFACE",
    ("_paper_route", "purpose is None"): "PURPOSE",
    ("_paper_route", "lane in PAPER_EXECUTION_LANES"): "LANE",
    ("_paper_route", "market in PAPER_EXECUTION_MARKETS"): "MARKET",
    ("_paper_route_contract", "not missing_required_tools"): "MISSING",
    ("_paper_route_contract", "not foreign_execution_tools"): "FOREIGN",
    ("build_route_plan", "paper"): "ALLOW_PAPER",
    ("build_route_plan", "route_contract['execution_ready']"): "ALLOW_READY",
}


def _guard_conjunctions(tree: ast.Module) -> dict[str, ast.BoolOp]:
    """The three fail-closed ``and`` chains, located on disk."""
    found: dict[str, ast.BoolOp] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if node.name == "_paper_route" and isinstance(child, ast.Return):
                found[node.name] = cast(ast.BoolOp, child.value)
            elif (
                node.name == "_paper_route_contract"
                and isinstance(child, ast.Assign)
                and ast.unparse(child.targets[0]) == "execution_ready"
            ):
                found[node.name] = cast(ast.BoolOp, child.value)
            elif (
                node.name == "build_route_plan"
                and isinstance(child, ast.If)
                and isinstance(child.test, ast.BoolOp)
                and "paper" in [ast.unparse(v) for v in child.test.values]
            ):
                found[node.name] = child.test
    for name, conjunction in found.items():
        assert isinstance(conjunction, ast.BoolOp), name
        assert isinstance(conjunction.op, ast.And), name
    return found


def _disk_operands() -> list[tuple[str, str]]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    return [
        (name, ast.unparse(value))
        for name, conjunction in sorted(_guard_conjunctions(tree).items())
        for value in conjunction.values
    ]


def _mutant(function: str, operand: str) -> types.ModuleType:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    conjunction = _guard_conjunctions(tree)[function]
    index = [ast.unparse(v) for v in conjunction.values].index(operand)
    conjunction.values[index] = ast.Constant(value=True)
    return _compile(tree, f"_route_lanes_mutant_{function}_{index}")


def _compile(tree: ast.Module, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE)
    sys.modules[name] = module
    try:
        exec(
            compile(ast.fix_missing_locations(tree), str(SOURCE), "exec"),
            module.__dict__,
        )
    finally:
        sys.modules.pop(name, None)
    return module


def test_every_fail_closed_operand_has_a_mutant() -> None:
    operands = _disk_operands()
    assert set(_guard_conjunctions(ast.parse(SOURCE.read_text()))) == {
        "_paper_route",
        "_paper_route_contract",
        "build_route_plan",
    }
    assert len(operands) == len(set(operands)) == 8
    assert set(operands) == set(DECLARED_OPERANDS)
    assert set(DECLARED_OPERANDS.values()) == set(INVARIANTS) - {"NO_FOREIGN"}
    assert len(_paper_excluded_assignments(ast.parse(SOURCE.read_text()))) == 1


def _paper_excluded_assignments(tree: ast.Module) -> list[ast.Assign]:
    [plan] = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_route_plan"
    ]
    return [
        node
        for node in ast.walk(plan)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "paper_excluded"
    ]


def test_no_foreign_mutant_breaks_its_invariant_by_assertion() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    [assignment] = _paper_excluded_assignments(tree)
    assert ast.unparse(assignment.value) == (
        "PAPER_FOREIGN_EXECUTION_TOOLS if paper else frozenset()"
    )
    assignment.value = ast.parse("frozenset()", mode="eval").body
    module = _compile(tree, "_route_lanes_mutant_no_foreign")
    with pytest.raises(AssertionError):
        invariant_no_foreign(module)


@pytest.mark.parametrize("key", sorted(INVARIANTS))
def test_invariant_holds_on_the_real_module(key: str) -> None:
    INVARIANTS[key](lanes)


@pytest.mark.parametrize(
    ("function", "operand"), sorted(DECLARED_OPERANDS), ids=lambda v: str(v)
)
def test_mutant_breaks_its_invariant_by_assertion(function: str, operand: str) -> None:
    mutant = _mutant(function, operand)
    with pytest.raises(AssertionError):
        INVARIANTS[DECLARED_OPERANDS[(function, operand)]](mutant)


# --- direct contract checks -------------------------------------------------


def test_paper_tools_are_the_two_paper_simulator_tools_only() -> None:
    assert lanes.PAPER_EXECUTION_TOOLS == {
        "paper_place_limit_order",
        "paper_cancel_pending_order",
    }
    assert lanes.PAPER_EXECUTION_REQUIRED_TOOLS <= H3
    assert lanes.PAPER_EXECUTION_REQUIRED_TOOLS  # registry-unknown => all missing
    for tool in lanes.PAPER_EXECUTION_TOOLS:
        assert tool.startswith("paper_")
    assert not lanes.PAPER_EXECUTION_TOOLS & lanes.PROPOSAL_LED_TOOLS


def test_registry_unavailable_paper_route_is_degraded() -> None:
    out = _plan(lanes, "buy_analysis", "crypto", None)
    assert out["success"] is False and out["degraded"] is True
    assert out["route_contract"]["execution_ready"] is False
    assert out["route_contract"]["missing_required_tools"] == sorted(
        lanes.PAPER_EXECUTION_REQUIRED_TOOLS
    )
    assert out["allowed_tools"] == []


@pytest.mark.parametrize("intent", ["profit_taking", "buy_analysis"])
def test_paper_route_on_the_h3_surface_is_ready(intent: str) -> None:
    out = _plan(lanes, intent, "crypto", H3)
    assert out["success"] is True and out["degraded"] is False
    assert set(out["allowed_tools"]) <= H3
    assert out["blocked_actions"] == []


@pytest.mark.parametrize(
    "foreign",
    sorted(lanes.PAPER_FOREIGN_EXECUTION_TOOLS),
)
def test_any_proposal_live_or_mock_order_tool_degrades_the_paper_route(
    foreign: str,
) -> None:
    out = _plan(lanes, "buy_analysis", "crypto", H3 | {foreign})
    assert out["success"] is False and out["degraded"] is True
    assert foreign in out["route_contract"]["foreign_execution_tools"]
    assert foreign not in out["allowed_tools"]
    assert foreign not in {s["tool"] for s in out["standard_tool_sequence"]}
    assert not lanes.PAPER_EXECUTION_TOOLS & set(out["allowed_tools"])


@pytest.mark.parametrize("intent", sorted(lanes.INTENT_TO_LANE))
@pytest.mark.parametrize("market", sorted(lanes.VALID_MARKETS))
def test_paper_surface_matches_proposal_led_off_crypto_buy_sell(
    intent: str, market: str
) -> None:
    moved = market == "crypto" and intent in {"profit_taking", "buy_analysis"}
    for registered in (H3, DEFAULT_LIKE, None):
        paper = _plan(lanes, intent, market, registered, surface=PAPER)
        proposal = _plan(lanes, intent, market, registered, surface=PROPOSAL)
        assert (paper != proposal) is moved


def test_unknown_surface_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown route execution surface"):
        _plan(lanes, "buy_analysis", "crypto", H3, surface="live")
    with pytest.raises(ValueError, match="unknown route execution surface"):
        register_route_request_tools(cast(Any, DummyMCP()), execution_surface="live")


def test_registered_paper_tool_answers_the_paper_contract() -> None:
    mcp = DummyMCP()
    for name in H3 - {"route_request"}:
        mcp.tools[name] = lambda: None
    register_route_request_tools(cast(Any, mcp), execution_surface=PAPER)
    out = asyncio.run(
        mcp.tools["route_request"](intent="buy_analysis", market="crypto")
    )
    assert _version(out) == "paper-execution-v1"
    assert out["success"] is True
    assert PAPER_SURFACE_DESCRIPTION.strip()


def test_only_the_h3_registrar_selects_the_paper_surface() -> None:
    """No other app module names the paper surface, so no profile can drift in."""
    allowed = {
        "app/mcp_server/tooling/route_request_lanes.py",
        "app/mcp_server/tooling/route_request_registration.py",
        "app/mcp_server/tooling/h3_crypto_paper_registration.py",
    }
    hits = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "ROUTE_SURFACE_PAPER_SIMULATOR" in text or '"paper_simulator"' in text:
            hits.add(str(path.relative_to(REPO_ROOT)))
    assert hits == allowed
    tree = ast.parse(
        (
            REPO_ROOT / "app/mcp_server/tooling/h3_crypto_paper_registration.py"
        ).read_text()
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "register_route_request_tools"
    ]
    assert len(calls) == 1
    assert [ast.unparse(k.value) for k in calls[0].keywords] == [
        "ROUTE_SURFACE_PAPER_SIMULATOR"
    ]
