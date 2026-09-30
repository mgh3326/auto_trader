"""#1112 — assertion-RED mutants for the inference rule and the no-order sweep.

Condition predicates are counted from ``kis_leftover_inference.py`` ON DISK
(every module-level ``_check_*`` function). Each mutant compiles a copy of the
module in which ONE predicate always answers "holds", then runs the scenario in
which only that condition is broken. The real module must block; the mutant
must let the row through — proving the scenario test would go red if the
condition were ever dropped. A new predicate without a declared invariant fails
``test_every_condition_predicate_has_a_mutant``.

Invariant sentences (one per mutant):
- KIS: a rung that is not a kis_live equity_kr resting buy with a broker order
  id is never closed by inference.
- LEDGER_ROW: a rung without exactly one owned, still-open order-ledger row of
  the same order/symbol/side is never closed by inference.
- DAY: a non-DAY order (anything but limit/market on both proposal and ledger)
  is never closed by inference.
- SESSION: an order not accepted and recorded inside the XKRX regular session,
  or reported on a non-KRX/SOR venue (NXT, after-hours, unknown), is never
  closed by inference.
- CLOSE: a rung is never closed by inference at or before the latest of submit
  day 15:30 KST, the calendar close and the ROB-671 expected expiry.
- COVERAGE: a rung is never closed by inference unless a committed KIS
  execution-ledger run covered accept..deadline and finished after it.
- NO_FILL: a rung with any fill or partial evidence for its order is never
  closed by inference.
- HOLDING: a rung whose symbol holding moved (or is unknown) since submit is
  never closed by inference.
- NO_ORDER: the night sweep modules never import a broker client, an order
  execution path, or the broker gateway; the only broker-package import is the
  stdlib-only ROB-671 expiry classifier.
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from app.services.order_proposals import kis_leftover_inference as rule
from tests.services.order_proposals.test_kis_leftover_inference import (
    DAY,
    ISOLATED_BREAKS,
    NOW,
    _facts,
    kst,
)

pytestmark = pytest.mark.unit

RULE_SOURCE = Path(rule.__file__)
REPO_ROOT = Path(__file__).resolve().parents[3]

# predicate function -> (condition name, invariant sentence key)
DECLARED_MUTANTS: dict[str, tuple[str, str]] = {
    "_check_kis_live_resting_buy_rung": (rule.COND_KIS_LIVE_RESTING_BUY, "KIS"),
    "_check_kis_order_ledger_row_open_and_owned": (
        rule.COND_ORDER_LEDGER_ROW,
        "LEDGER_ROW",
    ),
    "_check_day_order": (rule.COND_DAY_ORDER, "DAY"),
    "_check_regular_session_accept": (rule.COND_REGULAR_SESSION, "SESSION"),
    "_check_day_close_passed": (rule.COND_DAY_CLOSE_PASSED, "CLOSE"),
    "_check_execution_ledger_covers_order_day": (
        rule.COND_LEDGER_COVERAGE,
        "COVERAGE",
    ),
    "_check_no_fill_in_execution_ledger": (rule.COND_NO_FILL, "NO_FILL"),
    "_check_holding_quantity_unchanged": (rule.COND_HOLDING_UNCHANGED, "HOLDING"),
}


def predicates_on_disk() -> list[str]:
    tree = ast.parse(RULE_SOURCE.read_text("utf-8"))
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_check_")
    )


def test_every_condition_predicate_has_a_mutant() -> None:
    assert predicates_on_disk() == sorted(DECLARED_MUTANTS)
    # ...and every predicate is wired into the evaluated condition table.
    wired = {check.__name__ for _, check in rule.CONDITIONS}
    assert wired == set(DECLARED_MUTANTS)
    assert {cond for cond, _ in DECLARED_MUTANTS.values()} == set(ISOLATED_BREAKS)


class _AlwaysHolds(ast.NodeTransformer):
    def __init__(self, target: str) -> None:
        self.target = target
        self.replaced = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == self.target:
            self.replaced += 1
            node.body = [ast.Return(value=ast.Constant(value=True))]
        return node


def _mutant_module(target: str) -> types.ModuleType:
    tree = ast.parse(RULE_SOURCE.read_text("utf-8"))
    transformer = _AlwaysHolds(target)
    tree = ast.fix_missing_locations(transformer.visit(tree))
    assert transformer.replaced == 1, target
    name = f"_mutant_{target}"
    module = types.ModuleType(name)
    module.__file__ = str(RULE_SOURCE)
    sys.modules[name] = module  # dataclasses resolve annotations via sys.modules
    try:
        exec(compile(tree, str(RULE_SOURCE), "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)
    return module


def _scenario_now(condition: str) -> datetime.datetime:
    # Same clock the A2 scenario uses: the 16:30 sweep for the deadline break.
    if condition == rule.COND_DAY_CLOSE_PASSED:
        return kst(DAY, 16, 30)
    return NOW


@pytest.mark.parametrize("predicate", sorted(DECLARED_MUTANTS))
def test_mutant_turns_the_blocking_scenario_eligible(predicate: str) -> None:
    condition, _sentence = DECLARED_MUTANTS[predicate]
    now = _scenario_now(condition)
    facts = _facts(**ISOLATED_BREAKS[condition])

    real = rule.classify_leftover_rung(facts, now=now)
    assert real.eligible is False
    assert real.failed_conditions == (condition,)

    mutant = _mutant_module(predicate)
    mutated = mutant.classify_leftover_rung(facts, now=now)
    # RED proof: with the condition dropped, the same row would be closed.
    assert mutated.eligible is True, predicate


# --- NO_ORDER: the sweep modules cannot reach an order path -----------------

SWEEP_MODULES = (
    "app/services/order_proposals/kis_leftover_inference.py",
    "app/services/order_proposals/kis_leftover_inference_service.py",
    "app/services/order_proposals/kr_buy_blocking.py",
    "app/services/order_proposals/night_sweep.py",
)
ALLOWED_BROKER_IMPORTS = frozenset({"app.services.brokers.kis.live_order_expiry"})
FORBIDDEN_PREFIXES = (
    "app.services.brokers",
    "app.mcp_server",
    "app.services.order_proposals.broker_gateway",
    "app.services.order_proposals.dispatch",
    "app.services.order_proposals.revalidation",
    "app.services.order_proposals.telegram_callback",
    "app.services.kis_trading_service",
    "httpx",
    "requests",
    "aiohttp",
)


def forbidden_imports(source: str) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        for name in names:
            if name in ALLOWED_BROKER_IMPORTS or any(
                name.startswith(allowed + ".") for allowed in ALLOWED_BROKER_IMPORTS
            ):
                continue
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in FORBIDDEN_PREFIXES
            ):
                offenders.append(name)
    return offenders


@pytest.mark.parametrize("relpath", SWEEP_MODULES)
def test_sweep_module_imports_no_order_or_broker_path(relpath: str) -> None:
    source = (REPO_ROOT / relpath).read_text("utf-8")
    assert forbidden_imports(source) == []


@pytest.mark.parametrize(
    "injected",
    [
        "from app.services.brokers.kis import KISClient\n",
        "import app.services.brokers.toss.client\n",
        "from app.mcp_server.tooling.order_execution import _execute_and_record\n",
        "from app.services.order_proposals.broker_gateway import submit\n",
        "import httpx\n",
    ],
)
def test_no_order_mutant_is_caught(injected: str) -> None:
    source = (REPO_ROOT / SWEEP_MODULES[1]).read_text("utf-8")
    assert forbidden_imports(injected + source) != []


def test_allowed_import_is_really_stdlib_only() -> None:
    spec = importlib.util.find_spec("app.services.brokers.kis.live_order_expiry")
    assert spec is not None and spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("app."), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app."), alias.name
