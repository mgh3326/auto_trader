"""ROB-469 PR3: MCP watchdog decision-logic tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.mcp_watchdog import check_once, evaluate_heartbeat, read_heartbeat


def _write_hb(d: Path, color: str, *, is_running: bool, updated: float) -> None:
    (d / f"mcp-{color}.json").write_text(
        json.dumps(
            {"is_running": is_running, "updated_at_unix": updated, "color": color}
        )
    )


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


# --- check_once: full decision incl. loaded-gate + grace (is_loaded/kickstart injected) ---


def _stub_loaded(_label: str, *, uid: int) -> bool:
    return True


def _stub_unloaded(_label: str, *, uid: int) -> bool:
    return False


@pytest.mark.unit
def test_check_once_kickstarts_wedged_loaded(tmp_path: Path) -> None:
    _write_hb(tmp_path, "blue", is_running=True, updated=0.0)  # stale → wedged
    _write_hb(tmp_path, "green", is_running=True, updated=1_000.0)  # fresh → healthy
    kicks: list[str] = []
    statuses = check_once(
        tmp_path,
        stale_threshold_s=30.0,
        dry_run=False,
        uid=501,
        now=1_000.0,
        last_kickstart_at={},
        is_loaded=_stub_loaded,
        kickstart=lambda label, uid: kicks.append(label),
    )
    assert statuses == {"blue": "wedged", "green": "healthy"}
    assert kicks == ["com.robinco.auto-trader.mcp-blue"]


@pytest.mark.unit
def test_check_once_dry_run_never_kickstarts(tmp_path: Path) -> None:
    _write_hb(tmp_path, "blue", is_running=True, updated=0.0)
    kicks: list[str] = []
    check_once(
        tmp_path,
        stale_threshold_s=30.0,
        dry_run=True,
        uid=501,
        now=1_000.0,
        last_kickstart_at={},
        is_loaded=_stub_loaded,
        kickstart=lambda label, uid: kicks.append(label),
    )
    assert kicks == []


@pytest.mark.unit
def test_check_once_not_loaded_is_skipped(tmp_path: Path) -> None:
    _write_hb(tmp_path, "blue", is_running=True, updated=0.0)  # wedged, but not loaded
    kicks: list[str] = []
    check_once(
        tmp_path,
        stale_threshold_s=30.0,
        dry_run=False,
        uid=501,
        now=1_000.0,
        last_kickstart_at={},
        is_loaded=_stub_unloaded,
        kickstart=lambda label, uid: kicks.append(label),
    )
    assert kicks == []  # inactive/booted-out color is never restarted


@pytest.mark.unit
def test_check_once_grace_suppresses_reflap(tmp_path: Path) -> None:
    _write_hb(tmp_path, "blue", is_running=True, updated=0.0)  # persistently stale
    kicks: list[str] = []
    state: dict[str, float] = {}
    common = {
        "stale_threshold_s": 30.0,
        "dry_run": False,
        "uid": 501,
        "last_kickstart_at": state,
        "grace_s": 60.0,
        "is_loaded": _stub_loaded,
        "kickstart": lambda label, uid: kicks.append(label),
    }
    check_once(tmp_path, now=1_000.0, **common)  # kickstarts
    check_once(
        tmp_path, now=1_015.0, **common
    )  # 15s later, slow restart → grace blocks
    assert kicks == ["com.robinco.auto-trader.mcp-blue"]  # only ONCE
    check_once(
        tmp_path, now=1_061.0, **common
    )  # grace expired, still stale → acts again
    assert len(kicks) == 2
