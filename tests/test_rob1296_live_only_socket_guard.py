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
def test_an_integration_item_is_blocked_from_a_named_host(monkeypatch) -> None:
    """A hostname target is denied at connect -- proven without any DNS traffic.

    Resolution is stubbed to a fixed TEST-NET-3 sockaddr. Letting the real
    resolver run would both emit a DNS query and make the test pass for the wrong
    reason: an NXDOMAIN raises ``gaierror`` before the guarded ``connect`` is ever
    reached, so the guard would go untested.
    """

    resolved: list[tuple[object, ...]] = []

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        resolved.append((host, port))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (EXTERNAL_IPV4[0], 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    with pytest.raises(socket_guard.ExternalSocketBlocked):
        socket.create_connection((EXTERNAL_HOSTNAME, 443), timeout=1)

    assert resolved == [(EXTERNAL_HOSTNAME, 443)]


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


def test_forkserver_start_method_is_refused_outright() -> None:
    """Deliberately unsupported, and refused before anything launches.

    ``forkserver`` boots a long-lived server interpreter through a path this
    guard does not control, and that server then forks every child -- so no
    child policy can be guaranteed. An earlier version let the attempt proceed
    and relied on the ``AF_UNIX`` broker socket being blocked, but "the socket
    was refused" is not evidence that a child would have had the right policy.
    ROB-1296 never required ``forkserver``; nothing here selects it (all three
    ``get_context`` call sites ask for ``spawn``), so it fails closed at the
    boundary with an explanation.
    """

    if "forkserver" not in multiprocessing.get_all_start_methods():
        pytest.skip("forkserver start method unavailable on this platform")

    context = multiprocessing.get_context("forkserver")
    queue = context.Queue()
    process = context.Process(target=_child_guard_verdict, args=(queue,))

    with pytest.raises(socket_guard.SocketGuardInstallationError, match="forkserver"):
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


# --------------------------------------------------------------------------
# Child-launch classification and escape hatches
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["uv", "run", "python", "-c", "print(1)"], True),
        (["/opt/homebrew/bin/uv", "run", "python", "-c", "print(1)"], True),
        (["/usr/bin/env", "python3", "-c", "print(1)"], True),
        (["env", "FOO=1", "python", "-c", "print(1)"], True),
        (["poetry", "run", "python", "-c", "print(1)"], True),
        # Not Python children: rewriting their environment would break callers
        # that depend on exact child-env semantics for a non-Python tool.
        (["uv", "build", "--wheel"], False),
        (["env", "printenv"], False),
        (["env"], False),
        (["uv", "sync"], False),
        (["git", "status"], False),
        ([], False),
    ],
)
def test_python_launcher_wrapper_classifier(command: list[str], expected: bool) -> None:
    assert socket_guard._is_wrapped_python_launcher(tuple(command)) is expected


def test_env_scrubbed_python_is_rejected_before_it_starts() -> None:
    """``env -i python`` discards the startup hook, so it is refused outright.

    Rewriting the shell's environment cannot help here: ``env -i`` wipes whatever
    the parent injected, so the interpreter would come up unguarded. The command
    is blocked instead.
    """

    with pytest.raises(
        socket_guard.ExternalSubprocessBlocked, match="env-scrubbed-python"
    ):
        subprocess.run(["sh", "-c", f"env -i {sys.executable} -c 'pass'"], check=False)

    with pytest.raises(
        socket_guard.ExternalSubprocessBlocked, match="env-scrubbed-python"
    ):
        subprocess.run(["env", "--ignore-environment", sys.executable, "-c", "pass"])


