"""Tests for the crypto_insight_snapshots Prefect flow scaffold (ROB-452 follow-up).

Static checks mirror the crypto screener flow scaffold (prefect is not a project
dependency, so the flow cannot be imported at test time): file present, @flow/@task
declared, commit-gate + regime-populating providers referenced, dry_run/confirm wired to
the gate, and no deployment YAML (registration is operator-gated).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "invest_crypto_insight_snapshots_flow.py"
)


def test_flow_file_exists() -> None:
    assert _FLOW_PATH.exists(), f"Flow file not found at {_FLOW_PATH}"


def test_flow_declares_prefect_flow_and_task() -> None:
    text = _FLOW_PATH.read_text()
    assert "@flow" in text
    assert "@task" in text
    assert "invest_crypto_insight_snapshots" in text


def test_flow_references_commit_gate() -> None:
    text = _FLOW_PATH.read_text()
    assert "invest_screener_snapshots_commit_enabled" in text


def test_flow_default_providers_populate_regime_fields() -> None:
    # defillama (tvl/stablecoin) + tradingview (breadth) must be in the default set,
    # else get_crypto_market_regime stays mostly "missing".
    text = _FLOW_PATH.read_text()
    assert "defillama" in text
    assert "tradingview" in text


def test_flow_not_registered_via_deployment_yaml() -> None:
    project_root = _FLOW_PATH.parents[2]
    yaml_files = list(project_root.glob("**/*.yaml")) + list(
        project_root.glob("**/*.yml")
    )
    for yf in yaml_files:
        if ".venv" in str(yf) or ".git" in str(yf):
            continue
        if "invest_crypto_insight_snapshots" in yf.read_text():
            pytest.fail(
                f"Deployment YAML references the insight flow at {yf}; "
                "registration is deferred (operator-gated)."
            )


def test_commit_gate_wires_dry_run_and_confirm_statically() -> None:
    # prefect is not a project dependency, so the flow cannot be imported in unit tests
    # (mirrors the screener flow scaffold). Verify the gate wiring statically: dry_run /
    # confirm both derive from the commit gate, so a gate-off run is always dry-run.
    text = _FLOW_PATH.read_text()
    assert "dry_run=not commit_enabled" in text
    assert "confirm=commit_enabled" in text


@pytest.mark.skipif(
    True, reason="prefect not yet a project dependency; import verified when added"
)
def test_insight_flow_imports_cleanly() -> None:
    from app.flows.invest_crypto_insight_snapshots_flow import (  # noqa: F401
        invest_crypto_insight_snapshots_flow,
    )
