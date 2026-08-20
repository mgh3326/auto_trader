"""Reports the guard verdict of every child-creation route, for one pytest item.

Driven by tests/test_rob1296_live_only_socket_guard.py through nested pytest
sessions, once without ``--run-live`` and once with it, so the outer test can
assert that a child's policy matches its parent's regardless of *how* the child
was created. Deliberately not named ``test_*.py`` so the outer suite never
collects it.

Nothing here connects anywhere: children report the guard's own verdict for a
reserved TEST-NET-3 address through the side-effect-free predicate.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _socket_guard as socket_guard

EXTERNAL = ("203.0.113.1", 443)
RECORD_PATH_ENV = "ROB1296_CHILD_POLICY_RECORD"

_CHILD_SOURCE = (
    "import json;"
    "from tests._socket_guard import is_installed, is_current_test_exempt,"
    " is_socket_address_permitted;"
    "print(json.dumps({"
    "'installed': is_installed(),"
    "'exempt': is_current_test_exempt(),"
    "'external_permitted': is_socket_address_permitted(('203.0.113.1', 443)),"
    "}))"
)


def _child_verdict(queue) -> None:  # pragma: no cover - runs in the child
    from tests import _socket_guard as guard

    queue.put(
        {
            "installed": guard.is_installed(),
            "exempt": guard.is_current_test_exempt(),
            "external_permitted": guard.is_socket_address_permitted(
                ("203.0.113.1", 443)
            ),
        }
    )


def _multiprocessing_verdict(start_method: str) -> dict[str, object]:
    context = multiprocessing.get_context(start_method)
    queue = context.Queue()
    process = context.Process(target=_child_verdict, args=(queue,))
    try:
        process.start()
    except (
        socket_guard.ExternalSocketBlocked,
        socket_guard.SocketGuardInstallationError,
    ):
        # forkserver is refused outright: its server interpreter boots outside
        # the guard's control, so no child policy can be guaranteed.
        return {"blocked_by_guard": True}
    try:
        return queue.get(timeout=60)
    finally:
        process.join(timeout=60)


def _subprocess_verdict(
    argv: list[str], env: dict[str, str] | None = None
) -> dict[str, object]:
    # A deliberately bare environment: whatever the child ends up with must have
    # been injected by the guard, not inherited. The wrapper case needs ``PATH``
    # only so the launcher itself can be found.
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=str(socket_guard.PROJECT_ROOT),
        env={} if env is None else env,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _record(suffix: str, payload: dict[str, object]) -> None:
    # Driven by an outer test that supplies the destination. Run by hand without
    # it, skip rather than raising KeyError -- a bare KeyError here reads like
    # the guard collapsed, which it has not.
    if RECORD_PATH_ENV not in os.environ:
        pytest.skip(f"{RECORD_PATH_ENV} unset; this probe is driven by an outer test")
    base = Path(os.environ[RECORD_PATH_ENV])
    base.with_suffix(f".{suffix}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _collect_routes() -> dict[str, object]:
    parent = {
        "installed": socket_guard.is_installed(),
        "exempt": socket_guard.is_current_test_exempt(),
        "external_permitted": socket_guard.is_socket_address_permitted(EXTERNAL),
    }

    routes: dict[str, object] = {"parent": parent}
    for start_method in ("spawn", "fork", "forkserver"):
        if start_method in multiprocessing.get_all_start_methods():
            routes[f"multiprocessing:{start_method}"] = _multiprocessing_verdict(
                start_method
            )

    routes["subprocess:direct-python"] = _subprocess_verdict(
        [sys.executable, "-c", _CHILD_SOURCE]
    )
    uv_executable = shutil.which("uv")
    if uv_executable is not None:
        routes["subprocess:uv-wrapper"] = _subprocess_verdict(
            [uv_executable, "run", "python", "-c", _CHILD_SOURCE],
            {"PATH": os.environ.get("PATH", "")},
        )
    return routes


def test_env_tampering_cannot_exempt_a_child(monkeypatch) -> None:
    """Negative control: an ambient env value must not buy an exemption.

    A non-live item sets every guard env key it can see and then creates children
    by each route. All of them must still come back blocked -- the exemption is
    parent in-memory state carried out of band, not something the environment can
    assert.
    """

    # The legacy boolean is no longer consulted anywhere; setting it must be
    # inert rather than persuasive.
    monkeypatch.setenv("AUTO_TRADER_TEST_SOCKET_GUARD_EXEMPT", "1")
    monkeypatch.setenv("AUTO_TRADER_TEST_SOCKET_GUARD", "1")
    _record("tampered", _collect_routes())


def test_a_bogus_policy_descriptor_is_a_hard_failure() -> None:
    """A present-but-unusable channel must fail loudly, not read as "not exempt".

    Silently downgrading would let a genuinely broken channel look like an
    ordinary non-exempt child, hiding the breakage.
    """

    # ``subprocess`` cannot reach this state -- the interceptor rewrites the
    # child environment and mints a fresh descriptor every time -- so the child
    # is launched with ``os.posix_spawn``, which bypasses ``Popen`` entirely.
    # That is also the route an unguarded child would take, so it doubles as the
    # control for "no ``Popen``, still fail-closed".
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)  # now certainly a dead descriptor number

    pid = os.posix_spawn(
        sys.executable,
        [sys.executable, "-c", _CHILD_SOURCE],
        {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            socket_guard.ENV_ENABLED: "1",
            socket_guard.ENV_POLICY_FD: str(read_fd),
        },
    )
    _, status = os.waitpid(pid, 0)
    _record("bogus_fd", {"exit_status": status, "failed": status != 0})


def test_guard_env_tampering_cannot_unguard_a_child(monkeypatch) -> None:
    """Negative control: disabling the guard env must not reach a child.

    ``multiprocessing`` never goes through ``subprocess.Popen``, so nothing else
    repairs the environment before the child interpreter boots. Clearing the
    switch, or removing the startup directory from ``PYTHONPATH``, must still
    yield a fully guarded child.
    """

    monkeypatch.setenv(socket_guard.ENV_ENABLED, "0")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            entry
            for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
            if entry and entry != str(socket_guard.STARTUP_HOOK_DIRECTORY)
        ),
    )
    _record("guard_disabled", _collect_routes())


def test_guard_env_removal_cannot_unguard_a_child(monkeypatch) -> None:
    """Same control, but deleting the keys outright rather than setting them."""

    monkeypatch.delenv(socket_guard.ENV_ENABLED, raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    _record("guard_removed", _collect_routes())


def test_child_policy_when_not_exempt() -> None:
    """Unmarked item: the parent is blocked, so every child must be too."""

    _record("nonlive", _collect_routes())


@pytest.mark.integration
@pytest.mark.live
def test_child_policy_when_armed_live() -> None:
    """Armed ``live`` item: the exemption must reach every child route alike."""

    _record("live", _collect_routes())
