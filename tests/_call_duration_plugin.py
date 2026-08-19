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
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Module-level global rather than a config stash: this needs to survive a
# hook signature (``pytest_runtest_logreport``) that pytest does not pass
# ``config`` into. Safe because each pytest process (controller or xdist
# worker) gets its own fresh import of this module.
_call_durations: dict[str, float] = {}


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


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:  # noqa: ARG001
    """Aggregate xdist worker call durations onto the controller process.

    Mirrors the ``auto_trader_schema_metrics`` forwarding pattern in
    ``tests/conftest.py``.
    """
    worker_durations = node.workeroutput.get("auto_trader_call_durations", {})
    _call_durations.update(worker_durations)


def pytest_sessionfinish(session: pytest.Session) -> None:
    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        # Running inside an xdist worker: forward to the controller instead
        # of writing a file. The controller aggregates via
        # pytest_testnodedown and is the only process that writes output.
        worker_output["auto_trader_call_durations"] = dict(_call_durations)
        return

    out_path = session.config.getoption("--call-durations-out")
    if not out_path:
        return
    Path(out_path).write_text(
        json.dumps(_call_durations, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
