"""#1250 — assertion-RED mutants for the Q-46 kis_mock inference close.

Condition predicates are counted from ``kis_mock_inference_expiry.py`` ON DISK
(every module-level ``_check_*`` function). Each mutant compiles a copy of the
module in which ONE predicate always answers "holds", then runs the scenario in
which only that condition is broken. The real module must refuse; the mutant
must let the row through — proving the scenario test goes red if the condition
were ever dropped. A new predicate without a declared invariant fails
``test_every_condition_predicate_has_a_mutant``.

Invariant sentences (one per mutant):
- ACCEPTED_BUY: a row that is not a kis_mock/kis KR cash BUY with a positive
  broker accept (rt_cd 0, odno = order number, ord_tmd = order time), or that
  is a synthetic scalping row, is never closed.
- OPEN: a row that is not still accepted/pending (terminal, fill, anomaly) is
  never closed.
- DAY: a non-DAY order (anything but limit>0 / market=0, whole positive qty)
  is never closed.
- SESSION: an order not accepted and recorded inside the XKRX regular session
  is never closed.
- CLOSE: a row is never closed at or before the #1112 deadline.
- NO_FILL: a row with any fill evidence for its order (execution ledger
  kis/mock any source incl. quarantined, own reason codes / attributed qty,
  same-correlation rows) is never closed.
- HOLDING: a row whose holding at send is unknown, or whose symbol has any
  recorded fill that may postdate the accept instant, is never closed.
- BATCH_ALL: one refused row refuses the whole batch.
- BATCH_NOOP: a batch is a no-op only when every row was already closed by
  this rule (with its audit row).
- WAIVED_ONLY: exactly two #1112 conditions are waived (strategy_match and
  reconcile_coverage); the strategy field is read by no condition, the rule,
  service and CLI never read reconcile runs, every other condition is a wired
  predicate, and no waived name is also a predicate.
- NO_BROKER: the rule, the service and the CLI import no broker client, no
  order-execution path and no live ledger model.
- EXACT_INT: ids are accepted only as exactly the four built-in ints; floats,
  numpy ints, bools and strings that compare equal are refused.
- WRITER_VERIFY: the write chokepoint re-locks and re-classifies the batch
  itself, so a direct call cannot close a row the rule refuses.
- RECHECK: every fill source is re-read after the guarded UPDATEs and before
  commit; a fill committed in between refuses the batch with full rollback.
- AUDIT_COUPLING: the DB refuses an audit row whose ledger row is not closed by
  the same batch, and refuses at COMMIT a closed row without its audit row.
- TERMINAL: a row carrying the Q-46 inference marker cannot be updated or
  deleted at all, so clearing the marker cannot reopen it.
- COMMIT_GATE: at COMMIT the DB re-checks order, symbol and sibling fills and
  the four-row batch, so a fill committed after the service re-check, or a
  one-row raw close, refuses the whole close.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

from app.services import kis_mock_inference_expiry as rule
from tests.services.test_kis_mock_inference_expiry_unit import (
    ISOLATED_BREAKS,
    NOW,
    _decision,
    _evidence,
)

pytestmark = pytest.mark.unit

RULE_SOURCE = Path(rule.__file__)
REPO_ROOT = Path(__file__).resolve().parents[2]

# predicate function -> (condition name, invariant sentence key)
DECLARED_MUTANTS: dict[str, tuple[str, str]] = {
    "_check_kis_mock_accepted_buy_row": ("kis_mock_accepted_buy_row", "ACCEPTED_BUY"),
    "_check_kis_mock_row_open": ("kis_mock_row_open", "OPEN"),
    "_check_day_order": ("day_order", "DAY"),
    "_check_regular_session_accept": ("regular_session_accept", "SESSION"),
    "_check_day_close_passed": ("day_close_passed", "CLOSE"),
    "_check_no_fill_recorded_for_order": ("no_fill_recorded_for_order", "NO_FILL"),
    "_check_holding_quantity_unchanged": ("holding_quantity_unchanged", "HOLDING"),
}
INVARIANT_KEYS = {
    "ACCEPTED_BUY",
    "OPEN",
    "DAY",
    "SESSION",
    "CLOSE",
    "NO_FILL",
    "HOLDING",
    "BATCH_ALL",
    "BATCH_NOOP",
    "WAIVED_ONLY",
    "NO_BROKER",
    "EXACT_INT",
    "WRITER_VERIFY",
    "RECHECK",
    "AUDIT_COUPLING",
    "TERMINAL",
    "COMMIT_GATE",
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
    wired = {check.__name__ for _, check in rule.CONDITIONS}
    assert wired == set(DECLARED_MUTANTS)
    assert {cond for cond, _ in DECLARED_MUTANTS.values()} == set(ISOLATED_BREAKS)


def test_every_invariant_sentence_is_declared_in_the_docstring() -> None:
    doc = sys.modules[__name__].__doc__ or ""
    declared = {
        line.strip()[2:].split(":", 1)[0]
        for line in doc.splitlines()
        if line.strip().startswith("- ") and ":" in line
    }
    assert declared == INVARIANT_KEYS
    assert {key for _, key in DECLARED_MUTANTS.values()} <= INVARIANT_KEYS


class _ReturnConstant(ast.NodeTransformer):
    def __init__(self, target: str, value: object) -> None:
        self.target = target
        self.value = value
        self.replaced = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == self.target:
            self.replaced += 1
            node.body = [ast.Return(value=ast.Constant(value=self.value))]
        return node


class _AllToAny(ast.NodeTransformer):
    """Inside ``decide_batch``, turn the Nth ``all(...)`` call into ``any(...)``."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.seen = 0
        self.replaced = 0
        self.inside = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name != "decide_batch":
            return node
        self.inside = True
        self.generic_visit(node)
        self.inside = False
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if self.inside and isinstance(node.func, ast.Name) and node.func.id == "all":
            if self.seen == self.index:
                node.func = ast.Name(id="any", ctx=ast.Load())
                self.replaced += 1
            self.seen += 1
        return node


