"""Call-phase-only pytest duration telemetry (ROB-1295).

pytest-split's own shard-balancing timing (``--store-durations`` /
``--durations-path .test_durations``) measures the whole per-test protocol —
setup + call + teardown — so shared fixture/session bootstrap cost gets
folded into whichever test happens to trigger it. That pollutes any signal
derived from ``.test_durations`` beyond its intended purpose (shard
balancing, which stays unchanged and out of scope here).

This plugin instead records only the ``call`` phase per node id, so
downstream freshness telemetry (``scripts/call_durations.py``) reflects
actual test-body cost. It is strictly additive:

* opt-in only, loaded explicitly via ``-p tests._call_duration_plugin``
  together with ``--call-durations-out=PATH``; never active in the required
  ``test`` job or any other pytest invocation that does not pass both flags;
* never reads or writes ``.test_durations`` — the pytest-split consumer of
  that file is untouched.

ROB-1295 R1: a node whose ``setup`` phase itself skips (``@pytest.mark.skip``,
``skipif``, a fixture raising ``Skipped``/``unittest.SkipTest``, ...) never
gets a ``call`` report at all — pytest's runtest protocol only invokes the
``call`` phase after ``setup`` passes. Such nodes are collected but never
"called", so they cannot have a call-phase duration; they are recorded
separately as ``not_called`` rather than silently omitted (which the
downstream merge/validator would otherwise treat as a genuine missing
measurement) or coerced to a bogus ``0.0`` duration (indistinguishable from a
real, very-fast call). A node that skips *inside* its call phase (e.g.
``pytest.skip()`` called from the test body) is unaffected: pytest still
emits a real ``call`` report with a real ``report.duration``, so it is
captured in ``durations`` as usual.

ROB-1295 R2: every observation of a node id — from this process's own
``pytest_runtest_logreport`` and from every xdist worker forwarded via
``pytest_testnodedown`` — is recorded through ``_record_duration``/
``_record_not_called``, which keep ``durations``/``not_called`` disjoint by
construction. A *repeated* observation for the same node id is tolerated
only when it is identical to what is already recorded (same bucket, same
duration value) — genuinely harmless double-delivery. Any node id reported
with a different duration, or reported in both buckets, raises
``ConflictingCallObservationError`` immediately rather than being resolved
silently (dropping data or preferring one report over another).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Module-level globals rather than a config stash: this needs to survive a
# hook signature (``pytest_runtest_logreport``) that pytest does not pass
# ``config`` into. Safe because each pytest process (controller or xdist
# worker) gets its own fresh import of this module.
_call_durations: dict[str, float] = {}
_not_called: set[str] = set()


class ConflictingCallObservationError(RuntimeError):
    """A node id was observed twice with different data (duration or bucket)."""


def _record_duration(nodeid: str, duration: float) -> None:
    if nodeid in _not_called:
        raise ConflictingCallObservationError(
            f"{nodeid!r} was already recorded as not-called (setup-skip), but a "
            f"call report with duration={duration!r} arrived afterward"
        )
    existing = _call_durations.get(nodeid)
    if existing is not None and existing != duration:
        raise ConflictingCallObservationError(
            f"{nodeid!r} reported conflicting call durations: "
            f"{existing!r} vs {duration!r}"
        )
    _call_durations[nodeid] = duration


def _record_not_called(nodeid: str) -> None:
    if nodeid in _call_durations:
        raise ConflictingCallObservationError(
            f"{nodeid!r} was already recorded with a measured call duration, but a "
            "setup-skip report arrived afterward"
        )
    _not_called.add(nodeid)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("call durations")
    group.addoption(
        "--call-durations-out",
        action="store",
        default=None,
        metavar="PATH",
        help=(
            "write call-phase-only test durations (JSON) to PATH. "
            "Requires -p tests._call_duration_plugin."
        ),
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call":
        _record_duration(report.nodeid, report.duration)
    elif report.when == "setup" and report.skipped:
        _record_not_called(report.nodeid)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:  # noqa: ARG001
    """Aggregate xdist worker call durations onto the controller process.

    Mirrors the ``auto_trader_schema_metrics`` forwarding pattern in
    ``tests/conftest.py``, but merges through ``_record_duration``/
    ``_record_not_called`` instead of ``dict.update``/``set.update`` so a
    duplicate or contradictory node id across workers fails closed instead
    of last-write-wins silently overwriting or masking it.
    """
    worker_output = node.workeroutput
    for nodeid, duration in worker_output.get("auto_trader_call_durations", {}).items():
        _record_duration(nodeid, duration)
    for nodeid in worker_output.get("auto_trader_not_called", []):
        _record_not_called(nodeid)


def _current_output() -> dict[str, object]:
    # durations and not_called are disjoint by construction (_record_duration
    # / _record_not_called raise on any contradiction the instant it is
    # observed), so no reconciliation is needed here.
    return {"durations": dict(_call_durations), "not_called": sorted(_not_called)}


def pytest_sessionfinish(session: pytest.Session) -> None:
    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        # Running inside an xdist worker: forward to the controller instead
        # of writing a file. The controller aggregates via
        # pytest_testnodedown and is the only process that writes output.
        worker_output["auto_trader_call_durations"] = dict(_call_durations)
        worker_output["auto_trader_not_called"] = sorted(_not_called)
        return

    out_path = session.config.getoption("--call-durations-out")
    if not out_path:
        return
    Path(out_path).write_text(
        json.dumps(_current_output(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
