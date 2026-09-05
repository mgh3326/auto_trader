from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from app.services.lane_events import (
    LANE_EVENT_TEXT_LIMIT,
    LaneEmitResult,
    LaneEventConfig,
    emit_lane_event,
    sanitize_lane_event_text,
)


@pytest.fixture
def emit_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "fake_panewire_emit.py"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import time

dump_path = os.environ.get("LANE_EVENT_TEST_ARGV")
if dump_path:
    with open(dump_path, "w", encoding="utf-8") as dump_file:
        json.dump(sys.argv, dump_file)
mode = os.environ.get("LANE_EVENT_TEST_MODE", "success")
if mode == "file_only":
    print("emit: panewired unavailable; event recorded to file only", file=sys.stderr)
elif mode == "duplicate":
    print("emit: duplicate event_id", file=sys.stderr)
    raise SystemExit(2)
elif mode == "usage":
    print("flag provided but not defined: -bogus", file=sys.stderr)
    raise SystemExit(2)
elif mode == "failed":
    print("emit: event file could not be written: permission denied", file=sys.stderr)
    raise SystemExit(70)
elif mode == "hang":
    time.sleep(30)
""",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


@pytest.mark.parametrize("pane", ["", "w1:p1"])
def test_emit_argv_contract(
    emit_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("LANE_EVENT_TEST_ARGV", str(argv_file))
    config = LaneEventConfig(
        binary=str(emit_binary),
        host="host-a",
        pane=pane,
        inbox_root=str(tmp_path / "inbox"),
        timeout_s=3.0,
    )

    assert emit_lane_event(
        "lane-a", "event-1", "handoff", config=config
    ) == LaneEmitResult("emitted", 0, None)

    # The source of truth is ~/work/panewire/docs/r21-lane-event.md; CI has no
    # panewire binary, so the exact argv contract is fixed here as literals.
    expected = [
        str(emit_binary),
        "emit",
        "--kind",
        "lane.event",
        "--lane",
        "lane-a",
        "--event-id",
        "event-1",
        "--text",
        "handoff",
        "--host",
        "host-a",
    ]
    if pane:
        expected.extend(("--pane", pane))
    expected.extend(("--inbox-root", str(tmp_path / "inbox"), "--timeout", "2s"))
    assert json.loads(argv_file.read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize(
    ("mode", "timeout_s", "expected"),
    [
        ("success", 3.0, LaneEmitResult("emitted", 0, None)),
        ("file_only", 3.0, LaneEmitResult("emitted", 0, None)),
        ("duplicate", 3.0, LaneEmitResult("duplicate", 2, None)),
        ("usage", 3.0, LaneEmitResult("failed", 2, "usage")),
        ("failed", 3.0, LaneEmitResult("failed", 70, "exit_70")),
        ("hang", 0.1, LaneEmitResult("failed", None, "timeout")),
    ],
)
def test_emit_result_for_panewire_paths(
    emit_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    timeout_s: float,
    expected: LaneEmitResult,
) -> None:
    monkeypatch.setenv("LANE_EVENT_TEST_MODE", mode)
    result = emit_lane_event(
        "lane-a",
        "event-1",
        "handoff",
        config=LaneEventConfig(binary=str(emit_binary), timeout_s=timeout_s),
    )
    assert result == expected


def test_emit_validates_input_without_starting_a_process() -> None:
    calls: list[tuple[str, ...]] = []

    def command(argv: list[str]) -> Any:
        calls.append(tuple(argv))
        raise AssertionError("invalid inputs must not start a process")

    config = LaneEventConfig()
    assert emit_lane_event(
        "bad lane", "event-1", "handoff", config=config, command=command
    ) == LaneEmitResult("failed", None, "invalid_lane")
    assert emit_lane_event(
        "lane-a", "", "handoff", config=config, command=command
    ) == LaneEmitResult("failed", None, "invalid_event_id")
    assert emit_lane_event(
        "lane-a", "event\x00", "handoff", config=config, command=command
    ) == LaneEmitResult("failed", None, "invalid_event_id")
    assert emit_lane_event(
        "lane-a", "e" * 513, "handoff", config=config, command=command
    ) == LaneEmitResult("failed", None, "invalid_event_id")
    assert emit_lane_event(
        "lane-a", "event-1", "\t\r\n", config=config, command=command
    ) == LaneEmitResult("failed", None, "empty_text")
    assert calls == []


def test_emit_collapses_command_exceptions() -> None:
    def missing_binary(_argv: list[str]) -> Any:
        raise FileNotFoundError

    def os_error(_argv: list[str]) -> Any:
        raise OSError

    config = LaneEventConfig()
    assert emit_lane_event(
        "lane-a", "event-1", "handoff", config=config, command=missing_binary
    ) == LaneEmitResult("failed", None, "binary_not_found")
    assert emit_lane_event(
        "lane-a", "event-1", "handoff", config=config, command=os_error
    ) == LaneEmitResult("failed", None, "os_error")


def test_sanitize_lane_event_text_controls_whitespace_and_utf8_boundaries() -> None:
    assert (
        sanitize_lane_event_text("  handoff\twith\r\ncontrols\x00  ")
        == "handoff with controls"
    )

    sanitized = sanitize_lane_event_text("가" * 683)
    assert len(sanitized.encode("utf-8")) <= LANE_EVENT_TEXT_LIMIT
    assert sanitized.encode("utf-8").decode("utf-8") == sanitized
    assert sanitized == "가" * 682
