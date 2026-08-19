"""ROB-1295: proves tests/_call_duration_plugin.py records call-phase-only cost.

Runs a throwaway pytest suite with an expensive session-scoped fixture (its
setup and teardown sleep well past the assertion threshold below) in a real
subprocess, then asserts the written call-duration artifact reflects only
test-body execution — not fixture bootstrap/teardown time.
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

_SUITE_SOURCE = f"""
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


def test_call_duration_excludes_setup_and_teardown_cost(tmp_path: Path) -> None:
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "test_slow_fixture.py").write_text(_SUITE_SOURCE, encoding="utf-8")
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
        ],
        cwd=suite_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload) == ["test_slow_fixture.py::test_uses_slow_fixture"]

    call_duration = next(iter(payload.values()))
    assert call_duration < _CALL_DURATION_CEILING_SECONDS, (
        f"call-phase duration {call_duration}s should exclude the "
        f"{2 * _FIXTURE_PHASE_SLEEP_SECONDS}s of session setup+teardown sleep"
    )
