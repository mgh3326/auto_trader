"""ROB-1061 H3 (AC20, AC21, AC27) — structural guards, not convention.

Two independent guards over the WHOLE ``research/alpaca_track_signals/``
surface (recursive, excluding ``tests/``):

1. Import/wall-clock/broker guard, mirroring
   ``research/alpaca_track/tests/test_import_and_time_guards.py`` (H1): no
   app/DB/broker/scheduler/random/time/alpaca import, no wall-clock
   ``.now()``/``.utcnow()``/``.today()`` call, no broker mutation/scheduler
   token referenced lexically.

2. No-PnL-surface guard (AC20/AC21): no dataclass field named/containing
   ``pnl``, ``return``, ``forward_*``, or ``exit_price`` anywhere in this
   package's source, and no import of a name suggesting a PnL engine,
   scenario ledger, fill model, cost-scenario, or scorecard module — this is
   the property H5's PnL-blind dry-count gate depends on, enforced
   structurally rather than by code-review convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_EXCLUDED_DIR_NAMES = {"tests", "__pycache__"}


def _iter_source_modules(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in _EXCLUDED_DIR_NAMES for part in rel.parts[:-1]):
            continue
        if rel.name == "conftest.py":
            continue
        out.append(path)
    return out


_MODULES = [str(p.relative_to(_ROOT)) for p in _iter_source_modules(_ROOT)]

# `pit_universe_alpaca` is H1's OWN module name (ROB-1059, already merged) --
# it holds Alpaca ASSET-status/PIT-listing metadata (active/tradable/USD-pair
# flags), never Alpaca OHLCV/quote/order data (H1's own guard already
# enforces that distinction over its own tree). H3 legitimately imports it
# for `UniverseSnapshot`/`SymbolCandidate` -- this is the ONE explicit,
# name-based exception to the "no Alpaca-named import" scan; any OTHER
# Alpaca-named import (a quote/BBO/execution client) remains banned.
_ALLOWED_ALPACA_NAMED_IMPORTS = frozenset({"pit_universe_alpaca"})

_FORBIDDEN_IMPORT_ROOTS = {
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "taskiq",
    "prefect",
    "random",
    "time",
    "alpaca",
    "alpaca_trade_api",
}

_FORBIDDEN_NOW_ATTRS = {"now", "utcnow", "today"}

# AC20/AC21: no PnL/return/forward_*/exit_price field, no PnL-engine-shaped
# import. These substrings are matched case-insensitively against every
# import name and every dataclass field name in the package.
_FORBIDDEN_IMPORT_NAME_SUBSTRINGS = (
    "pnl",
    "scorecard",
    "scenario_ledger",
    "fill_model",
    "cost_scenario",
    "backtest_runner",
)
_FORBIDDEN_FIELD_NAME_EXACT = {"return", "exit_price"}
_FORBIDDEN_FIELD_NAME_SUBSTRINGS = ("pnl",)
_FORBIDDEN_FIELD_NAME_PREFIX = "forward_"


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def _dataclass_field_names(tree: ast.AST):
    """Every annotated-assignment name inside a ``class ...:`` body -- the
    exact shape a ``@dataclass`` field takes syntactically, whether or not
    the decorator is present (catches a field added to a plain class too)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    yield stmt.target.id


# --------------------------------------------------------------------------- #
# Shared scan primitives -- the REAL guard tests below and the regression
# tests at the bottom of this file both call THESE, so a regression test
# actually exercises the wired-up scanner used in production, not just a
# lower-level AST helper re-implemented inline (an earlier version of this
# file's regression tests called ``_dataclass_field_names``/``_imports``
# directly and never invoked the forbidden-name/prefix logic below at all).
# --------------------------------------------------------------------------- #


def _forbidden_import_root_violations(mod: str, tree: ast.AST) -> list[tuple[str, str]]:
    """Every import whose ROOT module name is in ``_FORBIDDEN_IMPORT_ROOTS``
    (app/DB/broker/scheduler/random/time/alpaca) -- the actual scanner
    ``test_no_forbidden_imports_in_h3_signal_engine`` calls, extracted so it
    can be self-tested against a synthetic fixture the same way every other
    scanner in this file is (ROB-1061 remediation: this scanner used to run
    entirely inline inside the test function, with no synthetic-violation
    regression coverage of its own)."""
    violations = []
    for name in _imports(tree):
        root = name.split(".")[0]
        if root in _FORBIDDEN_IMPORT_ROOTS:
            violations.append((mod, name))
    return violations


