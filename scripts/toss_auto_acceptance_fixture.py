"""Run only the no-network TOSS-AUTO-FULL acceptance fixtures.

This is deliberately *not* a live smoke command.  It has no broker imports,
no account reads, and no mutation mode.  The separate operator runbook owns
the two live acceptance procedures after independent verification.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence

_OFFLINE_TESTS = (
    "tests/services/order_proposals/test_toss_auto_acceptance.py",
    "tests/services/order_proposals/test_telegram_callback.py",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline-fixture",
        action="store_true",
        help="run the injected, no-network acceptance fixtures",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.offline_fixture:
        print(
            "Refusing to run: this command has no live mode. "
            "Pass --offline-fixture for the no-network test fixtures."
        )
        return 2
    command = [sys.executable, "-m", "pytest", "-q", *_OFFLINE_TESTS]
    print("Running offline-only TOSS-AUTO-FULL fixtures; no broker/account calls.")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
