"""Static guard: the KR backfill code may not write to the operational schema.

R1 shipped `INSERT INTO public.kr_candles_1m` against the operational database.
R2 replaced it with a `public._kr_dryrun_scratch_marker` CREATE+INSERT — and the
guard added in R2 **was green while missing it**, because it collected only the
literal fragments of an f-string and never resolved the `MARKER_TABLE` constant
that carried the word `public`.

So this module resolves SQL the way the code actually builds it:

* module-level `NAME = "literal"` bindings are folded in,
* f-strings are reassembled with those bindings substituted,
* `"a" + "b"` concatenation is folded.

`test_analyser_catches_the_r2_code` pins the regression: the exact R2 shape is
fed in as a fixture and must be detected. A guard that cannot fail on the bug it
was written for is not evidence.

Reading `public` stays legitimate — promotion reads it by design (R-2) and the
collector uses it as a witness probe — so this is DML-specific.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

SCANNED_DIRS = (
    REPO / "research" / "kr_backfill",
    REPO / "app" / "services" / "research_candles",
    REPO / "scripts" / "promote_kr_candles_to_research.py",
)

#: Marks a piece of an f-string we could not resolve, so a partially resolved
#: string can still be matched on the parts we do know.
UNRESOLVED = "\x00?\x00"

PUBLIC_DML = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|COPY|"
    r"MERGE\s+INTO|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|"
    r"CREATE\s+(?:MATERIALIZED\s+)?VIEW|DROP\s+TABLE|ALTER\s+TABLE)\s+public\.",
    re.IGNORECASE,
)

PUBLIC_CANDLES_WRITE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+"
    r"(public\.)?kr_candles_1m\b",
    re.IGNORECASE,
)


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, the ones f-strings interpolate."""
    binds: dict[str, str] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if not isinstance(target, ast.Name) or getattr(node, "value", None) is None:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            binds[target.id] = value.value
    return binds


def _resolve(node: ast.AST, binds: dict[str, str]) -> str | None:
    """Best-effort constant folding of a string expression."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return binds.get(node.id)
    if isinstance(node, ast.Attribute):
        return binds.get(node.attr)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append(_resolve(value.value, binds) or UNRESOLVED)
            else:
                parts.append(UNRESOLVED)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve(node.left, binds)
        right = _resolve(node.right, binds)
        if left is not None and right is not None:
            return left + right
    return None


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def resolved_sql_strings(source: str) -> list[str]:
    """Every non-docstring string expression, with constants folded in."""
    tree = ast.parse(source)
    binds = _module_string_constants(tree)
    skip = _docstring_ids(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                found.append(node.value)
        elif isinstance(node, ast.JoinedStr | ast.BinOp):
            resolved = _resolve(node, binds)
            if resolved:
                found.append(resolved)
    return found


def _python_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCANNED_DIRS:
        if entry.is_file():
            files.append(entry)
        elif entry.is_dir():
            files.extend(sorted(entry.rglob("*.py")))
    assert files, "guard scanned nothing — check SCANNED_DIRS"
    return files


# --- the regression that R2's version could not catch ------------------


R2_CODE_FIXTURE = '''
"""A docstring mentioning INSERT INTO public.kr_candles_1m must not trip this."""
MARKER_TABLE = "public._kr_dryrun_scratch_marker"


async def main(conn, run_id):
    await conn.execute(
        f"CREATE TABLE {MARKER_TABLE} (run_id TEXT NOT NULL, created_at TIMESTAMPTZ)"
    )
    await conn.execute(f"INSERT INTO {MARKER_TABLE} (run_id) VALUES ($1)", run_id)
'''


def test_analyser_catches_the_r2_code() -> None:
    """The exact shape that shipped in R2 while the old guard stayed green."""
    resolved = resolved_sql_strings(R2_CODE_FIXTURE)
    hits = [m.group(0).strip() for s in resolved for m in PUBLIC_DML.finditer(s)]

    assert any("CREATE TABLE public." in h for h in hits), hits
    assert any("INSERT INTO public." in h for h in hits), hits


def test_analyser_folds_string_concatenation() -> None:
    src = 'T = "public.x"\nSQL = "INSERT INTO " + T + " VALUES (1)"\n'
    assert any(PUBLIC_DML.search(s) for s in resolved_sql_strings(src))


def test_analyser_ignores_docstrings() -> None:
    src = '"""We used to run INSERT INTO public.kr_candles_1m here."""\nX = 1\n'
    assert not any(PUBLIC_DML.search(s) for s in resolved_sql_strings(src))


def test_analyser_allows_the_research_schema() -> None:
    src = 'SQL = """INSERT INTO research.kr_candles_1m (symbol) VALUES ($1)"""'
    assert not any(PUBLIC_DML.search(s) for s in resolved_sql_strings(src))


def test_analyser_catches_a_partially_resolved_fstring() -> None:
    """An unreadable placeholder must not hide the part we can read."""
    src = 'TBL = "public.thing"\ndef f(x):\n    return f"{x} INSERT INTO {TBL} VALUES (1)"\n'
    assert any(PUBLIC_DML.search(s) for s in resolved_sql_strings(src))


# --- the guard applied to the real tree --------------------------------


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_dml_against_public_schema(path: Path) -> None:
    hits = [
        m.group(0).strip()
        for s in resolved_sql_strings(path.read_text(encoding="utf-8"))
        for m in PUBLIC_DML.finditer(s)
    ]
    assert not hits, (
        f"{path.relative_to(REPO)} contains DML/DDL against the operational schema: "
        f"{hits}. The backfill writes to research.kr_candles_1m only."
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_write_to_operational_candle_table(path: Path) -> None:
    offending = [
        m.group(0).strip()
        for s in resolved_sql_strings(path.read_text(encoding="utf-8"))
        for m in PUBLIC_CANDLES_WRITE.finditer(s)
        if "research." not in m.group(0).lower()
    ]
    assert not offending, (
        f"{path.relative_to(REPO)} writes to an unqualified/public kr_candles_1m: "
        f"{offending}. Qualify it as research.kr_candles_1m."
    )


def test_dryrun_sends_no_public_schema_reference_at_all() -> None:
    """Beyond DML: `public.` must not appear in this CLI's SQL vocabulary."""
    source = (REPO / "research/kr_backfill/dryrun_upsert.py").read_text(
        encoding="utf-8"
    )
    leaked = [s for s in resolved_sql_strings(source) if "public." in s.lower()]
    assert not leaked, f"'public.' reached the target SQL vocabulary: {leaked}"


def test_dryrun_has_no_caller_chosen_server_or_database() -> None:
    source = (REPO / "research/kr_backfill/dryrun_upsert.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "--admin-url",
        "--database",
        "--target-db",
        "--dsn",
        "--host",
        "--port",
    ):
        assert f'"{forbidden}"' not in source, (
            f"{forbidden} would let a caller pick the server or target"
        )
    assert "SCRATCH_HOST" in source
    assert "ScratchGuardViolation" in source


def test_collector_targets_research_only() -> None:
    source = (REPO / "research/kr_backfill/collect.py").read_text(encoding="utf-8")
    assert "INSERT INTO research.kr_candles_1m" in source
    assert not any(PUBLIC_DML.search(s) for s in resolved_sql_strings(source))