def _alpaca_named_import_violations(mod: str, tree: ast.AST) -> list[tuple[str, str]]:
    """Every import whose full name mentions "alpaca" (case-insensitive),
    excluding the one explicit, name-based exception
    (``_ALLOWED_ALPACA_NAMED_IMPORTS``) -- extracted for the same
    self-testability reason as ``_forbidden_import_root_violations`` above."""
    violations = []
    for name in _imports(tree):
        if name in _ALLOWED_ALPACA_NAMED_IMPORTS:
            continue
        if "alpaca" in name.lower():
            violations.append((mod, name))
    return violations


def _forbidden_token_violations(mod: str, text: str) -> list[tuple[str, str]]:
    """Every forbidden broker-order/scheduler token found lexically in
    ``text`` -- the actual scanner
    ``test_no_broker_order_fill_or_scheduler_symbols_referenced`` calls,
    extracted for the same self-testability reason as the import-root
    scanner above (this used to run entirely inline inside the test
    function, with no synthetic-violation regression coverage)."""
    forbidden_tokens = (
        "submit_order",
        "place_order",
        "cancel_order",
        "TaskiqScheduler",
        "@broker.task",
        "prefect.flow",
    )
    violations = []
    for token in forbidden_tokens:
        if token in text:
            violations.append((mod, token))
    return violations


def _field_violations(mod: str, tree: ast.AST) -> list[tuple[str, str]]:
    violations = []
    for field_name in _dataclass_field_names(tree):
        lowered = field_name.lower()
        if (
            lowered in _FORBIDDEN_FIELD_NAME_EXACT
            or any(s in lowered for s in _FORBIDDEN_FIELD_NAME_SUBSTRINGS)
            or lowered.startswith(_FORBIDDEN_FIELD_NAME_PREFIX)
        ):
            violations.append((mod, field_name))
    return violations


def _import_violations(mod: str, tree: ast.AST) -> list[tuple[str, str, str]]:
    violations = []
    for name in _imports(tree):
        lowered = name.lower()
        for forbidden in _FORBIDDEN_IMPORT_NAME_SUBSTRINGS:
            if forbidden in lowered:
                violations.append((mod, name, forbidden))
    return violations


