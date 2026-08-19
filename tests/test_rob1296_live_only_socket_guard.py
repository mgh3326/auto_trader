"""ROB-1296 — the socket guard exempts an armed ``live`` item and nothing else.

Before ROB-1296 the guard exempted every ``integration`` item, which covered
~3,200 tests in the default ``-m "not live"`` gate and let any of them reach a
real external host. Integration tests need loopback PostgreSQL/Redis, which the
address allowlist already permits without any marker, so the marker was granting
reach it never needed.

These tests pin the resulting truth table end to end. They never open a socket to
a non-loopback address: the armed-live cell is proven through the guard's
side-effect-free verdict predicate, so the whole matrix runs offline.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _socket_guard as socket_guard
from tests import _socket_guard_plugin as socket_guard_plugin

PROBE_PATH = Path(__file__).parent / "_socket_guard_probes" / "marker_matrix_probe.py"
EXTERNAL_IPV4 = ("203.0.113.1", 443)  # RFC 5737 TEST-NET-3, never routable
EXTERNAL_HOSTNAME = "rob1296-guard-probe.invalid"  # RFC 6761 reserved TLD
LOOPBACK_POSTGRES = ("127.0.0.1", 5432)
LOOPBACK_REDIS = ("127.0.0.1", 6379)


# --------------------------------------------------------------------------
# Policy predicate — the decision the plugin makes for one item
# --------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, run_live: object) -> None:
        self._run_live = run_live

    def getoption(self, name: str, default: object = None) -> object:
        assert name == "--run-live"
        if self._run_live is _MISSING:
            return default
        return self._run_live


_MISSING = object()


class _FakeItem:
    def __init__(self, markers: set[str], config: _FakeConfig) -> None:
        self._markers = markers
        self.config = config

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._markers else None


@pytest.mark.parametrize(
    ("markers", "run_live", "expected"),
    [
        (set(), False, False),
        (set(), True, False),
        ({"integration"}, False, False),
        ({"integration"}, True, False),
        ({"live"}, False, False),
        ({"live"}, True, True),
        ({"integration", "live"}, False, False),
        ({"integration", "live"}, True, True),
        ({"slow", "unit"}, True, False),
    ],
)
def test_exemption_truth_table(
    markers: set[str], run_live: bool, expected: bool
) -> None:
    item = _FakeItem(markers, _FakeConfig(run_live))
    assert socket_guard_plugin.item_is_exempt(item) is expected


def test_live_exemption_is_not_armed_when_the_option_is_unregistered() -> None:
    """``research/**`` subtrees and ``--noconftest`` probes never register it."""

    assert socket_guard_plugin.live_exemption_is_armed(_FakeConfig(_MISSING)) is False


def test_live_exemption_is_not_armed_when_the_option_table_raises() -> None:
    class _RaisingConfig:
        def getoption(self, name: str, default: object = None) -> object:
            raise ValueError(f"no such option: {name}")

    assert socket_guard_plugin.live_exemption_is_armed(_RaisingConfig()) is False


# --------------------------------------------------------------------------
# End-to-end: real pytest sessions over a real marker matrix
# --------------------------------------------------------------------------


# A nested pytest session must not inherit this session's run-owned PostgreSQL
# identity. ``tests/_run_owned_database.py`` derives the database name from
# ``AUTO_TRADER_PYTEST_RUN_UID`` plus ``PYTEST_XDIST_WORKER``, and the owning
# session drops that exact database at teardown. A child that inherited both
# would compute *this worker's* database name and drop it out from under the
# outer run — which reliably reproduced as several hundred
# ``InvalidCatalogNameError`` failures across unrelated tests. Stripping these
# keys makes the child mint its own ``_main`` database and clean it up.
_RUN_OWNED_DATABASE_ENV_KEYS = (
    "AUTO_TRADER_PYTEST_RUN_UID",
    "AUTO_TRADER_PYTEST_OWNER_TOKEN",
    "AUTO_TRADER_XDIST_DATABASE_NAME",
    "AUTO_TRADER_XDIST_BASE_DATABASE_URL",
    "DATABASE_URL",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
)


def _nested_pytest_environment(**overrides: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _RUN_OWNED_DATABASE_ENV_KEYS
    }
    environment.update(overrides)
    return environment


def _run_probe_matrix(
    tmp_path: Path, *extra_args: str
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
    record_path = tmp_path / "records.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PROBE_PATH),
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        cwd=str(socket_guard.PROJECT_ROOT),
        env=_nested_pytest_environment(ROB1296_PROBE_RECORD=str(record_path)),
    )
    records = [
        json.loads(line)
        for line in record_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return result, records


def test_default_session_exempts_nothing_and_skips_live(tmp_path: Path) -> None:
    result, records = _run_probe_matrix(tmp_path)
    by_case = {record["case"]: record for record in records}

    assert result.returncode == 0, result.stdout + result.stderr

    # Live items never execute at all without --run-live, so they cannot reach
    # a socket regardless of the guard's verdict.
    assert set(by_case) == {"unmarked", "integration_only"}
    assert "2 skipped" in result.stdout

    for case in ("unmarked", "integration_only"):
        assert by_case[case]["exempt"] is False, case
        assert by_case[case]["external_permitted"] is False, case
        assert by_case[case]["loopback_permitted"] is True, case


def test_run_live_arms_only_the_live_items(tmp_path: Path) -> None:
    result, records = _run_probe_matrix(tmp_path, "--run-live")
    by_case = {record["case"]: record for record in records}

    assert result.returncode == 0, result.stdout + result.stderr
    assert set(by_case) == {
        "unmarked",
        "integration_only",
        "live_only",
        "integration_and_live",
    }

    for case in ("unmarked", "integration_only"):
        assert by_case[case]["exempt"] is False, case
        assert by_case[case]["external_permitted"] is False, case

    for case in ("live_only", "integration_and_live"):
        assert by_case[case]["exempt"] is True, case
        assert by_case[case]["external_permitted"] is True, case

    # Loopback stays reachable in every cell — the exemption never had to be the
    # mechanism that let integration tests talk to PostgreSQL/Redis.
    for record in records:
        assert record["loopback_permitted"] is True, record["case"]


def test_a_subtree_without_the_option_registered_stays_blocked(tmp_path: Path) -> None:
    """The real ``research/**`` shape: guard active, ``--run-live`` never registered.

    ``pyproject.toml`` adds ``-p tests._socket_guard_plugin`` to *every* pytest
    invocation, but only ``tests/conftest.py`` registers ``--run-live``. The
    ``research/**`` subtrees have their own conftests and are collected by path,
    so the option is absent there. If the lookup raised instead of degrading,
    every research CI job would die on the first ``live``-marked item.
    """

    config_path = tmp_path / "pytest.ini"
    config_path.write_text(
        "[pytest]\nmarkers =\n    live: live API tests\n", encoding="utf-8"
    )
    probe_path = tmp_path / "test_unregistered_option_probe.py"
    probe_path.write_text(
        "import pytest\n"
        "from tests._socket_guard import (\n"
        "    is_current_test_exempt,\n"
        "    is_socket_address_permitted,\n"
        ")\n"
        "\n"
        "@pytest.mark.live\n"
        "def test_live_without_a_registered_option():\n"
        "    assert is_current_test_exempt() is False\n"
        "    assert is_socket_address_permitted(('203.0.113.1', 443)) is False\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--noconftest",
            "-c",
            str(config_path),
            "-p",
            "tests._socket_guard_plugin",
            str(probe_path),
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=str(socket_guard.PROJECT_ROOT),
        env=_nested_pytest_environment(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_xdist_workers_apply_the_same_policy_and_report_evidence(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "guard-report.json"
    result, records = _run_probe_matrix(
        tmp_path,
        "-n",
        "2",
        "--dist=loadfile",
        "--socket-guard-report",
        str(report_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["active"] is True
    assert report["worker_count"] == 2
    assert report["blocked_attempts"] == 0

    by_case = {record["case"]: record for record in records}
    assert set(by_case) == {"unmarked", "integration_only"}
    for record in by_case.values():
        assert record["exempt"] is False
        assert record["external_permitted"] is False


# --------------------------------------------------------------------------
# Live-marker semantics without the option
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.live
def test_an_armed_live_item_holds_the_only_exemption() -> None:
    """Skipped in the default gate; asserts the exemption under ``--run-live``.

    Reaching this body at all proves ``--run-live`` was supplied, because
    ``tests/conftest.py`` skips every ``live`` item otherwise. The nested probe
    matrix above covers the skipped half of the option axis.
    """

    assert socket_guard.is_current_test_exempt() is True
    assert socket_guard.is_socket_address_permitted(EXTERNAL_IPV4) is True


# --------------------------------------------------------------------------
# In-session behaviour of the current (non-exempt) item
# --------------------------------------------------------------------------


def test_this_ordinary_item_is_not_exempt() -> None:
    assert socket_guard.is_current_test_exempt() is False


@pytest.mark.integration
def test_an_integration_item_is_no_longer_exempt() -> None:
    """The ROB-1296 inversion of the pre-existing ROB-1880 expectation."""

    assert socket_guard.is_current_test_exempt() is False
    assert socket_guard.is_socket_address_permitted(EXTERNAL_IPV4) is False


@pytest.mark.integration
def test_an_integration_item_still_reaches_loopback_services() -> None:
    for address in (LOOPBACK_POSTGRES, LOOPBACK_REDIS):
        assert socket_guard.is_socket_address_permitted(address) is True

    # Prove it end to end against the local services the suite actually uses.
    for address in (LOOPBACK_POSTGRES, LOOPBACK_REDIS):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(address)


@pytest.mark.integration
def test_an_integration_item_is_blocked_from_an_external_host() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(socket_guard.ExternalSocketBlocked):
            sock.connect(EXTERNAL_IPV4)


@pytest.mark.integration
def test_an_integration_item_is_blocked_from_a_named_host() -> None:
    """A hostname target is denied at connect, after resolution fails or not."""

    with pytest.raises((socket_guard.ExternalSocketBlocked, socket.gaierror)):
        socket.create_connection((EXTERNAL_HOSTNAME, 443), timeout=1)


@pytest.mark.integration
def test_an_integration_item_is_blocked_from_launching_a_network_client() -> None:
    with pytest.raises(socket_guard.ExternalSubprocessBlocked, match="network-client"):
        subprocess.run(["curl", "https://203.0.113.1"], check=False)


# --------------------------------------------------------------------------
# State isolation and restoration
# --------------------------------------------------------------------------


def test_exempt_state_is_restored_after_a_nested_protocol_frame() -> None:
    assert socket_guard.is_current_test_exempt() is False
    previous = socket_guard.set_current_test_exempt(True)
    try:
        assert socket_guard.is_current_test_exempt() is True
    finally:
        socket_guard.set_current_test_exempt(previous)
    assert socket_guard.is_current_test_exempt() is False


def test_a_preceding_integration_item_does_not_leak_its_state() -> None:
    """Ordering-independent: the plugin restores the flag in a ``finally``."""

    assert socket_guard.is_current_test_exempt() is False
    assert socket_guard.is_socket_address_permitted(EXTERNAL_IPV4) is False


# --------------------------------------------------------------------------
# Child-process propagation beyond subprocess.Popen
# --------------------------------------------------------------------------


def _child_guard_verdict(queue: object) -> None:  # pragma: no cover - child body
    from tests import _socket_guard as child_guard

    queue.put(  # type: ignore[attr-defined]
        {
            "installed": child_guard.is_installed(),
            "exempt": child_guard.is_current_test_exempt(),
            "external_permitted": child_guard.is_socket_address_permitted(
                ("203.0.113.1", 443)
            ),
        }
    )


@pytest.mark.parametrize("start_method", ["spawn", "fork"])
def test_multiprocessing_children_inherit_the_guard(start_method: str) -> None:
    """``spawn`` and ``fork`` children carry the policy, by two routes.

    ``fork`` inherits the already-patched ``socket`` module in memory; ``spawn``
    re-execs and picks the guard back up from the ``sitecustomize`` hook that
    ``activate_child_guard_environment`` planted in ``os.environ``. Neither path
    goes through ``subprocess.Popen``, so this is a genuinely separate
    propagation route from the direct-child tests.
    """

    if start_method not in multiprocessing.get_all_start_methods():
        pytest.skip(f"{start_method} start method unavailable on this platform")

    context = multiprocessing.get_context(start_method)
    queue = context.Queue()
    process = context.Process(target=_child_guard_verdict, args=(queue,))
    process.start()
    try:
        verdict = queue.get(timeout=60)
    finally:
        process.join(timeout=60)

    assert verdict["installed"] is True, start_method
    assert verdict["exempt"] is False, start_method
    assert verdict["external_permitted"] is False, start_method


def test_forkserver_start_method_is_fail_closed_not_silently_allowed() -> None:
    """Known, deliberate limitation — documented rather than allowlisted away.

    ``forkserver`` brokers every child through an ``AF_UNIX`` socket under a
    per-process temp directory. Admitting it would mean allowlisting a
    *directory prefix*, and "any AF_UNIX path is local enough" is exactly the
    bypass ROB-1880 closed. Nothing in this repository uses ``forkserver``: all
    three ``get_context`` call sites ask for ``spawn`` explicitly. So the guard
    blocks it, and this test pins that as intended behaviour so a future reader
    finds a decision instead of a mystery.
    """

    if "forkserver" not in multiprocessing.get_all_start_methods():
        pytest.skip("forkserver start method unavailable on this platform")

    context = multiprocessing.get_context("forkserver")
    queue = context.Queue()
    process = context.Process(target=_child_guard_verdict, args=(queue,))

    with pytest.raises(socket_guard.ExternalSocketBlocked):
        process.start()


def test_a_direct_python_child_of_a_non_exempt_item_is_guarded() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests._socket_guard import is_installed, is_socket_address_permitted;"
            " print(is_installed(), is_socket_address_permitted(('203.0.113.1', 443)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "True False"
