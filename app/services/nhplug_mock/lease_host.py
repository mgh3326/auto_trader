"""Local lease-host process witness for manual NHPLUG mock release."""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.services.nhplug_mock.ledger import LeaseIdentity

# Linux PROC_PID_INIT_INO: the inode of the initial (host) PID namespace.
# Only a scanner in this namespace sees every PID namespace on the host.
INIT_PID_NS = str(0xEFFFFFFC)

_MACHINE_ID = re.compile(r"[0-9a-f]{32}")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PID_NS_LINK = re.compile(r"pid:\[([1-9][0-9]{0,19})\]")


def current_lease_identity() -> LeaseIdentity:
    """Fail closed when Linux host identity cannot be read exactly."""

    machine = Path("/etc/machine-id").read_text().strip()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    # machine-id(5) and boot_id formats; other content can never be witnessed.
    if not _MACHINE_ID.fullmatch(machine) or not _BOOT_ID.fullmatch(boot):
        raise OSError("lease host identity unavailable")
    namespace = _pid_ns(Path("/proc/self/ns/pid"))
    pid = os.getpid()
    stat = Path(f"/proc/{pid}/stat").read_text()
    start = _starttime(stat)
    if start is None:
        raise OSError("lease host identity unavailable")
    identity = LeaseIdentity(machine, boot, namespace, pid, start)
    identity.validate()
    return identity


def _pid_ns(link: Path) -> str:
    """Read a pid namespace link; anything but an exact nsfs link raises."""

    match = _PID_NS_LINK.fullmatch(os.readlink(link))
    if match is None:
        raise ValueError("not a pid namespace link")
    return match.group(1)


def _starttime(raw: str) -> int | None:
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 1 :].split()
    if len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdecimal():
        return None
    return int(fields[19])


def host_pid_namespaces(
    *, proc_root: Path = Path("/proc"), expected_host_pid_ns: str | None = None
) -> frozenset[str] | None:
    """Every PID namespace in use on the host, or None when not provable.

    The scanner must itself run in the initial PID namespace, which must equal
    the inode measured on the host outside any container, and /proc/1 must be
    in it. Every PID entry must then yield an exact namespace link. One
    unreadable, malformed or permission-denied entry makes the whole scan
    unverifiable; only a PID directory that vanished during the scan is skipped.
    """

    try:
        own = _pid_ns(proc_root / "self/ns/pid")
        if (
            own != INIT_PID_NS
            or expected_host_pid_ns != own
            or _pid_ns(proc_root / "1/ns/pid") != own
        ):
            return None
        seen = {own}
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                seen.add(_pid_ns(entry / "ns/pid"))
            except FileNotFoundError:
                if entry.exists():
                    return None  # Live entry without a readable link.
    except (OSError, ValueError, UnicodeError):
        return None
    return frozenset(seen)


def process_gone_on_lease_host(
    identity: LeaseIdentity,
    *,
    machine_id: Path = Path("/etc/machine-id"),
    proc_root: Path = Path("/proc"),
    expected_host_pid_ns: str | None = None,
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
        namespace = _pid_ns(proc_root / "self/ns/pid")
        if namespace != identity.pid_ns:
            # Case (c): only a scanner proven to see the host PID namespace,
            # with every namespace link readable, may prove disappearance.
            visible = host_pid_namespaces(
                proc_root=proc_root, expected_host_pid_ns=expected_host_pid_ns
            )
            return visible is not None and identity.pid_ns not in visible
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
