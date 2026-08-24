"""Keep the ``--noconftest`` PostgreSQL boundary complete.

The normal pytest database fixtures are intentionally not part of this test.
This is a source-level contract for modules that can open PostgreSQL directly
when ``tests/conftest.py`` is absent.  A new direct-DB test therefore has to
add its guard in the same change, or this test fails before it can be merged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_ROOT = _REPO_ROOT / "tests"
_GUARD_NAME = "validate_run_owned_database_url"

# These two files are deliberately not standalone DB survivors: under
# ``--noconftest`` every collected test stops at a missing fixture before its
# scratch-engine/asyncpg body can run.  Keep this exception explicit and
# reviewable rather than making the scanner ignore a directory or a pattern.
_NOCONFTEST_ALLOWLIST: dict[str, str] = {
    "tests/services/order_proposals/callback_inbox/test_lock_cleanup_cancellation.py": (
        "all tests require the absent _bootstrap_test_schema fixture"
    ),
    "tests/services/paper_cohort/test_migration.py": (
        "all tests require the absent db_session fixture"
    ),
}


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _has_database_url_reference(node: ast.AST | None) -> bool:
    if node is None:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and (
            _dotted_name(child) == "settings.DATABASE_URL"
        ):
            return True
        if not isinstance(child, ast.Subscript):
            continue
        if _dotted_name(child.value) != "os.environ":
            continue
        slice_node = child.slice
        if isinstance(slice_node, ast.Constant) and slice_node.value == "DATABASE_URL":
            return True
    return False


def _call_is_database_access(node: ast.Call, *, postgres_engine: bool = False) -> bool:
    name = _dotted_name(node.func)
    if postgres_engine and name in {
        "engine.connect",
        "engine.begin",
        "engine.dispose",
    }:
        return True
    if name in {
        "AsyncSessionLocal",
        "SessionLocal",
        "asyncpg.connect",
        "asyncpg.create_pool",
        "shared_engine.connect",
        "shared_engine.begin",
        "_order_session_factory",
    }:
        return True
    # ``runner.AsyncSessionLocal`` is the dynamically loaded backtest runner's
    # real application factory, not a test double.
    if isinstance(node.func, ast.Attribute) and node.func.attr == "AsyncSessionLocal":
        return True
    if name == "async_sessionmaker":
        for keyword in node.keywords:
            if (
                postgres_engine
                and keyword.arg == "bind"
                and "engine" in _dotted_name(keyword.value)
            ):
                return True
    if name == "create_async_engine":
        first = node.args[0] if node.args else None
        if _has_database_url_reference(first):
            return True
        if isinstance(first, ast.JoinedStr) and first.values:
            first_value = first.values[0]
            if (
                isinstance(first_value, ast.Constant)
                and isinstance(first_value.value, str)
                and first_value.value.startswith("sqlite")
            ):
                return False
        return not (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.startswith("sqlite")
        )
    return False


def _function_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _has_database_access(node: ast.AST, *, postgres_engine: bool = False) -> bool:
    return _has_database_access_with_functions(
        node, postgres_engine=postgres_engine, database_functions=frozenset()
    )


def _has_database_access_with_functions(
    node: ast.AST,
    *,
    postgres_engine: bool,
    database_functions: frozenset[str],
) -> bool:
    return any(
        isinstance(child, ast.Call)
        and (
            _call_is_database_access(child, postgres_engine=postgres_engine)
            or _dotted_name(child.func) in database_functions
        )
        for child in ast.walk(node)
    )


def _tree_imports_app_engine(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.core.db"
        and any(
            (alias.asname or alias.name) in {"engine", "shared_engine"}
            for alias in node.names
        )
        for node in ast.walk(tree)
    )


def _custom_fixture_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    builtin_fixtures = {
        "capsys",
        "capfd",
        "caplog",
        "doctest_namespace",
        "monkeypatch",
        "pytestconfig",
        "record_property",
        "record_testsuite_property",
        "request",
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
    }
    names = {
        arg.arg for arg in (*node.args.args, *node.args.kwonlyargs) if arg.arg != "self"
    }
    return names - builtin_fixtures


def _module_has_custom_usefixtures(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        for call in ast.walk(node.value):
            if not isinstance(call, ast.Call):
                continue
            if _dotted_name(call.func) != "pytest.mark.usefixtures":
                continue
            if any(
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and argument.value not in {"monkeypatch", "request"}
                for argument in call.args
            ):
                return True
    return False


def _is_autouse_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if "fixture" not in _dotted_name(decorator.func):
            continue
        if any(
            keyword.arg == "autouse"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in decorator.keywords
        ):
            return True
    return False


def _database_function_names(
    tree: ast.Module, *, postgres_engine: bool
) -> frozenset[str]:
    """Propagate direct DB calls through local helpers used by fixtures/tests."""

    functions = {node.name: node for node in _function_nodes(tree) if node.name}
    direct: set[str] = set()
    for name, node in functions.items():
        if any(
            isinstance(child, ast.Call)
            and _call_is_database_access(child, postgres_engine=postgres_engine)
            for child in ast.walk(node)
        ):
            direct.add(name)

    changed = True
    while changed:
        changed = False
        for name, node in functions.items():
            if name in direct:
                continue
            if any(
                isinstance(child, ast.Call) and _dotted_name(child.func) in direct
                for child in ast.walk(node)
            ):
                direct.add(name)
                changed = True
    return frozenset(direct)


def _module_can_survive_noconftest(tree: ast.Module) -> bool:
    functions = _function_nodes(tree)
    test_functions = [node for node in functions if node.name.startswith("test_")]
    postgres_engine = _tree_imports_app_engine(tree)
    database_functions = _database_function_names(tree, postgres_engine=postgres_engine)
    if not test_functions:
        return False
    if _module_has_custom_usefixtures(tree):
        return False

    # Explicit asyncpg and scratch-engine modules do not depend on pytest
    # fixtures for the connection boundary itself.  Their tests are the
    # migration/schema cases for which the guard must be the first runtime
    # operation.
    if any(
        isinstance(node, ast.Call)
        and (
            _dotted_name(node.func) in {"asyncpg.connect", "asyncpg.create_pool"}
            or (
                _dotted_name(node.func) == "create_async_engine"
                and _call_is_database_access(node, postgres_engine=postgres_engine)
            )
        )
        for node in ast.walk(tree)
    ):
        return True

    # Import-time engine/session-factory construction and direct asyncpg
    # modules fail before fixture resolution, so they are always survivors.
    class _ModuleLevelAccessVisitor(ast.NodeVisitor):
        found = False

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            if _call_is_database_access(
                node, postgres_engine=_tree_imports_app_engine(tree)
            ):
                self.found = True
            self.generic_visit(node)

    module_level = _ModuleLevelAccessVisitor()
    module_level.visit(tree)
    if module_level.found:
        return True

    # A no-argument test can enter its direct database call without a
    # conftest-provided fixture.  The same is true for a no-argument autouse
    # cleanup fixture, provided at least one test can reach fixture setup.
    if any(
        not _custom_fixture_args(node)
        and _has_database_access_with_functions(
            node,
            postgres_engine=postgres_engine,
            database_functions=database_functions,
        )
        for node in test_functions
    ):
        return True
    if any(
        _is_autouse_fixture(node)
        and not _custom_fixture_args(node)
        and _has_database_access_with_functions(
            node,
            postgres_engine=postgres_engine,
            database_functions=database_functions,
        )
        for node in functions
    ) and any(not _custom_fixture_args(node) for node in test_functions):
        return True
    return False


def _find_database_test_modules() -> dict[str, ast.Module]:
    found: dict[str, ast.Module] = {}
    for path in sorted(_TEST_ROOT.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - pytest collection catches this
            raise AssertionError(f"cannot parse {path}: {exc}") from exc
        if not _module_can_survive_noconftest(tree):
            continue
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if relative not in _NOCONFTEST_ALLOWLIST:
            found[relative] = tree
    return found


def _calls_guard(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call) and _dotted_name(node.func).endswith(_GUARD_NAME)
        for node in ast.walk(tree)
    )


def test_every_noconftest_postgresql_survivor_calls_the_database_guard() -> None:
    assert all(
        path.startswith("tests/") and reason.strip()
        for path, reason in _NOCONFTEST_ALLOWLIST.items()
    )
    modules = _find_database_test_modules()
    missing = sorted(path for path, tree in modules.items() if not _calls_guard(tree))

    assert not missing, (
        "direct PostgreSQL test modules must call "
        f"{_GUARD_NAME} before opening a session/engine: {missing}"
    )
