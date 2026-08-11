"""B0-X kis_mock (KR) cycle runner — scheduleless manual kickoff only.

    # Preview: derive + plan, dispatch nothing. Safe outside session too (the
    # RTH gate just produces a zero-order cycle with a recorded reason).
    uv run python -m scripts.run_b0x_kr_cycle

    # Determinism proof: derive twice, compare bytes (no writes, no venue).
    uv run python -m scripts.run_b0x_kr_cycle --derivation-only --repeat 2

    # One manually requested mock acceptance attempt.  It is default-disabled,
    # is bounded to one submission, and fails closed unless the KR env gate,
    # writer lease, and NW-B4 preflight all pass during KRX RTH.
    uv run python -m scripts.run_b0x_kr_cycle --confirm

``--confirm`` is not a status change: KR remains
``OBSERVATION_DERIVATION_ONLY`` until an upstream decision says otherwise.
It cannot be combined with ``--now`` or ``--derivation-only``, so an operator
cannot replay an artificial session timestamp into the mutation path. There is
also no flag that can move an envelope value — the §4 caps live in
``scripts.b0x.envelope`` as module constants, re-asserted before every account
read.

No scheduler registration exists for this script — not TaskIQ, not Prefect,
not launchd. A cycle happens because an operator ran it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from scripts.b0x.envelope import load_envelope
from scripts.b0x.kr.cycle import MARKET, KrCycleOutcome, run_kr_cycle
from scripts.b0x.ledger import DEFAULT_OBSERVATION_DIR, WriterLockUnavailable
from scripts.b0x.table_source import DEFAULT_TABLE_DIR


def _print_outcome(outcome: KrCycleOutcome) -> None:
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


async def _derivation_only(args: argparse.Namespace) -> int:
    """Derive without acting — the byte-determinism check.

    Reads the table and a fresh kis_mock account snapshot, then re-derives
    ``args.repeat`` times against the *same* in-memory state and compares
    hashes. Writes nothing and submits nothing.
    """

    from scripts.b0x import kill_switch as kill_switch_module
    from scripts.b0x.derivation import derive_orders
    from scripts.b0x.kr import attribution as kr_attribution
    from scripts.b0x.kr import mock as kr_mock
    from scripts.b0x.kr import pending_ledger as kr_pending_ledger
    from scripts.b0x.kr.cycle import broker_state
    from scripts.b0x.kr.cycle import scoped_positions as kr_cycle_scope
    from scripts.b0x.table_source import TableUnavailable, load_policy_table

    now = dt.datetime.now(dt.UTC) if args.now is None else args.now
    envelope = load_envelope(MARKET)
    table = load_policy_table(
        market=MARKET, now=now, table_dir=Path(args.table_dir).expanduser()
    )
    if isinstance(table, TableUnavailable):
        print(f"ZERO ORDERS — reason={table.reason} detail={table.detail}")
        return 0

    client = kr_mock.ReadOnlyKISMockDomesticClient()
    try:
        fresh = await kr_mock.read_fresh_truth(client)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()

    # Contract v1.5 ①: account state is the fresh broker read and nothing else
    # — there is no lane state file to load, and re-introducing one would put
    # the per-cycle-reset defect straight back. Contract v1.6 ① adds exactly
    # one non-broker input, 자기 미체결, from the submission ledger; a failed
    # read of it returns the same PendingUnreadable state, not an empty book.
    own_pending = await kr_pending_ledger.read_own_pending(
        now=now, correlation_prefix=f"{kr_mock.CLIENT_ORDER_ID_PREFIX}-"
    )
    # §36차 2항: legacy 보유(B0-X 가 만들지 않은 것)를 자기 원장 fill 귀속으로
    # 갈라 낸다. 읽기 실패는 「자기 것 없음」이 아니라 tri-state 로 떨어지고,
    # 그 상태에서는 자기 포지션 0 + §4 상한 입력 = 계좌 전체가 된다.
    attribution = await kr_attribution.read_own_attribution(
        correlation_prefix=f"{kr_mock.CLIENT_ORDER_ID_PREFIX}-"
    )
    state = broker_state(fresh=fresh, own_pending=own_pending, attribution=attribution)

    hashes: list[str] = []
    result = None
    for _ in range(max(args.repeat, 1)):
        decision = kill_switch_module.evaluate(state=state, envelope=envelope)
        result = derive_orders(
            table=table,
            state=state,
            envelope=envelope,
            kill_switch=decision,
            lane_universe=None,
            apply_envelope=True,
        )
        hashes.append(result.derivation_hash())

    scoped = kr_cycle_scope(fresh=fresh, attribution=attribution)
    print(f"policy_table_hash={table.policy_table_hash}")
    print(f"account_state_hash={state.state_hash()}")
    print(
        f"attribution_readable={scoped.unreadable is None} "
        f"own_positions={len(scoped.own_positions)} "
        f"legacy_positions={len(scoped.legacy_symbols)} "
        f"cap_basis={scoped.cap_basis}"
    )
    for index, digest in enumerate(hashes):
        print(f"run[{index}] derivation_hash={digest}")
    identical = len(set(hashes)) == 1
    print(f"DERIVATION_DETERMINISM={'IDENTICAL' if identical else 'DIVERGED'}")
    if args.json and result is not None:
        print(json.dumps(result.canonical(), sort_keys=True, ensure_ascii=False))
    return 0 if identical else 1


async def _run(args: argparse.Namespace) -> int:
    if args.confirm and args.derivation_only:
        print("--confirm cannot be combined with --derivation-only", file=sys.stderr)
        return 2
    if args.confirm and args.now is not None:
        print("--confirm cannot be combined with --now", file=sys.stderr)
        return 2

    now = dt.datetime.now(dt.UTC) if args.now is None else args.now

    if args.derivation_only:
        return await _derivation_only(args)

    try:
        outcome = await run_kr_cycle(
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
        "--table-dir",
        default=str(DEFAULT_TABLE_DIR),
        help="where scripts/build_policy_table.py wrote latest-kr.json (read-only)",
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
            "derive and print hashes; write nothing, contact no venue "
            "(still reads a fresh kis_mock account snapshot for account state)"
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "one manual kis_mock acceptance attempt; default-disabled and "
            "fail-closed on every preflight gate (KRX RTH only)"
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
