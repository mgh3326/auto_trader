"""#1120 records-only invariants — static guards + assertion-RED mutants.

Invariant sentences (each has exactly one declared mutant, counted from
disk; the static seam scan is the guard for the SEAM sentence):

- SESSION: a session label outside the six contract labels is dropped and
  counted — never coerced into a market bucket and never passed through.
- THRESHOLD: a held symbol's spike fires exactly at its ±5%/±7% edge and
  not one tick before; boundary ticks are inclusive.
- CAP: the shadow budget is daily cap 2 per market plus a 60-minute
  cooldown — a third same-day firing is ``would_kick=False`` +
  ``daily_cap``.
- APPROACH: a ladder approach is recorded only inside ±0.5% of the rung
  anchor — a 0.57% gap emits nothing.
- DEDUPE: replaying a stream entry or ledger row can never produce a
  second stored row — ``dedupe_key`` uniqueness is the floor.
- SEAM: nothing in this package imports or calls a session-kick, order,
  broker, LLM, Prefect, or scheduler path (enforced by the scans below —
  there is no positive call site a mutant could remove, so its proof is
  the scan, and any new seam line fails the file-set + fragment tests).
"""

from __future__ import annotations

import ast
import pathlib
import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

INVARIANT_SENTENCES = {
    "SESSION": "an unknown session label is dropped and counted, never coerced",
    "THRESHOLD": "a held symbol fires exactly at its threshold edge, inclusive",
    "CAP": "the shadow would_kick budget is cap 2/day + 60-minute cooldown",
    "APPROACH": "a ladder approach is recorded only inside ±0.5% of the anchor",
    "DEDUPE": "replayed entries never produce a second stored row",
    "SEAM": "no file imports or calls a kick, order, broker, LLM, or scheduler",
}
MUTANT_FOR = {
    "SESSION": "session_coerce",
    "THRESHOLD": "threshold_exclusive",
    "CAP": "cap_relaxed",
    "APPROACH": "approach_wide",
    "DEDUPE": "dedupe_removed",
    # SEAM: static scans only — no positive call site exists to remove.
}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PKG = REPO_ROOT / "app" / "services" / "quotes_consumer"
JOB = REPO_ROOT / "app" / "jobs" / "quotes_consumer.py"
TASK = REPO_ROOT / "app" / "tasks" / "quotes_consumer_tasks.py"
CONFIG = REPO_ROOT / "app" / "core" / "config.py"
PACKAGE_FILES = sorted(PKG.glob("*.py"))
SCAN_FILES = PACKAGE_FILES + [JOB, TASK]

EXPECTED_PACKAGE = {
    "__init__.py",
    "consumer.py",
    "ladder.py",
    "repository.py",
    "stream.py",
    "triggers.py",
    "types.py",
}

FORBIDDEN_FRAGMENTS = (
    "fill_event_handoff",
    "ops_task_kick",
    "create_flow_run",
    "prefect",
    "place_order",
    "cancel_order",
    "app.services.brokers",
    "app.mcp_server",
    "anthropic",
    "openai",
    "gemini",
    "google.genai",
    "add_schedule",
    "cron",
)

FORBIDDEN_WRITE_CALLS = ("update", "delete", "merge", "expire", "expunge")

T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)  # 21:00 KST — three
# 61-minute-spaced kicks stay inside one KST day.


