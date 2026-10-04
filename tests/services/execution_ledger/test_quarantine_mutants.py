"""#1175 — assertion-RED mutants and reader inventory for the ledger quarantine.

Refusal guards are counted from ``quarantine.py`` ON DISK: every ``if`` inside
``evaluate_row`` whose body returns a refusal verdict. Each mutant compiles a
copy of the module with ONE guard forced false and runs the row that only that
guard should refuse; the real module refuses it, the mutant must not (it lets
the row through or crashes). A new guard without a declared invariant fails
``test_every_refusal_guard_has_a_mutant``.

Readers are counted from ``app/`` and ``scripts/`` ON DISK: every function that
references ``ExecutionLedger`` outside an annotation must either AND in
``execution_ledger_in_effect()`` (and have a DB test that also runs the
"filter dropped" mutant) or be declared exempt with its invariant sentence.

Invariant sentences (one per mutant / exemption):
- NOT_FOUND: an id that does not exist is never quarantined.
- ALREADY: an already-quarantined row is never re-quarantined or rewritten.
- WEBSOCKET: a reconciler or manual_import row (an authoritative fill) is never
  quarantined.
- KIS: an Upbit or Toss row is never quarantined.
- LIVE: a mock-account row is never quarantined.
- EQUITY_KR: a non-KR-equity row is never quarantined.
- RAW_MISSING: a row without a stored raw frame is never quarantined.
- RAW_TR: a row whose frame is not a live H0STCNI0 notice is never quarantined.
- RAW_FIELDS: a row whose frame fields are unreadable is never quarantined.
- FILL: a row whose own frame says CNTG_YN=2 (a real execution) is never
  quarantined.
- ACCEPT: a row whose CNTG_YN is anything but exactly 1 is never quarantined.
- ORDER: a row whose frame names a different order number is never
  quarantined.
- SYMBOL: a row whose frame names a different symbol is never quarantined.
- BATCH_ALL: one ineligible id refuses the whole batch.
- BATCH_NOOP: a batch is a no-op only when every id is already quarantined.
- READERS: every fill/lot/evidence/report reader excludes quarantined rows.
- EXEMPT: the listed functions are identity/writer/watermark reads that must
  see every row, or read only rows a quarantine can never touch.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

from app.services.execution_ledger import quarantine as q
from tests.services.execution_ledger.test_quarantine_unit import (
    REFUSALS,
    refusal_row,
)

pytestmark = pytest.mark.unit

SOURCE = Path(q.__file__)
REPO_ROOT = Path(__file__).resolve().parents[3]

# refusal verdict -> invariant sentence key
DECLARED_GUARDS: dict[str, str] = {
    "not_found": "NOT_FOUND",
    "already_quarantined": "ALREADY",
    "not_websocket": "WEBSOCKET",
    "not_kis": "KIS",
    "not_live": "LIVE",
    "not_equity_kr": "EQUITY_KR",
    "raw_payload_missing": "RAW_MISSING",
    "raw_payload_not_domestic_execution_notice": "RAW_TR",
    "raw_payload_fields_malformed": "RAW_FIELDS",
    "fill_notice_cntg_yn_2": "FILL",
    "cntg_yn_not_accept": "ACCEPT",
    "raw_order_no_mismatch": "ORDER",
    "raw_symbol_mismatch": "SYMBOL",
}


def _evaluate_row_node(tree: ast.Module) -> ast.FunctionDef:
    [node] = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "evaluate_row"
    ]
    return node


def _refusal_of(if_node: ast.If) -> str | None:
    for stmt in if_node.body:
        if (
            isinstance(stmt, ast.Return)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "RowVerdict"
            and len(stmt.value.args) >= 2
            and isinstance(stmt.value.args[1], ast.Constant)
        ):
            verdict = stmt.value.args[1].value
            if verdict != "accept_notice":
                return verdict
    return None


def guards_on_disk() -> list[str]:
    tree = ast.parse(SOURCE.read_text("utf-8"))
    found = [
        verdict
        for node in ast.walk(_evaluate_row_node(tree))
        if isinstance(node, ast.If) and (verdict := _refusal_of(node)) is not None
    ]
    assert len(found) == len(set(found)), found
    return sorted(found)


def test_every_refusal_guard_has_a_mutant() -> None:
    assert guards_on_disk() == sorted(DECLARED_GUARDS)
    assert set(DECLARED_GUARDS) == set(REFUSALS)
    sentences = set(re.findall(r"^- ([A-Z_]+):", __doc__ or "", re.MULTILINE))
    assert set(DECLARED_GUARDS.values()) <= sentences


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
    tree = ast.parse(SOURCE.read_text("utf-8"))
    tree = ast.fix_missing_locations(transformer.visit(tree))
    assert getattr(transformer, "replaced", 0) == 1, name
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE)
    sys.modules[name] = module
    try:
        exec(compile(tree, str(SOURCE), "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.mark.parametrize("verdict", sorted(DECLARED_GUARDS))
def test_guard_mutant_lets_the_isolated_row_through(verdict: str) -> None:
    row = refusal_row(verdict)
    real = q.evaluate_row(9, row)
    assert real.verdict == verdict
    assert not real.eligible

    mutant = _compile_mutant(_GuardOff(verdict), f"_mutant_guard_{verdict}")
    try:
        outcome = mutant.evaluate_row(9, row).verdict
    except (AttributeError, IndexError, TypeError):
        outcome = "crashed"
    assert outcome != verdict


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


def test_batch_decision_all_calls_counted_from_disk() -> None:
    tree = ast.parse(SOURCE.read_text("utf-8"))
    [decide] = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "decide"
    ]
    calls = [
        n
        for n in ast.walk(decide)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "all"
    ]
    assert len(calls) == 2  # BATCH_ALL (eligible) + BATCH_NOOP (already)


def _v(ledger_id: int, verdict: str) -> q.RowVerdict:
    return q.RowVerdict(ledger_id, verdict)  # type: ignore[arg-type]


def test_batch_all_mutant_would_commit_a_mixed_batch() -> None:
    mixed = [_v(1, "accept_notice"), _v(2, "fill_notice_cntg_yn_2")]
    assert q.decide((1, 2), mixed) == "refused"
    mutant = _compile_mutant(_AllToAny("eligible"), "_mutant_batch_all")
    assert mutant.decide((1, 2), mixed) == "eligible"


def test_batch_noop_mutant_would_swallow_a_mixed_batch() -> None:
    mixed = [_v(1, "already_quarantined"), _v(2, "accept_notice")]
    assert q.decide((1, 2), mixed) == "refused"
    mutant = _compile_mutant(_AllToAny("already_quarantined"), "_mutant_batch_noop")
    assert mutant.decide((1, 2), mixed) == "noop"


# ------------------------------------------------------------- readers

#: (path, function) -> DB test proof that runs the "filter dropped" mutant.
IN_EFFECT_READERS: dict[tuple[str, str], str] = {
    ("app/services/execution_ledger/kis_lots.py", "load_kis_live_kr_lot_blocks"): (
        "test_quarantine_readers_db::test_lots_provisional_listing_drops_the_quarantined_phantom"
    ),
    ("app/services/execution_ledger/kis_lots.py", "load_kis_live_us_lot_blocks"): (
        "test_quarantine_readers_db::test_us_lots_drop_the_quarantined_row"
    ),
    ("app/services/execution_ledger/query_service.py", "list_recent"): (
        "test_quarantine_readers_db::READERS[query_service.list_recent]"
    ),
    ("app/services/execution_ledger/query_service.py", "list_by_symbol"): (
        "test_quarantine_readers_db::READERS[query_service.list_by_symbol]"
    ),
    ("app/services/execution_ledger/query_service.py", "list_fills_today"): (
        "test_quarantine_readers_db::READERS[query_service.list_fills_today]"
    ),
    ("app/services/execution_ledger/query_service.py", "list_sell_history"): (
        "test_quarantine_readers_db::READERS[query_service.list_sell_history]"
    ),
    ("app/services/execution_ledger/repository.py", "has_fill_for_order"): (
        "test_quarantine_readers_db::READERS[repository.has_fill_for_order]"
    ),
    ("app/services/execution_ledger/repository.py", "list_recent_fills_for_triage"): (
        "test_quarantine_readers_db::READERS[repository.list_recent_fills_for_triage]"
    ),
    (
        "app/services/execution_ledger/repository.py",
        "net_quantity_by_match_key_since",
    ): (
        "test_quarantine_readers_db::READERS[repository.net_quantity_by_match_key_since]"
    ),
    ("app/services/execution_ledger/repository.py", "position_before_fill"): (
        "test_quarantine_readers_db::READERS[repository.position_before_fill]"
    ),
    ("app/services/fill_event_handoff/broker_risk.py", "list_fills_for_order"): (
        "test_quarantine_readers_db::READERS[broker_risk.list_fills_for_order]"
    ),
    ("app/services/market_close_digest/queries.py", "_execution_fills"): (
        "test_quarantine_readers_db::READERS[market_close_digest._execution_fills]"
    ),
    (
        "app/services/order_proposals/kis_leftover_inference_service.py",
        "_symbol_fills",
    ): "test_quarantine_readers_db::test_1112_inference_no_longer_sees_the_phantom_as_a_fill",
    ("app/services/protected_quantity_service.py", "_net_execution_quantity_since"): (
        "test_quarantine_readers_db::READERS[protected_quantity._net_execution_quantity_since]"
    ),
    ("app/services/quotes_consumer/repository.py", "fills_after"): (
        "test_quarantine_readers_db::READERS[quotes_consumer.fills_after]"
    ),
}

#: (path, function) -> why it deliberately reads every row (EXEMPT).
EXEMPT_READERS: dict[tuple[str, str], str] = {
    ("app/services/execution_ledger/repository.py", "rows_by_ids"): (
        "the quarantine tool's exact-id read must see a quarantined row to "
        "answer already_quarantined"
    ),
    ("app/services/execution_ledger/repository.py", "mark_quarantined"): (
        "the guarded UPDATE itself, which filters quarantined_at IS NULL directly"
    ),
    ("app/services/execution_ledger/repository.py", "get_by_key"): (
        "upsert identity: a replayed phantom frame must match its quarantined row "
        "and stay unchanged instead of inserting an unquarantined twin"
    ),
    ("app/services/execution_ledger/repository.py", "upsert_fill"): (
        "the writer; it never touches the quarantine columns"
    ),
    ("app/services/execution_ledger/repository.py", "apply_market_filter"): (
        "adds market predicates to a caller's statement that already carries the filter"
    ),
    ("app/services/execution_ledger/repository.py", "max_ledger_id"): (
        "id high-water mark for cursors, not a fill read"
    ),
    ("app/services/quotes_consumer/repository.py", "fills_watermark"): (
        "id high-water mark for the own_fill cursor, not a fill read"
    ),
    ("app/services/kis_mock_lifecycle_service.py", "_has_local_fill_row"): (
        "mock-only; the service refuses account_mode != live, and counting a row "
        "here only makes a mock cancel refuse (the safe direction)"
    ),
    ("app/services/protected_position_auto_follow.py", "_follow_ledger_row"): (
        "acts only on source=reconciler rows; a quarantined row is websocket by "
        "the DB CHECK and is already skipped as not_authoritative"
    ),
}


def _annotation_ids(node: ast.AST) -> set[int]:
    ids: set[int] = set()
    for inner in ast.walk(node):
        annotation = None
        if isinstance(inner, ast.arg):
            annotation = inner.annotation
        elif isinstance(inner, ast.AnnAssign):
            annotation = inner.annotation
        elif isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotation = inner.returns
        if annotation is not None:
            ids.update(id(n) for n in ast.walk(annotation))
    return ids


def readers_on_disk() -> dict[tuple[str, str], bool]:
    found: dict[tuple[str, str], bool] = {}
    for root in ("app", "scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel == "app/models/execution_ledger.py":
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                skip = _annotation_ids(node)
                refs = [
                    n
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name)
                    and n.id == "ExecutionLedger"
                    and id(n) not in skip
                ]
                if not refs:
                    continue
                filtered = any(
                    isinstance(n, ast.Name) and n.id == "execution_ledger_in_effect"
                    for n in ast.walk(node)
                )
                found[(rel, node.name)] = filtered
    return found


def test_every_ledger_reader_is_filtered_or_declared_exempt() -> None:
    found = readers_on_disk()
    assert set(found) == set(IN_EFFECT_READERS) | set(EXEMPT_READERS)
    assert not set(IN_EFFECT_READERS) & set(EXEMPT_READERS)
    unfiltered = sorted(k for k in IN_EFFECT_READERS if not found[k])
    assert unfiltered == []


def test_every_filtered_reader_has_a_db_mutant_proof() -> None:
    spec = importlib.util.find_spec(
        "tests.services.execution_ledger.test_quarantine_readers_db"
    )
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text("utf-8")
    for proof in IN_EFFECT_READERS.values():
        _module, name = proof.split("::", 1)
        if name.startswith("READERS["):
            assert f'"{name[len("READERS[") : -1]}"' in source, proof
        else:
            assert f"async def {name}(" in source, proof


_RAW_SQL = re.compile(r"\b(from|join|update|into)\s+review\.execution_ledger\b", re.I)


def test_no_raw_sql_reads_the_ledger_around_the_filter() -> None:
    offenders = []
    for root in ("app", "scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _RAW_SQL.search(node.value)
                ):
                    offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