def _compile(tree: ast.Module, label: str) -> types.ModuleType:
    name = f"_mutant_t1250_{label}"
    module = types.ModuleType(name)
    module.__file__ = str(RULE_SOURCE)
    sys.modules[name] = module  # dataclasses resolve annotations via sys.modules
    try:
        exec(  # noqa: S102
            compile(ast.fix_missing_locations(tree), str(RULE_SOURCE), "exec"),
            module.__dict__,
        )
    finally:
        sys.modules.pop(name, None)
    return module


def _mutant(target: str) -> types.ModuleType:
    tree = ast.parse(RULE_SOURCE.read_text("utf-8"))
    transformer = _ReturnConstant(target, True)
    tree = transformer.visit(tree)
    assert transformer.replaced == 1, target
    return _compile(tree, target)


@pytest.mark.parametrize("target", sorted(DECLARED_MUTANTS))
def test_predicate_mutant_lets_the_isolated_row_through(target: str) -> None:
    condition, _ = DECLARED_MUTANTS[target]
    evidence, now = ISOLATED_BREAKS[condition]
    real = rule.classify_row(evidence, now=now)
    assert real.verdict == "refused"
    assert real.failed_conditions == (condition,)

    mutant = _mutant(target)
    mutated = mutant.classify_row(mutant.RowEvidence(**evidence.__dict__), now=now)
    # RED by assertion: the real rule refuses, the mutant would close the row.
    assert mutated.verdict == "eligible"


def _batch_mutant(index: int) -> types.ModuleType:
    tree = ast.parse(RULE_SOURCE.read_text("utf-8"))
    transformer = _AllToAny(index)
    tree = transformer.visit(tree)
    assert transformer.replaced == 1
    return _compile(tree, f"batch_{index}")


IDS = (80, 66, 64, 63)


def test_batch_all_calls_counted_from_disk() -> None:
    tree = ast.parse(RULE_SOURCE.read_text("utf-8"))
    [fn] = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "decide_batch"
    ]
    calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "all"
    ]
    assert len(calls) == 2


def test_batch_noop_mutant_would_swallow_a_mixed_batch() -> None:
    mixed = tuple(
        _decision("already_closed" if i == 80 else "eligible", i) for i in IDS
    )
    assert rule.decide_batch(IDS, mixed) == "refused"
    mutant = _batch_mutant(0)
    assert mutant.decide_batch(IDS, mixed) == "noop"


def test_batch_all_mutant_would_commit_a_mixed_batch() -> None:
    mixed = tuple(_decision("refused" if i == 64 else "eligible", i) for i in IDS)
    assert rule.decide_batch(IDS, mixed) == "refused"
    mutant = _batch_mutant(1)
    assert mutant.decide_batch(IDS, mixed) == "eligible"


