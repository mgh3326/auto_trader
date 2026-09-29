"""Fail-closed local process-death witness with a disposable /proc tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.nhplug_mock.lease_host import process_gone_on_lease_host
from app.services.nhplug_mock.ledger import LeaseIdentity

pytestmark = pytest.mark.unit


def test_local_witness_reboot_dead_pid_and_stopped_process(tmp_path: Path) -> None:
    machine = tmp_path / "machine-id"
    machine.write_text("machine-a\n")
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-a\n")
    (proc / "self/ns").mkdir(parents=True)
    namespace = proc / "self/ns/pid"
    namespace.write_text("namespace")
    identity = LeaseIdentity(
        "machine-a", "boot-a", str(namespace.stat().st_ino), 123, 777
    )
    assert process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    (proc / "123").mkdir()
    (proc / "123/stat").write_text("123 (worker) T " + "0 " * 18 + "777")
    assert not process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    (proc / "123/stat").write_text("123 (worker) R " + "0 " * 18 + "778")
    assert process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    machine.write_text("another-machine\n")
    assert not process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)
    machine.write_text("machine-a\n")
    (proc / "sys/kernel/random/boot_id").write_text("boot-b\n")
    assert process_gone_on_lease_host(identity, machine_id=machine, proc_root=proc)


def test_unknown_namespace_or_proc_visibility_is_not_death_proof(
    tmp_path: Path,
) -> None:
    machine = tmp_path / "machine-id"
    machine.write_text("machine-a")
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-a")
    (proc / "self/ns").mkdir(parents=True)
    (proc / "self/ns/pid").write_text("namespace")
    wrong_namespace = LeaseIdentity(
        "machine-a", "boot-a", "unknown-namespace", 123, 777
    )
    assert not process_gone_on_lease_host(
        wrong_namespace, machine_id=machine, proc_root=proc
    )
    (proc / "sys/kernel/random/boot_id").unlink()
    assert not process_gone_on_lease_host(
        wrong_namespace, machine_id=machine, proc_root=proc
    )
