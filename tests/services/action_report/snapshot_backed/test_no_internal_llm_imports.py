"""ROB-501 — runtime in-process LLM static guard.

The auto_trader runtime app is deterministic evidence, validation, and
persistence code. LLM judgment is owned by MCP consumers or Hermes outside this
process. This guard scans all ``app/**/*.py`` runtime files and fails if an
in-process LLM provider surface is reintroduced.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterable

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

GUARDED_PATHS: tuple[pathlib.Path, ...] = (REPO_ROOT / "app",)

FORBIDDEN_IMPORT_MODULES: frozenset[str] = frozenset(
    {
        "app.core.model_rate_limiter",
        "app.services.ai_providers",
        "google.genai",
        "google.generativeai",
        "openai",
    }
)

FORBIDDEN_DEFINED_NAMES: frozenset[str] = frozenset(
    {
        "AiProvider",
        "AiProviderError",
        "AiProviderResult",
        "GeminiProvider",
        "ModelRateLimiter",
        "ModelRunner",
        "OpenAIProvider",
        "RateLimitedGeminiProvider",
    }
)

FORBIDDEN_RUNTIME_FILES: tuple[pathlib.Path, ...] = (
    REPO_ROOT / "app" / "core" / "model_rate_limiter.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "__init__.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "base.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "gemini_provider.py",
    REPO_ROOT / "app" / "services" / "ai_providers" / "openai_provider.py",
)


def _iter_python_files(roots: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(
            p
            for p in root.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts
        )
    return sorted(files)


def _imports_in(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names


def _attribute_accesses(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            found.add(f"{node.value.id}.{node.attr}")
    return found


def _forbidden_import_matches(imports: set[str]) -> set[str]:
    matches: set[str] = set()
    for imported in imports:
        for forbidden in FORBIDDEN_IMPORT_MODULES:
            if imported == forbidden or imported.startswith(f"{forbidden}."):
                matches.add(imported)
    return matches


@pytest.mark.parametrize("path", _iter_python_files(GUARDED_PATHS))
def test_no_runtime_in_process_llm_imports(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports = _imports_in(tree)
    definitions = _defined_names(tree)
    accesses = _attribute_accesses(tree)

    offending_imports = _forbidden_import_matches(imports)
    offending_definitions = definitions & FORBIDDEN_DEFINED_NAMES
    offending_accesses = {
        access
        for access in accesses
        if access.endswith(".ask")
        and any(
            access.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_DEFINED_NAMES
        )
    }

    if offending_imports or offending_definitions or offending_accesses:
        rel = path.relative_to(REPO_ROOT)
        messages: list[str] = []
        if offending_imports:
            messages.append(f"imports forbidden modules: {sorted(offending_imports)!r}")
        if offending_definitions:
            messages.append(
                f"defines forbidden names: {sorted(offending_definitions)!r}"
            )
        if offending_accesses:
            messages.append(f"calls forbidden .ask: {sorted(offending_accesses)!r}")
        pytest.fail(
            "ROB-501 guard violated — "
            f"{rel} re-introduced an in-process LLM runtime surface: "
            + "; ".join(messages)
        )


def test_forbidden_runtime_llm_files_are_absent() -> None:
    existing = [p.relative_to(REPO_ROOT) for p in FORBIDDEN_RUNTIME_FILES if p.exists()]
    assert existing == [], f"forbidden runtime LLM files still exist: {existing!r}"


def test_guard_paths_actually_scan_app_runtime() -> None:
    for root in GUARDED_PATHS:
        assert root.exists(), f"guard root missing: {root}"
    files = _iter_python_files(GUARDED_PATHS)
    assert len(files) > 100, "expected guard to inspect the app runtime package"

