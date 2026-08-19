"""ROB-1295: proves tests/_call_duration_plugin.py records call-phase-only cost.

Runs throwaway pytest suites in real subprocesses against the plugin and
asserts on its written ``{"durations": {...}, "not_called": [...]}`` output.

ROB-1295 R1: also proves the plugin's fix for a real weekly-refresh failure
reproduced against tests/services/daily_candles/test_migration_round_trip.py
on this tree — a node whose setup phase itself skips (``@pytest.mark.skip``)
never gets a "call" report at all, so naively treating every collected node
as measurable makes the freshness build fail closed on a false "missing"
even though nothing is actually wrong.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Setup and teardown alone cost >= 0.6s combined; the test body sleeps for
# 0s. Call-phase-only capture must report well under that.
_FIXTURE_PHASE_SLEEP_SECONDS = 0.3
_CALL_DURATION_CEILING_SECONDS = 0.2


def _run_call_duration_plugin(
    tmp_path: Path, source: str, *, extra_args: list[str] | None = None
) -> dict[str, object]:
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir(exist_ok=True)
    (suite_dir / "test_suite.py").write_text(source, encoding="utf-8")
    out_path = tmp_path / "call-durations.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            ".",
            "-p",
            "tests._call_duration_plugin",
            f"--call-durations-out={out_path}",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "-q",
            *(extra_args or []),
        ],
        cwd=suite_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode in (0, 1), result.stdout + result.stderr
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_call_duration_excludes_setup_and_teardown_cost(tmp_path: Path) -> None:
    source = f"""
import time

import pytest


@pytest.fixture(scope="session")
def slow_session_fixture():
    time.sleep({_FIXTURE_PHASE_SLEEP_SECONDS})
    yield "ready"
    time.sleep({_FIXTURE_PHASE_SLEEP_SECONDS})


def test_uses_slow_fixture(slow_session_fixture):
    assert slow_session_fixture == "ready"
"""
    payload = _run_call_duration_plugin(tmp_path, source)

    assert payload["not_called"] == []
    assert list(payload["durations"]) == ["test_suite.py::test_uses_slow_fixture"]

    call_duration = payload["durations"]["test_suite.py::test_uses_slow_fixture"]
    assert call_duration < _CALL_DURATION_CEILING_SECONDS, (
        f"call-phase duration {call_duration}s should exclude the "
        f"{2 * _FIXTURE_PHASE_SLEEP_SECONDS}s of session setup+teardown sleep"
    )


def test_setup_skip_is_recorded_as_not_called_not_a_missing_duration(
    tmp_path: Path,
) -> None:
    # Reproduces the exact failure mode: @pytest.mark.skip makes pytest skip
    # the item during "setup", before "call" ever runs, so there is no
    # report.duration to record for it at all.
    source = """
import pytest


@pytest.mark.skip(reason="ROB-1295 R1 repro")
def test_setup_skipped():
    raise AssertionError("must never execute")
"""
    payload = _run_call_duration_plugin(tmp_path, source)

    assert payload["durations"] == {}
    assert payload["not_called"] == ["test_suite.py::test_setup_skipped"]


def test_call_phase_skip_is_still_recorded_as_a_real_duration(tmp_path: Path) -> None:
    # A test that calls pytest.skip() from inside its own body *does* get a
    # real "call" report (report.when == "call", report.skipped == True,
    # report.duration reflects real elapsed call-phase time) — this must
    # stay in `durations`, not be reclassified as not_called.
    source = """
import time

import pytest


def test_skips_from_inside_call_phase():
    time.sleep(0.05)
    pytest.skip("skips from inside the call phase")
"""
    payload = _run_call_duration_plugin(tmp_path, source)

    assert payload["not_called"] == []
    assert list(payload["durations"]) == [
        "test_suite.py::test_skips_from_inside_call_phase"
    ]
    assert (
        payload["durations"]["test_suite.py::test_skips_from_inside_call_phase"] >= 0.04
    )


def test_setup_skip_contract_holds_under_xdist(tmp_path: Path) -> None:
    # The worker -> controller forwarding path (pytest_testnodedown) must
    # preserve the not_called/durations split exactly like the
    # single-process path above.
    source = """
import pytest


@pytest.mark.skip(reason="ROB-1295 R1 repro (xdist)")
def test_skipped_one():
    raise AssertionError("must never execute")


@pytest.mark.skip(reason="ROB-1295 R1 repro (xdist)")
def test_skipped_two():
    raise AssertionError("must never execute")


def test_runs_normally():
    assert True
"""
    payload = _run_call_duration_plugin(tmp_path, source, extra_args=["-n", "2"])

    assert sorted(payload["not_called"]) == [
        "test_suite.py::test_skipped_one",
        "test_suite.py::test_skipped_two",
    ]
    assert list(payload["durations"]) == ["test_suite.py::test_runs_normally"]