def _wall_clock_hit(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_NOW_ATTRS:
                return True
    return False


def test_no_forbidden_imports_in_h3_signal_engine():
    root_violations = []
    alpaca_violations = []
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        root_violations.extend(_forbidden_import_root_violations(mod, tree))
        alpaca_violations.extend(_alpaca_named_import_violations(mod, tree))
    assert root_violations == [], f"forbidden-root imports found: {root_violations}"
    assert alpaca_violations == [], (
        f"Alpaca-named imports found (H3 must never touch Alpaca quote/BBO/"
        f"execution -- SS13 is the live loop's scope, not the backtest's): "
        f"{alpaca_violations}"
    )


def test_no_wall_clock_now_calls_in_h3_signal_engine():
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        assert not _wall_clock_hit(tree), f"{mod}: forbidden wall-clock call found"


def test_no_broker_order_fill_or_scheduler_symbols_referenced():
    violations = []
    for mod in _MODULES:
        text = (_ROOT / mod).read_text()
        violations.extend(_forbidden_token_violations(mod, text))
    assert violations == [], f"forbidden broker/scheduler tokens found: {violations}"


def test_no_pnl_return_forward_or_exit_price_field_anywhere_in_h3():
    """AC20: the output schema (and every other module) must never carry a
    pnl/return/forward_*/exit_price field."""
    violations = []
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        violations.extend(_field_violations(mod, tree))
    assert violations == []


def test_no_pnl_engine_scorecard_or_scenario_ledger_import_anywhere_in_h3():
    """AC21: a static guard bans importing a PnL engine / scenario ledger /
    scorecard module -- these don't exist as real modules yet (H4/H5), so
    this scans for the SHAPE of such a name rather than a literal module
    that doesn't exist to name yet."""
    violations = []
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        violations.extend(_import_violations(mod, tree))
    assert violations == []


def test_no_pnl_forward_return_or_exit_price_token_anywhere_in_h3_source_text():
    """Belt-and-suspenders lexical scan (mirrors H1's broker-token scan):
    even a non-dataclass-field use of these tokens (a local variable, a
    dict key, a docstring reference to a REAL field) is worth catching."""
    forbidden_substrings = ("exit_price", "forward_return")
    for mod in _MODULES:
        text = (_ROOT / mod).read_text().lower()
        for token in forbidden_substrings:
            assert token not in text, f"{mod} references forbidden token {token!r}"


def test_all_h3_signal_engine_modules_are_accounted_for():
    """Drift guard, pinned to a literal explicit set -- a new module added
    ANYWHERE under research/alpaca_track_signals/ (root OR any subpackage)
    must be added here, keeping every guard above honest about what it
    actually scanned."""
    expected = {
        "dats_engine.py",
        "dats_state.py",
        "decision_calendar.py",
        "indicators.py",
        "output_schema.py",
        "reason_codes.py",
        "seal_consumption.py",
        "sizing.py",
        "wcmb_engine.py",
        "wcmb_ranking.py",
    }
    discovered = {str(p.relative_to(_ROOT)) for p in _iter_source_modules(_ROOT)}
    assert discovered == expected, (
        f"module list drift: discovered {sorted(discovered)}, expected "
        f"{sorted(expected)}"
    )


# --------------------------------------------------------------------------- #
# Regression coverage: prove the ACTUAL scanners the real guard tests above
# call (``_field_violations``/``_import_violations``/``_wall_clock_hit``, NOT
# the lower-level ``_dataclass_field_names``/``_imports`` AST helpers) fire
# against a synthetic fixture (none of these violations exist in the real
# package). An earlier version of these tests called the low-level helpers
# directly and never exercised the forbidden-name/prefix matching logic the
# real guards depend on -- a bug in THAT logic (e.g. a typo in
# ``_FORBIDDEN_FIELD_NAME_SUBSTRINGS``) could have shipped undetected.
# --------------------------------------------------------------------------- #
def test_forbidden_import_root_scanner_actually_catches_a_synthetic_violation(tmp_path):
    (tmp_path / "bad.py").write_text("import sqlalchemy\n")
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _forbidden_import_root_violations("bad.py", tree) == [
        ("bad.py", "sqlalchemy")
    ]


def test_forbidden_import_root_scanner_catches_a_dotted_submodule_by_its_root(tmp_path):
    (tmp_path / "bad.py").write_text("import alpaca_trade_api.rest\n")
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _forbidden_import_root_violations("bad.py", tree) == [
        ("bad.py", "alpaca_trade_api.rest")
    ]


def test_alpaca_named_import_scanner_actually_catches_a_synthetic_violation(tmp_path):
    (tmp_path / "bad.py").write_text("import alpaca_quote_client\n")
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _alpaca_named_import_violations("bad.py", tree) == [
        ("bad.py", "alpaca_quote_client")
    ]


def test_alpaca_named_import_scanner_exempts_the_one_allowed_module(tmp_path):
    (tmp_path / "ok.py").write_text("import pit_universe_alpaca\n")
    tree = ast.parse((tmp_path / "ok.py").read_text())
    assert _alpaca_named_import_violations("ok.py", tree) == []


@pytest.mark.parametrize(
    "token",
    [
        "submit_order",
        "place_order",
        "cancel_order",
        "TaskiqScheduler",
        "@broker.task",
        "prefect.flow",
    ],
)
def test_forbidden_token_scanner_actually_catches_each_synthetic_violation(token):
    text = f"def f():\n    return {token!r}  # {token}\n"
    assert _forbidden_token_violations("bad.py", text) == [("bad.py", token)]


def test_forbidden_token_scanner_is_clean_on_ordinary_source_text():
    text = "def compute_score(closes, ell):\n    return sum(closes) / ell\n"
    assert _forbidden_token_violations("ok.py", text) == []


def test_pnl_field_scanner_actually_catches_a_synthetic_violation(tmp_path):
    (tmp_path / "bad.py").write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Leak:\n"
        "    symbol: str\n"
        "    pnl: float\n"
    )
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _field_violations("bad.py", tree) == [("bad.py", "pnl")]


def test_exit_price_field_scanner_actually_catches_a_synthetic_violation(tmp_path):
    (tmp_path / "bad.py").write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Leak:\n"
        "    exit_price: float\n"
    )
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _field_violations("bad.py", tree) == [("bad.py", "exit_price")]


def test_forward_prefixed_field_scanner_actually_catches_a_synthetic_violation(
    tmp_path,
):
    (tmp_path / "bad.py").write_text(
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Leak:\n"
        "    forward_return_bp: float\n"
    )
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _field_violations("bad.py", tree) == [("bad.py", "forward_return_bp")]


def test_pnl_shaped_import_scanner_actually_catches_a_synthetic_violation(tmp_path):
    (tmp_path / "bad.py").write_text("import scorecard_builder\n")
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _import_violations("bad.py", tree) == [
        ("bad.py", "scorecard_builder", "scorecard")
    ]


@pytest.mark.parametrize("attr", sorted(_FORBIDDEN_NOW_ATTRS))
def test_wall_clock_scanner_actually_catches_each_forbidden_attribute(tmp_path, attr):
    (tmp_path / "bad.py").write_text(
        f"import datetime\n\n\ndef f():\n    return datetime.datetime.{attr}()\n"
    )
    tree = ast.parse((tmp_path / "bad.py").read_text())
    assert _wall_clock_hit(tree) is True
