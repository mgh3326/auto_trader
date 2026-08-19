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
emits a real ``call`` report with a real ``report.duration`` for the time
spent before the skip, so it is captured in ``durations`` as usual.
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
        _call_durations[report.nodeid] = report.duration
    elif report.when == "setup" and report.skipped:
        _not_called.add(report.nodeid)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:  # noqa: ARG001
    """Aggregate xdist worker call durations onto the controller process.

    Mirrors the ``auto_trader_schema_metrics`` forwarding pattern in
    ``tests/conftest.py``.
    """
    worker_output = node.workeroutput
    _call_durations.update(worker_output.get("auto_trader_call_durations", {}))
    _not_called.update(worker_output.get("auto_trader_not_called", []))


def _current_output() -> dict[str, object]:
    # A node id can only end up in both sets if a plugin/rerun oddity
    # produced a real call report for something we also saw skip at setup
    # (should not happen under normal pytest semantics — see module
    # docstring). Resolve deterministically in favor of the measured call
    # report: its existence proves the test body genuinely ran, which is
    # strictly stronger evidence than the earlier setup-skip observation.
    not_called = sorted(_not_called - set(_call_durations))
    return {"durations": dict(_call_durations), "not_called": not_called}


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
