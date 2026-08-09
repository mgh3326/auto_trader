"""B0-X US (``alpaca_paper_lab``) manual observation runner.

The runner is deliberately scheduleless and exposes no ``--confirm`` option.
It may derive/plan during US RTH after a fresh lab-only read, but it never
invokes Alpaca preview, submit, or cancellation in this command path.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from scripts.b0x.ledger import DEFAULT_OBSERVATION_DIR, WriterLockUnavailable
from scripts.b0x.table_source import DEFAULT_TABLE_DIR
from scripts.b0x.us.cycle import UsCycleOutcome, run_us_cycle


def _print_outcome(outcome: UsCycleOutcome) -> None:
    print(f"lane={outcome.lane} at={outcome.at.isoformat()}")
    if outcome.zero_order_reason:
        print(f"ZERO ORDERS — reason={outcome.zero_order_reason}")
    if outcome.table_hash:
        print(
            f"policy_table_hash={outcome.table_hash} age_s={outcome.table_age_seconds}"
        )
    if outcome.derivation is not None:
        print(
            f"cycle_id={outcome.derivation.cycle_id} "
            f"derivation_hash={outcome.derivation.derivation_hash()}"
        )
        print(
            f"orders={len(outcome.derivation.orders)} "
            f"skipped={len(outcome.derivation.skipped)} "
            f"kill_switch_tripped={outcome.derivation.kill_switch.tripped}"
        )
    if outcome.artifact_path:
        print(f"artifact={outcome.artifact_path}")


def _iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--now must be timezone-aware ISO8601")
    return parsed.astimezone(dt.UTC)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table-dir",
        default=str(DEFAULT_TABLE_DIR),
        help="where the separate US table builder wrote latest-us.json (read-only)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OBSERVATION_DIR),
        help="append-only B0-X observation artifact root",
    )
    parser.add_argument(
        "--now",
        type=_iso,
        default=None,
        help="override the cycle clock for replay/tests; must be timezone-aware",
    )
    parser.add_argument("--json", action="store_true", help="also emit the raw record")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    now = dt.datetime.now(dt.UTC) if args.now is None else args.now
    try:
        outcome = await run_us_cycle(
            now=now,
            table_dir=Path(args.table_dir).expanduser(),
            out_dir=Path(args.out_dir).expanduser(),
            confirm=False,
        )
    except WriterLockUnavailable as exc:
        print(f"WRITER_LOCK_UNAVAILABLE — {exc}", file=sys.stderr)
        return 2
    _print_outcome(outcome)
    if args.json:
        print(
            json.dumps(outcome.record, sort_keys=True, ensure_ascii=False, default=str)
        )
    return outcome.exit_code


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