def test_env_without_scrubbing_is_not_rejected() -> None:
    """The deny rule must key on the scrubbing flag, not on ``env`` itself."""

    result = subprocess.run(
        ["env", sys.executable, "-c", "print('env-ok')"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "env-ok"


def test_a_non_python_wrapper_child_keeps_its_exact_environment() -> None:
    """``uv build``-shaped commands must not have their environment rewritten."""

    result = subprocess.run(
        ["env"],
        capture_output=True,
        text=True,
        env={"ROB1296_ONLY": "1"},
        check=True,
    )
    assert result.stdout.strip() == "ROB1296_ONLY=1"


# --------------------------------------------------------------------------
# Child policy is identical across every creation route
# --------------------------------------------------------------------------

CHILD_POLICY_PROBE = (
    Path(__file__).parent / "_socket_guard_probes" / "child_policy_probe.py"
)


def _run_child_policy_probe(tmp_path: Path, *extra_args: str) -> dict[str, dict]:
    record = tmp_path / "childpol"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(CHILD_POLICY_PROBE),
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            *extra_args,
        ],
        capture_output=True,
        text=True,
        cwd=str(socket_guard.PROJECT_ROOT),
        env=_nested_pytest_environment(ROB1296_CHILD_POLICY_RECORD=str(record)),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    written = {}
    for path in tmp_path.glob("childpol.*.json"):
        written[path.name.split(".")[1]] = json.loads(path.read_text(encoding="utf-8"))
    return written


def _assert_routes_match_parent(routes: dict[str, dict], label: str) -> None:
    parent = routes["parent"]
    assert "uv-wrapper" in " ".join(routes), f"{label}: uv wrapper route not exercised"
    for name, verdict in routes.items():
        if verdict.get("blocked_by_guard"):
            # ``forkserver`` is refused outright, under either parent policy: its
            # server interpreter boots outside the guard's control. Refusing is
            # stricter than the parent, never looser, so it does not break the
            # same-policy contract.
            assert "forkserver" in name, f"{label}:{name} unexpectedly refused"
            continue
        assert verdict == parent, f"{label}:{name} disagrees with the parent"


def test_every_child_route_matches_a_non_exempt_parent(tmp_path: Path) -> None:
    routes = _run_child_policy_probe(tmp_path)
    _assert_routes_match_parent(routes["nonlive"], "nonlive")
    assert routes["nonlive"]["parent"]["exempt"] is False


def test_every_child_route_matches_an_armed_live_parent(tmp_path: Path) -> None:
    routes = _run_child_policy_probe(tmp_path, "--run-live")
    _assert_routes_match_parent(routes["live"], "live")
    assert routes["live"]["parent"]["exempt"] is True
    assert routes["live"]["multiprocessing:spawn"]["exempt"] is True
    assert routes["live"]["subprocess:uv-wrapper"]["exempt"] is True


def test_env_tampering_cannot_exempt_any_child_route(tmp_path: Path) -> None:
    """The general-bypass negative control, end to end."""

    routes = _run_child_policy_probe(tmp_path)
    tampered = routes["tampered"]
    assert tampered["parent"]["exempt"] is False
    for name, verdict in tampered.items():
        if verdict.get("blocked_by_guard"):
            continue
        assert verdict["exempt"] is False, f"{name} became exempt via the environment"
        assert verdict["external_permitted"] is False, name


def test_guard_env_tampering_cannot_unguard_any_child_route(tmp_path: Path) -> None:
    """Disabling the guard switch, or stripping the startup path, must not stick."""

    routes = _run_child_policy_probe(tmp_path)
    for label in ("guard_disabled", "guard_removed"):
        for name, verdict in routes[label].items():
            if verdict.get("blocked_by_guard"):
                continue
            assert verdict["installed"] is True, f"{label}:{name} came up unguarded"
            assert verdict["exempt"] is False, f"{label}:{name}"
            assert verdict["external_permitted"] is False, f"{label}:{name}"


# --------------------------------------------------------------------------
# Installation integrity of the multiprocessing policy channel
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_path", "owner_attr", "hook"),
    [
        ("multiprocessing.spawn", None, "get_preparation_data"),
        ("multiprocessing.spawn", None, "prepare"),
        ("multiprocessing.process", "BaseProcess", "start"),
    ],
)
def test_partially_restored_child_policy_channel_is_a_hard_failure(
    monkeypatch, module_path: str, owner_attr: str | None, hook: str
) -> None:
    """Restoring any one hook must fail the session, not quietly stop propagating.

    Each of these carries part of the child's policy. A partial restore would
    leave the aggregate evidence reporting ``active=True`` while re-execed
    children lost either their exemption or the guard itself.
    """

    import importlib

    module = importlib.import_module(module_path)
    owner = module if owner_attr is None else getattr(module, owner_attr)
    installed = getattr(owner, hook)
    original = installed._rob1296_original

    monkeypatch.setattr(owner, hook, original)

    with pytest.raises(socket_guard.SocketGuardInstallationError, match=hook):
        socket_guard.assert_installed()


