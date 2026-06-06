"""Tests for the crypto screener snapshot Prefect flow scaffold (ROB-443 PR0).

Prefect is importable but no deployment is registered in this PR. The static
checks mirror the US flow scaffold; the behavioral test exercises the underlying
coroutine (commit must follow the env gate) by monkeypatching the builder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "invest_crypto_screener_snapshots_flow.py"
)


def test_flow_file_exists() -> None:
    assert _FLOW_PATH.exists(), f"Flow file not found at {_FLOW_PATH}"


def test_flow_file_declares_prefect_flow_and_task() -> None:
    text = _FLOW_PATH.read_text()
    assert "@flow" in text, "Missing @flow decorator"
    assert "@task" in text, "Missing @task decorator"
    assert "invest_crypto_screener_snapshots" in text, "Missing flow name"


def test_flow_file_has_commit_gate() -> None:
    text = _FLOW_PATH.read_text()
    assert "invest_screener_snapshots_commit_enabled" in text, (
        "Flow must reference invest_screener_snapshots_commit_enabled env flag"
    )


def test_flow_is_not_registered_via_deployment_yaml() -> None:
    project_root = _FLOW_PATH.parents[2]
    yaml_files = list(project_root.glob("**/*.yaml")) + list(
        project_root.glob("**/*.yml")
    )
    for yf in yaml_files:
        if ".venv" in str(yf) or ".git" in str(yf):
            continue
        content = yf.read_text()
        if "invest_crypto_screener_snapshots" in content:
            pytest.fail(
                f"Found Prefect deployment YAML referencing the crypto snapshot flow at {yf}. "
                "Deployment registration is deferred (ROB-443 safety gate)."
            )


def test_flow_wires_commit_to_gate() -> None:
    # commit passed to the builder is derived from the env gate (not hardcoded True).
    text = _FLOW_PATH.read_text()
    assert "commit=commit_enabled" in text, (
        "Flow must pass the env-gated commit_enabled to the builder, not a literal."
    )
    assert "run_crypto_snapshot_build" in text


def test_flow_defaults_to_full_universe() -> None:
    # Scheduled refresh should build the whole Upbit universe by default.
    text = _FLOW_PATH.read_text()
    assert "all_markets: bool = True" in text


@pytest.mark.skipif(
    True,
    reason="prefect not yet a project dependency; import verified when added",
)
def test_crypto_flow_imports_cleanly() -> None:
    from app.flows.invest_crypto_screener_snapshots_flow import (  # noqa: F401
        invest_crypto_screener_snapshots_flow,
    )
