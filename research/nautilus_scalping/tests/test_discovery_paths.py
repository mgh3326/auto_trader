"""ROB-339 — artifact_paths: the D2 artifact-root resolver (pure, stdlib).

Unset env -> repo-internal results/ (current behavior, zero-config / CI-safe).
Env set -> that root. Namespace separation (discovery vs gate) keeps non-canonical
discovery output from being mistaken for gate run-cards.
"""

from __future__ import annotations

from pathlib import Path

import artifact_paths
import pytest

_ENV = "AUTO_TRADER_RESEARCH_ARTIFACT_ROOT"


def test_root_defaults_to_repo_results_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    root = artifact_paths.research_artifact_root()
    # module lives at research/nautilus_scalping/artifact_paths.py
    expected = Path(artifact_paths.__file__).resolve().parent / "results"
    assert root == expected


def test_root_uses_env_when_set(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path / "artifacts"))
    assert artifact_paths.research_artifact_root() == (tmp_path / "artifacts")


def test_empty_env_falls_back_to_results(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "   ")  # whitespace-only is treated as unset
    expected = Path(artifact_paths.__file__).resolve().parent / "results"
    assert artifact_paths.research_artifact_root() == expected


def test_resolve_joins_namespace_and_parts(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path))
    p = artifact_paths.resolve_artifact_path("discovery", "run1", "discovery.json")
    assert p == tmp_path / "discovery" / "run1" / "discovery.json"


def test_resolve_rejects_unknown_namespace(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path))
    with pytest.raises(ValueError):
        artifact_paths.resolve_artifact_path("not_a_namespace", "x.json")


def test_no_app_settings_import() -> None:
    """research-only boundary: the resolver must not pull in app config."""
    import sys

    src = Path(artifact_paths.__file__).read_text()
    assert "from app" not in src and "import app" not in src
    # and importing it did not drag in the app package
    assert "app.core.config" not in sys.modules