def test_summary_reports_inactive_when_the_child_policy_channel_is_broken(
    monkeypatch,
) -> None:
    """The session evidence must fold this channel into ``active``."""

    import multiprocessing.spawn as mp_spawn

    assert socket_guard.summary()["active"] is True

    monkeypatch.setattr(mp_spawn, "prepare", mp_spawn.prepare._rob1296_original)

    assert socket_guard.summary()["active"] is False


# --------------------------------------------------------------------------
# Selective environment stripping in front of an interpreter
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Full scrub.
        (["env", "-i", "python", "-c", "pass"], True),
        (["env", "--ignore-environment", "python", "-c", "pass"], True),
        # Scalpel: unset exactly the keys the startup hook rides on.
        (["env", "-u", "PYTHONPATH", "python", "-c", "pass"], True),
        (["env", "--unset=PYTHONPATH", "python", "-c", "pass"], True),
        (["env", "-u", "AUTO_TRADER_TEST_SOCKET_GUARD", "python", "-c", "pass"], True),
        (["unset", "PYTHONPATH;", "python", "-c", "pass"], True),
        # Override rather than unset.
        (["env", "PYTHONPATH=", "python", "-c", "pass"], True),
        (["env", "AUTO_TRADER_TEST_SOCKET_GUARD=0", "python3.13", "-c", "pass"], True),
        # Unrelated variables leave the hook intact, so these stay allowed.
        (["env", "-u", "EDITOR", "python", "-c", "pass"], False),
        (["env", "FOO=1", "python", "-c", "pass"], False),
        (["env", "python", "-c", "pass"], False),
        # No interpreter follows, so there is nothing to unguard.
        (["env", "-i", "printenv"], False),
        (["env", "-u", "PYTHONPATH", "printenv"], False),
        # A guard key mentioned *inside* the script is not a bypass.
        (["python", "-c", "import os; os.environ['PYTHONPATH'] = ''"], False),
    ],
)
def test_guard_environment_stripping_classifier(
    command: list[str], expected: bool
) -> None:
    assert (
        socket_guard._strips_guard_environment_before_python(tuple(command)) is expected
    )


@pytest.mark.parametrize(
    "name",
    ["python", "python3", "python3.13", "py", "pythonw", "/usr/bin/python3"],
)
def test_interpreter_names_are_recognised(name: str) -> None:
    assert socket_guard._is_python_executable(name) is True


@pytest.mark.parametrize("name", ["PYTHONPATH", "pythonpath", "uv", "pythonic-tool"])
def test_non_interpreter_names_are_not_mistaken_for_python(name: str) -> None:
    """``startswith("python")`` used to match ``PYTHONPATH`` itself.

    That made ``env -u PYTHONPATH python …`` look like it had already reached the
    interpreter, so its environment-stripping prefix was never examined.
    """

    assert socket_guard._is_python_executable(name) is False


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "-u", "PYTHONPATH", sys.executable, "-c", "pass"],
        ["env", "--unset=AUTO_TRADER_TEST_SOCKET_GUARD", sys.executable, "-c", "pass"],
        ["env", "PYTHONPATH=", sys.executable, "-c", "pass"],
    ],
)
def test_selective_env_stripping_is_rejected_before_it_starts(
    argv: list[str],
) -> None:
    with pytest.raises(
        socket_guard.ExternalSubprocessBlocked, match="env-scrubbed-python"
    ):
        subprocess.run(argv, check=False)


