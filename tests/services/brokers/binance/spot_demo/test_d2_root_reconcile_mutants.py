"""#1268 — assertion-RED mutants and static invariants for the D2 root reconcile.

Refusal guards are counted from ``d2_root_reconcile.py`` ON DISK: every ``if``
inside ``evaluate_row`` / ``evaluate_evidence`` whose body returns a refusal
verdict. Each mutant compiles a copy of the module with ONE guard forced false
and runs the input that only that guard should refuse; the real module refuses
it, the mutant must not (it lets the input through or crashes). A new guard
without a declared invariant fails the counting tests below.

Invariant sentences (one per mutant / static check):
- NOT_FOUND: an id that does not exist is never reconciled.
- ALREADY: a root this tool already reconciled is never moved or rewritten again.
- SPOT: a futures (or any non-spot) row is never reconciled.
- HOST: a row written against any host but demo-api.binance.com is never
  reconciled.
- ROOT: a child (close/reduce-only) leg is never reconciled.
- FILLED: only a root in filled is reconciled; every other state, terminal
  states included, is refused.
- WRITER: a root not written by d2_remediation_single is never reconciled.
- EXCEPTION: a root under any other strategy-order exception is never
  reconciled.
- REMEDIATION: a root under any other remediation id is never reconciled.
- CANARY: a root that does not carry canary_or_strategy_use=forbidden is never
  reconciled.
- CREDENTIAL: a root placed on any other account credential is never
  reconciled.
- BOUND: a root whose client order id is not one of the three sealed D2 orders
  is never reconciled.
- INSTRUMENT: a root whose instrument is not the bound order's binance spot
  symbol is never reconciled.
- SIDE: a root whose side differs from the bound order is never reconciled.
- TYPE: a root whose order type differs from the bound order is never
  reconciled.
- QTY: a root whose quantity differs from the bound order is never reconciled.
- PRICE: a root whose limit price differs from the bound order is never
  reconciled.
- BROKER_ID: a root without a broker order id is never reconciled.
- EV_NOT_FOUND: a root the broker does not know is never reconciled.
- EV_READ: a root whose broker read failed is never reconciled.
- EV_SHAPE: an unreadable broker answer never counts as evidence.
- EV_CID: broker evidence about another client order id never counts.
- EV_OID: broker evidence about another broker order id never counts.
- EV_SYMBOL: broker evidence for another symbol never counts.
- EV_SIDE: broker evidence for another side never counts.
- EV_TYPE: broker evidence for another order type never counts.
- EV_STATUS: only status FILLED counts as evidence.
- EV_ORIG_QTY: broker evidence with another order quantity never counts.
- EV_EXEC_QTY: a partial or over-fill never counts as evidence.
- EV_PRICE: broker evidence with another limit price never counts.
- EV_TIF: broker evidence with another time in force never counts.
- EV_FILL_ACTUAL: broker evidence contradicting a recorded fill actual never
  counts.
- BATCH_ALL: one ineligible id or one bad piece of evidence refuses the whole
  batch.
- BATCH_NOOP: a batch is a no-op only when every id was already reconciled by
  this tool.
- EVIDENCE_REQUIRED: a row-eligible root without matching broker evidence is
  never eligible.
- SERVICE_ONLY: the module writes only through BinanceDemoLedgerService
  record_closed then record_reconciled; it carries no SQL and never imports the
  repository.
- READ_ONLY_BROKER: the only broker call is get_order_status.
"""

from __future__ import annotations

import ast
import re
import sys
import types
from pathlib import Path

import pytest

from app.services.brokers.binance.spot_demo import d2_root_reconcile as r
from tests.services.brokers.binance.spot_demo._d2_root_fixtures import (
    EVIDENCE_REFUSALS,
    ROW_REFUSALS,
    refusal_evidence,
    refusal_row,
)

pytestmark = pytest.mark.unit

SOURCE = Path(r.__file__)
REPO_ROOT = Path(__file__).resolve().parents[5]
CLI = REPO_ROOT / "scripts/binance_spot_demo_d2_root_reconcile.py"

ROW_GUARDS: dict[str, str] = {
    "not_found": "NOT_FOUND",
    "already_reconciled": "ALREADY",
    "not_spot": "SPOT",
    "not_spot_demo_host": "HOST",
    "not_root": "ROOT",
    "not_filled": "FILLED",
    "not_d2_writer": "WRITER",
    "exception_mismatch": "EXCEPTION",
    "remediation_mismatch": "REMEDIATION",
    "canary_use_not_forbidden": "CANARY",
    "credential_fingerprint_mismatch": "CREDENTIAL",
    "not_bound_order": "BOUND",
    "instrument_mismatch": "INSTRUMENT",
    "side_mismatch": "SIDE",
    "order_type_mismatch": "TYPE",
    "qty_mismatch": "QTY",
    "price_mismatch": "PRICE",
    "broker_order_id_missing": "BROKER_ID",
}
EVIDENCE_GUARDS: dict[str, str] = {
    "evidence_order_not_found": "EV_NOT_FOUND",
    "evidence_read_failed": "EV_READ",
    "evidence_not_mapping": "EV_SHAPE",
    "evidence_client_order_id": "EV_CID",
    "evidence_order_id": "EV_OID",
    "evidence_symbol": "EV_SYMBOL",
    "evidence_side": "EV_SIDE",
    "evidence_type": "EV_TYPE",
    "evidence_status_not_filled": "EV_STATUS",
    "evidence_orig_qty": "EV_ORIG_QTY",
    "evidence_executed_qty": "EV_EXEC_QTY",
    "evidence_price": "EV_PRICE",
    "evidence_time_in_force": "EV_TIF",
    "evidence_fill_actual_conflict": "EV_FILL_ACTUAL",
}
STATIC_SENTENCES = {
    "BATCH_ALL",
    "BATCH_NOOP",
    "EVIDENCE_REQUIRED",
    "SERVICE_ONLY",
    "READ_ONLY_BROKER",
}

