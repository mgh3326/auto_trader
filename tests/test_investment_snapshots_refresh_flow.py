"""Tests for the ROB-269 snapshot bundle refresh Prefect flow scaffold.

Prefect is not yet a project dependency, so we validate the flow file
statically (decorator / function names present) and assert no Prefect
deployment YAML registers the flow. Mirrors the ROB-204 pattern at
``tests/test_invest_screener_snapshots_us_flow.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "investment_snapshots_refresh_flow.py"
)


def test_flow_file_exists() -> None:
    assert _FLOW_PATH.exists(), f"Flow file not found at {_FLOW_PATH}"


def test_flow_file_declares_prefect_flow_and_task() -> None:
    text = _FLOW_PATH.read_text()
    assert "@flow" in text, "Missing @flow decorator"
    assert "@task" in text, "Missing @task decorator"
    assert "investment_snapshots_refresh_flow" in text, "Missing flow function"
    assert "investment_snapshots_refresh_task" in text, "Missing task function"


def test_flow_file_defaults_to_kr_action_report_purpose() -> None:
    text = _FLOW_PATH.read_text()
    assert 'purpose: str = "kr_action_report"' in text, (
        "Flow must default to purpose='kr_action_report'"
    )
    assert 'policy_version: str = "intraday_action_report_v1"' in text, (
        "Flow must default to the Phase 2 policy_version"
    )


def test_flow_file_uses_scheduler_requested_by() -> None:
    text = _FLOW_PATH.read_text()
    assert 'requested_by="scheduler"' in text, (
        "Flow must mark snapshot runs with requested_by='scheduler'"
    )


def test_flow_file_uses_ensure_fresh_mode() -> None:
    text = _FLOW_PATH.read_text()
    assert 'mode="ensure_fresh"' in text, (
        "Flow must call ensure_snapshot_bundle with mode='ensure_fresh'"
    )


def test_flow_file_imports_phase2_ensure_service() -> None:
    text = _FLOW_PATH.read_text()
    assert "SnapshotBundleEnsureService" in text, (
        "Flow must import the Phase 2 ensure service"
    )


def test_flow_file_does_not_call_broker_mutation_verbs() -> None:
    """Safety guard — the flow must not call submit_/cancel_/modify_/place_."""
    text = _FLOW_PATH.read_text()
    for verb in (
        "submit_order(",
        "cancel_order(",
        "modify_order(",
        "place_order(",
        "create_watch_intent(",
    ):
        assert verb not in text, f"Flow file contains forbidden verb: {verb!r}"


def test_flow_is_not_registered_via_deployment_yaml() -> None:
    """Mirrors ROB-204: deployment registration is deferred. Fail loudly if
    a YAML file references this flow name."""
    project_root = _FLOW_PATH.parents[2]
    yaml_files = list(project_root.glob("**/*.yaml")) + list(
        project_root.glob("**/*.yml")
    )
    for yf in yaml_files:
        if ".venv" in str(yf) or ".git" in str(yf) or "node_modules" in str(yf):
            continue
        try:
            content = yf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "investment_snapshots_refresh_flow" in content:
            pytest.fail(
                f"Found Prefect deployment YAML referencing the snapshot refresh flow at {yf}. "
                "Deployment registration is deferred for ROB-269 Phase 4."
            )


@pytest.mark.skipif(
    True,
    reason="prefect not yet a project dependency; import verified when added",
)
def test_flow_imports_cleanly() -> None:
    from app.flows.investment_snapshots_refresh_flow import (  # noqa: F401
        investment_snapshots_refresh_flow,
    )
