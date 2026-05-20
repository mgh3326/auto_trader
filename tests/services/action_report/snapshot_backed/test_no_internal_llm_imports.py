"""ROB-287 — static guard test.

Asserts that the snapshot-backed report generation path
(``app/services/action_report/snapshot_backed/`` and the
``app/services/investment_stages/`` Hermes pieces) does NOT import or
call any in-process LLM provider. The previously-removed entry points
were:

* ``GeminiProvider`` / ``RateLimitedGeminiProvider`` /
  ``OpenAIProvider`` / generic ``AiProvider`` types
* ``app.services.investment_stages.composer.FinalComposer``
* ``app.services.investment_stages.budget.StageLLMBudget``
* ``BullReducerStage`` / ``BearReducerStage`` / ``RiskReviewStage`` /
  ``LLMReducerStage``

If any of those reappear in the staged path, this test fails. Hermes
performs all reasoning out of process and never owns an
``ask(...)`` call from inside auto_trader.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterable

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "GeminiProvider",
        "RateLimitedGeminiProvider",
        "OpenAIProvider",
        "AiProvider",
        "FinalComposer",
        "StageLLMBudget",
        "BullReducerStage",
        "BearReducerStage",
        "RiskReviewStage",
        "LLMReducerStage",
    }
)

FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "app.services.ai_providers.gemini_provider",
        "app.services.ai_providers.openai_provider",
        "app.services.ai_providers.base",
        "app.services.investment_stages.composer",
        "app.services.investment_stages.rate_limited_provider",
        "app.services.investment_stages.budget",
        "app.services.investment_stages.stages.bull_reducer",
        "app.services.investment_stages.stages.bear_reducer",
        "app.services.investment_stages.stages.risk_review",
        "app.services.investment_stages.stages.llm_reducer",
        "app.services.investment_stages.stages.llm_utils",
    }
)

GUARDED_PATHS: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "app" / "services" / "action_report" / "snapshot_backed",
    REPO_ROOT / "app" / "services" / "investment_stages",
)


def _iter_python_files(roots: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(p for p in root.rglob("*.py") if p.is_file())
    return files


def _imports_in(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return ``(imported_modules, imported_names)`` for a parsed file."""
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            for alias in node.names:
                names.add(alias.name)
    return modules, names


def _attribute_accesses(tree: ast.AST) -> set[str]:
    """Return ``Name.attr`` accesses to flag e.g. ``AiProvider.ask`` if both pieces appear."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            found.add(f"{node.value.id}.{node.attr}")
    return found


@pytest.mark.parametrize("path", _iter_python_files(GUARDED_PATHS))
def test_no_internal_llm_provider_imports(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    modules, names = _imports_in(tree)
    accesses = _attribute_accesses(tree)

    offending_modules = modules & FORBIDDEN_MODULES
    offending_names = names & FORBIDDEN_NAMES
    offending_accesses = {
        access
        for access in accesses
        if access.endswith(".ask")
        and any(access.startswith(forbidden + ".") for forbidden in FORBIDDEN_NAMES)
    }

    if offending_modules or offending_names or offending_accesses:
        rel = path.relative_to(REPO_ROOT)
        msgs: list[str] = []
        if offending_modules:
            msgs.append(f"imports forbidden modules: {sorted(offending_modules)!r}")
        if offending_names:
            msgs.append(f"imports forbidden names: {sorted(offending_names)!r}")
        if offending_accesses:
            msgs.append(f"calls forbidden .ask: {sorted(offending_accesses)!r}")
        pytest.fail(
            "ROB-287 guard violated — "
            f"{rel} re-introduced an in-process LLM dependency: " + "; ".join(msgs)
        )


def test_guard_paths_actually_exist() -> None:
    """Sanity check: the guard scans the intended directories."""
    for root in GUARDED_PATHS:
        assert root.exists(), f"guard root missing: {root}"
    files = _iter_python_files(GUARDED_PATHS)
    assert len(files) > 5, "expected guard to inspect multiple python files"
