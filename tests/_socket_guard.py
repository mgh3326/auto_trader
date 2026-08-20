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
import re
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

ENV_POLICY_FD: Final = "AUTO_TRADER_TEST_SOCKET_GUARD_POLICY_FD"
"""File descriptor carrying the parent's exemption to one Python child.

There is deliberately **no** environment variable holding the exemption itself. A
boolean in the environment is forgeable: any test could export it and then reach
the network from a child, which is precisely the general bypass this guard
exists to prevent. Instead the parent writes its in-memory decision into a
one-shot pipe and passes only the read end's descriptor. Inheriting a live
descriptor is something the parent grants, not something a child can assert, and
the payload is consumed exactly once.

A missing descriptor means "no exemption" (default deny). A descriptor that is
present but unreadable or malformed is a hard failure -- never a silent
downgrade.
"""

_PREPARATION_KEY: Final = "auto_trader_socket_guard_exempt"
"""Key added to ``multiprocessing.spawn`` preparation data."""

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
# Launchers that start a Python interpreter without ``python`` being argv[0].
# ``uv run python -c ...`` used to slip past the direct/shell checks entirely and
# ran without the startup hook.
_PYTHON_LAUNCHER_WRAPPERS: Final = frozenset(
    {"env", "hatch", "nox", "pdm", "pipenv", "poetry", "tox", "uv", "uvx"}
)
_UNSAFE_PYTHON_STARTUP_FLAGS: Final = frozenset({"-E", "-I", "-S"})
_ENV_SCRUBBING_FLAGS: Final = frozenset({"-i", "--ignore-environment"})
_ENV_UNSET_FLAGS: Final = frozenset({"-u", "--unset"})
_GUARD_CRITICAL_ENV_KEYS: Final = frozenset(
    {
        "PYTHONPATH",
        "AUTO_TRADER_TEST_SOCKET_GUARD",
        "AUTO_TRADER_TEST_SOCKET_GUARD_EXEMPT",
    }
)


class SocketGuardInstallationError(RuntimeError):
    """Raised when the mandatory startup patch is absent or has been replaced."""


class ExternalSocketBlocked(AssertionError):
    """Raised before a non-allowlisted socket operation can reach the kernel."""


class ExternalSubprocessBlocked(AssertionError):
    """Raised before a known network-capable subprocess can be started."""


class ExternalResolutionBlocked(ExternalSocketBlocked, socket.gaierror):
    """Raised instead of resolving a non-loopback name.

    Deliberately *also* a ``socket.gaierror`` -- and therefore an ``OSError`` --
    because resolution failures are mapped by every HTTP client
    (``httpx.ConnectError``, ``requests.ConnectionError``, …). A bare
    ``AssertionError`` escapes that mapping and propagates as a hard crash, which
    is how a child process died mid-request instead of seeing the ordinary
    "cannot resolve" it already handles. Still an ``ExternalSocketBlocked`` so
    existing guard assertions keep matching.
    """


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
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname

# Set when the multiprocessing policy channel is installed; both are mandatory
# members of the integrity check.
_GUARDED_GET_PREPARATION_DATA: Any = None
_GUARDED_PREPARE: Any = None
_GUARDED_PROCESS_START: Any = None
_GUARDED_GET_COMMAND_LINE: Any = None


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


def _raise_resolution_blocked(host: object) -> None:
    _record_block("getaddrinfo")
    raise ExternalResolutionBlocked(
        f"Blocked DNS resolution of {host!r}. Name resolution happens *before* "
        "connect, so letting it through would emit a real query even though the "
        "connection itself is refused. External resolution is reachable only "
        "from a test marked `live` under an explicit `--run-live`. Mock the "
        "client at its call site (see "
        "docs/runbooks/hermetic-test-socket-guard.md)."
    )


def _assert_resolution_allowed(host: object) -> None:
    if is_current_test_exempt():
        return
    # ``None``/empty means a passive lookup for a local bind, which never leaves
    # the machine.
    if host is None or host == "" or host == b"":
        return
    if _is_loopback_host(host):
        return
    _raise_resolution_blocked(host)


def _guarded_getaddrinfo(host: object, port: object, *args: object, **kwargs: object):
    _assert_resolution_allowed(host)
    return _ORIGINAL_GETADDRINFO(host, port, *args, **kwargs)  # type: ignore[arg-type]