def test_unrelated_env_manipulation_still_runs_and_stays_guarded() -> None:
    """The deny rule must be about the guard's keys, not about ``env`` at all."""

    result = subprocess.run(
        [
            "env",
            "-u",
            "EDITOR",
            sys.executable,
            "-c",
            "from tests._socket_guard import is_installed; print(is_installed())",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True"


# --------------------------------------------------------------------------
# Shell option bundles and executable overrides
# --------------------------------------------------------------------------

_CHILD_VERDICT_SOURCE = (
    "from tests._socket_guard import is_installed, is_socket_address_permitted;"
    " print(is_installed(), is_socket_address_permitted(('203.0.113.1', 443)))"
)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["sh", "-c", "python -c pass"], True),
        (["bash", "-lc", "/usr/bin/python3 -c pass"], True),
        (["sh", "-ec", "python -c pass"], True),
        (["bash", "--login", "-c", "python -c pass"], True),
        (["sh", "-c", "echo hello"], False),
        (["sh", "-lc", "printenv"], False),
        # ``-c`` here is git's config flag, not a shell command string.
        (["git", "-c", "user.name=x", "commit"], False),
    ],
)
def test_shell_wrapped_python_classifier(command: list[str], expected: bool) -> None:
    """``bash -lc`` bundles ``c`` with other options; a literal ``"-c" in parts``
    check missed it and the child started unguarded."""

    assert socket_guard._is_shell_wrapped_python(tuple(command)) is expected


@pytest.mark.parametrize(
    ("positional", "kwargs", "expected"),
    [
        ((), {"executable": "/usr/bin/python3"}, "/usr/bin/python3"),
        ((-1, "/usr/bin/python3"), {}, "/usr/bin/python3"),
        ((), {}, None),
        ((), {"executable": None}, None),
    ],
)
def test_popen_executable_override_is_resolved(
    positional: tuple, kwargs: dict, expected: str | None
) -> None:
    """``executable=`` decides what actually runs, whatever argv[0] claims."""

    assert socket_guard._resolve_popen_executable(positional, kwargs) == expected


@pytest.mark.parametrize(
    ("label", "argv", "popen_kwargs"),
    [
        ("sh -c", ["sh", "-c", None], {}),
        ("bash -lc", ["bash", "-lc", None], {}),
        ("sh -ec", ["sh", "-ec", None], {}),
        ("executable-override", ["ignored", "-c", _CHILD_VERDICT_SOURCE], "executable"),
    ],
)
def test_every_child_launch_shape_comes_up_guarded(
    label: str, argv: list, popen_kwargs: object
) -> None:
    """End to end, with a bare ``env={}``: the child must install the guard.

    Asserts the guard's verdict, never a connection, so no traffic is produced.
    """

    if popen_kwargs == "executable":
        command = argv
        kwargs = {"executable": sys.executable}
    else:
        command = [*argv[:2], f"{sys.executable} -c {_CHILD_VERDICT_SOURCE!r}"]
        kwargs = {}

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={},
        cwd=str(socket_guard.PROJECT_ROOT),
        **kwargs,
    )

    assert result.returncode == 0, f"{label}: {result.stderr}"
    assert result.stdout.strip() == "True False", f"{label}: {result.stdout!r}"


# --------------------------------------------------------------------------
# The policy channel itself
# --------------------------------------------------------------------------


def test_absent_policy_channel_means_no_exemption(monkeypatch) -> None:
    """Default deny: a child that was granted nothing gets nothing."""

    monkeypatch.delenv(socket_guard.ENV_POLICY_FD, raising=False)
    assert socket_guard.read_policy_channel() is False


@pytest.mark.parametrize("payload", [b"0", b"1"])
def test_policy_channel_round_trips_the_parent_decision(
    monkeypatch, payload: bytes
) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    monkeypatch.setenv(socket_guard.ENV_POLICY_FD, str(read_fd))

    assert socket_guard.read_policy_channel() is (payload == b"1")
    # Consumed exactly once: the descriptor is closed and the variable removed,
    # so a later child cannot replay this grant.
    assert socket_guard.ENV_POLICY_FD not in os.environ


