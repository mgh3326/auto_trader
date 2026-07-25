"""Bounded static guards and exhaustive emitted-numeric mutation checks.

The static analyzer intentionally claims only this finite scope: exact files in
``_SOURCE_PATHS``, their explicit import graph, lexical assignment aliases, and
statically foldable string flows into calls/getattr/dynamic imports.  It is not
presented as a proof about arbitrary runtime reflection or generated code.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
from pathlib import Path

import pytest
from rob1040_crs24_evidence import (
    _assert_campaign_numeric_derivation,
    _assert_cell_numeric_derivation,
    _cell_core_payload,
    _totals_payload,
    _walk_numeric_or_null,
    build_frozen_synthetic_evidence,
)
from rob1040_crs24_feasibility import ExitPresence, ReferenceKey, ReferenceSurface

_ROOT = Path(__file__).resolve().parents[1]
_WORKTREE = _ROOT.parents[1]
_SOURCE_PATHS = {
    "rob1040_crs24_contracts": _ROOT / "rob1040_crs24_contracts.py",
    "rob1040_crs24_features": _ROOT / "rob1040_crs24_features.py",
    "rob1040_crs24_feasibility": _ROOT / "rob1040_crs24_feasibility.py",
    "rob1040_crs24_evidence": _ROOT / "rob1040_crs24_evidence.py",
    "rob1040_crs24_synthetic": _ROOT / "rob1040_crs24_synthetic.py",
    "rob1040_crs24_cli": _ROOT / "rob1040_crs24_cli.py",
    "run_rob1040_crs24": _ROOT / "run_rob1040_crs24.py",
    "rob974_features": _ROOT / "rob974_features.py",
    "rob974_h4_contracts": _ROOT / "rob974_h4_contracts.py",
    "rob944_folds": _ROOT / "rob944_folds.py",
    "research_contracts.canonical_hash": (
        _WORKTREE / "research_contracts" / "canonical_hash.py"
    ),
}
_ROB1040_ROOTS = {
    name for name in _SOURCE_PATHS if name.startswith(("rob1040_", "run_rob1040_"))
}
_EXPECTED_IMPORTS = {
    "rob1040_crs24_contracts": {
        "__future__",
        "hashlib",
        "dataclasses",
        "decimal",
        "rob974_features",
        "rob974_h4_contracts",
        "research_contracts.canonical_hash",
    },
    "rob1040_crs24_features": {
        "__future__",
        "math",
        "collections.abc",
        "dataclasses",
        "types",
        "rob974_features",
        "rob1040_crs24_contracts",
        "research_contracts.canonical_hash",
    },
    "rob1040_crs24_feasibility": {
        "__future__",
        "math",
        "dataclasses",
        "decimal",
        "rob944_folds",
        "rob974_h4_contracts",
        "rob1040_crs24_contracts",
        "rob1040_crs24_features",
        "research_contracts.canonical_hash",
    },
    "rob1040_crs24_evidence": {
        "__future__",
        "copy",
        "json",
        "math",
        "dataclasses",
        "typing",
        "rob974_h4_contracts",
        "rob1040_crs24_contracts",
        "rob1040_crs24_feasibility",
        "rob1040_crs24_features",
        "rob1040_crs24_synthetic",
        "research_contracts.canonical_hash",
    },
    "rob1040_crs24_synthetic": {
        "__future__",
        "math",
        "dataclasses",
        "decimal",
        "rob974_features",
        "rob974_h4_contracts",
        "rob1040_crs24_contracts",
        "rob1040_crs24_feasibility",
        "rob1040_crs24_features",
        "research_contracts.canonical_hash",
    },
    "rob1040_crs24_cli": {
        "__future__",
        "argparse",
        "json",
        "sys",
        "collections.abc",
        "io",
        "rob1040_crs24_contracts",
    },
    "run_rob1040_crs24": {
        "__future__",
        "rob1040_crs24_cli",
    },
    "rob974_features": {
        "__future__",
        "math",
        "collections.abc",
        "dataclasses",
    },
    "rob974_h4_contracts": {
        "__future__",
        "dataclasses",
        "rob944_folds",
    },
    "rob944_folds": {
        "__future__",
        "dataclasses",
    },
    "research_contracts.canonical_hash": {
        "__future__",
        "hashlib",
        "json",
        "math",
        "datetime",
        "decimal",
        "typing",
    },
}
_ALLOWED_EXTERNAL_IMPORTS = {
    "__future__",
    "argparse",
    "collections.abc",
    "copy",
    "dataclasses",
    "datetime",
    "decimal",
    "hashlib",
    "io",
    "json",
    "math",
    "sys",
    "types",
    "typing",
}
_FORBIDDEN_IMPORT_PREFIXES = (
    "app",
    "asyncpg",
    "boto3",
    "celery",
    "http",
    "httpx",
    "importlib",
    "os",
    "pathlib",
    "psycopg",
    "redis",
    "requests",
    "socket",
    "sqlalchemy",
    "subprocess",
    "taskiq",
    "urllib",
    "websockets",
)
_FORBIDDEN_CALL_PATHS = {
    "__import__",
    "builtins.__import__",
    "builtins.open",
    "compile",
    "eval",
    "exec",
    "globals",
    "importlib.import_module",
    "locals",
    "open",
    "vars",
}
_FORBIDDEN_DYNAMIC_ATTRIBUTES = {
    "__import__",
    "connect",
    "open",
    "read_bytes",
    "read_text",
    "request",
    "send",
    "socket",
    "urlopen",
    "write_bytes",
    "write_text",
}


def _imports(tree: ast.Module) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            values.add(node.module or "")
    return values


@dataclasses.dataclass
class _Scope:
    paths: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    strings: dict[str, set[str]] = dataclasses.field(default_factory=dict)

    def child(self) -> _Scope:
        return _Scope(
            {name: set(values) for name, values in self.paths.items()},
            {name: set(values) for name, values in self.strings.items()},
        )


class _BoundedPrimitiveAnalyzer:
    """Conservative dataflow over the documented finite lexical/static subset."""

    def __init__(self) -> None:
        self.violations: set[str] = set()

    def analyze(self, tree: ast.Module) -> tuple[str, ...]:
        self._statements(tree.body, _Scope())
        return tuple(sorted(self.violations))

    def _strings(self, node: ast.AST, scope: _Scope) -> set[str]:
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return {node.value}
        if isinstance(node, ast.Name):
            return set(scope.strings.get(node.id, ()))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return {
                left + right
                for left in self._strings(node.left, scope)
                for right in self._strings(node.right, scope)
            }
        if isinstance(node, ast.JoinedStr):
            parts: list[set[str]] = []
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    parts.append(self._strings(value.value, scope))
                else:
                    parts.append(self._strings(value, scope))
            answers = {""}
            for part in parts:
                answers = {prefix + suffix for prefix in answers for suffix in part}
            return answers
        return set()

    def _paths(self, node: ast.AST, scope: _Scope) -> set[str]:
        if isinstance(node, ast.Name):
            return set(scope.paths.get(node.id, {node.id}))
        if isinstance(node, ast.Attribute):
            return {f"{base}.{node.attr}" for base in self._paths(node.value, scope)}
        if isinstance(node, ast.Call):
            return self._call(node, scope)
        return set()

    def _flag_paths(self, paths: set[str]) -> None:
        for path in paths:
            if path in _FORBIDDEN_CALL_PATHS:
                self.violations.add(f"call:{path}")
            if path.rsplit(".", maxsplit=1)[-1] in _FORBIDDEN_DYNAMIC_ATTRIBUTES:
                self.violations.add(f"call-attribute:{path}")

    def _call(self, node: ast.Call, scope: _Scope) -> set[str]:
        function_paths = self._paths(node.func, scope)
        self._flag_paths(function_paths)
        for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
            self._expr(argument, scope)
        if function_paths & {"getattr", "builtins.getattr"} and len(node.args) >= 2:
            attributes = self._strings(node.args[1], scope)
            for attribute in attributes & _FORBIDDEN_DYNAMIC_ATTRIBUTES:
                self.violations.add(f"dynamic-attribute:{attribute}")
            return {
                f"{base}.{attribute}"
                for base in self._paths(node.args[0], scope)
                for attribute in attributes
            }
        if function_paths & {"__import__", "builtins.__import__"} and node.args:
            return self._strings(node.args[0], scope)
        if "importlib.import_module" in function_paths and node.args:
            return self._strings(node.args[0], scope)
        return {f"{path}()" for path in function_paths}

    def _expr(self, node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, ast.Call):
            self._call(node, scope)
            return
        if isinstance(node, ast.Lambda):
            child = scope.child()
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                child.paths.pop(argument.arg, None)
                child.strings.pop(argument.arg, None)
            self._expr(node.body, child)
            return
        for child in ast.iter_child_nodes(node):
            self._expr(child, scope)

    @staticmethod
    def _bind_name(
        target: ast.AST,
        *,
        paths: set[str],
        strings: set[str],
        scope: _Scope,
    ) -> None:
        if isinstance(target, ast.Name):
            if paths:
                scope.paths[target.id] = paths
            else:
                scope.paths.pop(target.id, None)
            if strings:
                scope.strings[target.id] = strings
            else:
                scope.strings.pop(target.id, None)

    def _branch(
        self,
        branches: tuple[list[ast.stmt], ...],
        scope: _Scope,
    ) -> None:
        children: list[_Scope] = []
        for branch in branches:
            child = scope.child()
            self._statements(branch, child)
            children.append(child)
        names = set().union(*(child.paths for child in children))
        for name in names:
            merged = set().union(*(child.paths.get(name, set()) for child in children))
            if merged:
                scope.paths[name] = merged
        names = set().union(*(child.strings for child in children))
        for name in names:
            merged = set().union(
                *(child.strings.get(name, set()) for child in children)
            )
            if merged:
                scope.strings[name] = merged

    def _statements(self, statements: list[ast.stmt], scope: _Scope) -> None:
        for statement in statements:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
                    scope.paths[bound] = {alias.name}
                continue
            if isinstance(statement, ast.ImportFrom):
                module = statement.module or ""
                for alias in statement.names:
                    bound = alias.asname or alias.name
                    scope.paths[bound] = {f"{module}.{alias.name}".strip(".")}
                continue
            if isinstance(statement, ast.Assign):
                self._expr(statement.value, scope)
                paths = self._paths(statement.value, scope)
                strings = self._strings(statement.value, scope)
                for target in statement.targets:
                    self._bind_name(
                        target,
                        paths=paths,
                        strings=strings,
                        scope=scope,
                    )
                continue
            if isinstance(statement, ast.AnnAssign) and statement.value is not None:
                self._expr(statement.value, scope)
                self._bind_name(
                    statement.target,
                    paths=self._paths(statement.value, scope),
                    strings=self._strings(statement.value, scope),
                    scope=scope,
                )
                continue
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                for decorator in statement.decorator_list:
                    self._expr(decorator, scope)
                for default in (*statement.args.defaults, *statement.args.kw_defaults):
                    if default is not None:
                        self._expr(default, scope)
                child = scope.child()
                arguments = (
                    *statement.args.posonlyargs,
                    *statement.args.args,
                    *statement.args.kwonlyargs,
                )
                for argument in arguments:
                    child.paths.pop(argument.arg, None)
                    child.strings.pop(argument.arg, None)
                self._statements(statement.body, child)
                continue
            if isinstance(statement, ast.ClassDef):
                self._statements(statement.body, scope.child())
                continue
            if isinstance(statement, ast.If):
                self._expr(statement.test, scope)
                self._branch((statement.body, statement.orelse), scope)
                continue
            if isinstance(statement, ast.Try):
                branches = [statement.body, statement.orelse, statement.finalbody]
                branches.extend(handler.body for handler in statement.handlers)
                self._branch(tuple(branches), scope)
                continue
            if isinstance(statement, ast.With | ast.AsyncWith):
                for item in statement.items:
                    self._expr(item.context_expr, scope)
                self._statements(statement.body, scope.child())
                continue
            if isinstance(statement, ast.For | ast.AsyncFor | ast.While):
                for node in ast.iter_child_nodes(statement):
                    if isinstance(node, ast.expr):
                        self._expr(node, scope)
                self._branch((statement.body, statement.orelse), scope)
                continue
            for node in ast.iter_child_nodes(statement):
                if isinstance(node, ast.expr):
                    self._expr(node, scope)


def _source_violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    violations = set(_BoundedPrimitiveAnalyzer().analyze(tree))
    for imported in _imports(tree):
        if imported.startswith(_FORBIDDEN_IMPORT_PREFIXES):
            violations.add(f"import:{imported}")
    return tuple(sorted(violations))


def _transitive_imports(
    roots: set[str],
    graph: dict[str, set[str]],
) -> set[str]:
    pending = list(roots)
    visited: set[str] = set()
    external: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for imported in graph[current]:
            if imported in graph:
                pending.append(imported)
            else:
                external.add(imported)
    return external


def _payload_keys(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _payload_keys(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _payload_keys(item)


def _numeric_paths(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _numeric_paths(item, (*path, str(key)))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _numeric_paths(item, (*path, str(index)))
    elif type(value) in {int, float}:
        yield path


def _replace_path(
    current: object,
    parts: tuple[str, ...],
    replacement: object,
) -> object:
    if not parts:
        return replacement
    head, *tail = parts
    remaining = tuple(tail)
    if type(current) is dict:
        current[head] = _replace_path(current[head], remaining, replacement)
        return current
    if type(current) is list:
        index = int(head)
        current[index] = _replace_path(current[index], remaining, replacement)
        return current
    if type(current) is tuple:
        index = int(head)
        values = list(current)
        values[index] = _replace_path(values[index], remaining, replacement)
        return tuple(values)
    raise TypeError("mutation path escaped supported payload containers")


def _set_path(
    payload: dict[str, object],
    path: str,
    value: object,
) -> dict[str, object]:
    answer = _replace_path(payload, tuple(path.split(".")), value)
    if type(answer) is not dict:
        raise TypeError("mutation changed the payload root type")
    return answer


def _mutated_numeric(value: int | float | None) -> int | float:
    if value is None:
        return 0
    if type(value) is int:
        return value + 1
    return value + 0.125


def test_exact_direct_and_transitive_import_allowlists() -> None:
    graph = {
        name: _imports(ast.parse(path.read_text()))
        for name, path in _SOURCE_PATHS.items()
    }
    assert graph == _EXPECTED_IMPORTS
    assert _transitive_imports(_ROB1040_ROOTS, graph) <= _ALLOWED_EXTERNAL_IMPORTS


def test_documented_static_scope_has_no_external_state_primitive() -> None:
    for name, path in _SOURCE_PATHS.items():
        assert _source_violations(path.read_text()) == (), name


@pytest.mark.parametrize(
    "source",
    (
        "import builtins as b\nprimitive = b.open\nprimitive('x')",
        (
            "import builtins\nleft = 'op'\nright = 'en'\n"
            "name = left + right\nlookup = getattr\n"
            "primitive = lookup(builtins, name)\nprimitive('x')"
        ),
        (
            "import importlib as il\nleft = 'sock'\nright = 'et'\n"
            "module_name = left + right\nloader = il.import_module\n"
            "loader(module_name)"
        ),
        (
            "from pathlib import Path as P\ninstance = P('x')\n"
            "method = 'read_' + 'text'\nreader = getattr(instance, method)\nreader()"
        ),
        "alias = __import__\nmodule = alias('builtins')\nmodule.open('x')",
        (
            "import builtins as b\nif flag:\n    action = b.open\n"
            "else:\n    action = b.open\naction('x')"
        ),
        "import os",
        "import requests",
    ),
)
def test_alias_and_variable_composed_dynamic_primitives_are_rejected(
    source: str,
) -> None:
    assert _source_violations(source), source


def test_transitive_wrapper_dependency_is_rejected() -> None:
    sources = {
        "root": "from safe_wrapper import read\nread('x')",
        "safe_wrapper": (
            "import builtins as b\nprimitive = b.open\n"
            "def read(path):\n    return primitive(path)\n"
        ),
    }
    graph = {name: _imports(ast.parse(source)) for name, source in sources.items()}
    assert "safe_wrapper" in graph["root"]
    assert _source_violations(sources["root"]) == ()
    assert _source_violations(sources["safe_wrapper"])


def test_exit_presence_schema_cannot_carry_a_numeric_value() -> None:
    assert tuple(field.name for field in dataclasses.fields(ReferenceKey)) == (
        "symbol",
        "timestamp_ms",
    )
    assert tuple(field.name for field in dataclasses.fields(ExitPresence)) == (
        "key",
        "present",
    )
    assert tuple(field.name for field in dataclasses.fields(ReferenceSurface)) == (
        "entries",
        "exit_presence",
    )


def test_emitted_payload_has_no_disallowed_outcome_fields_or_numeric_hash_values() -> (
    None
):
    payload = build_frozen_synthetic_evidence().to_payload()
    forbidden_fragments = (
        "forward_" + "return",
        "exit_" + "price",
        "trade_" + "return",
        "p" + "nl",
        "win_" + "rate",
        "m" + "dd",
        "scenario_" + "ledger",
        "score" + "card",
    )
    forbidden_exact = {
        "e" + "0",
        "e" + "13",
        "e" + "17",
        "e" + "22",
        "p" + "f",
        "p" + "bo",
    }
    keys = tuple(key.lower() for key in _payload_keys(payload))
    assert not any(key in forbidden_exact for key in keys)
    assert not any(fragment in key for key in keys for fragment in forbidden_fragments)
    for cell in payload["cells"]:
        assert tuple(_numeric_paths(cell["hashes"])) == ()
        assert tuple(_numeric_paths(cell["authority"])) == ()


def test_every_emitted_cell_numeric_or_null_leaf_has_mutation_detection() -> None:
    evidence = build_frozen_synthetic_evidence()
    model = evidence.cells[0]
    original = _cell_core_payload(model)
    leaves = _walk_numeric_or_null(original)
    assert leaves
    for path, value in leaves.items():
        mutated = copy.deepcopy(original)
        mutated = _set_path(mutated, path, _mutated_numeric(value))
        with pytest.raises(ValueError, match="numeric derivation mismatch"):
            _assert_cell_numeric_derivation(model, mutated)


def test_every_emitted_campaign_numeric_leaf_has_mutation_detection() -> None:
    evidence = build_frozen_synthetic_evidence()
    original = _totals_payload(evidence.cells, evidence.totals)
    leaves = _walk_numeric_or_null(original)
    assert leaves
    for path, value in leaves.items():
        mutated = copy.deepcopy(original)
        mutated = _set_path(mutated, path, _mutated_numeric(value))
        with pytest.raises(ValueError, match="numeric derivation mismatch"):
            _assert_campaign_numeric_derivation(evidence.cells, mutated)


def test_numeric_walker_and_mutator_cover_nested_lists_and_tuples() -> None:
    probe: dict[str, object] = {"future": [1, None, (2.5,)]}
    leaves = _walk_numeric_or_null(probe)
    assert leaves == {
        "future.0": 1,
        "future.1": None,
        "future.2.0": 2.5,
    }
    for path, value in leaves.items():
        mutated = _set_path(
            copy.deepcopy(probe),
            path,
            _mutated_numeric(value),
        )
        assert _walk_numeric_or_null(mutated)[path] != value

    evidence = build_frozen_synthetic_evidence()
    model = evidence.cells[0]
    payload = _cell_core_payload(model)
    payload["future_numeric_sequence"] = [1, (None, 2.5)]
    with pytest.raises(ValueError, match="numeric derivation mismatch"):
        _assert_cell_numeric_derivation(model, payload)


def test_top_level_numeric_scope_is_only_fixed_contract_shape() -> None:
    payload = build_frozen_synthetic_evidence().to_payload()
    without_cells_or_campaign = {
        key: value for key, value in payload.items() if key not in {"cells", "campaign"}
    }
    assert set(_numeric_paths(without_cells_or_campaign)) == {
        ("cell_shape", "0"),
        ("cell_shape", "1"),
    }
    assert without_cells_or_campaign["cell_shape"] == [3, 8]


def test_terminal_exact_flags_are_replay_derived_not_literal_true() -> None:
    source = _SOURCE_PATHS["rob1040_crs24_evidence"].read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "event_terminal_exact":
                assert not (isinstance(value, ast.Constant) and value.value is True)