def _guarded_gethostbyname(hostname: str) -> str:
    _assert_resolution_allowed(hostname)
    return _ORIGINAL_GETHOSTBYNAME(hostname)


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


_PYTHON_EXECUTABLE_PATTERN: Final = re.compile(r"^(?:py|pythonw?[0-9.]*)$")


def _is_python_executable(value: str) -> bool:
    """Whether *value* names a Python interpreter.

    Matched exactly rather than by prefix: ``startswith("python")`` also matched
    the literal string ``PYTHONPATH``, so ``env -u PYTHONPATH python …`` looked
    like it had already reached the interpreter and its environment-stripping
    prefix went unexamined.
    """

    return bool(_PYTHON_EXECUTABLE_PATTERN.fullmatch(Path(value).name.lower()))


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


def _is_wrapped_python_launcher(parts: Sequence[str]) -> bool:
    """Whether *parts* starts a Python interpreter behind a known launcher.

    Both halves are required. ``uv run python -c ...`` and ``/usr/bin/env python``
    are Python children that must inherit the startup hook; ``uv build`` and
    ``env printenv`` are not, and rewriting their environment would break
    callers that depend on exact child-env semantics for a non-Python tool.
    """

    if not parts:
        return False
    return Path(
        parts[0]
    ).name.lower() in _PYTHON_LAUNCHER_WRAPPERS and _command_contains_python(parts)


def _shell_command_payload(parts: Sequence[str]) -> tuple[str, ...]:
    """Return the command string a shell invocation will execute, if any.

    ``sh -c '…'`` is only the tidiest spelling; ``bash -lc '…'`` and ``sh -ec '…'``
    bundle ``c`` with other short options and used to slip past a literal
    ``"-c" in parts`` check. Bundles are matched by decomposing the option token,
    so an argument that merely contains the letter ``c`` is not mistaken for one.
    """

    if not parts or Path(parts[0]).name.lower() not in _SHELL_EXECUTABLES:
        return ()
    for index, part in enumerate(parts[1:], start=1):
        if not part.startswith("-") or part == "--":
            break
        if part.startswith("--"):
            continue
        if "c" in part[1:]:
            payload = parts[index + 1 : index + 2]
            return tuple(payload)
    return ()


def _is_shell_wrapped_python(parts: Sequence[str]) -> bool:
    payload = _shell_command_payload(parts)
    if not payload:
        return False
    return _command_contains_python(_nested_command_parts(payload))


def _strips_guard_environment_before_python(parts: Sequence[str]) -> bool:
    """Whether *parts* removes the guard's startup state and then runs Python.

    ``sh -c 'env -i python …'`` was the obvious case, but the same hole opens
    with a scalpel: ``env -u PYTHONPATH python``, ``env --unset=…``,
    ``PYTHONPATH= python``, or ``unset PYTHONPATH; python`` all leave the
    interpreter without the ``sitecustomize`` hook. Rewriting the launcher's
    environment cannot help -- the payload undoes it afterwards -- so the command
    is refused instead.

    Only tokens *before* the interpreter are considered, because that is where
    environment manipulation takes effect; a ``PYTHONPATH=`` substring inside a
    ``-c`` script is not a bypass and must not be misread as one.
    """

    python_positions = [
        index for index, part in enumerate(parts) if _is_python_executable(part)
    ]
    if not python_positions:
        return False

    prefix = [part.rstrip(";") for part in parts[: python_positions[0]]]
    for index, part in enumerate(prefix):
        if part in _ENV_SCRUBBING_FLAGS:
            return True
        if part in _ENV_UNSET_FLAGS:
            if any(key in _GUARD_CRITICAL_ENV_KEYS for key in prefix[index + 1 :][:1]):
                return True
        if part.startswith("--unset="):
            if part.split("=", 1)[1] in _GUARD_CRITICAL_ENV_KEYS:
                return True
        if part == "unset":
            if any(key in _GUARD_CRITICAL_ENV_KEYS for key in prefix[index + 1 :]):
                return True
        if "=" in part and part.split("=", 1)[0] in _GUARD_CRITICAL_ENV_KEYS:
            return True
    return False


def _raise_subprocess_blocked(reason: str, command: object) -> None:
    _record_block(f"subprocess:{reason}")
    raise ExternalSubprocessBlocked(
        f"Blocked subprocess ({reason}) during an offline pytest run: {command!r}. "
        "Use a mocked client or mark the test integration/live when a real "
        "boundary is intentional."
    )


