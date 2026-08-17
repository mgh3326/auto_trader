"""Pytest startup plugin for the repository-wide ROB-1880 socket guard."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from tests import _socket_guard as socket_guard

_WORKER_SUMMARIES_KEY = pytest.StashKey[list[dict[str, object]]]()
_FINAL_SUMMARY_KEY = pytest.StashKey[dict[str, object]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("socket guard")
    group.addoption(
        "--socket-guard-report",
        action="store",
        default=None,
        metavar="PATH",
        help="write the aggregated ROB-1880 socket guard session evidence to PATH",
    )


def pytest_configure(config: pytest.Config) -> None:
    # ``-p tests._socket_guard_plugin`` is loaded before test collection and
    # module import. It is intentionally not a conftest fixture.
    socket_guard.activate_child_guard_environment()
    socket_guard.install()
    config.stash[_WORKER_SUMMARIES_KEY] = []


def pytest_sessionstart(session: pytest.Session) -> None:
    try:
        socket_guard.assert_installed()
    except socket_guard.SocketGuardInstallationError as error:
        raise pytest.UsageError(str(error)) from error


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """Allow explicit integration/live items while retaining import-time default deny."""

    exempt = bool(
        item.get_closest_marker("integration") or item.get_closest_marker("live")
    )
    previous = socket_guard.set_current_test_exempt(exempt)
    try:
        socket_guard.assert_installed()
        yield
    finally:
        socket_guard.set_current_test_exempt(previous)
        socket_guard.assert_installed()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    local_summary = socket_guard.summary()
    worker_output = getattr(session.config, "workeroutput", None)
    if worker_output is not None:
        worker_output["rob1880_socket_guard"] = local_summary
        return

    aggregate = _aggregate(
        [local_summary, *session.config.stash[_WORKER_SUMMARIES_KEY]]
    )
    session.config.stash[_FINAL_SUMMARY_KEY] = aggregate
    report_path = session.config.getoption("--socket-guard-report")
    if report_path:
        socket_guard.write_report(report_path, aggregate)
    if not aggregate["active"]:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: BaseException | None) -> None:  # noqa: ARG001
    """Collect each xdist worker's evidence before the controller reports it."""

    summary = node.workeroutput.get("rob1880_socket_guard")
    if summary is None:
        gateway = getattr(node, "gateway", None)
        summary = {
            "active": False,
            "worker_id": gateway.id if gateway is not None else "unknown",
            "blocked_attempts": 0,
            "blocked_by_operation": {},
            "error": "worker exited without socket guard evidence",
        }
    node.config.stash[_WORKER_SUMMARIES_KEY].append(summary)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    summary = terminalreporter.config.stash.get(_FINAL_SUMMARY_KEY, None)
    if summary is None:
        return
    terminalreporter.write_line(
        "ROB-1880 socket guard: "
        f"active={summary['active']} "
        f"blocked_attempts={summary['blocked_attempts']} "
        f"workers={summary['worker_count']}"
    )


def _aggregate(participants: list[dict[str, object]]) -> dict[str, object]:
    blocked_by_operation: Counter[str] = Counter()
    blocked_attempts = 0
    for participant in participants:
        blocked_attempts += int(participant.get("blocked_attempts", 0))
        raw_counts = participant.get("blocked_by_operation", {})
        if isinstance(raw_counts, dict):
            blocked_by_operation.update(
                {str(operation): int(count) for operation, count in raw_counts.items()}
            )

    worker_summaries = [
        participant
        for participant in participants
        if participant.get("worker_id") not in {"controller", "master"}
    ]
    return {
        "guard": "rob-1880-socket-guard",
        "active": all(bool(participant.get("active")) for participant in participants),
        "blocked_attempts": blocked_attempts,
        "blocked_by_operation": dict(sorted(blocked_by_operation.items())),
        "worker_count": len(worker_summaries),
        "participants": participants,
    }
