"""Static guard for accidental operational-table writes in KR backfill research.

Coverage boundary: this AST guard folds the explicitly implemented static Python
string forms below, then recognizes public or search_path-dependent INSERT,
UPDATE, DELETE, MERGE, and table DDL. It permits only an *unqualified* CREATE
TEMP[ORARY] TABLE, because PostgreSQL creates that relation in a per-session
temporary schema rather than ``public``. The optional ``IF NOT EXISTS`` clause
is consumed before checking the table target, so a qualified ``public``
temporary table is still rejected with or without that clause. In this grammar,
the only tokens between ``CREATE TEMP[ORARY]`` and the table name are ``TABLE``
and optional ``IF NOT EXISTS``; ``OR REPLACE``, ``ONLY``, and ``UNLOGGED`` are
not valid intervening CREATE TEMP TABLE syntax. ``GLOBAL``/``LOCAL`` precede
``TEMP[ORARY]`` and ``UNLOGGED`` is an alternative pre-``TABLE`` modifier, so
none is eligible for this narrow exemption. SQL comments can syntactically sit
between tokens, but comment parsing remains an explicitly documented grammar
gap rather than an exemption.

It intentionally is not a SQL parser or an evaluator: runtime values (arguments,
environment, imports, files, ORM metadata), unsupported expression operations
(for example ``AugAssign``, ``replace``, slices, ``format_map``, conversions),
and static SQL grammar gaps (comments between verb and target, COPY, SELECT INTO,
bytes SQL, and DDL outside the listed grammar) can evade it. This is a targeted
regression guard, not a claim that all possible database writes are prevented.
R1 accidentally caught DML text in docstrings; R2 intentionally exempted true
docstrings. That means deliberately retrieving ``__doc__`` to execute its text
is a newly opened runtime-smuggling channel, outside this non-evaluator guard.
It does not connect to a database or import database drivers.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KR_BACKFILL_ROOT = REPOSITORY_ROOT / "research" / "kr_backfill"
FIXTURE_ROOT = Path(__file__).with_name("fixtures") / "kr_backfill_public_dml"
REGRESSION_FIXTURES = (
    "r1_literal.py",
    "r2_fstring_marker.py",
    "concat_constants.py",
    "format_and_join.py",
    "container_indirection.py",
    "nested_fstring_local.py",
    "unqualified_target.py",
)
TEMP_PUBLIC_REGRESSION_FIXTURES = (
    "t9_temp_if_not_exists_public.py",
    "t10_temporary_if_not_exists_public.py",
)

_DML_OR_DDL = re.compile(
    r"""\b(?:
        INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|
        CREATE\s+(?P<temporary>TEMP(?:ORARY)?\s+)?TABLE
            (?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE|
        DROP\s+TABLE|TRUNCATE(?:\s+TABLE)?
    )\s+(?P<table>(?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:\"[^\"]+\"|[A-Za-z_][\w$]*))?)""",
    re.IGNORECASE | re.VERBOSE,
)


def _as_strings(value: object) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)) and all(
        isinstance(item, str) for item in value
    ):
        return tuple(value)
    return None