def _effective_command_parts(
    args: object, positional: tuple[object, ...], kwargs: dict[str, object]
) -> tuple[str, ...]:
    """argv with ``executable=`` substituted in, which is what actually runs.

    Every downstream rule -- launcher classification, the network-client deny
    list, unsafe interpreter flags, environment scrubbing -- must read the same
    representation. Classifying on the override while denying on the raw argv let
    ``Popen(["ignored", "-I", …], executable=sys.executable)`` start unguarded and
    ``Popen(["ignored", url], executable="/usr/bin/curl")`` skip the deny list.
    """

    parts = _command_parts(args)
    executable = _resolve_popen_executable(positional, kwargs)
    if executable is None:
        return parts
    if _resolve_popen_shell(positional, kwargs):
        # With ``shell=True`` the override names the *shell* and ``args`` is the
        # command string it will run. Replacing argv[0] would throw the payload
        # away, so keep both: the launcher and what it launches.
        return (executable, *parts)
    return (executable, *parts[1:])


def _resolve_popen_shell(
    positional: tuple[object, ...], kwargs: dict[str, object]
) -> bool:
    # ``Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn,
    #         close_fds, shell, …)`` -- ``shell`` is the eighth positional after
    # ``args``.
    shell = kwargs.get("shell")
    if shell is None and len(positional) > 7:
        shell = positional[7]
    return bool(shell)


def _expanded(parts):
    return (*parts, *_nested_command_parts(parts))


def _assert_network_client_allowed(command, parts=None) -> None:
    """Policy half: reaching a network client directly. Exemptible."""

    resolved = tuple(parts) if parts is not None else _command_parts(command)
    executable_names = {Path(part).name.lower() for part in _expanded(resolved)}
    if _NETWORK_CLIENT_EXECUTABLES.intersection(executable_names):
        _raise_subprocess_blocked("network-client", command)


def _assert_child_is_governable(command, parts=None) -> None:
    """Integrity half: the child must be able to load the guard at all.

    Never exemptible. An armed ``live`` test is allowed to reach the network; it
    is not allowed to launch an interpreter the guard cannot govern, because the
    exemption is scoped to one item and an ungovernable child outlives it.
    """

    resolved = tuple(parts) if parts is not None else _command_parts(command)
    expanded_parts = _expanded(resolved)

    for position, part in enumerate(expanded_parts):
        if _is_python_executable(part) and _UNSAFE_PYTHON_STARTUP_FLAGS.intersection(
            expanded_parts[position + 1 :]
        ):
            _raise_subprocess_blocked("python-startup-bypass", command)

    if _strips_guard_environment_before_python(expanded_parts):
        _raise_subprocess_blocked("env-scrubbed-python", command)


def _assert_subprocess_allowed(command, parts=None) -> None:
    """Both halves, for callers that are not the ``Popen`` interceptor."""

    _assert_child_is_governable(command, parts)
    _assert_network_client_allowed(command, parts)


def _open_policy_channel(exempt: bool) -> int:
    """One-shot pipe carrying this launch's exemption to exactly one child.

    The payload is written and the write end closed immediately, so the child
    reads a complete message followed by EOF and never blocks. A fresh pipe per
    launch means a descriptor cannot be replayed by a later, unrelated child.
    """

    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    try:
        os.write(write_fd, b"1" if exempt else b"0")
    finally:
        # Closed immediately so the child sees a complete message then EOF.
        os.close(write_fd)
    return read_fd


def _merge_pass_fds(kwargs: dict, policy_fd: int) -> dict:
    """Add *policy_fd* to ``pass_fds`` without dropping the caller's own."""

    merged = dict(kwargs)
    existing = merged.get("pass_fds") or ()
    merged["pass_fds"] = (*tuple(existing), policy_fd)
    # ``pass_fds`` implies ``close_fds=True``; be explicit so a caller-supplied
    # ``close_fds=False`` cannot strand the descriptor.
    merged["close_fds"] = True
    return merged


