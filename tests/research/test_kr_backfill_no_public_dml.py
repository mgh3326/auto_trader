"""Static guard: the KR backfill code may not write to the operational schema.

BLOCKER-1 of the #1762 R1 review: research/kr_backfill/dryrun_upsert.py executed
`INSERT INTO public.kr_candles_1m` against the operational database inside a
transaction and rolled back. A rolled-back INSERT is still an INSERT, and the
brief's condition was "operational DB write 0".

The backfill destination is `research.kr_candles_1m`. Reading `public` is
legitimate — promotion reads it by design (R-2), and the collector uses it as a
latency/witness probe — so this guard is DML-specific, not a blanket ban.

It scans source text rather than a live database, so it holds without any
database connection and fails the build if a `public` write is reintroduced.

Only **executable** strings are scanned. Docstrings are excluded, because this
module's own history has to be describable in prose — the reviewer drew the same
line when re-checking the migration ("public 언급은 주석뿐").
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

#: DML verbs aimed at the operational schema, in any casing or whitespace.
PUBLIC_DML = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|"
    r"COPY|MERGE\s+INTO)\s+public\.",
    re.IGNORECASE,
)

#: Writes to the operational candle table specifically, however qualified.
PUBLIC_CANDLES_WRITE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+"
    r"(public\.)?kr_candles_1m\b",
    re.IGNORECASE,
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every Constant node that is a docstring, so prose is excluded."""
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


def executable_strings(source: str) -> list[str]:
    """Every string literal that is not a docstring, including f-string parts."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                found.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.append(part.value)
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


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_dml_against_public_schema(path: Path) -> None:
    hits = [
        m.group(0).strip()
        for literal in executable_strings(path.read_text(encoding="utf-8"))
        for m in PUBLIC_DML.finditer(literal)
    ]
    assert not hits, (
        f"{path.relative_to(REPO)} contains DML against the operational schema: {hits}. "
        "The backfill writes to research.kr_candles_1m only."
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_write_to_operational_candle_table(path: Path) -> None:
    """Catches an unqualified `INSERT INTO kr_candles_1m` too.

    `research.kr_candles_1m` is explicitly allowed; a bare or public-qualified
    name is not, because search_path could resolve a bare name to the
    operational table.
    """
    offending = [
        m.group(0).strip()
        for literal in executable_strings(path.read_text(encoding="utf-8"))
        for m in PUBLIC_CANDLES_WRITE.finditer(literal)
        if "research." not in m.group(0).lower()
    ]
    assert not offending, (
        f"{path.relative_to(REPO)} writes to an unqualified/public kr_candles_1m: "
        f"{offending}. Qualify it as research.kr_candles_1m."
    )


def test_dryrun_has_no_caller_chosen_dml_database() -> None:
    """The scratch target must be generated, never accepted as an argument."""
    source = (REPO / "research/kr_backfill/dryrun_upsert.py").read_text(
        encoding="utf-8"
    )

    # No argument may name the DML target database.
    for forbidden in ("--database", "--target-db", "--dsn", "--database-url"):
        assert f'"{forbidden}"' not in source, (
            f"{forbidden} would let a caller pick the target"
        )

    # The guard must interrogate the live connection, not just the DSN string.
    assert "SELECT current_database()" in source
    assert "ScratchGuardViolation" in source
    assert "DENY_DATABASES" in source
    assert "auto_trader" in source, (
        "the operational database must be deny-listed by name"
    )


def test_collector_targets_research_only() -> None:
    source = (REPO / "research/kr_backfill/collect.py").read_text(encoding="utf-8")
    assert "INSERT INTO research.kr_candles_1m" in source
    assert not any(PUBLIC_DML.search(lit) for lit in executable_strings(source))


def test_guard_would_catch_a_reintroduced_public_write() -> None:
    """The guard must actually fire — a green test that cannot fail is worthless."""
    planted = 'SQL = """INSERT INTO public.kr_candles_1m (symbol) VALUES ($1)"""'
    assert any(PUBLIC_DML.search(lit) for lit in executable_strings(planted))

    # ...and must not fire on the same text sitting in a docstring.
    prose = '"""We used to run INSERT INTO public.kr_candles_1m here."""\nX = 1\n'
    assert not any(PUBLIC_DML.search(lit) for lit in executable_strings(prose))

    # ...nor on a legitimate research-schema write.
    ok = 'SQL = """INSERT INTO research.kr_candles_1m (symbol) VALUES ($1)"""'
    assert not any(PUBLIC_DML.search(lit) for lit in executable_strings(ok))