def _static_value(node: ast.expr, names: dict[str, object]) -> object | None:
    """Resolve the static string/container forms used to construct SQL."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_static_value(element, names) for element in node.elts]
        if not all(value is not None for value in values):
            return None
        if isinstance(node, ast.Tuple):
            return tuple(values)
        if isinstance(node, ast.Set):
            return set(values)
        return values
    if isinstance(node, ast.Dict):
        values: dict[object, object] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                return None
            resolved_key = _static_value(key, names)
            resolved_value = _static_value(value, names)
            if resolved_key is None or resolved_value is None:
                return None
            values[resolved_key] = resolved_value
        return values
    if isinstance(node, ast.Subscript):
        container = _static_value(node.value, names)
        key = _static_value(node.slice, names)
        if isinstance(container, dict):
            return container.get(key)
        if isinstance(container, (list, tuple)) and isinstance(key, int):
            return container[key] if -len(container) <= key < len(container) else None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_value(node.left, names)
        right = _static_value(node.right, names)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
            return [*left, *right]
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template = _static_value(node.left, names)
        values = _static_value(node.right, names)
        if isinstance(template, str) and values is not None:
            try:
                return template % values
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                pieces.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                value = _static_value(part.value, names)
                if (
                    not isinstance(value, str)
                    or part.conversion != -1
                    or part.format_spec
                ):
                    return None
                pieces.append(value)
            else:
                return None
        return "".join(pieces)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = _static_value(node.func.value, names)
        if (
            node.func.attr == "join"
            and isinstance(receiver, str)
            and len(node.args) == 1
        ):
            values = _as_strings(_static_value(node.args[0], names))
            return receiver.join(values) if values is not None else None
        if node.func.attr == "format" and isinstance(receiver, str):
            args = [_static_value(argument, names) for argument in node.args]
            kwargs = {
                keyword.arg: _static_value(keyword.value, names)
                for keyword in node.keywords
                if keyword.arg is not None
            }
            if any(value is None for value in args) or any(
                value is None for value in kwargs.values()
            ):
                return None
            try:
                return receiver.format(*args, **kwargs)
            except (IndexError, KeyError, ValueError):
                return None
    return None


def _static_names(tree: ast.Module) -> dict[str, object]:
    """Fold statically resolvable assignments, including function-local constants."""
    names: dict[str, object] = {}
    assignments = [
        statement
        for statement in ast.walk(tree)
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
    ]
    for _ in assignments:
        changed = False
        for statement in assignments:
            value_node: ast.expr | None = None
            targets: list[ast.expr] = []
            if isinstance(statement, ast.Assign):
                value_node, targets = statement.value, statement.targets
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                value_node, targets = statement.value, [statement.target]
            if value_node is None:
                continue
            value = _static_value(value_node, names)
            if value is None:
                continue
            before = dict(names)
            for target in targets:
                _bind_static_target(target, value, names)
            changed |= names != before
        if not changed:
            break
    return names


def _bind_static_target(
    target: ast.expr, value: object, names: dict[str, object]
) -> None:
    if isinstance(target, ast.Name):
        names[target.id] = value
    elif isinstance(target, (ast.List, ast.Tuple)) and isinstance(value, (list, tuple)):
        if len(target.elts) != len(value):
            return
        for child, child_value in zip(target.elts, value, strict=True):
            _bind_static_target(child, child_value, names)


def _is_docstring(
    node: ast.Constant, parent: ast.AST | None, grandparent: ast.AST | None
) -> bool:
    return (
        isinstance(node.value, str)
        and isinstance(parent, ast.Expr)
        and isinstance(
            grandparent,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        )
        and grandparent.body
        and grandparent.body[0] is parent
        and parent.value is node
    )


def find_public_dml(paths: Iterable[Path]) -> list[str]:
    """Return static public/unqualified DML or DDL occurrences for Python files."""
    violations: list[str] = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _static_names(tree)
        parents = {
            id(child): parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.expr):
                continue
            parent = parents.get(id(node))
            if isinstance(node, ast.Constant) and _is_docstring(
                node, parent, parents.get(id(parent)) if parent else None
            ):
                continue
            value = _static_value(node, names)
            if not isinstance(value, str):
                continue
            for match in _DML_OR_DDL.finditer(value):
                table = re.sub(r"\s+", "", match.group("table")).replace('"', "")
                schema, separator, _ = table.partition(".")
                if not separator and match.group("temporary"):
                    continue
                if not separator or schema.lower() == "public":
                    violations.append(f"{path}:{node.lineno}: {match.group(0)!r}")
    return violations


def assert_no_public_dml(paths: Iterable[Path]) -> None:
    violations = find_public_dml(paths)
    assert not violations, (
        "public or search_path-dependent DML/DDL found:\n" + "\n".join(violations)
    )


def test_kr_backfill_has_no_public_or_unqualified_dml() -> None:
    assert_no_public_dml(KR_BACKFILL_ROOT.rglob("*.py"))


@pytest.mark.parametrize(
    "fixture_name",
    REGRESSION_FIXTURES,
)
def test_self_regression_cases_are_rejected(fixture_name: str) -> None:
    fixture = FIXTURE_ROOT / fixture_name
    with pytest.raises(AssertionError, match="public or search_path-dependent DML/DDL"):
        assert_no_public_dml([fixture])


def test_self_regression_summary() -> None:
    cases = [FIXTURE_ROOT / fixture_name for fixture_name in REGRESSION_FIXTURES]
    caught = 0
    for fixture in cases:
        try:
            assert_no_public_dml([fixture])
        except AssertionError as error:
            caught += 1
            print(f"CAUGHT {fixture.name}: {str(error).splitlines()[1]}")
    print(f"SELF_REGRESSION_CASES_CAUGHT = {caught}/{len(cases)}")
    assert caught == len(cases)


def test_research_schema_and_prose_positive_controls_are_allowed() -> None:
    assert_no_public_dml([FIXTURE_ROOT / "positive_control.py"])


def test_only_unqualified_temporary_table_ddl_is_relaxed(tmp_path: Path) -> None:
    temporary = tmp_path / "temporary.py"
    temporary.write_text('SQL = "CREATE TEMP TABLE scratch (id integer)"\n')
    temporary_alias = tmp_path / "temporary_alias.py"
    temporary_alias.write_text('SQL = "CREATE TEMPORARY TABLE scratch (id integer)"\n')
    temporary_if_missing = tmp_path / "temporary_if_missing.py"
    temporary_if_missing.write_text(
        'SQL = "CREATE TEMP TABLE IF NOT EXISTS scratch (id integer)"\n'
    )
    assert_no_public_dml([temporary, temporary_alias, temporary_if_missing])

    public_temporary = tmp_path / "public_temporary.py"
    public_temporary.write_text(
        'SQL = "CREATE TEMP TABLE public.scratch (id integer)"\n'
    )
    regular_unqualified = tmp_path / "regular_unqualified.py"
    regular_unqualified.write_text('SQL = "CREATE TABLE scratch (id integer)"\n')
    for unsafe_path in (public_temporary, regular_unqualified):
        with pytest.raises(
            AssertionError, match="public or search_path-dependent DML/DDL"
        ):
            assert_no_public_dml([unsafe_path])


@pytest.mark.parametrize("fixture_name", TEMP_PUBLIC_REGRESSION_FIXTURES)
def test_temp_if_not_exists_public_regressions_are_rejected(fixture_name: str) -> None:
    with pytest.raises(AssertionError, match="public or search_path-dependent DML/DDL"):
        assert_no_public_dml([FIXTURE_ROOT / fixture_name])


def test_only_real_docstrings_are_exempt(tmp_path: Path) -> None:
    non_docstring = tmp_path / "non_docstring.py"
    non_docstring.write_text(
        "def query() -> None:\n"
        "    marker = 1\n"
        "    \"INSERT INTO public.kr_candles_1m (symbol) VALUES ('005930')\"\n"
    )
    with pytest.raises(AssertionError, match="public or search_path-dependent DML/DDL"):
        assert_no_public_dml([non_docstring])
