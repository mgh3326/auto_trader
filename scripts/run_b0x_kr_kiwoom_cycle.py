"""B0-X kiwoom_mock (KR) cycle runner — scheduleless manual kickoff only.

    # Preview: derive + plan, dispatch nothing.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle

    # Read-only preflight probe: account truth + 귀속 + broker pending, no plan.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle --readiness

    # One manually requested mock acceptance round trip. Default-disabled,
    # bounded to one submission, and it ALWAYS cancels what it sent.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle --confirm

    # Explicitly selected INTERIM DAY-order path. It is never the default and
    # requires the same per-call confirmation gate.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle --interim-ordering --confirm

``--confirm`` alone is ``ACCEPTANCE_ONLY``: one submit followed by broker-proven
cancel. ``--interim-ordering --confirm`` is ``INTERIM_ORDERING``: it submits
every envelope-derived DAY order and deliberately does not auto-cancel. Both
are default-disabled, cannot be combined with ``--now``, and expose no envelope
override. The §4 caps are module constants in ``scripts.b0x.envelope``,
re-asserted before every read.

🔴 There is no ``--no-cancel`` for acceptance. The cancellation it requires
must be broker-proven or exits ``2``; that safe acceptance behavior is not
silently transformed into the interim mode.

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

from scripts.b0x.ledger import DEFAULT_OBSERVATION_DIR, WriterLockUnavailable
from scripts.b0x.table_source import DEFAULT_TABLE_DIR


def _print_outcome(outcome) -> None:  # noqa: ANN001 — local formatting helper
    print(f"lane={outcome.lane} at={outcome.at.isoformat()}")
    if outcome.zero_order_reason:
        print(f"ZERO ORDERS — reason={outcome.zero_order_reason}")
        detail = outcome.record.get("zero_order_detail")
        if detail:
            print(f"  detail: {detail}")
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
    for trip in outcome.record.get("round_trip") or []:
        print(
            f"ROUND_TRIP symbol={trip['symbol']} order_no={trip['order_no']} "
            f"qty={trip['quantity']} price={trip['price']} "
            f"notional_krw={trip['notional_krw']} "
            f"observed_resting={trip['observed_resting']} "
            f"cancel_confirmed={trip['cancel_confirmed']} "
            f"complete={trip['round_trip_complete']}"
        )
    for failure in outcome.record.get("round_trip_failures") or []:
        print(f"ROUND_TRIP_FAILURE — {failure}", file=sys.stderr)
    for order in outcome.record.get("day_orders") or []:
        print(
            f"DAY_ORDER symbol={order['symbol']} order_no={order['order_no']} "
            f"side={order['side']} qty={order['quantity']} "
            f"price={order['price']} automatic_cancel={order['automatic_cancel']}"
        )
    for failure in outcome.record.get("day_order_failures") or []:
        print(f"DAY_ORDER_FAILURE — {failure}", file=sys.stderr)
    if outcome.artifact_path:
        print(f"artifact={outcome.artifact_path}")


async def _readiness(args: argparse.Namespace) -> int:
    """Read-only probe: can this lane see what it needs to, right now?

    Touches the account with reads only — no plan, no derivation, no order.
    Exists so an operator can confirm the four evidence surfaces answer before
    arming ``--confirm`` inside a bounded RTH window.
    """

    from app.services.kis_mock_runner.session import is_krx_regular_session
    from scripts.b0x.kr import kiwoom as kiwoom_lane
    from scripts.b0x.kr import kiwoom_attribution as kiwoom_attr
    from scripts.b0x.kr import kiwoom_cycle

    kiwoom_lane.assert_kiwoom_lane_enabled()
    identity = kiwoom_lane.account_identity_summary()
    journal = kiwoom_attr.OwnOrderJournal.for_lane(
        root=Path(args.out_dir).expanduser(), lane=kiwoom_lane.LANE
    )
    account = kiwoom_lane.ReadOnlyKiwoomMockAccount()

    own_ids = journal.own_order_ids()
    fresh = await kiwoom_lane.read_fresh_truth(account)
    pending = await kiwoom_lane.read_broker_pending(account, own_order_ids=own_ids)
    attribution = await kiwoom_attr.read_own_attribution(
        journal=journal, read_order_detail=account.read_order_detail
    )
    scoped = kiwoom_cycle.scoped_positions(fresh=fresh, attribution=attribution)
    now = dt.datetime.now(dt.UTC)
    foreign = await kiwoom_cycle.foreign_same_day_orders(
        account, own_order_ids=own_ids, order_date=kiwoom_attr.kst_order_date(now)
    )

    payload = {
        "at": now.isoformat(),
        "account": identity,
        "krx_regular_session": is_krx_regular_session(now),
        "journal": {"path": str(journal.path), "own_order_count": len(own_ids)},
        "fresh_truth": fresh.status_only(pending),
        "attribution": scoped.canonical(),
        "foreign_same_day_orders": foreign.canonical(),
    }
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.interim_ordering and not args.confirm:
        print("--interim-ordering requires --confirm", file=sys.stderr)
        return 2
    if args.confirm and args.now is not None:
        print("--confirm cannot be combined with --now", file=sys.stderr)
        return 2
    if args.confirm and args.readiness:
        print("--confirm cannot be combined with --readiness", file=sys.stderr)
        return 2

    if args.readiness:
        return await _readiness(args)

    from scripts.b0x.kr.kiwoom_cycle import run_kiwoom_cycle

    now = dt.datetime.now(dt.UTC) if args.now is None else args.now
    try:
        outcome = await run_kiwoom_cycle(
            now=now,
            table_dir=Path(args.table_dir).expanduser(),
            out_dir=Path(args.out_dir).expanduser(),
            confirm=args.confirm,
            interim_ordering=args.interim_ordering,
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
        "--readiness",
        action="store_true",
        help="read-only evidence probe (account, 귀속, broker pending); no plan",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "arm the bounded acceptance round trip (submit 1 → verify resting → "
            "cancel → reconcile). Default-disabled; also needs B0X_KR_KIWOOM_ENABLED"
        ),
    )
    parser.add_argument(
        "--interim-ordering",
        action="store_true",
        help=(
            "select INTERIM_ORDERING DAY-order retention; requires --confirm. "
            "Without this flag, --confirm remains the one-order acceptance cancel"
        ),
    )
    parser.add_argument("--now", type=_iso, default=None, help="override cycle time")
    parser.add_argument("--json", action="store_true", help="print the full record")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
