import json
import subprocess
from pathlib import Path

import pytest

from scripts import ncp_mcp_watchdog

NOW = 1_800_000_000


def _heartbeat(path: Path, *, updated_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"is_running": True, "updated_at_unix": updated_at}),
        encoding="utf-8",
    )


def _install_command_fakes(monkeypatch, shiftgate_stdout, *, shiftgate_error=None):
    calls = []
    restarts = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "shiftgate":
            if shiftgate_error:
                raise shiftgate_error
            return subprocess.CompletedProcess(args, 0, shiftgate_stdout, "")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "true\n", "")
        if args[:2] == ["docker", "restart"]:
            restarts.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args!r}")

    monkeypatch.setattr(ncp_mcp_watchdog.subprocess, "run", fake_run)
    return calls, restarts


def _shiftgate_status(color="blue"):
    return json.dumps(
        [
            {
                "name": "at-mcp",
                "state": {"active": {"color": color}},
            }
        ]
    )


def test_legacy_source_still_restarts_its_active_wedged_container(
    tmp_path, monkeypatch
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("green\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-green.json", updated_at=NOW - 46)
    calls, restarts = _install_command_fakes(monkeypatch, json.dumps([]))

    assert (
        ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW)
        == "restarted"
    )

    assert restarts == ["at-mcp-green"]
    assert [call[-1] for call in calls if call[:2] == ["docker", "inspect"]] == [
        "at-mcp-green"
    ]


def test_shiftgate_active_color_adds_its_namespaced_heartbeat_and_container(
    tmp_path, monkeypatch
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-blue.json", updated_at=NOW)
    _heartbeat(run_dir / "mcp-heartbeat/mcp-sg-green.json", updated_at=NOW - 46)
    calls, restarts = _install_command_fakes(monkeypatch, _shiftgate_status("green"))

    assert (
        ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW) == "healthy"
    )

    assert calls[0] == [
        "shiftgate",
        "--config",
        "/etc/shiftgate/at.yaml",
        "status",
        "--json",
    ]
    assert restarts == ["shiftgate-at-mcp-green"]


@pytest.mark.parametrize(
    "status",
    [
        [],
        [{"name": "at-mcp", "state": {}}],
        [{"name": "at-mcp", "state": {"active": None}}],
    ],
)
def test_missing_shiftgate_state_is_silent_and_legacy_is_still_checked(
    tmp_path, monkeypatch, caplog, status
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-blue.json", updated_at=NOW)
    _install_command_fakes(monkeypatch, json.dumps(status))

    assert (
        ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW) == "healthy"
    )

    assert "shiftgate_status_failed" not in caplog.text
    assert "shiftgate_status_parse_failed" not in caplog.text


def test_shiftgate_heartbeat_parse_error_logs_and_does_not_restart_other_color(
    tmp_path, monkeypatch, caplog
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-blue.json", updated_at=NOW)
    heartbeat = run_dir / "mcp-heartbeat/mcp-sg-green.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text("{invalid", encoding="utf-8")
    _, restarts = _install_command_fakes(monkeypatch, _shiftgate_status("green"))

    assert (
        ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW) == "healthy"
    )

    assert "heartbeat_unavailable source=shiftgate color=green" in caplog.text
    assert restarts == []


def test_shiftgate_heartbeat_invalid_utf8_logs_and_skips_that_source(
    tmp_path, monkeypatch, caplog
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-blue.json", updated_at=NOW)
    heartbeat = run_dir / "mcp-heartbeat/mcp-sg-green.json"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_bytes(b"\xff")
    _, restarts = _install_command_fakes(monkeypatch, _shiftgate_status("green"))

    assert (
        ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW) == "healthy"
    )

    assert "heartbeat_unavailable source=shiftgate color=green" in caplog.text
    assert restarts == []


def test_legacy_heartbeat_invalid_utf8_does_not_block_shiftgate_check(
    tmp_path, monkeypatch, caplog
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    legacy_heartbeat = run_dir / "mcp-heartbeat/mcp-blue.json"
    legacy_heartbeat.parent.mkdir(parents=True, exist_ok=True)
    legacy_heartbeat.write_bytes(b"\xff")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-sg-green.json", updated_at=NOW - 46)
    _, restarts = _install_command_fakes(monkeypatch, _shiftgate_status("green"))

    ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW)

    assert "heartbeat_unavailable source=legacy color=blue" in caplog.text
    assert restarts == ["shiftgate-at-mcp-green"]


def test_missing_shiftgate_heartbeat_logs_and_keeps_legacy_source_active(
    tmp_path, monkeypatch, caplog
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-blue.json", updated_at=NOW)
    _, restarts = _install_command_fakes(monkeypatch, _shiftgate_status("green"))

    assert (
        ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW) == "healthy"
    )

    assert "heartbeat_unavailable source=shiftgate color=green" in caplog.text
    assert restarts == []


@pytest.mark.parametrize(
    ("stdout", "error"),
    [("{invalid", None), ("", FileNotFoundError("shiftgate unavailable"))],
)
def test_shiftgate_status_read_or_parse_error_logs_and_skips_only_that_source(
    tmp_path, monkeypatch, caplog, stdout, error
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-blue.json", updated_at=NOW - 46)
    _, restarts = _install_command_fakes(monkeypatch, stdout, shiftgate_error=error)

    assert (
        ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW)
        == "restarted"
    )

    assert restarts == ["at-mcp-blue"]
    assert "mcp.watchdog.shiftgate_status_" in caplog.text


@pytest.mark.parametrize(
    ("legacy_updated_at", "shiftgate_updated_at", "expected_restart"),
    [
        (NOW, NOW - 46, "shiftgate-at-mcp-green"),
        (NOW - 46, NOW, "at-mcp-blue"),
    ],
)
def test_sources_never_restart_the_other_source_as_fallback(
    tmp_path,
    monkeypatch,
    legacy_updated_at,
    shiftgate_updated_at,
    expected_restart,
):
    run_dir = tmp_path
    (run_dir / "mcp-active-color").write_text("blue\n", encoding="utf-8")
    _heartbeat(run_dir / "mcp-heartbeat/mcp-blue.json", updated_at=legacy_updated_at)
    _heartbeat(
        run_dir / "mcp-heartbeat/mcp-sg-green.json", updated_at=shiftgate_updated_at
    )
    _, restarts = _install_command_fakes(monkeypatch, _shiftgate_status("green"))

    ncp_mcp_watchdog.check_once(run_dir, stale_threshold_s=45, now=NOW)

    assert restarts == [expected_restart]


def test_heartbeat_stall_threshold_and_timer_cadence_are_unchanged(
    monkeypatch,
):
    captured = {}

    def fake_check_once(run_directory, *, stale_threshold_s):
        captured["threshold"] = stale_threshold_s
        return "healthy"

    monkeypatch.setattr(ncp_mcp_watchdog, "check_once", fake_check_once)

    assert ncp_mcp_watchdog.main([]) == 0
    assert captured["threshold"] == 45.0
    assert (
        ncp_mcp_watchdog.evaluate_heartbeat(
            {"is_running": True, "updated_at_unix": NOW}, NOW + 45, 45
        )
        == "healthy"
    )
    assert (
        ncp_mcp_watchdog.evaluate_heartbeat(
            {"is_running": True, "updated_at_unix": NOW}, NOW + 46, 45
        )
        == "wedged"
    )
    timer = Path("ops/ncp/systemd/at-mcp-watchdog.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=15s" in timer