_ROW_PASS = "d2_filled_root"
_EVIDENCE_PASS = "broker_filled_match"


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text("utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    [node] = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    ]
    return node


def _refusal_of(if_node: ast.If) -> str | None:
    """The refusal verdict a guard returns, if it returns one."""
    for stmt in if_node.body:
        if not (
            isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
        ):
            continue
        call = stmt.value
        if call.func.id == "RowVerdict" and len(call.args) >= 2:
            arg = call.args[1]
            passing = _ROW_PASS
        elif call.func.id == "EvidenceVerdict" and call.args:
            arg = call.args[0]
            passing = _EVIDENCE_PASS
        else:
            continue
        if isinstance(arg, ast.Constant) and arg.value != passing:
            return arg.value
    return None


def guards_on_disk(function: str) -> list[str]:
    found = [
        verdict
        for node in ast.walk(_function(_tree(), function))
        if isinstance(node, ast.If) and (verdict := _refusal_of(node)) is not None
    ]
    assert len(found) == len(set(found)), found
    return sorted(found)


def test_every_row_guard_has_a_mutant() -> None:
    assert guards_on_disk("evaluate_row") == sorted(ROW_GUARDS)
    assert set(ROW_GUARDS) == set(ROW_REFUSALS)


def test_every_evidence_guard_has_a_mutant() -> None:
    assert guards_on_disk("evaluate_evidence") == sorted(EVIDENCE_GUARDS)
    assert set(EVIDENCE_GUARDS) == set(EVIDENCE_REFUSALS)


def test_every_mutant_has_an_invariant_sentence() -> None:
    sentences = set(re.findall(r"^- ([A-Z_]+):", __doc__ or "", re.MULTILINE))
    declared = set(ROW_GUARDS.values()) | set(EVIDENCE_GUARDS.values())
    assert declared | STATIC_SENTENCES == sentences
    assert len(sentences) == len(ROW_GUARDS) + len(EVIDENCE_GUARDS) + len(
        STATIC_SENTENCES
    )


class _GuardOff(ast.NodeTransformer):
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.replaced = 0

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if _refusal_of(node) == self.verdict:
            self.replaced += 1
            node.test = ast.Constant(value=False)
        return node


def _compile_mutant(transformer: ast.NodeTransformer, name: str) -> types.ModuleType:
    tree = ast.fix_missing_locations(transformer.visit(_tree()))
    assert getattr(transformer, "replaced", 0) == 1, name
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE)
    sys.modules[name] = module
    try:
        exec(compile(tree, str(SOURCE), "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.mark.parametrize("verdict", sorted(ROW_GUARDS))
def test_row_guard_mutant_lets_the_isolated_row_through(verdict: str) -> None:
    row, instrument = refusal_row(verdict)
    real = r.evaluate_row(9, row, instrument)
    assert real.verdict == verdict
    assert not real.row_eligible

    mutant = _compile_mutant(_GuardOff(verdict), f"_d2_mutant_row_{verdict}")
    try:
        outcome = mutant.evaluate_row(9, row, instrument).verdict
    except (AttributeError, KeyError, TypeError):
        outcome = "crashed"
    assert outcome != verdict


@pytest.mark.parametrize("verdict", sorted(EVIDENCE_GUARDS))
def test_evidence_guard_mutant_lets_the_isolated_body_through(verdict: str) -> None:
    row, body = refusal_evidence(verdict)
    real = r.evaluate_evidence(row, body)
    assert real.verdict == verdict
    assert not real.matches

    mutant = _compile_mutant(_GuardOff(verdict), f"_d2_mutant_ev_{verdict}")
    try:
        outcome = mutant.evaluate_evidence(row, body).verdict
    except (AttributeError, KeyError, TypeError):
        outcome = "crashed"
    assert outcome != verdict


# ------------------------------------------------------------ batch decision


class _AllToAny(ast.NodeTransformer):
    def __init__(self, attribute: str) -> None:
        self.attribute = attribute
        self.replaced = 0
        self._in_decide = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name != "decide":
            return node
        self._in_decide = True
        self.generic_visit(node)
        self._in_decide = False
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if (
            self._in_decide
            and isinstance(node.func, ast.Name)
            and node.func.id == "all"
            and self.attribute in ast.dump(node)
        ):
            self.replaced += 1
            node.func = ast.Name(id="any", ctx=ast.Load())
        return node


def _decide_all_calls() -> list[str]:
    decide = _function(_tree(), "decide")
    return sorted(
        attr.attr
        for call in ast.walk(decide)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "all"
        for attr in ast.walk(call)
        if isinstance(attr, ast.Attribute)
    )


def test_batch_decision_all_calls_counted_from_disk() -> None:
    assert _decide_all_calls() == ["already_reconciled", "eligible"]


def _verdict(ledger_id: int, *, ok: bool) -> r.RowVerdict:
    if not ok:
        return r.RowVerdict(ledger_id, "not_d2_writer")
    return r.RowVerdict(
        ledger_id, "d2_filled_root", {}, r.EvidenceVerdict("broker_filled_match")
    )


def test_batch_all_mutant_accepts_a_partially_bad_batch() -> None:
    verdicts = (_verdict(1, ok=True), _verdict(2, ok=False))
    assert r.decide((1, 2), verdicts) == "refused"
    mutant = _compile_mutant(_AllToAny("eligible"), "_d2_mutant_batch_all")
    rows = tuple(
        mutant.RowVerdict(v.ledger_id, v.verdict, v.detail, v.evidence)
        for v in verdicts
    )
    assert mutant.decide((1, 2), rows) == "eligible"


def test_batch_noop_mutant_skips_an_unreconciled_root() -> None:
    verdicts = (r.RowVerdict(1, "already_reconciled"), _verdict(2, ok=True))
    assert r.decide((1, 2), verdicts) == "refused"
    mutant = _compile_mutant(_AllToAny("already_reconciled"), "_d2_mutant_batch_noop")
    rows = tuple(
        mutant.RowVerdict(v.ledger_id, v.verdict, v.detail, v.evidence)
        for v in verdicts
    )
    assert mutant.decide((1, 2), rows) == "noop"


class _DropEvidenceClause(ast.NodeTransformer):
    """``eligible`` reduced to the row verdict alone."""

    def __init__(self) -> None:
        self.replaced = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == "eligible":
            node.body = [
                ast.Return(
                    value=ast.Attribute(
                        value=ast.Name(id="self", ctx=ast.Load()),
                        attr="row_eligible",
                        ctx=ast.Load(),
                    )
                )
            ]
            self.replaced += 1
        return node


def test_evidence_required_mutant_accepts_an_unread_root() -> None:
    unread = r.RowVerdict(1, "d2_filled_root")
    assert r.decide((1,), (unread,)) == "refused"
    mismatched = r.RowVerdict(
        2, "d2_filled_root", {}, r.EvidenceVerdict("evidence_status_not_filled")
    )
    assert r.decide((2,), (mismatched,)) == "refused"
    mutant = _compile_mutant(_DropEvidenceClause(), "_d2_mutant_evidence_required")
    assert mutant.decide((1,), (mutant.RowVerdict(1, "d2_filled_root"),)) == "eligible"


# --------------------------------------------------------- static invariants


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _method_names(tree: ast.AST) -> list[str]:
    return [
        call.func.attr for call in _calls(tree) if isinstance(call.func, ast.Attribute)
    ]


@pytest.mark.parametrize("path", [SOURCE, CLI])
def test_service_only_no_sql_and_no_repository(path: Path) -> None:
    source = path.read_text("utf-8")
    tree = ast.parse(source)
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("repository" in name for name in imported), imported
    assert not any(
        name.startswith("sqlalchemy")
        and name
        not in {
            "sqlalchemy.ext.asyncio",
            "sqlalchemy.engine",
        }
        for name in imported
    ), imported
    names = set(_method_names(tree)) | {
        call.func.id for call in _calls(tree) if isinstance(call.func, ast.Name)
    }
    for forbidden in (
        "execute",
        "add",
        "delete",
        "merge",
        "flush",
        "text",
        "insert",
        "update",
        "update_state",
        "insert_planned",
    ):
        assert forbidden not in names, (path.name, forbidden)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|SELECT)\b", source), (
        path.name
    )


def test_service_only_transitions_are_closed_then_reconciled() -> None:
    commit = _function(_tree(), "commit_reconcile")
    record_calls = [
        name for name in _method_names(commit) if name.startswith("record_")
    ]
    assert record_calls == ["record_closed", "record_reconciled"]
    module_records = {
        name for name in _method_names(_tree()) if name.startswith("record_")
    }
    assert module_records == {"record_closed", "record_reconciled"}


def test_read_only_broker_only_get_order_status() -> None:
    evaluate = _function(_tree(), "_evaluate")
    client_calls = {
        call.func.attr
        for call in _calls(evaluate)
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "client"
    }
    assert client_calls == {"get_order_status"}
    for path in (SOURCE, CLI):
        names = set(_method_names(ast.parse(path.read_text("utf-8"))))
        for forbidden in (
            "submit_order",
            "cancel_order",
            "order_test",
            "preview_submit",
            "post",
            "put",
        ):
            assert forbidden not in names, (path.name, forbidden)
