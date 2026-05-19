# scripts/snapshot_bundle_smoke.py
"""ROB-269 Phase 1 dry-run smoke.

Local-only. Inserts a fake snapshot run + 2 snapshots + 1 bundle + 2 links
against the dev DB, prints the resulting UUIDs, then exits. Always uses
``requested_by='user'`` and ``policy_version='intraday_action_report_v1_smoke'``.

Safety: no broker/order/network mutation. All payloads are static. Run only
against a non-production DB.

Usage:
    uv run python -m scripts.snapshot_bundle_smoke --dry-run
    uv run python -m scripts.snapshot_bundle_smoke --commit  # actually commits the tx
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import uuid

from app.core.db import AsyncSessionLocal
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC).replace(microsecond=0)


async def _run(commit: bool) -> int:
    async with AsyncSessionLocal() as session:
        repo = InvestmentSnapshotsRepository(session)
        
        print("--- Creating Run ---")
        run = await repo.insert_run(
            SnapshotRunCreate(
                purpose="manual_refresh",
                market="kr",
                account_scope="kis_live",
                requested_by="user",
                policy_version="intraday_action_report_v1_smoke",
                policy_snapshot_json={"smoke": True},
                refresh_reason="rob-269 phase 1 local smoke",
            )
        )
        print(f"Run created: {run.run_uuid}")

        print("--- Creating Snapshots ---")
        snap1 = await repo.insert_snapshot(
            SnapshotCreate(
                run_uuid=run.run_uuid,
                snapshot_kind="portfolio",
                market="kr",
                account_scope="kis_live",
                source_kind="manual",
                payload_json={"cash_krw": 1234567, "holdings": []},
                as_of=_now(),
                freshness_status="fresh",
            )
        )
        print(f"Snapshot 1 (portfolio) created: {snap1.snapshot_uuid}")

        snap2 = await repo.insert_snapshot(
            SnapshotCreate(
                run_uuid=run.run_uuid,
                snapshot_kind="market",
                market="kr",
                source_kind="domain_ref",
                source_table="market_quote_snapshots",
                source_id=999,
                source_uri="market_quote_snapshots:999",
                payload_json={"kospi": 2700.5},
                as_of=_now(),
                freshness_status="fresh",
            )
        )
        print(f"Snapshot 2 (market) created: {snap2.snapshot_uuid}")

        print("--- Creating Bundle ---")
        bundle = await repo.insert_bundle(
            BundleCreate(
                purpose="kr_action_report_smoke",
                market="kr",
                account_scope="kis_live",
                policy_version="intraday_action_report_v1_smoke",
                as_of=_now(),
                status="complete",
                coverage_summary={"required": {"portfolio": "fresh", "market": "fresh"}},
                freshness_summary={"portfolio": {"status": "fresh"}, "market": {"status": "fresh"}},
            )
        )
        print(f"Bundle created: {bundle.bundle_uuid}")

        print("--- Linking Items ---")
        item1 = await repo.link_bundle_item(
            bundle_uuid=bundle.bundle_uuid,
            item=BundleItemCreate(snapshot_uuid=snap1.snapshot_uuid, role="required"),
        )
        item2 = await repo.link_bundle_item(
            bundle_uuid=bundle.bundle_uuid,
            item=BundleItemCreate(snapshot_uuid=snap2.snapshot_uuid, role="required"),
        )
        print(f"Linked 2 items to bundle.")

        if commit:
            print("Committing transaction...")
            await session.commit()
            print("Done.")
        else:
            print("Dry-run: rolling back transaction.")
            await session.rollback()
            print("Done.")
        
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Roll back at the end")
    group.add_argument("--commit", action="store_true", help="Commit at the end")
    
    args = parser.parse_args()
    
    # Load environment variables if needed, though they should be in .env
    # For this script, we assume the environment is already set up.
    
    sys.exit(asyncio.run(_run(args.commit)))


if __name__ == "__main__":
    main()