def test_a_dead_policy_descriptor_is_a_hard_failure(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    monkeypatch.setenv(socket_guard.ENV_POLICY_FD, str(read_fd))

    with pytest.raises(socket_guard.SocketGuardInstallationError, match="could not be"):
        socket_guard.read_policy_channel()


def test_a_non_numeric_policy_descriptor_is_a_hard_failure(monkeypatch) -> None:
    monkeypatch.setenv(socket_guard.ENV_POLICY_FD, "not-a-descriptor")

    with pytest.raises(socket_guard.SocketGuardInstallationError, match="malformed"):
        socket_guard.read_policy_channel()


def test_a_malformed_policy_payload_is_a_hard_failure(monkeypatch) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"yes please")
    os.close(write_fd)
    monkeypatch.setenv(socket_guard.ENV_POLICY_FD, str(read_fd))

    with pytest.raises(socket_guard.SocketGuardInstallationError, match="payload"):
        socket_guard.read_policy_channel()


def test_integrity_denials_are_not_exemptible() -> None:
    """Integrity is not policy: an armed ``live`` item cannot launch an
    ungovernable interpreter either.

    The exemption is scoped to one test item; a child that never installed the
    guard outlives it.
    """

    previous = socket_guard.set_current_test_exempt(True)
    try:
        for flag in ("-S", "-I", "-E"):
            with pytest.raises(
                socket_guard.ExternalSubprocessBlocked, match="python-startup-bypass"
            ):
                socket_guard._assert_child_is_governable(
                    [sys.executable, flag, "-c", "pass"]
                )
        with pytest.raises(
            socket_guard.ExternalSubprocessBlocked, match="env-scrubbed-python"
        ):
            socket_guard._assert_child_is_governable(
                ["env", "-i", sys.executable, "-c", "pass"]
            )
    finally:
        socket_guard.set_current_test_exempt(previous)


def test_network_client_denial_remains_a_policy_rule() -> None:
    """Reaching ``curl`` directly is policy, so an armed live item may do it."""

    with pytest.raises(socket_guard.ExternalSubprocessBlocked, match="network-client"):
        socket_guard._assert_network_client_allowed(["curl", "https://203.0.113.1"])

    # ...and the integrity half says nothing about it.
    socket_guard._assert_child_is_governable(["curl", "https://203.0.113.1"])


# --------------------------------------------------------------------------
# DNS is blocked, not just connections
# --------------------------------------------------------------------------


def test_external_name_resolution_is_blocked() -> None:
    """Resolution happens *before* connect, so it needs its own refusal.

    Without this the guard would still refuse the connection, but the lookup
    would already have left the machine.
    """

    with pytest.raises(socket_guard.ExternalSocketBlocked, match="DNS resolution"):
        socket.getaddrinfo(EXTERNAL_HOSTNAME, 443)

    with pytest.raises(socket_guard.ExternalSocketBlocked, match="DNS resolution"):
        socket.gethostbyname(EXTERNAL_HOSTNAME)


def test_external_numeric_resolution_is_blocked() -> None:
    with pytest.raises(socket_guard.ExternalSocketBlocked, match="DNS resolution"):
        socket.getaddrinfo(EXTERNAL_IPV4[0], 443)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_resolution_is_permitted(host: str) -> None:
    """Local PostgreSQL/Redis resolve by name and must keep working."""

    assert socket.getaddrinfo(host, 5432)


@pytest.mark.parametrize("host", [None, "", b""])
def test_passive_resolution_is_permitted(host: object) -> None:
    """``host=None`` is a passive lookup for a local bind; it never leaves."""

    assert socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)


def test_blocked_resolution_is_counted_under_its_own_operation() -> None:
    before = socket_guard.summary()["blocked_by_operation"].get("getaddrinfo", 0)
    with pytest.raises(socket_guard.ExternalSocketBlocked):
        socket.getaddrinfo("rob1296-dns-counter.invalid", 443)
    after = socket_guard.summary()["blocked_by_operation"].get("getaddrinfo", 0)
    assert after == before + 1


def test_resolution_is_permitted_for_an_armed_live_item() -> None:
    """The exemption reaches resolution too, or an armed live test cannot connect."""

    previous = socket_guard.set_current_test_exempt(True)
    try:
        # Predicate only -- asserting the guard would allow it, without resolving.
        socket_guard._assert_resolution_allowed(EXTERNAL_HOSTNAME)
    finally:
        socket_guard.set_current_test_exempt(previous)
