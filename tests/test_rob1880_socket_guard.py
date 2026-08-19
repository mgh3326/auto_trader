"""Durable negative controls for the repository-wide ROB-1880 socket guard."""

from __future__ import annotations

import asyncio
import shlex
import socket
import subprocess
import sys

import pytest

from tests import _socket_guard as socket_guard

IPV4_EXTERNAL_HTTPS = ("203.0.113.1", 443)
IPV6_EXTERNAL_HTTPS = ("2001:db8::1", 443, 0, 0)


@pytest.mark.parametrize(
    ("family", "address", "operation"),
    [
        (socket.AF_INET, IPV4_EXTERNAL_HTTPS, "connect"),
        (socket.AF_INET6, IPV6_EXTERNAL_HTTPS, "connect"),
        (socket.AF_INET, IPV4_EXTERNAL_HTTPS, "connect_ex"),
        (socket.AF_INET6, IPV6_EXTERNAL_HTTPS, "connect_ex"),
    ],
)
def test_sync_external_tcp_operations_fail_closed(
    family: socket.AddressFamily, address: tuple[object, ...], operation: str
) -> None:
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        with pytest.raises(socket_guard.ExternalSocketBlocked):
            getattr(sock, operation)(address)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "address", "operation"),
    [
        (socket.AF_INET, IPV4_EXTERNAL_HTTPS, "connect"),
        (socket.AF_INET6, IPV6_EXTERNAL_HTTPS, "connect"),
        (socket.AF_INET, IPV4_EXTERNAL_HTTPS, "connect_ex"),
        (socket.AF_INET6, IPV6_EXTERNAL_HTTPS, "connect_ex"),
    ],
)
async def test_async_external_tcp_operations_fail_closed(
    family: socket.AddressFamily, address: tuple[object, ...], operation: str
) -> None:
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        with pytest.raises(socket_guard.ExternalSocketBlocked):
            if operation == "connect":
                await asyncio.get_running_loop().sock_connect(sock, address)
            else:
                await asyncio.to_thread(sock.connect_ex, address)


def test_sync_udp_sendto_fails_closed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(socket_guard.ExternalSocketBlocked):
            sock.sendto(b"guard-probe", IPV4_EXTERNAL_HTTPS)


@pytest.mark.asyncio
async def test_async_udp_sendto_fails_closed() -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        with pytest.raises(socket_guard.ExternalSocketBlocked):
            await asyncio.get_running_loop().sock_sendto(
                sock, b"guard-probe", IPV6_EXTERNAL_HTTPS
            )


def test_local_allowlist_is_exact() -> None:
    assert socket_guard.is_allowed_local_address(("127.0.0.1", 5432))
    assert socket_guard.is_allowed_local_address(("::1", 5432, 0, 0))
    assert socket_guard.is_allowed_local_address("/tmp/.s.PGSQL.5432")
    assert not socket_guard.is_allowed_local_address("/tmp/rob1880-unlisted.sock")
    assert not socket_guard.is_allowed_local_address(("203.0.113.1", 443))


def test_unlisted_unix_socket_path_fails_closed() -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        with pytest.raises(socket_guard.ExternalSocketBlocked):
            sock.connect("/tmp/rob1880-unlisted.sock")


def test_replaced_installation_is_a_hard_failure(monkeypatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", socket_guard._ORIGINAL_CONNECT)

    with pytest.raises(socket_guard.SocketGuardInstallationError, match="connect"):
        socket_guard.assert_installed()


def test_child_python_inherits_guard_even_with_an_explicit_empty_env() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('203.0.113.1', 443))",
        ],
        capture_output=True,
        env={},
        text=True,
    )

    assert result.returncode != 0
    assert "ExternalSocketBlocked" in (result.stdout + result.stderr)


def test_direct_network_client_subprocess_is_rejected_before_startup() -> None:
    with pytest.raises(socket_guard.ExternalSubprocessBlocked, match="network-client"):
        subprocess.run(["curl", "https://203.0.113.1"], check=True)


def test_shell_wrapped_network_client_subprocess_is_rejected_before_startup() -> None:
    with pytest.raises(socket_guard.ExternalSubprocessBlocked, match="network-client"):
        subprocess.run(["sh", "-c", "curl https://203.0.113.1"], check=True)


def test_shell_wrapped_child_python_inherits_guard_with_an_empty_env() -> None:
    script = " ".join(
        [
            shlex.quote(sys.executable),
            "-c",
            shlex.quote(
                "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('203.0.113.1', 443))"
            ),
        ]
    )
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        env={},
        text=True,
    )

    assert result.returncode != 0
    assert "ExternalSocketBlocked" in (result.stdout + result.stderr)


def test_child_python_cannot_disable_sitecustomize() -> None:
    with pytest.raises(
        socket_guard.ExternalSubprocessBlocked, match="python-startup-bypass"
    ):
        subprocess.run([sys.executable, "-S", "-c", "print('unreachable')"], check=True)


def test_import_phase_and_conftest_unloaded_mutant_are_fail_closed(tmp_path) -> None:
    """The startup hook, not conftest, must reject an import-time socket call."""

    config_path = tmp_path / "pytest.ini"
    config_path.write_text("[pytest]\n", encoding="utf-8")
    probe_path = tmp_path / "test_import_phase_socket.py"
    probe_path.write_text(
        "import socket\n"
        "socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('203.0.113.1', 443))\n",
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
            str(probe_path),
            "-q",
            "-s",
        ],
        capture_output=True,
        env={},
        text=True,
    )

    assert result.returncode != 0
    assert "ExternalSocketBlocked" in (result.stdout + result.stderr)


@pytest.mark.integration
def test_integration_marker_no_longer_opens_the_network_boundary() -> None:
    """ROB-1296 replaced the marker exemption with a live-plus-``--run-live`` one.

    Integration items keep loopback PostgreSQL/Redis through the address
    allowlist, which never depended on a marker. See
    ``tests/test_rob1296_live_only_socket_guard.py`` for the full truth table.
    """

    assert not socket_guard.is_current_test_exempt()


@pytest.mark.integration
def test_integration_marker_leaves_a_direct_python_child_guarded() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests._socket_guard import is_installed; print(is_installed())",
        ],
        capture_output=True,
        env={},
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "True"
