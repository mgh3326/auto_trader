"""Keep the ``--noconftest`` PostgreSQL boundary complete.

The normal pytest database fixtures are intentionally not part of this test.
This is a source-level contract for modules that can open PostgreSQL directly
when ``tests/conftest.py`` is absent. A new direct-DB test therefore has to
add its guard in the same change, or this test fails before it can be merged.

The AST check deliberately proves *coverage*, not statement ordering. The
runtime guard itself is called before a connection is opened. Ordering is
checked only by a manual no-conftest dynamic sweep outside this repository;
there is no automated sweep in CI. That external procedure is the ordering
oracle, while this test is only the static coverage check.

Known intentional scanner blind spots (not repaired here): N2 package-relative
imports (``from .rel_helper import ...``), N3 class ``staticmethod`` dispatch,
N4 aliased ``create_async_engine`` imports (``as _mk``), N5 dynamic imports
(``importlib.import_module("app.core.db")``), and N6 ``from tests import X as
Y``. N6 is a real repository idiom, not a synthetic probe: it appears at
``tests/conftest.py:19-20``, ``tests/test_call_duration_plugin.py:34``, and
``tests/test_rob1296_external_http_boundary.py:25``. ``_module_info()`` only
recognizes ``module.startswith("tests.")``, so the dotless ``from tests
import ...`` form is skipped. These gaps are documented rather than chased by
another static-pattern pass; dynamic-sweep automation is a separate backlog
item.

Guarantee strength: this provides accidental prevention plus static detection;
it is not structural impossibility. The current tree has zero active exposure:
there is no open hole today, but these are limits on how future regressions are
detected.

Contract boundary:

* Guaranteed: a test module that directly opens a PostgreSQL session, engine,
  or asyncpg connection is required to validate its URL before the open. This
  remains fail-closed when ``--noconftest`` bypasses pytest's conftest guard:
  a database URL that is not owned by the current run is rejected before
  connection.
* Not guaranteed: a module with no direct database code that calls application
  code (an MCP tool or service) which opens the session. The manual dynamic
  sweep found 14 such modules; they are intentionally outside this direct-DB
  completeness contract and are not hidden by this test.

Follow-up candidate (not implemented here): a process-wide pytest plugin or
``sitecustomize`` guard that blocks external resolution globally, without an
``app/`` change.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_ROOT = _REPO_ROOT / "tests"
_GUARD_NAME = "validate_run_owned_database_url"

_NOCONFTEST_ALLOWLIST: dict[str, str] = {
    "tests/services/order_proposals/callback_inbox/test_lock_cleanup_cancellation.py": (
        "all tests require the absent _bootstrap_test_schema fixture"
    ),
    "tests/services/paper_cohort/test_migration.py": (
        "all tests require the absent retrospective_action_control_lock fixture"
    ),
}

# This one app-owned factory import is an explicit survivor registration. The
# app module is outside the test-module database index, and broadening the
# pattern here would also classify unrelated pre-existing integration modules.
_EXPLICIT_APP_DATABASE_SURVIVORS = frozenset(
    {"tests/brokers/kis/mock_scalping_exec/test_reservation.py"}
)


class _ModuleInfo:
    def __init__(self, path: Path, tree: ast.Module) -> None:
        self.path = path
        self.tree = tree
        self.module_name = _module_name(path)
        self.engine_names: set[str] = set()
        self.engine_roots: set[str] = set()
        self.sessionmaker_names: set[str] = set()
        self.imported_database_names: set[str] = set()
        self.imported_symbols: dict[str, str] = {}
        self.imported_modules: dict[str, str] = {}
        self.factory_names: set[str] = set()
        self.fixture_names: set[str] = set()


def _module_name(path: Path) -> str:
    relative = path.relative_to(_REPO_ROOT).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


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
        if (
            isinstance(child, ast.Attribute)
            and _dotted_name(child) == "settings.DATABASE_URL"
        ):
            return True
        if (
            not isinstance(child, ast.Subscript)
            or _dotted_name(child.value) != "os.environ"
        ):
            continue
        if (
            isinstance(child.slice, ast.Constant)
            and child.slice.value == "DATABASE_URL"
        ):
            return True
    return False


def _is_engine_reference(
    node: ast.AST,
    *,
    engine_names: frozenset[str],
    engine_roots: frozenset[str],
) -> bool:
    name = _dotted_name(node)
    return name in engine_names or name in engine_roots or name.endswith(".engine")


def _call_is_database_access(
    node: ast.Call,
    *,
    postgres_engine: bool = False,
    engine_names: frozenset[str] = frozenset(),
    engine_roots: frozenset[str] = frozenset(),
    sessionmaker_names: frozenset[str] = frozenset(),
    factory_names: frozenset[str] = frozenset(),
    imported_database_names: frozenset[str] = frozenset(),
) -> bool:
    name = _dotted_name(node.func)
    if postgres_engine and any(
        name == f"{root}.{method}"
        for root in (*engine_roots, *engine_names)
        for method in ("connect", "begin", "dispose")
    ):
        return True
    if name in factory_names or name in imported_database_names:
        return True
    if name in {
        "AsyncSessionLocal",
        "SessionLocal",
        "asyncpg.connect",
        "asyncpg.create_pool",
        "shared_engine.connect",
        "shared_engine.begin",
    }:
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr == "AsyncSessionLocal":
        return True
    if name in sessionmaker_names or name in {"async_sessionmaker", "sessionmaker"}:
        if not postgres_engine:
            return False
        first = node.args[0] if node.args else None
        if first is not None and _is_engine_reference(
            first, engine_names=engine_names, engine_roots=engine_roots
        ):
            return True
        return any(
            keyword.arg == "bind"
            and _is_engine_reference(
                keyword.value,
                engine_names=engine_names,
                engine_roots=engine_roots,
            )
            for keyword in node.keywords
        )
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


def _resolve_call_targets(
    node: ast.Call, *, info: _ModuleInfo | None, module_name: str
) -> set[str]:
    if info is None:
        return set()
    name = _dotted_name(node.func)
    if not name:
        return set()
    if "." not in name:
        if name in info.imported_symbols:
            return {info.imported_symbols[name]}
        return {f"{module_name}.{name}"}
    first, rest = name.split(".", 1)
    if first in info.imported_modules:
        return {f"{info.imported_modules[first]}.{rest}"}
    return {f"{module_name}.{name}"}


def _has_database_access_with_functions(
    node: ast.AST,
    *,
    postgres_engine: bool,
    database_functions: frozenset[str],
    database_qualnames: frozenset[str] = frozenset(),
    module_name: str | None = None,
    info: _ModuleInfo | None = None,
) -> bool:
    return any(
        isinstance(child, ast.Call)
        and (
            _call_is_database_access(
                child,
                postgres_engine=postgres_engine,
                engine_names=frozenset(info.engine_names) if info else frozenset(),
                engine_roots=frozenset(info.engine_roots) if info else frozenset(),
                sessionmaker_names=(
                    frozenset(info.sessionmaker_names) if info else frozenset()
                ),
                factory_names=frozenset(info.factory_names) if info else frozenset(),
                imported_database_names=(
                    frozenset(info.imported_database_names) if info else frozenset()
                ),
            )
            or _dotted_name(child.func) in database_functions
            or (
                module_name is not None
                and bool(
                    _resolve_call_targets(child, info=info, module_name=module_name)
                    & database_qualnames
                )
            )
        )
        for child in ast.walk(node)
    )


def _is_fixture_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        (isinstance(decorator, ast.Call) and "fixture" in _dotted_name(decorator.func))
        or _dotted_name(decorator) in {"pytest.fixture", "pytest_asyncio.fixture"}
        for decorator in node.decorator_list
    )


def _module_info(path: Path, tree: ast.Module) -> _ModuleInfo:
    info = _ModuleInfo(path, tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                if module == "app.core.db":
                    if alias.name in {"engine", "shared_engine"}:
                        info.engine_names.add(local)
                    if alias.name in {
                        "AsyncSessionLocal",
                        "SessionLocal",
                        "async_sessionmaker",
                        "sessionmaker",
                    }:
                        info.imported_database_names.add(local)
                    info.imported_symbols[local] = f"{module}.{alias.name}"
                elif module == "app.core" and alias.name == "db":
                    info.engine_roots.add(f"{local}.engine")
                elif module in {
                    "sqlalchemy.ext.asyncio",
                    "sqlalchemy.orm",
                } and alias.name in {
                    "async_sessionmaker",
                    "sessionmaker",
                }:
                    info.sessionmaker_names.add(local)
                elif module.startswith("tests."):
                    info.imported_symbols[local] = f"{module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("tests."):
                    local = alias.asname or alias.name.split(".")[0]
                    info.imported_modules[local] = alias.name

    for node in _function_nodes(tree):
        if _is_fixture_function(node):
            info.fixture_names.add(node.name)

    # The assigned factory name is the authority. This intentionally avoids a
    # hardcoded ``SessionLocal``/``Factory`` allowlist.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not _call_is_database_access(
            node.value,
            postgres_engine=bool(info.engine_names or info.engine_roots),
            engine_names=frozenset(info.engine_names),
            engine_roots=frozenset(info.engine_roots),
            sessionmaker_names=frozenset(info.sessionmaker_names),
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                info.factory_names.add(target.id)
    return info


def _tree_imports_app_engine(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and (
            (
                node.module == "app.core.db"
                and any(
                    (alias.asname or alias.name) in {"engine", "shared_engine"}
                    for alias in node.names
                )
            )
            or (
                node.module == "app.core"
                and any(alias.name == "db" for alias in node.names)
            )
        )
        for node in ast.walk(tree)
    )


def _custom_fixture_args(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    local_fixture_names: frozenset[str] = frozenset(),
) -> set[str]:
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
    return names - builtin_fixtures - local_fixture_names


def _module_usefixtures_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        for call in ast.walk(node.value):
            if (
                not isinstance(call, ast.Call)
                or _dotted_name(call.func) != "pytest.mark.usefixtures"
            ):
                continue
            names.update(
                argument.value
                for argument in call.args
                if isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            )
    return names


def _module_has_custom_usefixtures(
    tree: ast.Module, *, local_fixture_names: frozenset[str] = frozenset()
) -> bool:
    return bool(_module_usefixtures_names(tree) - local_fixture_names)


def _is_autouse_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or "fixture" not in _dotted_name(
            decorator.func
        ):
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


def _build_database_index(infos: dict[str, _ModuleInfo]) -> frozenset[str]:
    database_qualnames: set[str] = set()
    for info in infos.values():
        for function in _function_nodes(info.tree):
            if _has_database_access_with_functions(
                function,
                postgres_engine=bool(info.engine_names or info.engine_roots),
                database_functions=frozenset(),
                info=info,
            ):
                database_qualnames.add(f"{info.module_name}.{function.name}")

    changed = True
    while changed:
        changed = False
        for info in infos.values():
            for function in _function_nodes(info.tree):
                qualified = f"{info.module_name}.{function.name}"
                if qualified in database_qualnames:
                    continue
                if any(
                    _resolve_call_targets(call, info=info, module_name=info.module_name)
                    & database_qualnames
                    for call in ast.walk(function)
                    if isinstance(call, ast.Call)
                ):
                    database_qualnames.add(qualified)
                    changed = True
    return frozenset(database_qualnames)


def _module_can_survive_noconftest(
    tree: ast.Module,
    *,
    info: _ModuleInfo | None = None,
    database_qualnames: frozenset[str] = frozenset(),
) -> bool:
    functions = _function_nodes(tree)
    test_functions = [node for node in functions if node.name.startswith("test_")]
    if not test_functions or info is None:
        return False
    postgres_engine = bool(info.engine_names or info.engine_roots)
    local_fixture_names = frozenset(info.fixture_names)
    database_functions = _database_function_names(tree, postgres_engine=postgres_engine)
    usefixture_names = _module_usefixtures_names(tree)
    if usefixture_names - local_fixture_names:
        return False
    if any(
        f"{info.module_name}.{name}" in database_qualnames
        for name in usefixture_names & local_fixture_names
    ):
        return True

    # A module-local fixture is also a live dependency when it appears in a
    # test signature. It must be subtracted from the *external* fixture set,
    # but its own database work still makes the module a survivor.
    for test in test_functions:
        argument_names = {
            arg.arg
            for arg in (*test.args.args, *test.args.kwonlyargs)
            if arg.arg != "self"
        }
        if any(
            f"{info.module_name}.{name}" in database_qualnames
            for name in argument_names & local_fixture_names
        ):
            return True

    class _ModuleLevelAccessVisitor(ast.NodeVisitor):
        found = False

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            if _call_is_database_access(
                node,
                postgres_engine=postgres_engine,
                engine_names=frozenset(info.engine_names),
                engine_roots=frozenset(info.engine_roots),
                sessionmaker_names=frozenset(info.sessionmaker_names),
                factory_names=frozenset(info.factory_names),
                imported_database_names=frozenset(info.imported_database_names),
            ) or bool(
                _resolve_call_targets(node, info=info, module_name=info.module_name)
                & database_qualnames
            ):
                self.found = True
            self.generic_visit(node)

    module_level = _ModuleLevelAccessVisitor()
    module_level.visit(tree)
    if module_level.found:
        return True

    if any(
        not _custom_fixture_args(node, local_fixture_names=local_fixture_names)
        and _has_database_access_with_functions(
            node,
            postgres_engine=postgres_engine,
            database_functions=database_functions,
            database_qualnames=database_qualnames,
            module_name=info.module_name,
            info=info,
        )
        for node in test_functions
    ):
        return True

    # A no-argument autouse fixture is set up before test fixture resolution;
    # its teardown therefore still runs after a setup error.
    if any(
        _is_autouse_fixture(node)
        and not _custom_fixture_args(node, local_fixture_names=local_fixture_names)
        and _has_database_access_with_functions(
            node,
            postgres_engine=postgres_engine,
            database_functions=database_functions,
            database_qualnames=database_qualnames,
            module_name=info.module_name,
            info=info,
        )
        for node in functions
    ):
        return True
    return False


def _find_database_test_modules() -> dict[str, ast.Module]:
    all_infos: dict[str, _ModuleInfo] = {}
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - pytest collection catches this
            raise AssertionError(f"cannot parse {path}: {exc}") from exc
        relative = path.relative_to(_REPO_ROOT).as_posix()
        all_infos[relative] = _module_info(path, tree)

    database_qualnames = _build_database_index(all_infos)
    found: dict[str, ast.Module] = {}
    for relative, info in all_infos.items():
        if relative in _EXPLICIT_APP_DATABASE_SURVIVORS:
            found[relative] = info.tree
            continue
        if not _module_can_survive_noconftest(
            info.tree, info=info, database_qualnames=database_qualnames
        ):
            continue
        if relative not in _NOCONFTEST_ALLOWLIST:
            found[relative] = info.tree
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
        f"{_GUARD_NAME}; runtime call ordering is checked by the dynamic sweep: "
        f"{missing}"
    )
