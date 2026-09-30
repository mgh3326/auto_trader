"""Read-only T14 lease-host process witness; never releases a reservation.

The wrapper runs this in a one-shot container with host PID visibility. The
operator records a positive result with the separate T14 authorization.
"""

from __future__ import annotations

import argparse

from app.services.nhplug_mock.lease_host import (
    current_lease_identity,
    process_gone_on_lease_host,
)
from app.services.nhplug_mock.ledger import LeaseIdentity, LedgerConflict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-pid-ns", required=True)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--machine-id")
    parser.add_argument("--boot-id")
    parser.add_argument("--pid-ns")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--process-start", type=int)
    args = parser.parse_args()
    try:
        current = current_lease_identity()
        if args.host_pid_ns != current.pid_ns:
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
        pass
    print("lease_process_gone_not_proven")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
