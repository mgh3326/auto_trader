"""B0-X kiwoom_mock (KR) cycle runner — scheduleless manual kickoff only.

    # Preview: derive + plan, dispatch nothing.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle

    # Read-only preflight probe: account truth + 귀속 + broker pending, no plan.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle --readiness

    # One manually requested mock acceptance round trip. Default-disabled,
    # bounded to one submission, and it ALWAYS cancels what it sent.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle --confirm

    # Explicitly selected ORDERING DAY-order path. It is never the default and
    # requires the same per-call confirmation gate.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle --ordering --confirm

    # Registered-seal bounded owner. The seal is an exact four-field JSON
    # object and the ports factory is reviewed in-process code. This path still
    # requires the per-call confirmation gate and is never selected by default.
    uv run python -m scripts.run_b0x_kr_kiwoom_cycle \
      --bounded-send --seal /operator/path/seal.json \
      --durable-ports-factory package.module:build_ports --ordering --confirm

``--confirm`` alone is ``ACCEPTANCE_ONLY``: one submit followed by broker-proven
cancel. ``--ordering --confirm`` is ``ORDERING``: it submits eligible
envelope-derived DAY orders and deliberately does not auto-cancel them. Its
separate runtime path re-reads pending and same-day foreign broker trace before
every mutation, checks its account writer lease, and refuses an unreadable
realized-P&L input. Both paths are default-disabled, cannot be combined with
``--now``, and expose no envelope override. The §4 caps are module constants in
``scripts.b0x.envelope``, re-asserted before every read.

``--bounded-send`` only selects the registered-seal coordination factory. It
requires ``--confirm``, an exact seal JSON file, and an explicitly nominated
durable-ports factory. The CLI performs a structural snapshot check only;
``build_bounded_send_kiwoom_coordination_factory`` snapshots again, checks
registry/currentness on invocation, and alone owns the one-shot consumption.
Without the flag the production grant-only factory remains the selected path.

🔴 There is no ``--no-cancel`` for acceptance. The cancellation it requires
must be broker-proven or exits ``2``; that safe acceptance behavior is not
silently transformed into ORDERING.

No scheduler registration exists for this script — not TaskIQ, not Prefect,
not launchd. A cycle happens because an operator ran it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

from scripts.b0x.ledger import DEFAULT_OBSERVATION_DIR, WriterLockUnavailable
from scripts.b0x.table_source import DEFAULT_TABLE_DIR


class BoundedSendCliConfigurationError(RuntimeError):
    """Closed, secret-free refusal for malformed bounded-send CLI inputs."""


def _load_bounded_send_seal(path_value: str) -> dict[str, object]:
    """Load a seal object without validating or consuming its authority."""

    path = Path(path_value).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise BoundedSendCliConfigurationError(
            "bounded_send_seal_file_unavailable"
        ) from None
    except json.JSONDecodeError:
        raise BoundedSendCliConfigurationError(
            "bounded_send_seal_file_invalid"
        ) from None
    if type(payload) is not dict:
        raise BoundedSendCliConfigurationError("bounded_send_seal_file_invalid")
    return payload


def _load_durable_ports_factory(reference: str) -> Callable[..., object]:
    """Resolve one explicit ``module:callable`` reviewed by the operator."""

    module_name, separator, attribute_name = reference.partition(":")
    if (
        separator != ":"
        or not module_name
        or not attribute_name
        or ":" in attribute_name
    ):
        raise BoundedSendCliConfigurationError(
            "bounded_send_ports_factory_reference_invalid"
        )
    try:
        module = importlib.import_module(module_name)
    except (ImportError, ValueError):
        raise BoundedSendCliConfigurationError(
            "bounded_send_ports_factory_unavailable"
        ) from None
    candidate = vars(module).get(attribute_name)
    if not callable(candidate):
        raise BoundedSendCliConfigurationError("bounded_send_ports_factory_unavailable")
    return candidate


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
    if args.ordering and not args.confirm:
        print("--ordering requires --confirm", file=sys.stderr)
        return 2
    if args.bounded_send and not args.confirm:
        print("--bounded-send requires --confirm", file=sys.stderr)
        return 2
    bounded_inputs_present = (
        args.seal is not None or args.durable_ports_factory is not None
    )
    if not args.bounded_send and bounded_inputs_present:
        print(
            "--seal/--durable-ports-factory require --bounded-send",
            file=sys.stderr,
        )
        return 2
    if args.bounded_send and (args.seal is None or args.durable_ports_factory is None):
        print(
            "--bounded-send requires --seal and --durable-ports-factory",
            file=sys.stderr,
        )
        return 2
    if args.confirm and args.now is not None:
        print("--confirm cannot be combined with --now", file=sys.stderr)
        return 2
    if args.confirm and args.readiness:
        print("--confirm cannot be combined with --readiness", file=sys.stderr)
        return 2

    if args.readiness:
        return await _readiness(args)

    from scripts.b0x.kr.kiwoom_bounded_send import (
        KiwoomBoundedSendSealRejected,
        snapshot_bounded_send_seal,
    )
    from scripts.b0x.kr.kiwoom_coordination import (
        KIWOOM_KR_LANE_ID,
        KiwoomCoordinationOwnerRejected,
        build_bounded_send_kiwoom_coordination_factory,
        production_kiwoom_coordination_factory,
        resolve_kiwoom_lane_entry,
    )
    from scripts.b0x.kr.kiwoom_cycle import run_kiwoom_cycle

    now = dt.datetime.now(dt.UTC) if args.now is None else args.now
    coordination_entry = resolve_kiwoom_lane_entry(KIWOOM_KR_LANE_ID)
    if args.bounded_send:
        try:
            seal = snapshot_bounded_send_seal(
                _load_bounded_send_seal(args.seal)
            ).canonical()
            ports_factory = _load_durable_ports_factory(args.durable_ports_factory)
            coordination_factory = build_bounded_send_kiwoom_coordination_factory(
                seal=seal,
                ports_factory=ports_factory,  # type: ignore[arg-type]
            )
        except BoundedSendCliConfigurationError as exc:
            print(f"BOUNDED_SEND_CONFIGURATION_INVALID — {exc}", file=sys.stderr)
            return 2
        except (
            KiwoomBoundedSendSealRejected,
            KiwoomCoordinationOwnerRejected,
        ) as exc:
            print(
                f"BOUNDED_SEND_CONFIGURATION_INVALID — {exc.code}",
                file=sys.stderr,
            )
            return 2
    else:
        coordination_factory = production_kiwoom_coordination_factory()
    try:
        outcome = await run_kiwoom_cycle(
            now=now,
            table_dir=Path(args.table_dir).expanduser(),
            out_dir=Path(args.out_dir).expanduser(),
            confirm=args.confirm,
            ordering=args.ordering,
            coordination_factory=coordination_factory,
            coordination_entry=coordination_entry,
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
        "--ordering",
        action="store_true",
        help=(
            "select ORDERING DAY-order retention; requires --confirm. "
            "Without this flag, --confirm remains the one-order acceptance cancel"
        ),
    )
    parser.add_argument(
        "--bounded-send",
        action="store_true",
        help=(
            "select the registered-seal coordination factory; requires --confirm, "
            "--seal, and --durable-ports-factory"
        ),
    )
    parser.add_argument(
        "--seal",
        default=None,
        metavar="JSON_PATH",
        help="exact four-field bounded-send seal JSON (loaded but consumed by factory)",
    )
    parser.add_argument(
        "--durable-ports-factory",
        default=None,
        metavar="MODULE:CALLABLE",
        help="reviewed in-process factory returning exact durable Kiwoom ports",
    )
    parser.add_argument("--now", type=_iso, default=None, help="override cycle time")
    parser.add_argument("--json", action="store_true", help="print the full record")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
