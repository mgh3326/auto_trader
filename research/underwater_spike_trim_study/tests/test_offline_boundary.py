"""Static proof that the study cannot reach a broker, a database or a network.

The pre-registration requires "브로커/DB/네트워크 호출 없이 코퍼스 파일로만".
That is a property of the import graph, so it is checked as one: an AST scan
of every module in the package, plus a transitive walk of the research
packages the loaders reach into.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent

FORBIDDEN_PREFIXES = (
    "app",
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "redis",
    "httpx",
    "requests",
    "aiohttp",
    "urllib.request",
    "socket",
    "websockets",
    "ccxt",
    "yfinance",
    "pykrx",
)

# The corpus packages the loaders import.  Their *collection* code legitimately
# speaks HTTP; only the modules this study actually imports are walked.
ALLOWED_RESEARCH_MODULES = {
    "research.crypto_corpus.loader",
    "research.crypto_corpus.policy",
    "research.crypto_corpus.constants",
    "research.kr_corpus.backtest.holdout_guard",
    "research.us_corpus.holdout_gate",
    "research.us_corpus.config",
}


def _module_files() -> list[Path]:
    return sorted(p for p in PACKAGE.glob("*.py"))


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", _module_files(), ids=lambda p: p.name)
def test_no_forbidden_import(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _imported_names(tree):
        for prefix in FORBIDDEN_PREFIXES:
            assert not (name == prefix or name.startswith(prefix + ".")), (
                f"{path.name} imports {name!r}; the study must stay offline"
            )


def test_research_dependencies_are_the_declared_ones():
    """The only cross-package imports are the corpus loaders and their guards."""
    seen: set[str] = set()
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen.update(n for n in _imported_names(tree) if n.startswith("research."))
    assert seen <= ALLOWED_RESEARCH_MODULES, seen - ALLOWED_RESEARCH_MODULES


def test_transitively_imported_corpus_modules_are_also_offline():
    repo_root = PACKAGE.parent.parent
    for dotted in sorted(ALLOWED_RESEARCH_MODULES):
        module_path = repo_root.joinpath(*dotted.split(".")).with_suffix(".py")
        assert module_path.exists(), module_path
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            for prefix in FORBIDDEN_PREFIXES:
                assert not (name == prefix or name.startswith(prefix + ".")), (
                    f"{dotted} imports {name!r}"
                )


def test_no_module_names_a_sealed_holdout_path():
    """No module may build a path into a sealed holdout tree.

    Checked over string literals for a path *segment*, so prose that merely
    names the sealed tree cannot trip it while a real path into it would.
    """
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "/holdout" not in node.value.lower(), (
                    f"{path.name} builds a holdout path literal: {node.value!r}"
                )


def test_us_reads_go_through_the_corpus_holdout_gate():
    """Reading the US tree must call ``guard_read``, not open paths directly."""
    source = (PACKAGE / "corpora.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "guard_read" in called
    assert "assert_path_not_holdout" in called
