"""Guard: market-close digest flow has no registered deployment (ROB-1297)."""

from __future__ import annotations

import re
from pathlib import Path

_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "flows"
    / "market_close_digest_flow.py"
)


def test_flow_file_exists_and_has_flow_decorator() -> None:
    assert _FLOW_PATH.exists()
    text = _FLOW_PATH.read_text()
    assert "@flow" in text
    assert "@task" in text
    assert "market_close_digest_flow" in text


def test_flow_defaults_send_off() -> None:
    text = _FLOW_PATH.read_text()
    assert "send: bool = False" in text


def test_no_prefect_deployment_registered_in_repo() -> None:
    repo_root = _FLOW_PATH.parents[2]
    deployment_pattern = re.compile(r"Deployment\s*\(")
    flow_name = "market_close_digest"
    for path in repo_root.rglob("*.py"):
        if any(
            part.startswith(".") or part in {"__pycache__", ".venv", "node_modules"}
            for part in path.parts
        ):
            continue
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if flow_name in text and deployment_pattern.search(text):
            raise AssertionError(
                f"Found a Prefect Deployment registration referencing {flow_name!r} "
                f"in {path}."
            )


def test_no_deployment_yaml_for_digest() -> None:
    project_root = _FLOW_PATH.parents[2]
    for yf in [*project_root.glob("**/*.yaml"), *project_root.glob("**/*.yml")]:
        if any(part in {".venv", ".git", "node_modules"} for part in yf.parts):
            continue
        content = yf.read_text(encoding="utf-8", errors="ignore")
        if "market_close_digest" in content:
            raise AssertionError(f"deployment yaml references digest at {yf}")
