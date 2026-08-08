"""B0-X crypto cycle runner — manual kickoff only (contract §5, v1).

    # 본선: Upbit shadow-sim. Zero real orders on any venue, ever.
    uv run python -m scripts.run_b0x_cycle --lane shadow

    # 사이드카: Binance Spot Demo. Read-only + dry-run by default.
    B0X_SIDECAR_ENABLED=true BINANCE_SPOT_DEMO_ENABLED=true \\
        uv run python -m scripts.run_b0x_cycle --lane sidecar

    # Same, actually dispatching Demo orders (operator gate).
    ... uv run python -m scripts.run_b0x_cycle --lane sidecar --confirm

    # Determinism proof: derive twice, compare bytes (no writes, no venue).
    uv run python -m scripts.run_b0x_cycle --lane shadow --derivation-only --repeat 2

There is deliberately **no flag that changes an envelope value**. The §4 caps
live in ``scripts.b0x.envelope`` as module constants and are re-asserted before
every network call; see ``tests/scripts/b0x/test_envelope_locked.py``, which
proves no CLI dest and no environment variable can move them.

No scheduler registration exists for this script — not TaskIQ, not Prefect, not
launchd. A cycle happens because an operator ran it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from scripts.b0x.cycle import (
    CycleOutcome,
    run_shadow_cycle,
    run_sidecar_cycle,
)
from scripts.b0x.envelope import load_envelope
from scripts.b0x.ledger import DEFAULT_OBSERVATION_DIR, WriterLockUnavailable
from scripts.b0x.table_source import DEFAULT_TABLE_DIR

MARKET = "crypto"


def _print_outcome(outcome: CycleOutcome) -> None:
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
    if outcome.contaminated:
        print("CONTAMINATED — venue state not created by B0-X (submission blocked)")
    if outcome.artifact_path:
        print(f"artifact={outcome.artifact_path}")


async def _derivation_only(args: argparse.Namespace) -> int:
    """Derive without acting — the byte-determinism check.

    Runs the shadow lane's pure path: load the table, build the account state
    from the persisted virtual portfolio, derive. Writes nothing and contacts
    no venue.
    """

    import datetime as _dt

    from scripts.b0x import kill_switch as kill_switch_module
    from scripts.b0x.crypto import shadow as shadow_lane
    from scripts.b0x.derivation import derive_orders
    from scripts.b0x.ledger import ObservationLedger, load_json_state
    from scripts.b0x.table_source import TableUnavailable, load_policy_table

    now = _dt.datetime.now(_dt.UTC) if args.now is None else args.now
    envelope = load_envelope(MARKET)
    table = load_policy_table(
        market=MARKET, now=now, table_dir=Path(args.table_dir).expanduser()
    )
    if isinstance(table, TableUnavailable):
        print(f"ZERO ORDERS — reason={table.reason} detail={table.detail}")
        return 0

    ledger = ObservationLedger(
        lane=shadow_lane.LANE, root=Path(args.out_dir).expanduser()
    )
    stored = load_json_state(ledger.lane_dir / "portfolio.json")
    portfolio = (
        shadow_lane.VirtualPortfolio.from_json(stored)
        if stored
        else shadow_lane.VirtualPortfolio.seed(now=now)
    )
    state = portfolio.account_state()

    hashes: list[str] = []
    for _ in range(max(args.repeat, 1)):
        decision = kill_switch_module.evaluate(state=state, envelope=envelope)
        result = derive_orders(
            table=table,
            state=state,
            envelope=envelope,
            kill_switch=decision,
            lane_universe=None,
            apply_envelope=False,
        )
        hashes.append(result.derivation_hash())

    print(f"policy_table_hash={table.policy_table_hash}")
    print(f"account_state_hash={state.state_hash()}")
    for index, digest in enumerate(hashes):
        print(f"run[{index}] derivation_hash={digest}")
    identical = len(set(hashes)) == 1
    print(f"DERIVATION_DETERMINISM={'IDENTICAL' if identical else 'DIVERGED'}")
    if args.json:
        print(json.dumps(result.canonical(), sort_keys=True, ensure_ascii=False))
    return 0 if identical else 1


async def _run(args: argparse.Namespace) -> int:
    now = dt.datetime.now(dt.UTC) if args.now is None else args.now

    if args.derivation_only:
        return await _derivation_only(args)

    try:
        if args.lane == "shadow":
            outcome = await run_shadow_cycle(
                now=now,
                table_dir=Path(args.table_dir).expanduser(),
                out_dir=Path(args.out_dir).expanduser(),
            )
        else:
            outcome = await run_sidecar_cycle(
                now=now,
                table_dir=Path(args.table_dir).expanduser(),
                out_dir=Path(args.out_dir).expanduser(),
                confirm=args.confirm,
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


def _iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--now must be timezone-aware ISO8601")
    return parsed.astimezone(dt.UTC)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane",
        choices=["shadow", "sidecar"],
        default="shadow",
        help="shadow = Upbit synthetic 본선 (no venue); sidecar = Binance Spot Demo",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "sidecar only: pass confirm=True to the ROB-298 client so mutations "
            "actually dispatch. Without it, zero mutation HTTP is sent."
        ),
    )
    parser.add_argument(
        "--table-dir",
        default=str(DEFAULT_TABLE_DIR),
        help="where scripts/build_policy_table.py wrote latest-crypto.json (read-only)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OBSERVATION_DIR),
        help="observation ledger + artifact root",
    )
    parser.add_argument(
        "--derivation-only",
        action="store_true",
        help=(
            "derive and print hashes; write nothing, contact no venue. Always "
            "runs the shadow lane's pure path (--lane is ignored) because the "
            "sidecar's account state requires a venue read."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="--derivation-only: how many times to re-derive (determinism check)",
    )
    parser.add_argument(
        "--now",
        type=_iso,
        default=None,
        help="override the cycle clock (tests/replay); must be tz-aware ISO8601",
    )
    parser.add_argument("--json", action="store_true", help="also emit the raw record")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
