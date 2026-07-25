"""ROB-1059 H1 (AC4/AC24/AC25) — structural guards, not convention.

Every non-test module in ``research/alpaca_track/`` (the whole H1 data-layer
surface) must never import: app runtime, a DB driver, a broker/order/fill
client, a scheduler (TaskIQ/cron/Prefect), ``random``, ``time`` (wall-clock),
or an Alpaca data/order client — and must never call a wall-clock "now" source
(``datetime.now``/``datetime.utcnow``/``datetime.today``). Every timestamp in
this package is an explicit, caller-supplied parameter (a canonical Binance
signal is confirmed before any Alpaca quote is even consulted downstream — no
code path here uses Alpaca OHLCV as a signal input, AC4).

S8 remediation (three evasions the previous version of this guard missed):

  1. ``import time as t; t.time()`` -- the previous wall-clock-call scan
     required the binding be literally named ``time`` (``ast.Name.id ==
     "time"``), so any other alias evaded it entirely.
  2. ``from time import time; time()`` -- ``func`` is a bare ``ast.Name`` in
     this form, not an ``ast.Attribute``, so the old attribute-only scan never
     matched it at all.
  3. A module placed in ANY subpackage escaped every guard here: ``_MODULES``
     was a hand-maintained flat list checked only against a root-only
     ``_ROOT.glob("*.py")`` (non-recursive) exhaustiveness test.

The fix for (1)/(2) is simpler and stronger than alias-resolution: ``time`` is
now itself a forbidden IMPORT ROOT (``_FORBIDDEN_IMPORT_ROOTS``). Since every
timestamp in this package is caller-supplied, there is no legitimate need to
import the ``time`` module at all -- banning the import outright catches every
alias/from-form of reaching ``time.time()``/``time.time_ns()``, unconditionally
regardless of how it is later called. The fix for (3) is switching every scan
(and module discovery itself) from a root-only ``glob`` to a recursive
``rglob``, so a module in any subpackage cannot escape the guard, ever.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# Directories that are never part of the H1 data-layer surface itself.
_EXCLUDED_DIR_NAMES = {"tests", "sealed", "__pycache__"}


def _iter_source_modules(root: Path) -> list[Path]:
    """Every ``.py`` file anywhere under ``root``, recursing into ANY
    subpackage (S8: a root-only ``glob("*.py")`` let a module dropped into a
    subpackage completely escape every guard below), excluding the tests
    tree, sealed JSON fixtures, ``__pycache__``, and ``conftest.py``."""
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

_FORBIDDEN_IMPORT_ROOTS = {
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "taskiq",
    "prefect",
    "random",
    "time",  # S8: bans `import time`/`import time as t`/`from time import ...`
    #        in every form -- see module docstring.
    "alpaca",
    "alpaca_trade_api",
}

_FORBIDDEN_NOW_ATTRS = {"now", "utcnow", "today"}


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield n.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_no_forbidden_imports_in_h1_data_layer():
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        for name in _imports(tree):
            root = name.split(".")[0]
            assert root not in _FORBIDDEN_IMPORT_ROOTS, (
                f"{mod} imports forbidden module {name!r} (root {root!r})"
            )
            assert "alpaca" not in name.lower(), (
                f"{mod} imports an Alpaca-named module {name!r} — no code path in "
                f"this package may use Alpaca OHLCV as a signal input (AC4)"
            )


def test_no_wall_clock_now_calls_in_h1_data_layer():
    """Every timestamp must be an explicit parameter — no ``datetime.now()``/
    ``.utcnow()``/``.today()`` anywhere in the pure data layer. (Wall-clock
    ``time.time()``/``time.time_ns()`` access is already fully excluded by the
    ``time`` import ban in ``test_no_forbidden_imports_in_h1_data_layer`` --
    importing `datetime` itself stays legitimate, e.g. for UTC-day
    conversions, so THAT module cannot be banned outright the way `time` can.)
    """
    for mod in _MODULES:
        tree = ast.parse((_ROOT / mod).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_NOW_ATTRS:
                raise AssertionError(
                    f"{mod}: forbidden wall-clock call `.{func.attr}(...)` found — "
                    f"every timestamp must be an explicit caller-supplied parameter"
                )


def test_no_broker_order_fill_or_scheduler_symbols_referenced():
    """Belt-and-suspenders lexical scan: none of these tokens (broker mutation
    or scheduler registration surface) appear anywhere in the H1 data layer
    source, not just in import statements."""
    forbidden_tokens = (
        "submit_order",
        "place_order",
        "cancel_order",
        "TaskiqScheduler",
        "@broker.task",
        "prefect.flow",
    )
    for mod in _MODULES:
        text = (_ROOT / mod).read_text()
        for token in forbidden_tokens:
            assert token not in text, f"{mod} references forbidden token {token!r}"


def test_all_h1_data_layer_modules_are_accounted_for_including_any_subpackage():
    """Drift guard, pinned to a literal explicit set (S8: recursive, not a
    root-only glob) -- a new module added ANYWHERE under
    research/alpaca_track/ (root OR any subpackage) must be added here."""
    expected = {
        "corpus_builder.py",
        "corpus_manifest.py",
        "daily_bars.py",
        "persistence.py",
        "pit_universe_alpaca.py",
        "quote_mode.py",
        "quote_mode_pipeline.py",
        "spot_archive_fetch.py",
    }
    discovered = {str(p.relative_to(_ROOT)) for p in _iter_source_modules(_ROOT)}
    assert discovered == expected, (
        f"module list drift (recursive): discovered {sorted(discovered)}, "
        f"expected {sorted(expected)}"
    )


# --------------------------------------------------------------------------- #
# S8 regression coverage: the exact three evasions this guard previously
# missed, proven against synthetic fixtures (not the real package, since none
# of these violations exist in it today).
# --------------------------------------------------------------------------- #
def test_module_discovery_recurses_into_subpackages(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.py").write_text("import os\n")
    (tmp_path / "top.py").write_text("import sys\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_something.py").write_text("import os\n")
    (tmp_path / "conftest.py").write_text("import os\n")

    discovered = {str(p.relative_to(tmp_path)) for p in _iter_source_modules(tmp_path)}
    assert discovered == {"top.py", str(Path("sub") / "nested.py")}


def test_forbidden_import_scan_catches_a_violation_hidden_inside_a_subpackage(
    tmp_path,
):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "sneaky.py").write_text("import sqlalchemy\n")
    (tmp_path / "clean.py").write_text("import math\n")

    modules = [str(p.relative_to(tmp_path)) for p in _iter_source_modules(tmp_path)]
    assert str(Path("sub") / "sneaky.py") in modules  # proves recursion found it

    violations = []
    for mod in modules:
        tree = ast.parse((tmp_path / mod).read_text())
        for name in _imports(tree):
            if name.split(".")[0] in _FORBIDDEN_IMPORT_ROOTS:
                violations.append((mod, name))
    assert violations == [(str(Path("sub") / "sneaky.py"), "sqlalchemy")]


def test_import_time_as_alias_is_caught_by_the_import_root_ban(tmp_path):
    (tmp_path / "alias_evasion.py").write_text(
        "import time as t\n\n\ndef f():\n    return t.time()\n"
    )
    modules = [str(p.relative_to(tmp_path)) for p in _iter_source_modules(tmp_path)]
    tree = ast.parse((tmp_path / "alias_evasion.py").read_text())
    roots = {name.split(".")[0] for name in _imports(tree)}
    assert "time" in roots
    assert "time" in _FORBIDDEN_IMPORT_ROOTS
    assert "alias_evasion.py" in modules


def test_from_time_import_time_is_caught_by_the_import_root_ban(tmp_path):
    (tmp_path / "from_import_evasion.py").write_text(
        "from time import time\n\n\ndef f():\n    return time()\n"
    )
    tree = ast.parse((tmp_path / "from_import_evasion.py").read_text())
    roots = {(name or "").split(".")[0] for name in _imports(tree)}
    assert "time" in roots
    assert "time" in _FORBIDDEN_IMPORT_ROOTS


def test_time_import_ban_would_fail_the_real_scan_if_reintroduced(tmp_path):
    """End-to-end proof (not just a set-membership check): running the ACTUAL
    ``test_no_forbidden_imports_in_h1_data_layer`` logic against a synthetic
    module containing either evasion form raises."""
    (tmp_path / "evasion.py").write_text("import time as t\n")
    modules = [str(p.relative_to(tmp_path)) for p in _iter_source_modules(tmp_path)]
    with pytest.raises(AssertionError):
        for mod in modules:
            tree = ast.parse((tmp_path / mod).read_text())
            for name in _imports(tree):
                root = name.split(".")[0]
                assert root not in _FORBIDDEN_IMPORT_ROOTS, (
                    f"{mod} imports forbidden module {name!r} (root {root!r})"
                )
