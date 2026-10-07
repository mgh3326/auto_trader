"""#1244 — the paper-execution route contract and its fail-closed guards.

The h3-crypto-paper profile (#1171) has no proposal tool by design, so the
proposal-led buy/sell contract was degraded on every H3 run and the runner's
bootstrap check halted. route_request now takes an explicit
``execution_surface`` chosen at registration; only that profile selects the
paper simulator, and only crypto buy/sell move to ``paper-execution-v1``.
Round 3 adds the Alpaca paper surface for h3-us-paper (#1257): us buy/sell.

Mutants are counted from ``route_request_lanes.py`` ON DISK: every operand of
the two fail-closed conjunctions (``_paper_route``'s ``selected`` and the
paper contract's ``execution_ready``), plus the declared assignment sites in
``DECLARED_ASSIGNMENTS`` (spec lookup, paper-tool allowance, hidden set). Each
operand mutant compiles a copy of the module with ONE
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
  mock order tool or a non-paper reconcile writer, even when one is registered.

The foreign and hidden sets are also pinned to literal oracles written in this
file (not derived from the production constants), so dropping a union term
from either constant is RED (r1 tester survivor: PROPOSAL_LIFECYCLE_TOOLS).

The NO_FOREIGN mutant replaces the single on-disk ``paper_excluded``
assignment in ``build_route_plan`` with an empty set; the SURFACE mutant makes
the single on-disk ``spec`` lookup in ``_paper_route`` ignore the registered
surface (a proposal-led registration then gets the crypto paper spec).
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
from app.mcp_server.tooling.h3_us_paper_registration import (
    H3_US_PAPER_TOOL_NAMES,
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
    hidden = _plan(module, "profit_taking", "crypto", H3 | {"live_reconcile_orders"})
    assert hidden["success"] is True
    assert "live_reconcile_orders" not in hidden["allowed_tools"]


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
    ("_paper_route", "purpose is None"): "PURPOSE",
    ("_paper_route", "lane in PAPER_EXECUTION_LANES"): "LANE",
    ("_paper_route", "market == spec_market"): "MARKET",
    ("_paper_route_contract", "not missing_required_tools"): "MISSING",
    ("_paper_route_contract", "not foreign_execution_tools"): "FOREIGN",
}
# Assignment mutants (anchor = the unparsed right-hand side on disk).
DECLARED_ASSIGNMENTS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("_paper_route", "spec"): (
        "PAPER_SURFACE_SPECS.get(execution_surface)",
        "PAPER_SURFACE_SPECS.get(ROUTE_SURFACE_PAPER_SIMULATOR)",
        "SURFACE",
    ),
    ("build_route_plan", "paper_tools"): (
        "paper.execution_tools if paper is not None else frozenset()",
        "PAPER_EXECUTION_TOOLS",
        "ALLOW_PAPER",
    ),
    ("build_route_plan", "paper_allowed"): (
        "paper_tools if route_contract['execution_ready'] else frozenset()",
        "paper_tools",
        "ALLOW_READY",
    ),
    ("build_route_plan", "paper_excluded"): (
        "paper_route_excluded_tools(paper) if paper is not None else frozenset()",
        "frozenset()",
        "NO_FOREIGN",
    ),
}


def _guard_conjunctions(tree: ast.Module) -> dict[str, ast.BoolOp]:
    """The three fail-closed ``and`` chains, located on disk."""
    found: dict[str, ast.BoolOp] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if (
                node.name == "_paper_route"
                and isinstance(child, ast.Assign)
                and ast.unparse(child.targets[0]) == "selected"
            ):
                found[node.name] = cast(ast.BoolOp, child.value)
            elif (
                node.name == "_paper_route_contract"
                and isinstance(child, ast.Assign)
                and ast.unparse(child.targets[0]) == "execution_ready"
            ):
                found[node.name] = cast(ast.BoolOp, child.value)
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
    }
    assert len(operands) == len(set(operands)) == 5
    assert set(operands) == set(DECLARED_OPERANDS)
    declared = set(DECLARED_OPERANDS.values()) | {
        key for _a, _r, key in DECLARED_ASSIGNMENTS.values()
    }
    assert declared == set(INVARIANTS)
    tree = ast.parse(SOURCE.read_text())
    for (function, target), (anchor, _r, _k) in DECLARED_ASSIGNMENTS.items():
        [assignment] = _assignments(tree, function, target)
        assert ast.unparse(assignment.value) == anchor


def _assignments(tree: ast.Module, function: str, target: str) -> list[ast.Assign]:
    [fn] = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function
    ]
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == target
    ]


@pytest.mark.parametrize("site", sorted(DECLARED_ASSIGNMENTS), ids=lambda v: str(v))
def test_assignment_mutant_breaks_its_invariant_by_assertion(
    site: tuple[str, str],
) -> None:
    anchor, replacement, key = DECLARED_ASSIGNMENTS[site]
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    [assignment] = _assignments(tree, *site)
    assert ast.unparse(assignment.value) == anchor
    assignment.value = ast.parse(replacement, mode="eval").body
    module = _compile(tree, f"_route_lanes_mutant_{site[0]}_{site[1]}")
    with pytest.raises(AssertionError):
        INVARIANTS[key](module)


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
    registrars = {
        "app/mcp_server/tooling/h3_crypto_paper_registration.py": (
            "ROUTE_SURFACE_PAPER_SIMULATOR"
        ),
        "app/mcp_server/tooling/h3_us_paper_registration.py": (
            "ROUTE_SURFACE_ALPACA_PAPER"
        ),
    }
    allowed = {
        "app/mcp_server/tooling/route_request_lanes.py",
        "app/mcp_server/tooling/route_request_registration.py",
        *registrars,
    }
    hits = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if (
            "ROUTE_SURFACE_PAPER_SIMULATOR" in text
            or "ROUTE_SURFACE_ALPACA_PAPER" in text
            or '"paper_simulator"' in text
            or "execution_surface=" in text
        ):
            hits.add(str(path.relative_to(REPO_ROOT)))
    assert hits == allowed
    for registrar, surface in registrars.items():
        tree = ast.parse((REPO_ROOT / registrar).read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "register_route_request_tools"
        ]
        assert len(calls) == 1, registrar
        assert [ast.unparse(k.value) for k in calls[0].keywords] == [surface]


# Literal oracles (#1244 r2) — independent of DIRECT_BROKER_MUTATION_TOOLS,
# PROPOSAL_LIFECYCLE_TOOLS and RECONCILE_TOOLS, so removing a member or a union
# term from the production constants cannot also remove its test.
FOREIGN_ORACLE = frozenset(
    {
        "alpaca_paper_automated_submit_order",
        "alpaca_paper_cancel_order",
        "alpaca_paper_submit_order",
        "cancel_order",
        "kis_live_cancel_order",
        "kis_live_modify_order",
        "kis_live_place_order",
        "kis_mock_cancel_order",
        "kis_mock_mirror_execute_report",
        "kis_mock_modify_order",
        "kis_mock_place_order",
        "kiwoom_mock_cancel_order",
        "kiwoom_mock_modify_order",
        "kiwoom_mock_place_order",
        "kiwoom_mock_us_cancel_order",
        "kiwoom_mock_us_modify_order",
        "kiwoom_mock_us_place_order",
        "modify_order",
        "nh_mock_cancel_order",
        "nh_mock_modify_order",
        "nh_mock_place_order",
        "place_order",
        "toss_cancel_order",
        "toss_modify_order",
        "toss_place_order",
        "order_proposal_create",
        "order_proposal_expire_sweep",
        "order_proposal_redispatch",
        "order_proposal_void",
        "proposal_revalidate",
        "support_reserve_net_consume",
    }
)
HIDDEN_RECONCILE_ORACLE = frozenset(
    {
        "alpaca_paper_reconcile_orders",
        "kis_live_reconcile_orders",
        "kis_mock_reconciliation_run",
        "live_reconcile_orders",
        "nh_mock_reconcile_orders",
        "toss_reconcile_orders",
    }
)


def test_foreign_and_hidden_sets_equal_the_literal_oracles() -> None:
    # A new direct broker mutation must be added here too, deliberately.
    assert lanes.PAPER_FOREIGN_EXECUTION_TOOLS == FOREIGN_ORACLE
    assert lanes.PAPER_ROUTE_EXCLUDED_TOOLS == FOREIGN_ORACLE | HIDDEN_RECONCILE_ORACLE
    assert "paper_reconcile_orders" not in lanes.PAPER_ROUTE_EXCLUDED_TOOLS


@pytest.mark.parametrize("foreign", sorted(FOREIGN_ORACLE))
def test_each_oracle_foreign_tool_degrades_the_paper_route(foreign: str) -> None:
    out = _plan(lanes, "profit_taking", "crypto", H3 | {foreign})
    assert out["success"] is False and out["degraded"] is True
    assert out["route_contract"]["foreign_execution_tools"] == [foreign]
    named = set(out["allowed_tools"]) | {
        s["tool"] for s in out["standard_tool_sequence"]
    }
    assert foreign not in named
    assert not lanes.PAPER_EXECUTION_TOOLS & set(out["allowed_tools"])


@pytest.mark.parametrize("writer", sorted(HIDDEN_RECONCILE_ORACLE))
def test_non_paper_reconcile_writers_are_hidden_on_the_paper_route(writer: str) -> None:
    out = _plan(lanes, "buy_analysis", "crypto", H3 | {writer})
    assert out["success"] is True  # hidden, not foreign
    named = set(out["allowed_tools"]) | {
        s["tool"] for s in out["standard_tool_sequence"]
    }
    assert writer not in named
    assert "paper_reconcile_orders" in out["allowed_tools"]
    # The proposal-led route keeps its existing reconcile allowance.
    proposal = _plan(lanes, "buy_analysis", "crypto", H3 | {writer}, surface=PROPOSAL)
    assert writer in proposal["allowed_tools"]


# --- Alpaca paper surface (h3-us-paper, #1257; #1244 r3) --------------------

ALPACA = lanes.ROUTE_SURFACE_ALPACA_PAPER
H3_US = set(H3_US_PAPER_TOOL_NAMES)
ALPACA_ORDER_TOOLS = frozenset(
    {"alpaca_paper_submit_order", "alpaca_paper_cancel_order"}
)
CRYPTO_PAPER_ORDER_TOOLS = frozenset(
    {"paper_place_limit_order", "paper_cancel_pending_order"}
)
US_FOREIGN_ORACLE = (FOREIGN_ORACLE - ALPACA_ORDER_TOOLS) | CRYPTO_PAPER_ORDER_TOOLS
US_HIDDEN_RECONCILE_ORACLE = HIDDEN_RECONCILE_ORACLE | {"paper_reconcile_orders"}
US_SPEC = lanes.PAPER_SURFACE_SPECS[ALPACA]


def test_us_foreign_and_hidden_sets_equal_the_literal_oracles() -> None:
    assert lanes.paper_foreign_execution_tools(US_SPEC) == US_FOREIGN_ORACLE
    assert (
        lanes.paper_route_excluded_tools(US_SPEC)
        == US_FOREIGN_ORACLE | US_HIDDEN_RECONCILE_ORACLE
    )
    assert US_SPEC.market == "us"
    assert US_SPEC.execution_tools == ALPACA_ORDER_TOOLS
    assert US_SPEC.required_tools == ALPACA_ORDER_TOOLS | {
        "alpaca_paper_list_orders",
        "alpaca_paper_get_order",
        "alpaca_paper_list_positions",
        "market_quote_snapshot_ensure",
    }
    assert US_SPEC.required_tools <= H3_US


@pytest.mark.parametrize("intent", ["profit_taking", "buy_analysis"])
def test_alpaca_paper_route_on_the_h3_us_surface_is_ready(intent: str) -> None:
    out = _plan(lanes, intent, "us", H3_US, surface=ALPACA)
    assert out["success"] is True and out["degraded"] is False
    assert out["route_contract"]["execution_mode"] == "alpaca_paper"
    assert set(out["allowed_tools"]) <= H3_US
    assert ALPACA_ORDER_TOOLS <= set(out["allowed_tools"])
    assert out["blocked_actions"] == []


@pytest.mark.parametrize("foreign", sorted(US_FOREIGN_ORACLE))
def test_each_us_oracle_foreign_tool_degrades_the_alpaca_paper_route(
    foreign: str,
) -> None:
    out = _plan(lanes, "buy_analysis", "us", H3_US | {foreign}, surface=ALPACA)
    assert out["success"] is False and out["degraded"] is True
    assert out["route_contract"]["foreign_execution_tools"] == [foreign]
    named = set(out["allowed_tools"]) | {
        s["tool"] for s in out["standard_tool_sequence"]
    }
    assert foreign not in named
    assert not ALPACA_ORDER_TOOLS & set(out["allowed_tools"])


@pytest.mark.parametrize("writer", sorted(US_HIDDEN_RECONCILE_ORACLE))
def test_every_reconcile_writer_is_hidden_on_the_alpaca_paper_route(
    writer: str,
) -> None:
    out = _plan(lanes, "profit_taking", "us", H3_US | {writer}, surface=ALPACA)
    assert out["success"] is True
    named = set(out["allowed_tools"]) | {
        s["tool"] for s in out["standard_tool_sequence"]
    }
    assert writer not in named


@pytest.mark.parametrize("missing", sorted(US_SPEC.required_tools))
def test_alpaca_paper_route_is_degraded_without_a_required_tool(missing: str) -> None:
    out = _plan(lanes, "buy_analysis", "us", H3_US - {missing}, surface=ALPACA)
    assert out["success"] is False and out["degraded"] is True
    assert out["route_contract"]["missing_required_tools"] == [missing]
    assert not ALPACA_ORDER_TOOLS & set(out["allowed_tools"])


@pytest.mark.parametrize("intent", sorted(lanes.INTENT_TO_LANE))
@pytest.mark.parametrize("market", sorted(lanes.VALID_MARKETS))
def test_alpaca_surface_matches_proposal_led_off_us_buy_sell(
    intent: str, market: str
) -> None:
    moved = market == "us" and intent in {"profit_taking", "buy_analysis"}
    for registered in (H3_US, H3_US | {"order_proposal_create"}, None):
        paper = _plan(lanes, intent, market, registered, surface=ALPACA)
        proposal = _plan(lanes, intent, market, registered, surface=PROPOSAL)
        assert (paper != proposal) is moved
