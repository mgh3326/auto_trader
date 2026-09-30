"""Read-only T14 lease-host process witness; never releases a reservation.

The wrapper runs this in a one-shot container sharing the host PID namespace.
Both answers fail closed: visibility is verified, and death is proven, only
after this process has read every host PID namespace link itself. A private
(sibling container) namespace, a namespace inode that differs from the one
measured on the host, or a single unreadable link reports not verified and
not proven. A positive result does not itself release any reservation.
"""

from __future__ import annotations

import argparse

from app.services.nhplug_mock.lease_host import (
    current_lease_identity,
    host_pid_namespaces,
    process_gone_on_lease_host,
)
from app.services.nhplug_mock.ledger import LeaseIdentity, LedgerConflict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-pid-ns", required=True)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--machine-id")
    parser.add_argument("--boot-id")
    parser.add_argument("--pid-ns")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process-start", type=int)
    args = parser.parse_args(argv)
    try:
        current_lease_identity()  # machine-id must be mounted and exact.
        if host_pid_namespaces(expected_host_pid_ns=args.host_pid_ns) is None:
            print("host_pid_visibility_unverified")
            return 1
        if args.self_check:
            print("host_pid_visibility_verified")
            return 0
        identity = LeaseIdentity(
            args.machine_id, args.boot_id, args.pid_ns, args.pid, args.process_start
        )
        identity.validate()
        if process_gone_on_lease_host(identity, expected_host_pid_ns=args.host_pid_ns):
            print("lease_process_gone_proven")
            return 0
    except (OSError, ValueError, TypeError, LedgerConflict):
        if args.self_check:
            print("host_pid_visibility_unverified")
            return 1
    print("lease_process_gone_not_proven")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
