"""Static checks for the Phase 2 demo scalping review Prefect flow.

Prefect is not a project dependency, so the flow module is validated by file
content (decorators / names / delegation / deferred deployment), not import.
The real logic is unit-tested in tests/jobs/test_binance_demo_scalping_review.py.
"""

from __future__ import annotations

from pathlib import Path

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "binance_demo_scalping_review_flow.py"
)


def test_flow_file_exists() -> None:
    assert _FLOW_PATH.exists(), f"Flow file not found at {_FLOW_PATH}"


def test_flow_declares_prefect_flow_and_task() -> None:
    text = _FLOW_PATH.read_text()
    assert "@flow" in text, "Missing @flow decorator"
    assert "@task" in text, "Missing @task decorator"
    assert "binance_demo_scalping_review" in text, "Missing flow name"


def test_flow_delegates_to_job() -> None:
    text = _FLOW_PATH.read_text()
    assert "run_demo_scalping_review_refresh" in text, (
        "Flow must delegate to the prefect-free job helper"
    )


def test_flow_does_not_attach_in_repo_schedule() -> None:
    text = _FLOW_PATH.read_text()
    assert "@broker.task" not in text and "schedule=" not in text, (
        "Recurrence is Prefect-only; no in-repo TaskIQ schedule"
    )


def test_flow_not_registered_via_deployment_yaml() -> None:
    project_root = _FLOW_PATH.parents[2]
    import pytest

    yaml_files = list(project_root.glob("**/*.yaml")) + list(
        project_root.glob("**/*.yml")
    )
    for yf in yaml_files:
        if ".venv" in str(yf) or ".git" in str(yf):
            continue
        if "binance_demo_scalping_review" in yf.read_text():
            pytest.fail(
                f"Deployment YAML references the flow at {yf}; "
                "registration is deferred (external robin-prefect-automations)."
            )
