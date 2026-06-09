"""ROB-469 PR3: MCP watchdog decision-logic tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.mcp_watchdog import evaluate_heartbeat, read_heartbeat


@pytest.mark.unit
def test_evaluate_missing_is_skipped() -> None:
    assert evaluate_heartbeat(None, now=1000.0, stale_threshold_s=30.0) == "missing"


@pytest.mark.unit
def test_evaluate_stopped_is_skipped() -> None:
    data = {"updated_at_unix": 1000.0, "is_running": False, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "stopped"


@pytest.mark.unit
def test_evaluate_fresh_is_healthy() -> None:
    data = {"updated_at_unix": 980.0, "is_running": True, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "healthy"


@pytest.mark.unit
def test_evaluate_stale_running_is_wedged() -> None:
    data = {"updated_at_unix": 900.0, "is_running": True, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "wedged"


@pytest.mark.unit
def test_evaluate_running_without_timestamp_is_missing() -> None:
    data = {"is_running": True, "color": "blue"}
    assert evaluate_heartbeat(data, now=1000.0, stale_threshold_s=30.0) == "missing"


@pytest.mark.unit
def test_read_heartbeat_missing_returns_none(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path / "nope.json") is None


@pytest.mark.unit
def test_read_heartbeat_corrupt_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "mcp-blue.json"
    p.write_text("{not json")
    assert read_heartbeat(p) is None


@pytest.mark.unit
def test_read_heartbeat_valid_returns_dict(tmp_path: Path) -> None:
    p = tmp_path / "mcp-blue.json"
    p.write_text(json.dumps({"is_running": True, "updated_at_unix": 1.0}))
    assert read_heartbeat(p) == {"is_running": True, "updated_at_unix": 1.0}
