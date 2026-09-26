"""Local lease-host process witness for manual NHPLUG mock release."""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.services.nhplug_mock.ledger import LeaseIdentity


def current_lease_identity() -> LeaseIdentity:
    """Fail closed when Linux host identity cannot be read exactly."""

    machine = Path("/etc/machine-id").read_text().strip()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    namespace = str(Path("/proc/self/ns/pid").stat().st_ino)
    pid = os.getpid()
    stat = Path(f"/proc/{pid}/stat").read_text()
    start = _starttime(stat)
    if not machine or not boot or start is None:
        raise OSError("lease host identity unavailable")
    identity = LeaseIdentity(machine, boot, namespace, pid, start)
    identity.validate()
    return identity


def _starttime(raw: str) -> int | None:
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 1 :].split()
    if len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdecimal():
        return None
    return int(fields[19])


def process_gone_on_lease_host(
    identity: LeaseIdentity,
    *,
    machine_id: Path = Path("/etc/machine-id"),
    proc_root: Path = Path("/proc"),
) -> bool:
    """Return true only with same-host reboot or positive Linux /proc death evidence.

    Missing permissions, another machine, a stopped process, and incomplete
    namespace visibility all return false. Path overrides exist for disposable
    test filesystems; production calls use the fixed defaults.
    """

    identity.validate()
    try:
        if machine_id.read_text().strip() != identity.machine_id:
            return False
        current_boot = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
        if not current_boot:
            return False
        if current_boot != identity.boot_id:
            return True
        namespace = str((proc_root / "self/ns/pid").stat().st_ino)
        if namespace != identity.pid_ns:
            # This process cannot prove host-wide namespace disappearance.
            # A dedicated host-visible witness is required before T14.
            return False
        stat_path = proc_root / str(identity.pid) / "stat"
        try:
            current_start = _starttime(stat_path.read_text())
        except FileNotFoundError:
            # A vanished /proc/PID on the same boot and namespace is proof.
            return True
        return current_start is not None and current_start != identity.process_start
    except (OSError, ValueError, UnicodeError):
        return False


def lease_identity_from_row(row: dict[str, object]) -> LeaseIdentity:
    values = (
        row.get("lease_machine_id"),
        row.get("lease_boot_id"),
        row.get("lease_pid_ns"),
        row.get("lease_pid"),
        row.get("lease_process_start"),
    )
    if any(value is None for value in values):
        raise ValueError("lease identity absent")
    machine, boot, namespace, pid, start = values
    if not all(
        type(value) is str and re.fullmatch(r"[^\s]{1,256}", value)
        for value in (machine, boot, namespace)
    ):
        raise ValueError("lease identity invalid")
    if type(pid) is not int or type(start) is not int:
        raise ValueError("lease identity invalid")
    return LeaseIdentity(machine, boot, namespace, pid, start)
