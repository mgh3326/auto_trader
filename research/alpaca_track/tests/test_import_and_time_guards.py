"""ROB-1059 H1 (AC4/AC24/AC25) — structural guards, not convention.

Every non-test module in ``research/alpaca_track/`` (the whole H1 data-layer
surface) must never import: app runtime, a DB driver, a broker/order/fill
client, a scheduler (TaskIQ/cron/Prefect), ``random``, or an Alpaca data/order
client — and must never call a wall-clock "now" source
(``datetime.now``/``datetime.utcnow``/``time.time``/``time.time_ns``). Every
timestamp in this package is an explicit, caller-supplied parameter (a
canonical Binance signal is confirmed before any Alpaca quote is even
consulted downstream — no code path here uses Alpaca OHLCV as a signal input,
AC4).
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = [
    "corpus_builder.py",
    "corpus_manifest.py",
    "daily_bars.py",
    "persistence.py",
    "pit_universe_alpaca.py",
    "quote_mode.py",
    "spot_archive_fetch.py",
]

_FORBIDDEN_IMPORT_ROOTS = {
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "taskiq",
    "prefect",
    "random",
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
    ``time.time()``/etc. anywhere in the pure data layer."""
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
            if isinstance(func, ast.Attribute) and func.attr in ("time", "time_ns"):
                value = func.value
                if isinstance(value, ast.Name) and value.id == "time":
                    raise AssertionError(
                        f"{mod}: forbidden `time.{func.attr}()` call found"
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


def test_pit_data_root_style_module_list_is_exhaustive():
    """Every ``.py`` file directly in the package root (excluding this test
    tree, sealed fixtures, and conftest) must be covered by ``_MODULES`` above
    — a new module added later must be swept into this guard, not silently
    exempted."""
    actual = {f.name for f in _ROOT.glob("*.py") if f.name not in ("conftest.py",)}
    assert actual == set(_MODULES), (
        f"module list drift: guard covers {sorted(_MODULES)}, "
        f"actual root .py files are {sorted(actual)}"
    )
