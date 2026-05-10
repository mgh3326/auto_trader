"""Guard: the forexfactory calendar flow has no registered deployment (ROB-184)."""

import re
from pathlib import Path

import pytest


def test_forexfactory_flow_file_exists_and_has_flow_decorator():
    """The flow stub file must exist and declare the Prefect flow (static check)."""
    flow_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "flows"
        / "forexfactory_calendar_flow.py"
    )
    assert flow_path.exists(), f"Flow stub not found at {flow_path}"
    text = flow_path.read_text()
    assert "forexfactory_calendar_rolling_window_flow" in text
    assert "@flow" in text


@pytest.mark.skipif(
    True,
    reason="prefect not yet a project dependency; import verified when added",
)
def test_forexfactory_flow_imports_cleanly():
    from app.flows.forexfactory_calendar_flow import (  # noqa: F401
        forexfactory_calendar_rolling_window_flow,
    )


def test_no_prefect_deployment_registered_in_repo():
    """Assert no Deployment(...) registration call references the FF flow in app code."""
    repo_root = Path(__file__).resolve().parents[1]
    deployment_pattern = re.compile(r"Deployment\s*\(")
    flow_name = "forexfactory_calendar_rolling_window"
    for path in repo_root.rglob("*.py"):
        # Skip venv, cache, and test files themselves
        if any(
            p.startswith(".") or p in ("__pycache__", ".venv", "node_modules")
            for p in path.parts
        ):
            continue
        if path.parent.name == "tests" or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if flow_name in text and deployment_pattern.search(text):
            raise AssertionError(
                f"Found a Prefect Deployment registration referencing {flow_name!r} "
                f"in {path}. Activation is approval-gated (ROB-184)."
            )