def test_only_strategy_and_reconcile_coverage_are_waived() -> None:
    tree = ast.parse(RULE_SOURCE.read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_check_"):
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert "strategy" not in attrs, node.name
    assert rule.WAIVED_CONDITIONS == ("strategy_match", "reconcile_coverage")
    wired = {name for name, _ in rule.CONDITIONS}
    assert not wired & set(rule.WAIVED_CONDITIONS)
    # The six other #1112 conditions (rung/ledger-row ownership is folded into
    # the accepted-buy predicate) stay wired: 7 predicates, none waived.
    assert len(wired) == 7
    # The waiver vocabulary does not make a broken row pass.
    evidence, now = ISOLATED_BREAKS["holding_quantity_unchanged"]
    assert rule.classify_row(evidence, now=now).verdict == "refused"
    assert rule.classify_row(_evidence(), now=NOW).verdict == "eligible"


@pytest.mark.parametrize(
    "rel",
    [
        "app/services/kis_mock_inference_expiry.py",
        "app/services/kis_mock_inference_expiry_service.py",
        "scripts/expire_kis_mock_rows_by_inference.py",
    ],
)
def test_waived_coverage_is_not_silently_half_read(rel: str) -> None:
    text = (REPO_ROOT / rel).read_text("utf-8")
    assert "ExecutionLedgerReconcileRun" not in text
    assert "execution_ledger_reconcile_runs" not in text


_GUARDED_FILES = (
    "app/services/kis_mock_inference_expiry.py",
    "app/services/kis_mock_inference_expiry_service.py",
    "scripts/expire_kis_mock_rows_by_inference.py",
)
_FORBIDDEN_IMPORT_PREFIXES = (
    "app.services.brokers.kis.client",
    "app.services.brokers.kis.domestic",
    "app.services.brokers.kis.overseas",
    "app.services.brokers.kis.account",
    "app.services.brokers.kis.kis",
    "app.services.order_execution",
    "app.mcp_server",
    "httpx",
    "requests",
    "aiohttp",
)
_ALLOWED_BROKER_IMPORTS = {"app.services.brokers.kis.live_order_expiry"}


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("rel", _GUARDED_FILES)
def test_no_broker_client_order_path_or_live_ledger(rel: str) -> None:
    path = REPO_ROOT / rel
    for name in _imports(path):
        assert not name.startswith(_FORBIDDEN_IMPORT_PREFIXES), (rel, name)
        if name.startswith("app.services.brokers"):
            assert name in _ALLOWED_BROKER_IMPORTS, (rel, name)
    text = path.read_text("utf-8")
    assert "KISLiveOrderLedger" not in text
    assert "kis_live_order_ledger" not in text


class _AnyToFalse(ast.NodeTransformer):
    """Inside ``exact_ids``, drop the exact-type check (``any(...)`` -> False)."""

    def __init__(self) -> None:
        self.replaced = 0
        self.inside = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name != "exact_ids":
            return node
        self.inside = True
        self.generic_visit(node)
        self.inside = False
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if self.inside and isinstance(node.func, ast.Name) and node.func.id == "any":
            self.replaced += 1
            return ast.Constant(value=False)
        return node


def test_exact_int_mutant_would_accept_float_ids() -> None:
    floats = (80.0, 66.0, 64.0, 63.0)
    with pytest.raises(rule.InferenceInputError):
        rule.exact_ids(floats)
    tree = ast.parse(RULE_SOURCE.read_text("utf-8"))
    transformer = _AnyToFalse()
    tree = transformer.visit(tree)
    assert transformer.replaced == 1
    mutant = _compile(tree, "exact_ids")
    assert mutant.exact_ids(floats) == floats


# The DB-level invariants are proven by DB tests that run their own mutant
# (monkeypatched no-op) and by the trigger refusals; pin that they exist.
_DB_PROOFS = {
    "WRITER_VERIFY": (
        "test_r2_direct_writer_re_verifies_and_writes_nothing",
        "test_r2_writer_verify_mutant_would_close_a_filled_row",
    ),
    "RECHECK": (
        "test_r2_fill_committed_just_before_the_first_update_refuses",
        "test_r2_recheck_mutant_would_close_despite_the_fill",
    ),
    "AUDIT_COUPLING": (
        "test_an_audit_row_without_a_close_is_refused",
        "test_a_close_committed_without_its_audit_is_refused_at_commit",
    ),
    "EXACT_INT": ("test_r2_non_builtin_int_ids_refuse_before_any_sql",),
    "TERMINAL": (
        "test_r3_clearing_the_marker_cannot_reopen_a_closed_row",
        "test_r3_update_order_terms_cannot_strip_the_marker",
        "test_r3_a_closed_row_cannot_be_deleted",
    ),
    "COMMIT_GATE": (
        "test_r3_order_fill_after_the_recheck_refuses_at_commit",
        "test_r3_symbol_fill_after_the_recheck_refuses_at_commit",
        "test_r3_sibling_fill_after_the_recheck_refuses_at_commit",
        "test_r3_a_one_row_raw_close_with_its_audit_is_refused",
        "test_r3_commit_gate_mutant_would_close_despite_a_late_fill",
    ),
}


def test_db_level_invariants_have_db_proofs() -> None:
    source = (
        REPO_ROOT / "tests/services/test_kis_mock_inference_expiry_db.py"
    ).read_text("utf-8")
    assert set(_DB_PROOFS) <= INVARIANT_KEYS
    for names in _DB_PROOFS.values():
        for name in names:
            assert f"async def {name}(" in source, name
