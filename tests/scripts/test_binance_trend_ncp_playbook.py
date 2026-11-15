"""The trend runbook's commands must be the real scripts' arguments.

Like ``test_binance_h5_ncp_playbook.py``: parse every invocation in the runbook
(``/app/.venv/bin/python -m scripts.binance_trend_*``), and check each flag
against that module's real argparse parser. Also pin the operational sentences
that the copy-paste steps depend on: no ``--restart``/detach, no scheduler, and
the three demo flags are set per container.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from scripts import binance_trend_demo as runner
from scripts import binance_trend_heartbeat_watch as watcher
from scripts import binance_trend_tick_export as exporter
from scripts import binance_trend_truth_gate as gate

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "docs/runbooks/binance-trend-demo.md"
PYTHON = "/app/.venv/bin/python"
PARSERS = {
    "scripts.binance_trend_demo": runner._parser,
    "scripts.binance_trend_truth_gate": gate._parser,
    "scripts.binance_trend_heartbeat_watch": watcher._parser,
    "scripts.binance_trend_tick_export": exporter._parser,
}


def logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        lines.append((current + line).strip())
        current = ""
    if current:
        lines.append(current.strip())
    return lines


def invocations(text: str) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    for line in logical_lines(text):
        if PYTHON not in line or " -m " not in line:
            continue
        tail = line.split(PYTHON, 1)[1]
        tokens = shlex.split(tail)
        assert tokens[0] == "-m", tokens
        found.append((tokens[1], tokens[2:]))
    return found


def option_strings(parser) -> set[str]:
    return {option for action in parser._actions for option in action.option_strings}


@pytest.fixture(scope="module")
def text() -> str:
    return RUNBOOK.read_text()


@pytest.fixture(scope="module")
def parsed(text) -> list[tuple[str, list[str]]]:
    return invocations(text)


def test_the_runbook_exists_and_invokes_every_pinned_script(parsed):
    assert {module for module, _ in parsed} == set(PARSERS)


@pytest.mark.parametrize("module", PARSERS)
def test_every_flag_in_the_runbook_exists_in_the_real_parser(module, parsed):
    parser = PARSERS[module]()
    allowed = option_strings(parser)
    used = [
        token.split("=", 1)[0]
        for invoked, args in parsed
        if invoked == module
        for token in args
        if token.startswith("--")
    ]
    assert used, module
    unknown = sorted(set(used) - allowed)
    assert unknown == [], (module, unknown)


def test_the_runbook_covers_all_four_modes_and_the_four_scripts_flags(parsed):
    runner_flags = {
        token.split("=", 1)[0]
        for invoked, args in parsed
        if invoked == "scripts.binance_trend_demo"
        for token in args
    }
    assert {
        "--once",
        "--loop",
        "--manage-only",
        "--readiness",
        "--confirm-demo",
        "--help",
    } <= runner_flags
    watcher_flags = {
        token
        for invoked, args in parsed
        if invoked == "scripts.binance_trend_heartbeat_watch"
        for token in args
    }
    assert {
        "--loop",
        "--send-test",
        "--miss-minutes",
        "--poll-seconds",
    } <= watcher_flags
    gate_flags = {
        token
        for invoked, args in parsed
        if invoked == "scripts.binance_trend_truth_gate"
        for token in args
    }
    assert {"--confirm-demo", "--help"} <= gate_flags


def test_every_starting_command_carries_confirm_demo(parsed):
    for module, args in parsed:
        if module in (
            "scripts.binance_trend_heartbeat_watch",  # DB reads only
            "scripts.binance_trend_tick_export",  # log text -> snapshot file only
        ):
            continue  # no Binance call and no order path
        if "--help" in args:
            continue
        assert "--confirm-demo" in args, (module, args)


def test_no_supervisor_or_scheduler_is_invoked(text):
    for line in logical_lines(text):
        if "docker run" not in line:
            continue
        assert "--restart" not in line, line
        assert " -d " not in line and line.endswith("-d") is False, line
        assert "docker-compose" not in line


def test_the_three_demo_flags_are_per_container_and_never_an_env_file_edit(text):
    for name in (runner.ENV_TREND, runner.ENV_FUTURES, runner.ALERT_ENV):
        assert name in text
        assert f"-e {name}=true" in text, name
    assert "sed -i" not in text
    assert '>> "$env_file"' not in text
    assert ".env.prod" not in text


def test_the_runbook_names_the_state_tables_and_the_absence_rule(text):
    assert "review.binance_trend_signals" in text or "trend 상태 테이블" in text
    assert "runner_absent" in text or "잠금 부재" in text
    assert "스케줄러에도 등록되지 않는다" in text
