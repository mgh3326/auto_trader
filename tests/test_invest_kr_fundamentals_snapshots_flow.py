"""Tests for the KR fundamentals snapshot Prefect flow scaffold (ROB-429 follow-up).

Prefect is not a project dependency, so the flow file is validated statically
(decorators/name/gate wiring present + no deployment YAML), mirroring the crypto /
US screener flow tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "invest_kr_fundamentals_snapshots_flow.py"
)


def test_flow_file_exists() -> None:
    assert _FLOW_PATH.exists(), f"Flow file not found at {_FLOW_PATH}"


def test_flow_file_declares_prefect_flow_and_task() -> None:
    text = _FLOW_PATH.read_text()
    assert "@flow" in text, "Missing @flow decorator"
    assert "@task" in text, "Missing @task decorator"
    assert "invest_kr_fundamentals_snapshots" in text, "Missing flow name"


def test_flow_file_has_commit_gate_and_builder() -> None:
    text = _FLOW_PATH.read_text()
    assert "invest_screener_snapshots_commit_enabled" in text, (
        "Flow must reference the commit env gate"
    )
    assert "commit=commit_enabled" in text, "commit must be env-gated, not literal"
    assert "run_kr_fundamentals_snapshot_build" in text


def test_flow_defaults_full_universe_and_guarded_partial() -> None:
    text = _FLOW_PATH.read_text()
    assert "all_symbols: bool = True" in text  # daily refresh = full universe
    assert "allow_partial: bool = False" in text  # ROB-429 coverage guard stays on


def test_flow_is_not_registered_via_deployment_yaml() -> None:
    project_root = _FLOW_PATH.parents[2]
    yaml_files = list(project_root.glob("**/*.yaml")) + list(
        project_root.glob("**/*.yml")
    )
    for yf in yaml_files:
        if ".venv" in str(yf) or ".git" in str(yf):
            continue
        if "invest_kr_fundamentals_snapshots" in yf.read_text():
            pytest.fail(
                f"Found Prefect deployment YAML referencing the KR fundamentals flow at {yf}. "
                "Deployment registration is deferred (operator-gated)."
            )