def _docstrings(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            doc = body[0].value
            lines.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return lines


def _code_tokens(path: pathlib.Path) -> str:
    tree = ast.parse(path.read_text())
    doc_lines = _docstrings(tree)
    kept = [
        line.split("#", 1)[0]
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if number not in doc_lines
    ]
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Static seam guards (the SEAM invariant)
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_package_files_are_the_expected_set() -> None:
    assert {p.name for p in PACKAGE_FILES} == EXPECTED_PACKAGE


@pytest.mark.unit
def test_no_forbidden_seam_fragments_anywhere() -> None:
    offenders: list[str] = []
    for path in SCAN_FILES:
        tokens = _code_tokens(path)
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in tokens:
                offenders.append(f"{path.name}: {fragment}")
    assert offenders == []


@pytest.mark.unit
def test_repository_has_no_update_or_delete_path() -> None:
    """Append-only is enforced in code too: repository.py holds every DB
    write for this package, and only select/insert calls may exist in it.
    Plain dict ``.update()`` elsewhere is not a write path.
    """
    offenders: list[str] = []
    for path in [PKG / "repository.py"]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_WRITE_CALLS:
                    offenders.append(f"{path.name}:{node.lineno} .{node.func.attr}(")
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in {"update", "delete"}:
                        offenders.append(
                            f"{path.name}:{node.lineno} import {alias.name}"
                        )
    assert offenders == []


@pytest.mark.unit
def test_config_gate_defaults_off() -> None:
    tree = ast.parse(CONFIG.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "quotes_toss_consumer_enabled":
                assert isinstance(node.value, ast.Constant)
                assert node.value.value is False
                return
    pytest.fail("quotes_toss_consumer_enabled not declared in config.py")


@pytest.mark.unit
def test_task_declares_no_schedule() -> None:
    tree = ast.parse(TASK.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                assert kw.arg != "schedule", f"schedule kwarg at line {node.lineno}"
                if kw.arg == "labels" and isinstance(kw.value, ast.Dict):
                    for k in kw.value.keys:
                        assert not (
                            isinstance(k, ast.Constant) and k.value == "schedule"
                        )


# ---------------------------------------------------------------------------
# Mutant machinery — same shape as tests/services/nhplug_mock
# ---------------------------------------------------------------------------
def _load_mutant(path: pathlib.Path, transform: ast.NodeTransformer, name: str):
    tree = ast.parse(path.read_text())
    tree = ast.fix_missing_locations(transform.visit(tree))
    applied = getattr(transform, "applied", 0)
    assert applied >= 1, f"{name}: mutation site not found in {path.name}"
    module_name = f"app.services.quotes_consumer._mutant_{name}"
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = "app.services.quotes_consumer"
    sys.modules[module_name] = module
    try:
        exec(compile(tree, str(path), "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(module_name, None)
    return module


class _CoerceSession(ast.NodeTransformer):
    """SESSION_MARKET.get(session) → SESSION_MARKET.get(session) or 'kr'."""

    def __init__(self) -> None:
        self.applied = 0

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "SESSION_MARKET"
        ):
            self.applied += 1
            return ast.BoolOp(op=ast.Or(), values=[node, ast.Constant("kr")])
        return node


class _ExclusiveThresholds(ast.NodeTransformer):
    """Every ``>=`` inside evaluate_tick becomes ``>`` — edge ticks miss."""

    def __init__(self) -> None:
        self.applied = 0
        self._inside = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        previous = self._inside
        self._inside = self._inside or node.name == "evaluate_tick"
        self.generic_visit(node)
        self._inside = previous
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if self._inside:
            ops = []
            for op in node.ops:
                if isinstance(op, ast.GtE):
                    ops.append(ast.Gt())
                    self.applied += 1
                else:
                    ops.append(op)
            node.ops = ops
        return node


class _CapOfThree(ast.NodeTransformer):
    def __init__(self) -> None:
        self.applied = 0

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        self.generic_visit(node)
        if any(
            isinstance(t, ast.Name) and t.id == "KICK_DAILY_CAP" for t in node.targets
        ):
            node.value = ast.Constant(3)
            self.applied += 1
        return node


class _WideApproach(ast.NodeTransformer):
    """APPROACH_BAND Decimal('0.005') → Decimal('0.05') (a 10x band)."""

    def __init__(self) -> None:
        self.applied = 0

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if node.value == "0.005":
            self.applied += 1
            return ast.Constant("0.05")
        return node


class _NoOnConflict(ast.NodeTransformer):
    """Strip every .on_conflict_do_nothing() call from repository inserts."""

    def __init__(self) -> None:
        self.applied = 0

    def visit_Call(self, node: ast.Call) -> ast.AST:
        node = self.generic_visit(node)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "on_conflict_do_nothing":
            self.applied += 1
            return func.value
        return node


# ---------------------------------------------------------------------------
# Invariant checks — run on the real module (pass) and on the mutant (fail)
# ---------------------------------------------------------------------------
def _tick(symbol: str, price: str, ts: datetime = T0):
    from app.services.quotes_consumer.types import QuoteTick

    return QuoteTick(
        entry_id="1-1",
        symbol=symbol,
        source_symbol=symbol,
        ts=ts,
        session="krx_regular",
        market="kr",
        kind="trade",
        price=Decimal(price),
        bid1=None,
        bid_qty=None,
        ask1=None,
        ask_qty=None,
    )


def _holdings(held: dict[str, str], core: frozenset[str]):
    from app.services.quotes_consumer.triggers import HoldingsView

    return HoldingsView(held=held, core=core)


def _check_session_drop(parse) -> None:
    fields = {
        "symbol": "005930",
        "ts": "2026-09-30T13:00:00.000+00:00",
        "price": "106000",
        "bid1": "",
        "bid_qty": "",
        "ask1": "",
        "ask_qty": "",
        "session": "nxt_unknown",
    }
    result = parse("1-0", fields)
    assert type(result).__name__ == "DroppedEntry"
    assert result.reason == "unknown_session"


def _check_threshold_inclusive(evaluator_cls) -> None:
    ev = evaluator_cls()
    holdings = _holdings({"005930": "kr"}, frozenset({"005930"}))
    prev = {"005930": Decimal("100000")}
    rows = ev.evaluate_tick(_tick("005930", "105000"), holdings, prev)
    assert len(rows) == 1
    assert rows[0].trigger_type == "holding_spike"


def _check_cap_two(evaluator_cls) -> None:
    """Two kicks (spaced past the 60-minute cooldown) exhaust the day."""
    ev = evaluator_cls()
    symbols = ("AAA", "BBB", "CCC")
    holdings = _holdings(dict.fromkeys(symbols, "kr"), frozenset(symbols))
    prev = {s: Decimal("100000") for s in symbols}
    rows = [
        row
        for index, s in enumerate(symbols)
        for row in ev.evaluate_tick(
            _tick(s, "106000", ts=T0 + timedelta(minutes=61 * index)),
            holdings,
            prev,
        )
    ]
    assert [r.would_kick for r in rows] == [True, True, False]
    assert rows[2].suppress_reason == "daily_cap"


def _check_approach_band(tracker_cls) -> None:
    from app.services.quotes_consumer.types import RungAnchor

    rung = RungAnchor(
        ledger_name="toss_live_order_ledger",
        ledger_id=501,
        symbol="005930",
        market="kr",
        side="buy",
        anchor_price=Decimal("70000"),
        broker_order_id=None,
        client_order_id=None,
        correlation_id=None,
        received_at=None,
        died_at=None,
        nxt_tradable=None,
    )
    tracker = tracker_cls()
    tracker.prime([rung])
    # 70399 vs 70000 is 0.57% — outside the ±0.5% approach band.
    rows = tracker.on_trade_tick(_tick("005930", "70399"), [rung])
    assert rows == []


# ---------------------------------------------------------------------------
# Mutant tests — each must turn its invariant assertion RED
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_session_mutant_is_assertion_red() -> None:
    from app.services.quotes_consumer import stream as real

    _check_session_drop(real.parse_quote_entry)  # real module passes
    mutant = _load_mutant(PKG / "stream.py", _CoerceSession(), "session_coerce")
    with pytest.raises(AssertionError):
        _check_session_drop(mutant.parse_quote_entry)


@pytest.mark.unit
def test_threshold_mutant_is_assertion_red() -> None:
    from app.services.quotes_consumer import triggers as real

    _check_threshold_inclusive(real.TriggerEvaluator)
    mutant = _load_mutant(
        PKG / "triggers.py", _ExclusiveThresholds(), "threshold_exclusive"
    )
    with pytest.raises(AssertionError):
        _check_threshold_inclusive(mutant.TriggerEvaluator)


@pytest.mark.unit
def test_cap_mutant_is_assertion_red() -> None:
    from app.services.quotes_consumer import triggers as real

    _check_cap_two(real.TriggerEvaluator)
    mutant = _load_mutant(PKG / "triggers.py", _CapOfThree(), "cap_relaxed")
    with pytest.raises(AssertionError):
        _check_cap_two(mutant.TriggerEvaluator)


@pytest.mark.unit
def test_approach_mutant_is_assertion_red() -> None:
    from app.services.quotes_consumer import ladder as real

    _check_approach_band(real.LadderTracker)
    mutant = _load_mutant(PKG / "ladder.py", _WideApproach(), "approach_wide")
    with pytest.raises(AssertionError):
        _check_approach_band(mutant.LadderTracker)


def _firing_row():
    from app.services.quotes_consumer.types import TriggerRow

    return TriggerRow(
        dedupe_key="mutant-dedupe-probe",
        trigger_type="holding_spike",
        outcome="fired",
        symbol="ZZMUT",
        source_symbol=None,
        market="kr",
        session="krx_regular",
        reference_price=Decimal("100"),
        current_price=Decimal("106"),
        window="day",
        event_ts=T0,
        kst_date="2026-09-30",
        would_kick=False,
        suppress_reason=None,
        daily_would_kick_count=0,
        last_would_kick_at=None,
        not_evaluable_reason=None,
        source_ref=None,
        detail={},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dedupe_mutant_is_assertion_red(db_session) -> None:
    from app.services.quotes_consumer import repository as real

    async def _check(repo_cls) -> None:
        repo = repo_cls(db_session)
        row = _firing_row()
        assert await repo.insert_firings([row]) == 1
        try:
            second = await repo.insert_firings([row])
        except Exception as exc:  # mutant path: raw IntegrityError
            raise AssertionError(f"dedupe mutant surfaced {exc!r}") from exc
        assert second == 0

    try:
        await _check(real.QuotesConsumerRepository)  # real passes
        await db_session.rollback()
        mutant = _load_mutant(PKG / "repository.py", _NoOnConflict(), "dedupe_removed")
        with pytest.raises(AssertionError):
            await _check(mutant.QuotesConsumerRepository)
    finally:
        await db_session.rollback()


# ---------------------------------------------------------------------------
# Parity — invariants and mutants are counted from disk
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_every_invariant_sentence_has_its_mutant() -> None:
    assert len(INVARIANT_SENTENCES) == 6
    assert set(MUTANT_FOR) == set(INVARIANT_SENTENCES) - {"SEAM"}
    assert len(MUTANT_FOR) == 5