def read_policy_channel() -> bool:
    """Consume the inherited exemption, or fail closed.

    Called once by the child's ``sitecustomize``. An absent descriptor means no
    exemption. A descriptor that is present but unreadable or malformed is a hard
    failure: a broken channel must never be mistaken for a policy decision.
    """

    raw = os.environ.pop(ENV_POLICY_FD, None)
    if raw is None:
        return False
    try:
        descriptor = int(raw)
    except ValueError as error:
        raise SocketGuardInstallationError(
            f"malformed {ENV_POLICY_FD}: {raw!r}"
        ) from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as channel:
            payload = channel.read()
    except OSError as error:
        raise SocketGuardInstallationError(
            f"{ENV_POLICY_FD}={raw} could not be read"
        ) from error
    if payload not in (b"0", b"1"):
        raise SocketGuardInstallationError(
            f"malformed socket guard policy payload: {payload!r}"
        )
    return payload == b"1"


def _with_guard_environment(
    environment: Mapping[str, str] | None, *, enabled: bool = True
) -> dict[str, str]:
    """Child environment carrying the startup hook -- and never an exemption."""

    child_environment = dict(os.environ if environment is None else environment)
    child_environment[ENV_ENABLED] = "1" if enabled else "0"
    # A stale descriptor must never be inherited by an unrelated child.
    child_environment.pop(ENV_POLICY_FD, None)

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


def _guard_child_environment(
    explicit_env: Mapping[str, str] | None, policy_fd: int | None
) -> dict[str, str]:
    child_environment = _with_guard_environment(explicit_env, enabled=True)
    if policy_fd is not None:
        child_environment[ENV_POLICY_FD] = str(policy_fd)
    return child_environment


def activate_child_guard_environment() -> None:
    """Make every subsequently spawned child Python inherit the startup hook."""

    os.environ.update(_with_guard_environment(os.environ, enabled=True))


