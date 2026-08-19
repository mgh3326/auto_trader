"""ROB-1295: proves tests/_call_duration_plugin.py records call-phase-only cost.

Runs throwaway pytest suites in real subprocesses against the plugin and
asserts on its written ``{"durations": {...}, "not_called": [...]}`` output.
Also unit-tests the plugin's conflict-detection helpers directly (fast,
in-process) for the xdist aggregation contract.

ROB-1295 R1: proves the plugin's fix for a real weekly-refresh failure
reproduced against tests/services/daily_candles/test_migration_round_trip.py
on this tree — a node whose setup phase itself skips (``@pytest.mark.skip``)
never gets a "call" report at all, so naively treating every collected node
as measurable makes the freshness build fail closed on a false "missing"
even though nothing is actually wrong.

ROB-1295 R2 (post-verify hardening): proves xdist worker/controller
aggregation fails closed on a duplicate node id reported with a different
duration or a contradictory bucket (call vs. not-called), instead of
silently last-write-wins overwriting or masking it — including under the
weekly workflow's actual ``-n 4 --dist=loadfile`` topology, not just a
simulated in-process counterexample.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _call_duration_plugin as plugin

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Setup and teardown alone cost >= 0.6s combined; the test body sleeps for
# 0s. Call-phase-only capture must report well under that.
_FIXTURE_PHASE_SLEEP_SECONDS = 0.3
_CALL_DURATION_CEILING_SECONDS = 0.2


def _run_call_duration_plugin(
    tmp_path: Path,
    source: str,
    *,
    extra_args: list[str] | None = None,
    expect_returncode: int = 0,
) -> dict[str, object]:
    """Run `source` as a throwaway pytest suite against the plugin.

    Defaults to requiring a clean pytest exit (0) — skips are not failures,
    so any test proving ordinary (even all-skipped) behavior should still
    exit 0. Pass ``expect_returncode`` explicitly only for a test that is
    deliberately proving an error-path outcome (e.g. a genuine setup error).
    """
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

    assert result.returncode == expect_returncode, result.stdout + result.stderr
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


def test_setup_error_is_correctly_omitted_and_helper_requires_explicit_nonzero(
    tmp_path: Path,
) -> None:
    # A genuine setup ERROR (not a skip) is neither a measured call nor a
    # legitimate setup-skip -- the node is correctly absent from both
    # `durations` and `not_called`. This is safe in practice only because
    # the weekly workflow's "Measure shard" step does not run with
    # `if: always()` on the upload -- a non-zero pytest exit fails that CI
    # step before the incomplete artifact could ever reach build/validate.
    source = """
import pytest


@pytest.fixture
def broken_fixture():
    raise RuntimeError("setup blows up")


def test_setup_error(broken_fixture):
    assert True


def test_ok():
    assert True
"""
    payload = _run_call_duration_plugin(tmp_path, source, expect_returncode=1)

    assert payload["not_called"] == []
    assert list(payload["durations"]) == ["test_suite.py::test_ok"]


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


def test_weekly_xdist_topology_n4_dist_loadfile_regression(tmp_path: Path) -> None:
    # Reproduces the weekly refresh workflow's actual measure-shard
    # concurrency (test-durations-refresh.yml: `-n 4 --dist=loadfile`) with
    # a suite spread across several files/workers, including setup-skip and
    # normal nodes, to prove the R2 fail-closed contradiction detection
    # does not raise false positives under real (not simulated) worker
    # aggregation.
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    for i in range(6):
        (suite_dir / f"test_file_{i}.py").write_text(
            f"""
import pytest


@pytest.mark.skip(reason="weekly xdist n4 regression")
def test_skipped_{i}():
    raise AssertionError("must never execute")


def test_ok_{i}():
    assert True
""",
            encoding="utf-8",
        )
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
            "-n",
            "4",
            "--dist=loadfile",
        ],
        cwd=suite_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    expected_ok = {f"test_file_{i}.py::test_ok_{i}" for i in range(6)}
    expected_skipped = {f"test_file_{i}.py::test_skipped_{i}" for i in range(6)}
    assert set(payload["durations"]) == expected_ok
    assert set(payload["not_called"]) == expected_skipped


# --- in-process unit tests for the conflict-detection contract itself ------


@pytest.fixture(autouse=True)
def _reset_plugin_state():
    # These tests mutate the plugin module's globals directly; subprocess
    # tests above are unaffected (each subprocess gets its own fresh import
    # of the module), but sibling in-process tests below must not leak
    # state into one another.
    plugin._call_durations.clear()
    plugin._not_called.clear()
    yield
    plugin._call_durations.clear()
    plugin._not_called.clear()


def _fake_worker_node(
    durations: dict[str, float] | None = None, not_called: list[str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        workeroutput={
            "auto_trader_call_durations": dict(durations or {}),
            "auto_trader_not_called": list(not_called or []),
        }
    )


def test_record_duration_is_idempotent_for_an_identical_repeat() -> None:
    plugin._record_duration("n", 1.0)
    plugin._record_duration("n", 1.0)
    assert plugin._call_durations == {"n": 1.0}


def test_record_duration_raises_on_conflicting_value() -> None:
    plugin._record_duration("n", 1.0)
    with pytest.raises(
        plugin.ConflictingCallObservationError, match="conflicting call durations"
    ):
        plugin._record_duration("n", 9.0)


def test_record_not_called_is_idempotent_for_a_repeat() -> None:
    plugin._record_not_called("n")
    plugin._record_not_called("n")
    assert plugin._not_called == {"n"}


def test_record_not_called_after_duration_raises() -> None:
    plugin._record_duration("n", 1.0)
    with pytest.raises(
        plugin.ConflictingCallObservationError,
        match="already recorded with a measured call duration",
    ):
        plugin._record_not_called("n")


def test_record_duration_after_not_called_raises() -> None:
    plugin._record_not_called("n")
    with pytest.raises(
        plugin.ConflictingCallObservationError, match="already recorded as not-called"
    ):
        plugin._record_duration("n", 1.0)


def test_testnodedown_merges_disjoint_worker_data_cleanly() -> None:
    node = _fake_worker_node(durations={"a": 1.0}, not_called=["b"])
    plugin.pytest_testnodedown(node, None)
    assert plugin._call_durations == {"a": 1.0}
    assert plugin._not_called == {"b"}


def test_testnodedown_reproduces_verifier_counterexample_and_hard_fails() -> None:
    # Verifier's exact minimal counterexample: the controller already has
    # n=1.0 from its own logreport; a worker then forwards n with a
    # DIFFERENT duration (9.0) *and* as not_called simultaneously. The old
    # dict.update()/set.update() implementation resolved this to
    # {"durations": {"n": 9.0}, "not_called": []} -- silently dropping the
    # original value and hiding the bucket contradiction. It must now raise.
    plugin._call_durations["n"] = 1.0
    node = _fake_worker_node(durations={"n": 9.0}, not_called=["n"])

    with pytest.raises(plugin.ConflictingCallObservationError):
        plugin.pytest_testnodedown(node, None)


def test_current_output_reflects_disjoint_state() -> None:
    plugin._call_durations["a"] = 1.0
    plugin._not_called.add("b")
    assert plugin._current_output() == {"durations": {"a": 1.0}, "not_called": ["b"]}
