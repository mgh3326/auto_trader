"""ROB-693 — static boundary scan: auto_trader must never self-author
``invalidation_triggers`` content.

``invalidation_triggers`` is a Hermes-authored advisory narrative field
("what would invalidate this thesis") — auto_trader's role is PERSIST + RENDER
only, mirroring the ROB-501 in-process-LLM boundary (see
``tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py``
for the sibling guard this one is modeled on). If a deterministic generator
(``app/analysis/**``, ``app/services/action_report/**``, etc.) ever starts
synthesizing non-empty invalidation_triggers content, that is a boundary
regression this test must catch.

Allowlist is exactly the two files the write path legitimately touches:
- ``app/schemas/investment_reports.py`` — field definition + duplicate-reject
  guard (schema-level, not content synthesis).
- ``app/services/investment_reports/ingestion.py`` — write-time pass-through
  that merges the caller-supplied list verbatim into evidence_snapshot (no
  transform/synthesis of the narrative content itself).
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]

GUARDED_PATHS: tuple[pathlib.Path, ...] = (REPO_ROOT / "app",)

ALLOWED_FILES: frozenset[pathlib.Path] = frozenset(
    {
        REPO_ROOT / "app" / "schemas" / "investment_reports.py",
        REPO_ROOT / "app" / "services" / "investment_reports" / "ingestion.py",
    }
)

FIELD_NAME = "invalidation_triggers"


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


def _is_definitely_empty(node: ast.AST) -> bool:
    """True only for a provably-empty RHS (``[]`` / ``list()`` / ``None``).

    Anything else (a Name, a Call to something other than bare ``list()``, a
    non-empty list literal, ...) is treated as a potential non-empty write and
    flagged — conservative by design, since the only two legitimate writers
    are allowlisted by file below.
    """
    if isinstance(node, ast.List) and not node.elts:
        return True
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "list"
        and not node.args
        and not node.keywords
    ):
        return True
    return False


def _find_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                is_dict_key_target = (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == FIELD_NAME
                )
                is_attr_target = (
                    isinstance(target, ast.Attribute) and target.attr == FIELD_NAME
                )
                if (is_dict_key_target or is_attr_target) and not _is_definitely_empty(
                    node.value
                ):
                    violations.append(f"assignment at line {node.lineno}")
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == FIELD_NAME
                    and not _is_definitely_empty(value)
                ):
                    violations.append(f"dict literal at line {node.lineno}")
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == FIELD_NAME and not _is_definitely_empty(kw.value):
                    violations.append(f"call kwarg at line {node.lineno}")
    return violations


def test_no_self_authored_invalidation_triggers_outside_allowlist() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _iter_python_files(GUARDED_PATHS):
        if path in ALLOWED_FILES:
            continue
        source = path.read_text(encoding="utf-8")
        if FIELD_NAME not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        violations = _find_violations(tree)
        if violations:
            offenders[str(path.relative_to(REPO_ROOT))] = violations

    assert offenders == {}, (
        "ROB-501-style boundary violated — non-allowlisted app/** code "
        f"self-authors non-empty {FIELD_NAME!r}: {offenders!r}. "
        "invalidation_triggers is Hermes-authored advisory narrative; "
        "auto_trader must only persist + render it (see "
        "app/services/investment_reports/ingestion.py's verbatim pass-through)."
    )


def test_guard_paths_actually_scan_app_runtime() -> None:
    for root in GUARDED_PATHS:
        assert root.exists(), f"guard root missing: {root}"
    files = _iter_python_files(GUARDED_PATHS)
    assert len(files) > 100, "expected guard to inspect the app runtime package"


def test_allowlist_files_exist_and_reference_the_field() -> None:
    for allowed in ALLOWED_FILES:
        assert allowed.exists(), f"allowlisted file missing: {allowed}"
        assert FIELD_NAME in allowed.read_text(encoding="utf-8"), (
            f"{allowed} is allowlisted but no longer references {FIELD_NAME!r} — "
            "allowlist entry is stale, remove it"
        )
