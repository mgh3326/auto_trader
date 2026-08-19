"""Fail-closed socket guard shared by the pytest startup plugin and children.

The guard deliberately lives outside ``conftest.py``: pytest imports a ``-p``
plugin before collecting test modules, while child Python interpreters load the
same implementation from ``sitecustomize``. Keeping the policy here makes the
two installation paths byte-for-byte identical.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import socket
import subprocess
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

ENV_ENABLED: Final = "AUTO_TRADER_TEST_SOCKET_GUARD"
"""Environment switch consumed by the child-process ``sitecustomize`` hook."""

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
STARTUP_HOOK_DIRECTORY: Final = PROJECT_ROOT / "tests" / "_socket_guard_startup"

# The local-service paths are intentionally finite. A bare ``AF_UNIX`` path is
# not proof that the peer is a local test service; accepting every path was the
# pre-ROB-1880 bypass. TCP loopback remains independently allowed.
LOCAL_UNIX_SOCKET_ALLOWLIST: Final = frozenset(
    {
        "/tmp/.s.PGSQL.5432",
        "/private/tmp/.s.PGSQL.5432",
        "/var/run/postgresql/.s.PGSQL.5432",
        "/var/run/redis/redis-server.sock",
    }
)

_NETWORK_CLIENT_EXECUTABLES: Final = frozenset(
    {
        "curl",
        "ftp",
        "http",
        "https",
        "nc",
        "ncat",
        "netcat",
        "rsync",
        "scp",
        "sftp",
        "ssh",
        "telnet",
        "wget",
    }
)
_SHELL_EXECUTABLES: Final = frozenset({"bash", "dash", "fish", "sh", "zsh"})
_UNSAFE_PYTHON_STARTUP_FLAGS: Final = frozenset({"-E", "-I", "-S"})


class SocketGuardInstallationError(RuntimeError):
    """Raised when the mandatory startup patch is absent or has been replaced."""


class ExternalSocketBlocked(AssertionError):
    """Raised before a non-allowlisted socket operation can reach the kernel."""


class ExternalSubprocessBlocked(AssertionError):
    """Raised before a known network-capable subprocess can be started."""


_INSTALL_LOCK = threading.RLock()
_STATE_LOCK = threading.Lock()
_INSTALLED = False
_CURRENT_TEST_IS_EXEMPT = False
_BLOCKED_BY_OPERATION: Counter[str] = Counter()

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SENDTO = socket.socket.sendto
_ORIGINAL_SENDMSG = getattr(socket.socket, "sendmsg", None)
_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__


def _record_block(operation: str) -> None:
    with _STATE_LOCK:
        _BLOCKED_BY_OPERATION[operation] += 1


def set_current_test_exempt(exempt: bool) -> bool:
    """Set the marker-derived exemption and return the prior state.

    Pytest executes one item per worker process at a time. A process-global
    flag intentionally also covers helper threads created by an integration
    test, unlike a task-local context variable would.
    """

    global _CURRENT_TEST_IS_EXEMPT
    with _STATE_LOCK:
        previous = _CURRENT_TEST_IS_EXEMPT
        _CURRENT_TEST_IS_EXEMPT = exempt
        return previous


def is_current_test_exempt() -> bool:
    with _STATE_LOCK:
        return _CURRENT_TEST_IS_EXEMPT


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    mapped = getattr(parsed, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _is_allowlisted_unix_path(address: str | bytes) -> bool:
    try:
        path = os.fsdecode(address)
    except TypeError:
        return False
    return path in LOCAL_UNIX_SOCKET_ALLOWLIST


def is_allowed_local_address(address: object) -> bool:
    """Return whether *address* is an exact local socket allowlist member."""

    if isinstance(address, (str, bytes)):
        return _is_allowlisted_unix_path(address)
    if not isinstance(address, tuple) or not address:
        return False
    return _is_loopback_host(address[0])


def _raise_socket_blocked(operation: str, address: object) -> None:
    _record_block(operation)
    raise ExternalSocketBlocked(
        f"Blocked outbound socket.{operation} to {address!r}. External "
        "sockets are reachable only from a test marked `live` under an "
        "explicit `--run-live`; the `integration` marker does not grant "
        "network access (ROB-1296). Mock the external client at its call site "
        "(see docs/runbooks/hermetic-test-socket-guard.md) instead of letting "
        "the test reach a real host."
    )


def is_socket_address_permitted(address: object) -> bool:
    """Return the guard's current verdict for *address* without side effects.

    Exposed so policy tests can assert the marker/option truth table without
    emitting a single packet and without incrementing the blocked-attempt
    counters that back the CI evidence artifact.
    """

    return is_current_test_exempt() or is_allowed_local_address(address)


def _assert_socket_address_allowed(operation: str, address: object) -> None:
    if is_socket_address_permitted(address):
        return
    _raise_socket_blocked(operation, address)


def _guarded_connect(self: socket.socket, address: object) -> None:
    _assert_socket_address_allowed("connect", address)
    _ORIGINAL_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    _assert_socket_address_allowed("connect_ex", address)
    return _ORIGINAL_CONNECT_EX(self, address)


def _guarded_sendto(
    self: socket.socket, data: bytes, *args: object, **kwargs: object
) -> int:
    address = kwargs.get("address")
    if address is None and args:
        # ``sendto(data, address)`` and ``sendto(data, flags, address)`` both
        # put the peer address last.
        address = args[-1]
    if address is not None:
        _assert_socket_address_allowed("sendto", address)
    return _ORIGINAL_SENDTO(self, data, *args, **kwargs)


def _guarded_sendmsg(
    self: socket.socket, buffers: object, *args: object, **kwargs: object
) -> int:
    address = kwargs.get("address")
    # sendmsg(buffers[, ancdata[, flags[, address]]])
    if address is None and len(args) >= 3:
        address = args[2]
    if address is not None:
        _assert_socket_address_allowed("sendmsg", address)
    if _ORIGINAL_SENDMSG is None:  # pragma: no cover - platform defensive
        raise SocketGuardInstallationError("socket.sendmsg is unavailable")
    return _ORIGINAL_SENDMSG(self, buffers, *args, **kwargs)


def _command_parts(command: object) -> tuple[str, ...]:
    if isinstance(command, os.PathLike):
        return (os.fspath(command),)
    if isinstance(command, bytes):
        return tuple(shlex.split(os.fsdecode(command)))
    if isinstance(command, str):
        return tuple(shlex.split(command))
    if isinstance(command, Sequence):
        return tuple(
            os.fsdecode(part) if isinstance(part, bytes) else str(part)
            for part in command
        )
    return ()


def _is_python_executable(value: str) -> bool:
    executable = Path(value).name.lower()
    return executable == "py" or executable.startswith("python")


def _nested_command_parts(parts: Sequence[str]) -> tuple[str, ...]:
    """Expose a shell ``-c`` payload without attempting to execute it.

    ``Popen(["sh", "-c", "curl …"])`` must not turn a direct-client deny
    rule into a superficial one-token check.  One level is sufficient for the
    usual shell wrapper while avoiding a speculative shell parser.
    """

    nested: list[str] = []
    for part in parts:
        try:
            parsed = shlex.split(part)
        except ValueError:
            continue
        if len(parsed) > 1:
            nested.extend(parsed)
    return tuple(nested)


def _command_contains_python(parts: Sequence[str]) -> bool:
    return any(
        _is_python_executable(part) for part in (*parts, *_nested_command_parts(parts))
    )


def _is_shell_wrapped_python(parts: Sequence[str]) -> bool:
    if not parts or Path(parts[0]).name.lower() not in _SHELL_EXECUTABLES:
        return False
    return "-c" in parts and _command_contains_python(_nested_command_parts(parts))


def _raise_subprocess_blocked(reason: str, command: object) -> None:
    _record_block(f"subprocess:{reason}")
    raise ExternalSubprocessBlocked(
        f"Blocked subprocess ({reason}) during an offline pytest run: {command!r}. "
        "Use a mocked client or mark the test integration/live when a real "
        "boundary is intentional."
    )


def _assert_subprocess_allowed(command: object) -> None:
    parts = _command_parts(command)
    expanded_parts = (*parts, *_nested_command_parts(parts))
    executable_names = {Path(part).name.lower() for part in expanded_parts}
    if _NETWORK_CLIENT_EXECUTABLES.intersection(executable_names):
        _raise_subprocess_blocked("network-client", command)
    for position, part in enumerate(expanded_parts):
        if _is_python_executable(part) and _UNSAFE_PYTHON_STARTUP_FLAGS.intersection(
            expanded_parts[position + 1 :]
        ):
            _raise_subprocess_blocked("python-startup-bypass", command)


def _with_guard_environment(
    environment: Mapping[str, str] | None, *, enabled: bool
) -> dict[str, str]:
    child_environment = dict(os.environ if environment is None else environment)
    child_environment[ENV_ENABLED] = "1" if enabled else "0"

    if enabled:
        existing = [
            entry
            for entry in child_environment.get("PYTHONPATH", "").split(os.pathsep)
            if entry
        ]
        required = [str(STARTUP_HOOK_DIRECTORY), str(PROJECT_ROOT)]
        for entry in reversed(required):
            if entry in existing:
                existing.remove(entry)
            existing.insert(0, entry)
        child_environment["PYTHONPATH"] = os.pathsep.join(existing)

    return child_environment


def activate_child_guard_environment() -> None:
    """Make every subsequently spawned child Python inherit the startup hook."""

    os.environ.update(_with_guard_environment(os.environ, enabled=True))


def _replace_popen_environment(
    positional: tuple[object, ...], kwargs: dict[str, object], *, enabled: bool
) -> tuple[tuple[object, ...], dict[str, object]]:
    # ``env`` is the tenth argument after ``args`` in subprocess.Popen's
    # positional signature. Keyword usage is overwhelmingly common, but
    # supporting both prevents an explicit child environment from bypassing
    # the inherited startup hook.
    env_index = 9
    if len(positional) > env_index:
        rewritten = list(positional)
        explicit_env = rewritten[env_index]
        if explicit_env is not None and not isinstance(explicit_env, Mapping):
            raise SocketGuardInstallationError("subprocess env must be a mapping")
        rewritten[env_index] = _with_guard_environment(explicit_env, enabled=enabled)
        return tuple(rewritten), kwargs

    rewritten_kwargs = dict(kwargs)
    explicit_env = rewritten_kwargs.get("env")
    if explicit_env is not None and not isinstance(explicit_env, Mapping):
        raise SocketGuardInstallationError("subprocess env must be a mapping")
    rewritten_kwargs["env"] = _with_guard_environment(explicit_env, enabled=enabled)
    return positional, rewritten_kwargs


def _guarded_popen_init(
    self: subprocess.Popen[Any], args: object, *positional: object, **kwargs: object
) -> None:
    exempt = is_current_test_exempt()
    parts = _command_parts(args)
    if not exempt:
        _assert_subprocess_allowed(args)
    if (parts and _is_python_executable(parts[0])) or _is_shell_wrapped_python(parts):
        # The marker exemption applies to a direct Python child too.  The
        # parent process has the startup hook in its own environment, so an
        # exempt child needs an explicit off switch rather than merely leaving
        # its environment unchanged.  The same applies to ``sh -c python``:
        # the shell is only a launcher, not a bypass around child inheritance.
        rewritten_positional, rewritten_kwargs = _replace_popen_environment(
            positional, kwargs, enabled=not exempt
        )
    else:
        # Do not alter the environment of non-Python tools. In particular,
        # strict subprocess harnesses must retain byte-for-byte child-env
        # semantics; direct network-client executables were rejected above.
        rewritten_positional, rewritten_kwargs = positional, kwargs
    _ORIGINAL_POPEN_INIT(self, args, *rewritten_positional, **rewritten_kwargs)


def install() -> None:
    """Install every intercept once, raising instead of silently degrading."""

    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            assert_installed()
            return

        socket.socket.connect = _guarded_connect
        socket.socket.connect_ex = _guarded_connect_ex
        socket.socket.sendto = _guarded_sendto
        if _ORIGINAL_SENDMSG is not None:
            socket.socket.sendmsg = _guarded_sendmsg
        subprocess.Popen.__init__ = _guarded_popen_init
        _INSTALLED = True
        assert_installed()


def is_installed() -> bool:
    try:
        assert_installed()
    except SocketGuardInstallationError:
        return False
    return True


def assert_installed() -> None:
    """Assert all expected intercepts remain intact; never warn-and-continue."""

    expected: tuple[tuple[object, str, object], ...] = (
        (socket.socket, "connect", _guarded_connect),
        (socket.socket, "connect_ex", _guarded_connect_ex),
        (socket.socket, "sendto", _guarded_sendto),
        (subprocess.Popen, "__init__", _guarded_popen_init),
    )
    if _ORIGINAL_SENDMSG is not None:
        expected += ((socket.socket, "sendmsg", _guarded_sendmsg),)

    failures = [
        f"{owner.__name__}.{name}"
        for owner, name, replacement in expected
        if getattr(owner, name, None) is not replacement
    ]
    if not _INSTALLED or failures:
        details = ", ".join(failures) if failures else "startup installation"
        raise SocketGuardInstallationError(
            "ROB-1880 socket guard is not installed correctly: " + details
        )


def summary() -> dict[str, object]:
    """Return JSON-safe evidence for terminal output and CI artifacts."""

    with _STATE_LOCK:
        blocked_by_operation = dict(sorted(_BLOCKED_BY_OPERATION.items()))
    return {
        "guard": "rob-1880-socket-guard",
        "active": is_installed(),
        "pid": os.getpid(),
        "worker_id": os.environ.get("PYTEST_XDIST_WORKER", "controller"),
        "blocked_attempts": sum(blocked_by_operation.values()),
        "blocked_by_operation": blocked_by_operation,
        "local_unix_allowlist": sorted(LOCAL_UNIX_SOCKET_ALLOWLIST),
        "child_python_startup_hook": str(STARTUP_HOOK_DIRECTORY),
    }


def write_report(path: str | os.PathLike[str], payload: Mapping[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
