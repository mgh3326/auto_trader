"""Unit tests for the ROB-371 event-coverage artifact location helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.event_coverage.artifact_paths import (
    ENV_VAR,
    coverage_artifact_path,
    event_coverage_artifact_root,
)


@pytest.mark.unit
def test_root_uses_env_when_set(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "/tmp/research-root")
    assert event_coverage_artifact_root() == Path("/tmp/research-root")


@pytest.mark.unit
def test_root_falls_back_to_repo_results_when_unset(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    root = event_coverage_artifact_root()
    assert root.name == "results"
    assert "event_coverage" in str(root)


@pytest.mark.unit
def test_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "   ")
    assert event_coverage_artifact_root().name == "results"


@pytest.mark.unit
def test_coverage_artifact_path_joins_parts(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "/tmp/r")
    p = coverage_artifact_path("us_earnings_coverage.json")
    assert p == Path("/tmp/r/event_coverage/us_earnings_coverage.json")