def _replace_popen_environment(
    positional: tuple[object, ...],
    kwargs: dict[str, object],
    *,
    policy_fd: int | None,
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
        rewritten[env_index] = _guard_child_environment(explicit_env, policy_fd)
        return tuple(rewritten), kwargs

    rewritten_kwargs = dict(kwargs)
    explicit_env = rewritten_kwargs.get("env")
    if explicit_env is not None and not isinstance(explicit_env, Mapping):
        raise SocketGuardInstallationError("subprocess env must be a mapping")
    rewritten_kwargs["env"] = _guard_child_environment(explicit_env, policy_fd)
    return positional, rewritten_kwargs


def _resolve_popen_executable(
    positional: tuple[object, ...], kwargs: dict[str, object]
) -> str | None:
    """The real program ``Popen`` will exec, when overridden away from argv[0].

    ``Popen(["ignored", "-c", …], executable=sys.executable)`` runs Python while
    argv[0] says otherwise, so classifying on ``args`` alone let the child start
    without the startup hook.
    """

    # ``Popen(args, bufsize, executable, …)`` -- ``executable`` is the second
    # positional after ``args``.
    executable = kwargs.get("executable")
    if executable is None and len(positional) > 1:
        executable = positional[1]
    if executable is None:
        return None
    if isinstance(executable, os.PathLike):
        return os.fspath(executable)
    if isinstance(executable, bytes):
        return os.fsdecode(executable)
    return executable if isinstance(executable, str) else None


def _guarded_popen_init(
    self: subprocess.Popen[Any], args: object, *positional: object, **kwargs: object
) -> None:
    exempt = is_current_test_exempt()
    parts = _effective_command_parts(args, positional, kwargs)

    # Integrity first, and unconditionally. These rules are not about *policy* --
    # they are about whether the child can be governed at all. A command that
    # discards the startup hook produces an ungovernable interpreter whether or
    # not the parent happens to be exempt, so an armed ``live`` test must not be
    # able to launch one either.
    _assert_child_is_governable(args, parts)
    if not exempt:
        # Reaching a network client directly is a policy question, so this half
        # stays under the exemption.
        _assert_network_client_allowed(args, parts)
    launches_python = (
        (parts and _is_python_executable(parts[0]))
        or _is_shell_wrapped_python(parts)
        or _is_wrapped_python_launcher(parts)
        # ``shell=True``: the payload is a command string, so there is no ``-c``
        # token to key on -- a Python interpreter anywhere in it is the signal.
        or (
            _resolve_popen_shell(positional, kwargs) and _command_contains_python(parts)
        )
    )
    if launches_python:
        # The guard is always installed in a Python child; the exemption travels
        # separately.  Disabling the guard outright for an exempt parent used to
        # make the child's policy depend on how it was launched, and left
        # ``uv run python`` -- neither a direct python argv[0] nor ``sh -c`` --
        # with no hook at all.
        policy_fd = _open_policy_channel(exempt)
        try:
            rewritten_positional, rewritten_kwargs = _replace_popen_environment(
                positional, kwargs, policy_fd=policy_fd
            )
            rewritten_kwargs = _merge_pass_fds(rewritten_kwargs, policy_fd)
            _ORIGINAL_POPEN_INIT(self, args, *rewritten_positional, **rewritten_kwargs)
        finally:
            # The child inherited its own duplicate; the parent's copy is done
            # either way, success or failure.
            os.close(policy_fd)
        return
    else:
        # Do not alter the environment of non-Python tools. In particular,
        # strict subprocess harnesses must retain byte-for-byte child-env
        # semantics; direct network-client executables were rejected above.
        rewritten_positional, rewritten_kwargs = positional, kwargs
    _ORIGINAL_POPEN_INIT(self, args, *rewritten_positional, **rewritten_kwargs)


def _install_multiprocessing_policy_channel() -> None:
    """Carry the exemption to ``spawn``/``forkserver`` children out of band.

    ``fork`` children inherit the in-memory flag, but a re-execed child starts
    from the module default, so the same armed ``live`` test used to get a
    different policy depending on the start method. The environment is the wrong
    channel for this: a boolean any test could set would be exactly the general
    bypass this guard exists to prevent. ``multiprocessing`` already ships a
    parent-controlled, in-memory payload to each child, so the policy travels
    there and a tampered ``os.environ`` cannot reach the child.

    Installed in children too, which is what covers the ``forkserver`` server
    process: it is a fresh interpreter that imports ``multiprocessing`` itself
    and would otherwise run an unpatched ``prepare``.
    """

    global _GUARDED_GET_PREPARATION_DATA, _GUARDED_PREPARE
    global _GUARDED_PROCESS_START, _GUARDED_GET_COMMAND_LINE

    # Both hooks must be intact to skip: a partial installation -- one hook
    # restored by a test or plugin -- would keep the session evidence green while
    # ``spawn``/``forkserver`` children silently lost their policy.
    import multiprocessing.process as _mp_process_check
    import multiprocessing.spawn as mp_spawn

    if (
        _GUARDED_GET_PREPARATION_DATA is not None
        and mp_spawn.get_preparation_data is _GUARDED_GET_PREPARATION_DATA
        and _GUARDED_PREPARE is not None
        and mp_spawn.prepare is _GUARDED_PREPARE
        and _GUARDED_PROCESS_START is not None
        and _mp_process_check.BaseProcess.start is _GUARDED_PROCESS_START
        and _GUARDED_GET_COMMAND_LINE is not None
        and mp_spawn.get_command_line is _GUARDED_GET_COMMAND_LINE
    ):
        return

    original_get_preparation_data = getattr(
        mp_spawn.get_preparation_data,
        "_rob1296_original",
        mp_spawn.get_preparation_data,
    )
    original_prepare = getattr(mp_spawn.prepare, "_rob1296_original", mp_spawn.prepare)

    def _guarded_get_preparation_data(name: str) -> dict[str, Any]:
        data = original_get_preparation_data(name)
        data[_PREPARATION_KEY] = is_current_test_exempt()
        return data

    def _guarded_prepare(data: Mapping[str, Any]) -> None:
        # Strict: the key must be present and a real bool. A missing or oddly
        # typed value means the channel is broken, and a broken channel must be a
        # hard failure rather than a silent "not exempt" that hides the breakage.
        if _PREPARATION_KEY not in data:
            raise SocketGuardInstallationError(
                "multiprocessing child received no socket guard policy"
            )
        exempt = data[_PREPARATION_KEY]
        if not isinstance(exempt, bool):
            raise SocketGuardInstallationError(
                f"socket guard policy must be a bool, got {exempt!r}"
            )
        # The interpreter must already be governed before any user module is
        # imported by ``prepare``.
        assert_installed()
        set_current_test_exempt(exempt)
        original_prepare({k: v for k, v in data.items() if k != _PREPARATION_KEY})

    import multiprocessing.process as mp_process

    original_start = getattr(
        mp_process.BaseProcess.start, "_rob1296_original", mp_process.BaseProcess.start
    )

    def _guarded_start(self):
        # ``forkserver`` boots a long-lived server interpreter through a path
        # this guard does not control, and that server then forks every child.
        # Rather than pretend, it is refused outright: ROB-1296 never required
        # it, nothing in this repository selects it, and a late "the AF_UNIX
        # socket was blocked" sentinel is not proof that a child would have had
        # the right policy.
        if type(self)._start_method == "forkserver":
            raise SocketGuardInstallationError(
                "the forkserver start method is not supported under the ROB-1296 "
                "socket guard: its server interpreter is launched outside the "
                "guard's control. Use 'spawn' or 'fork'."
            )

        # Integrity only -- deliberately no ``os.environ`` mutation. Repairing
        # the environment here would leave a permanent, cross-test side effect on
        # a shared mapping, and would silently rewrite what non-Python children
        # of the same process see. The child's guard installation is instead
        # forced by the bootstrap in ``get_command_line`` below, which does not
        # depend on the ambient environment at all.
        assert_installed()
        return original_start(self)

    _guarded_start._rob1296_original = original_start  # type: ignore[attr-defined]
    mp_process.BaseProcess.start = _guarded_start
    _GUARDED_PROCESS_START = _guarded_start

    original_get_command_line = getattr(
        mp_spawn.get_command_line, "_rob1296_original", mp_spawn.get_command_line
    )

    def _guarded_get_command_line(**kwds: Any) -> list[str]:
        """Force the child to install the guard before ``spawn_main`` runs.

        ``multiprocessing`` builds its own ``python -c …`` bootstrap, and that
        interpreter is reached without ever passing through ``subprocess.Popen``.
        Relying on ``PYTHONPATH``/``sitecustomize`` would mean relying on the
        ambient environment, which a test can edit. Instead the absolute project
        root is baked into the generated program, so installation does not depend
        on anything the child inherits.
        """

        command = list(original_get_command_line(**kwds))
        for index, token in enumerate(command):
            if token == "-c" and index + 1 < len(command):
                command[index + 1] = (
                    f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
                    "from tests._socket_guard import install as _rob1296_install; "
                    "_rob1296_install(); " + command[index + 1]
                )
                break
        return command

    _guarded_get_command_line._rob1296_original = (  # type: ignore[attr-defined]
        original_get_command_line
    )
    mp_spawn.get_command_line = _guarded_get_command_line
    _GUARDED_GET_COMMAND_LINE = _guarded_get_command_line

    _guarded_get_preparation_data._rob1296_original = (  # type: ignore[attr-defined]
        original_get_preparation_data
    )
    _guarded_prepare._rob1296_original = original_prepare  # type: ignore[attr-defined]
    mp_spawn.get_preparation_data = _guarded_get_preparation_data
    mp_spawn.prepare = _guarded_prepare
    _GUARDED_GET_PREPARATION_DATA = _guarded_get_preparation_data
    _GUARDED_PREPARE = _guarded_prepare


def install() -> None:
    """Install every intercept once, raising instead of silently degrading."""

    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            assert_installed()
            return

        _install_multiprocessing_policy_channel()
        socket.getaddrinfo = _guarded_getaddrinfo
        socket.gethostbyname = _guarded_gethostbyname
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

    import multiprocessing.process as mp_process
    import multiprocessing.spawn as mp_spawn

    expected: tuple[tuple[object, str, object], ...] = (
        (socket, "getaddrinfo", _guarded_getaddrinfo),
        (socket, "gethostbyname", _guarded_gethostbyname),
        (socket.socket, "connect", _guarded_connect),
        (socket.socket, "connect_ex", _guarded_connect_ex),
        (socket.socket, "sendto", _guarded_sendto),
        (subprocess.Popen, "__init__", _guarded_popen_init),
        # The child-policy channel is load-bearing, not decorative: without it a
        # re-execed child of an armed ``live`` test disagrees with a ``fork``
        # child of the same test. A partial restore must fail the session, not
        # merely stop propagating.
        (mp_spawn, "get_preparation_data", _GUARDED_GET_PREPARATION_DATA),
        (mp_spawn, "prepare", _GUARDED_PREPARE),
        (mp_process.BaseProcess, "start", _GUARDED_PROCESS_START),
        (mp_spawn, "get_command_line", _GUARDED_GET_COMMAND_LINE),
    )
    if _ORIGINAL_SENDMSG is not None:
        expected += ((socket.socket, "sendmsg", _guarded_sendmsg),)

    failures = [
        f"{getattr(owner, '__name__', owner)}.{name}"
        for owner, name, replacement in expected
        if replacement is None or getattr(owner, name, None) is not replacement
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
