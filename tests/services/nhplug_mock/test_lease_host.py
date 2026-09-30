"""Fail-closed local process-death witness with a disposable /proc tree.

Namespace links are modelled as symlinks whose target is the exact nsfs text
("pid:[N]"), which is what os.readlink returns on a real /proc.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import app.services.nhplug_mock.lease_host as lease_host
from app.services.nhplug_mock.lease_host import (
    INIT_PID_NS,
    current_lease_identity,
    host_pid_namespaces,
    process_gone_on_lease_host,
)
from app.services.nhplug_mock.ledger import LeaseIdentity

pytestmark = pytest.mark.unit

MACHINE = "0123456789abcdef0123456789abcdef"
BOOT = "01234567-89ab-cdef-0123-456789abcdef"
SIBLING_NS = "4026532100"
LEASE_NS = "4026532200"
OTHER_NS = "4026532300"


def _link(path: Path, namespace: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(f"pid:[{namespace}]")


def _tree(
    tmp_path: Path,
    *,
    own: str = INIT_PID_NS,
    proc1: str | None = None,
    entries: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    machine = tmp_path / "machine-id"
    machine.write_text(MACHINE + "\n")
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text(BOOT + "\n")
    _link(proc / "self/ns/pid", own)
    _link(proc / "1/ns/pid", own if proc1 is None else proc1)
    for pid, namespace in (entries or {}).items():
        _link(proc / pid / "ns/pid", namespace)
    return machine, proc


def _lease(namespace: str = LEASE_NS) -> LeaseIdentity:
    return LeaseIdentity(MACHINE, BOOT, namespace, 7, 777)


def test_local_witness_reboot_dead_pid_and_stopped_process(tmp_path: Path) -> None:
    machine, proc = _tree(tmp_path, own=LEASE_NS)
    identity = LeaseIdentity(MACHINE, BOOT, LEASE_NS, 123, 777)
    assert process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    (proc / "123").mkdir()
    (proc / "123/stat").write_text("123 (worker) T " + "0 " * 18 + "777")
    assert not process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    (proc / "123/stat").write_text("123 (worker) R " + "0 " * 18 + "778")
    assert process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    machine.write_text("another-machine\n")
    assert not process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    machine.write_text(MACHINE + "\n")
    (proc / "sys/kernel/random/boot_id").write_text("boot-b\n")
    assert process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)


def test_unknown_namespace_or_proc_visibility_is_not_death_proof(
    tmp_path: Path,
) -> None:
    machine, proc = _tree(tmp_path, own=SIBLING_NS)
    wrong_namespace = _lease("unknown-namespace")
    assert not process_gone_on_lease_host(
        wrong_namespace, machine_id=machine, proc_root=proc
    )
    (proc / "sys/kernel/random/boot_id").unlink()
    assert not process_gone_on_lease_host(
        wrong_namespace, machine_id=machine, proc_root=proc
    )


def test_host_scan_proves_absence_only_after_reading_every_link(
    tmp_path: Path,
) -> None:
    """A full, readable scan from the host namespace proves only absence."""

    machine, proc = _tree(tmp_path, entries={"40": OTHER_NS, "41": LEASE_NS})
    assert host_pid_namespaces(proc_root=proc, expected_host_pid_ns=INIT_PID_NS) == {
        INIT_PID_NS,
        OTHER_NS,
        LEASE_NS,
    }
    assert not process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=INIT_PID_NS
    )
    (proc / "41/ns/pid").unlink()
    (proc / "41/ns").rmdir()
    (proc / "41").rmdir()
    assert process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=INIT_PID_NS
    )
    # Same scan, another machine: never proof.
    machine.write_text("f" * 32 + "\n")
    assert not process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=INIT_PID_NS
    )


def test_sibling_container_namespace_is_never_host_visibility(tmp_path: Path) -> None:
    """A private PID namespace that names itself as host proves nothing (B2)."""

    machine, proc = _tree(tmp_path, own=SIBLING_NS, entries={"9": SIBLING_NS})
    # The lease namespace is absent from this sibling's view, as in the live-
    # dispatcher counterexample; asserting its own inode must not help.
    assert host_pid_namespaces(proc_root=proc, expected_host_pid_ns=SIBLING_NS) is None
    assert not process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=SIBLING_NS
    )


@pytest.mark.parametrize("expected", [None, "", SIBLING_NS, INIT_PID_NS + "0"])
def test_host_namespace_measured_outside_must_match(
    tmp_path: Path, expected: str | None
) -> None:
    """A missing or different outside measurement is not proof, lease absent."""

    machine, proc = _tree(tmp_path, entries={"40": OTHER_NS})
    assert host_pid_namespaces(proc_root=proc, expected_host_pid_ns=expected) is None
    assert not process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=expected
    )


def test_proc1_outside_scanner_namespace_is_not_proof(tmp_path: Path) -> None:
    machine, proc = _tree(tmp_path, proc1=OTHER_NS)
    assert host_pid_namespaces(proc_root=proc, expected_host_pid_ns=INIT_PID_NS) is None
    assert not process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=INIT_PID_NS
    )


def _unreadable_as_directory(entry: Path) -> None:
    # Kernel 7.x under ptrace denial: stat of ns/pid silently yields a directory.
    (entry / "ns/pid").mkdir(parents=True)


def _unreadable_as_loop(entry: Path) -> None:
    (entry / "ns").mkdir(parents=True)
    (entry / "ns/pid").symlink_to("pid")


def _unreadable_as_other_namespace_kind(entry: Path) -> None:
    (entry / "ns").mkdir(parents=True)
    (entry / "ns/pid").symlink_to(f"net:[{LEASE_NS}]")


def _link_missing_on_live_entry(entry: Path) -> None:
    (entry / "ns").mkdir(parents=True)


@pytest.mark.parametrize(
    "make_unreadable",
    [
        _unreadable_as_directory,
        _unreadable_as_loop,
        _unreadable_as_other_namespace_kind,
        _link_missing_on_live_entry,
    ],
)
@pytest.mark.parametrize("entry_name", ["1", "50"])
def test_any_unreadable_link_is_not_proof(
    tmp_path: Path, make_unreadable, entry_name: str
) -> None:
    """One unreadable link, including /proc/1, makes the scan unverifiable (B1)."""

    machine, proc = _tree(tmp_path, entries={"40": OTHER_NS})
    target = proc / entry_name
    if entry_name == "1":
        (target / "ns/pid").unlink()
        (target / "ns").rmdir()
    make_unreadable(target)
    assert host_pid_namespaces(proc_root=proc, expected_host_pid_ns=INIT_PID_NS) is None
    assert not process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=INIT_PID_NS
    )


def test_permission_denied_link_is_not_treated_as_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine, proc = _tree(tmp_path, entries={"40": OTHER_NS, "50": OTHER_NS})
    real_readlink = os.readlink
    denied = proc / "50/ns/pid"

    def readlink(path: os.PathLike[str] | str) -> str:
        if Path(path) == denied:
            raise PermissionError(13, "ptrace denied")
        return real_readlink(path)

    monkeypatch.setattr(lease_host.os, "readlink", readlink)
    assert host_pid_namespaces(proc_root=proc, expected_host_pid_ns=INIT_PID_NS) is None
    assert not process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=INIT_PID_NS
    )


def test_only_a_vanished_pid_entry_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine, proc = _tree(tmp_path, entries={"40": OTHER_NS})
    real_iterdir = Path.iterdir

    def iterdir(self: Path):
        yield from real_iterdir(self)
        if self == proc:
            yield proc / "99999"  # exited between listing and reading

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert process_gone_on_lease_host(
        _lease(), machine_id=machine, proc_root=proc, expected_host_pid_ns=INIT_PID_NS
    )


def test_unprivileged_scan_of_the_real_proc_is_unverifiable() -> None:
    """Without ptrace rights over root's /proc/1 the real scan cannot be proof."""

    if os.geteuid() == 0:
        pytest.skip("root may hold ptrace rights over every process")
    own = os.readlink("/proc/self/ns/pid")[5:-1]
    me = current_lease_identity()
    never_seen = LeaseIdentity(me.machine_id, me.boot_id, "1", 1, 1)
    assert host_pid_namespaces(expected_host_pid_ns=own) is None
    assert not process_gone_on_lease_host(never_seen, expected_host_pid_ns=own)


@pytest.mark.parametrize(
    ("machine", "boot"),
    [
        ("abc\x00def" + "0" * 25, BOOT),
        ("abc def" + "0" * 25, BOOT),
        ("0" * 300, BOOT),
        (MACHINE.upper(), BOOT),
        ("", BOOT),
        (MACHINE, "boot id"),
        (MACHINE, ""),
    ],
)
def test_lease_identity_rejects_content_that_cannot_be_witnessed(
    monkeypatch: pytest.MonkeyPatch, machine: str, boot: str
) -> None:
    files = {"/etc/machine-id": machine, "/proc/sys/kernel/random/boot_id": boot}
    real_read_text = Path.read_text

    def read_text(self: Path, *args, **kwargs) -> str:
        if str(self) in files:
            return files[str(self)] + "\n"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(OSError):
        current_lease_identity()
