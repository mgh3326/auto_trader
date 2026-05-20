# scripts/rob278_kr_dryrun.py
"""ROB-278 — KR snapshot-backed report dry-run.

Drives ``SnapshotBackedReportGenerator`` end-to-end against the local /
dev DB with ``auto_emit_from_evidence=True``, prints the generated
``items`` and their ``operation`` / ``apply_policy`` / ``evidence_snapshot``,
and asserts that **no rows were inserted into any broker / order / watch /
order-intent mutation table** during the run.

Safety
------
* Read-only KIS calls only — the symbol/portfolio collectors go through
  the existing ``KISHomeReader`` / ``inquire_price`` / ``inquire_orderbook``
  surfaces, never the order client.
* The entire generator run is wrapped in a single ``AsyncSessionLocal``
  transaction that is **rolled back** at the end (unless ``--commit`` is
  passed). The dry-run also reads pre/post row counts on a handful of
  mutation-watched tables and aborts (exit 2) if any of them grew.
* No watch activation, no order placement / cancel / modify, no
  scheduler registration.

Usage
-----
    uv run python -m scripts.rob278_kr_dryrun --dry-run --user-id 1
    uv run python -m scripts.rob278_kr_dryrun --dry-run --user-id 1 --no-auto-emit
    uv run python -m scripts.rob278_kr_dryrun --commit --user-id 1  # NOT recommended
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)
from app.services.action_report.snapshot_backed.request import ReportGenerationRequest

# Tables guarded against any row-count growth during the dry run. These
# are the broker/order/watch/order-intent surfaces the lockdown forbids
# mutating from the report-generation path. ``schema.table`` form is
# accepted by Postgres directly via ``information_schema.tables`` lookups.
_MUTATION_GUARDED_TABLES: tuple[str, ...] = (
    "review.investment_watch_alerts",
    "review.investment_watch_events",
    "review.investment_report_item_decisions",
    "pending_orders",
    "order_preview_session",
    "order_preview_leg",
    "order_execution_request",
)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def _table_exists(session, qualified_name: str) -> bool:
    if "." in qualified_name:
        schema, table = qualified_name.split(".", 1)
    else:
        schema, table = "public", qualified_name
    row = await session.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": schema, "table": table},
    )
    return row.scalar() is not None


async def _count_rows(session, qualified_name: str) -> int:
    if "." in qualified_name:
        schema, table = qualified_name.split(".", 1)
        ref = f'"{schema}"."{table}"'
    else:
        ref = f'"{qualified_name}"'
    row = await session.execute(sa.text(f"SELECT count(*) FROM {ref}"))
    return int(row.scalar() or 0)


async def _snapshot_mutation_counts(session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in _MUTATION_GUARDED_TABLES:
        try:
            if not await _table_exists(session, name):
                continue
            counts[name] = await _count_rows(session, name)
        except Exception as exc:  # noqa: BLE001 — defensive
            print(f"  [warn] could not count rows on {name}: {exc}")
    return counts


def _summarise_items(items) -> None:
    if not items:
        print("  (no items)")
        return
    for idx, item in enumerate(items):
        ev = item.evidence_snapshot or {}
        proposer = ev.get("proposer") if isinstance(ev, dict) else None
        snap_uuid = ev.get("snapshot_uuid") if isinstance(ev, dict) else None
        print(
            f"  [{idx}] kind={item.item_kind} side={item.side} symbol={item.symbol} "
            f"operation={item.operation} apply_policy={item.apply_policy} "
            f"intent={item.intent} proposer={proposer} "
            f"evidence_snapshot_uuid={snap_uuid}"
        )


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        print("=== ROB-278 KR snapshot-backed report dry-run ===")
        print("Pre-flight: snapshotting mutation-guarded row counts ...")
        before = await _snapshot_mutation_counts(session)
        for table, count in before.items():
            print(f"  before {table:50s} {count}")

        request = ReportGenerationRequest(
            market="kr",
            account_scope="kis_live",
            status="draft",  # never publish from the dry-run
            requested_by="claude_code",
            created_by_profile=args.profile,
            title=f"ROB-278 KR dry-run {dt.datetime.now(tz=dt.UTC).isoformat()}",
            summary="dry-run; do not publish",
            kst_date=dt.date.today().isoformat(),
            user_id=args.user_id,
            auto_emit_from_evidence=not args.no_auto_emit,
        )
        print(
            f"\nGenerating report (auto_emit={request.auto_emit_from_evidence}, "
            f"user_id={request.user_id}) ..."
        )
        generator = SnapshotBackedReportGenerator(session)
        try:
            response = await generator.generate(request)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[ERROR] generator failed: {type(exc).__name__}: {exc}")
            await session.rollback()
            return 3

        print(f"\nReport UUID: {response.report_uuid}")
        print(f"Bundle UUID: {response.snapshot_bundle_uuid}")
        print(f"Bundle status: {response.bundle_status}")
        print(f"Bundle reused: {response.bundle_reused}")
        print(f"Items count:  {response.items_count}")
        print(
            f"Overall freshness: {response.snapshot_freshness_summary.get('overall')}"
        )
        if response.warnings:
            print("Warnings:")
            for w in response.warnings:
                print(f"  - {w}")

        # Read items back to print full provenance.
        item_rows = (
            await session.execute(
                sa.text(
                    "SELECT item_kind, side, symbol, operation, apply_policy, "
                    "intent, evidence_snapshot FROM review.investment_report_items "
                    "WHERE report_id IN (SELECT id FROM review.investment_reports "
                    "WHERE report_uuid = :uuid) ORDER BY id"
                ),
                {"uuid": response.report_uuid},
            )
        ).all()
        print("\nGenerated items (review.investment_report_items):")
        if not item_rows:
            print("  (none persisted)")
        for row in item_rows:
            print(
                "  - "
                f"kind={row.item_kind} side={row.side} symbol={row.symbol} "
                f"operation={row.operation} apply_policy={row.apply_policy} "
                f"intent={row.intent}"
            )

        print("\nPost-flight: re-snapshotting mutation-guarded row counts ...")
        after = await _snapshot_mutation_counts(session)
        for table, count in after.items():
            delta = count - before.get(table, count)
            marker = "" if delta == 0 else " <<< CHANGED"
            print(f"  after  {table:50s} {count}  (Δ={delta}){marker}")

        deltas = {t: after[t] - before[t] for t in after if t in before}
        bad = {t: d for t, d in deltas.items() if d != 0}
        if bad:
            print("\n[FAIL] mutation-guarded tables changed during the dry-run:")
            for t, d in bad.items():
                print(f"  {t}: Δ={d}")
            await session.rollback()
            return 2

        if args.commit:
            print("\nCommitting transaction (--commit) ...")
            await session.commit()
        else:
            print("\nDry-run: rolling back transaction.")
            await session.rollback()

        print("Done.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true", help="Roll back at the end (default)"
    )
    group.add_argument(
        "--commit", action="store_true", help="Commit the report (NOT recommended)"
    )
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Operator user_id for KIS read-only calls (no implicit default)",
    )
    parser.add_argument(
        "--profile",
        default="rob278-dryrun",
        help="created_by_profile string (default: rob278-dryrun)",
    )
    parser.add_argument(
        "--no-auto-emit",
        action="store_true",
        help="Skip the deterministic evidence-driven auto-emitter",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
