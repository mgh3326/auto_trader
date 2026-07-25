# scripts/forecast_legacy_rule_migration.py
"""1-off audit-capable migration script for legacy forecasts missing outcome_rule_version.

ROB-1038 / Forecast Recovery:
Fixes legacy forecast records lacking `outcome_rule_version`.
Identifies 119 legacy open forecasts and applies operator decisions:
  1) 12 rows (session_label 'directional-forecast-lab-round-1%', id != 140):
     Backfilled with rule_version "terminal-close-v1-up-gte-down-lt".
  2) 1 row (id 140):
     Marked as superseded (status 'closed_no_claim', resolution_source 'quarantine_legacy_superseded').
     Reason: id 140 and id 143 share identical SMCI symbol, D+20 horizon, and review_date (2026-08-20)
     with opposite directions; 143 (GPT Pro evidence follow-up) supersedes 140.
  3) 106 rows (other legacy open forecasts missing outcome_rule_version):
     Closed as 'closed_no_claim' (resolution_source 'quarantine_legacy_cleanup') per ROB-1041.

PROVENANCE BASIS:
  Recorded as contemporaneous internal records (동시대 내부 기록):
  (1) Creation-time horizon field ('D+5 trading sessions' / 'D+20 trading sessions').
  (2) Audit report (rob-1036-smci-codex-audit.md) citing natural language contract of SMCI forecasts as review-date regular close.

SAFETY GUARANTEES:
  - Default dry-run mode (requires explicit --commit flag).
  - Target selection counts are validated against expectations (12 backfill, 1 superseded, 106 closed_no_claim).
  - Any count discrepancy halts the migration immediately without mutation.
  - Pre-change snapshot is saved to a file before applying commits.
  - Raw forecast text, probability, direction, and target price are NEVER altered.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.review import TradeForecast

logger = logging.getLogger(__name__)

RULE_VERSION_TERMINAL_CLOSE = "terminal-close-v1-up-gte-down-lt"
LAB_ROUND_1_PREFIX = "directional-forecast-lab-round-1%"
SUPERSEDED_ID = 140

PROVENANCE_BACKFILL_DETAIL = (
    "Backfilled outcome_rule_version terminal-close-v1-up-gte-down-lt based on "
    "contemporaneous internal records: (1) Creation-time horizon field "
    "('D+5 trading sessions'/'D+20 trading sessions'). (2) Audit report "
    "(rob-1036-smci-codex-audit.md) citing natural language contract of SMCI "
    "forecasts as review-date regular close."
)

PROVENANCE_SUPERSEDED_DETAIL = (
    "Marked superseded (closed_no_claim) based on contemporaneous internal records: "
    "ID 140 and 143 share identical SMCI symbol, D+20 horizon, and review_date (2026-08-20) "
    "with opposite directions. ID 143 (GPT Pro evidence follow-up) supersedes 140. "
    "Excluded from scoring targets to prevent artificial calibration inflation."
)

PROVENANCE_CLOSED_NO_CLAIM_DETAIL = (
    "Closed as closed_no_claim per ROB-1041 (Claim Immutability): Legacy price_target "
    "missing outcome_rule_version and lacks contemporaneous internal or pre-registration "
    "evidence specifying evaluation rule."
)


class MigrationTargetCountMismatch(Exception):
    """Raised when target query counts do not match operator expectations."""


def is_missing_rule_version(forecast_target: dict[str, Any] | None) -> bool:
    """Return True if forecast_target lacks outcome_rule_version."""
    if not isinstance(forecast_target, dict):
        return True
    rv = forecast_target.get("outcome_rule_version")
    return rv is None or rv == ""


async def fetch_migration_targets(
    db: AsyncSession,
    *,
    expected_backfill: int = 12,
    expected_superseded: int = 1,
    expected_closed_no_claim: int = 106,
) -> tuple[list[TradeForecast], TradeForecast | None, list[TradeForecast]]:
    """Fetch and validate all migration targets from review.trade_forecasts.

    Mutually exclusive target selection:
    1) Backfill targets: open status, session_label LIKE 'directional-forecast-lab-round-1%', id != 140, missing rule_version.
    2) Superseded target: open status, id == 140, missing rule_version.
    3) Closed no claim targets: open status, missing rule_version, NOT lab round 1.
    """
    stmt = (
        select(TradeForecast)
        .where(TradeForecast.status == "open")
        .order_by(TradeForecast.id.asc())
    )
    result = await db.execute(stmt)
    all_open = list(result.scalars().all())

    legacy_open = [f for f in all_open if is_missing_rule_version(f.forecast_target)]

    backfill_targets: list[TradeForecast] = []
    superseded_target: TradeForecast | None = None
    closed_no_claim_targets: list[TradeForecast] = []

    for f in legacy_open:
        s_label = f.session_label or ""
        is_lab_1 = s_label.startswith("directional-forecast-lab-round-1")
        if is_lab_1:
            if f.id == SUPERSEDED_ID:
                superseded_target = f
            else:
                backfill_targets.append(f)
        else:
            closed_no_claim_targets.append(f)

    actual_backfill = len(backfill_targets)
    actual_superseded = 1 if superseded_target is not None else 0
    actual_closed_no_claim = len(closed_no_claim_targets)

    if (
        actual_backfill != expected_backfill
        or actual_superseded != expected_superseded
        or actual_closed_no_claim != expected_closed_no_claim
    ):
        msg = (
            f"Migration target count mismatch! Aborting.\n"
            f"Expected: backfill={expected_backfill}, superseded={expected_superseded}, closed_no_claim={expected_closed_no_claim} (Total {expected_backfill + expected_superseded + expected_closed_no_claim})\n"
            f"Actual:   backfill={actual_backfill}, superseded={actual_superseded}, closed_no_claim={actual_closed_no_claim} (Total {actual_backfill + actual_superseded + actual_closed_no_claim})"
        )
        raise MigrationTargetCountMismatch(msg)

    return backfill_targets, superseded_target, closed_no_claim_targets


def create_snapshot(
    backfill_targets: list[TradeForecast],
    superseded_target: TradeForecast | None,
    closed_no_claim_targets: list[TradeForecast],
) -> list[dict[str, Any]]:
    """Create a JSON-serializable snapshot of target rows prior to migration."""
    snapshot: list[dict[str, Any]] = []

    all_targets = list(backfill_targets)
    if superseded_target is not None:
        all_targets.append(superseded_target)
    all_targets.extend(closed_no_claim_targets)

    all_targets.sort(key=lambda f: f.id)

    for f in all_targets:
        snapshot.append(
            {
                "id": f.id,
                "forecast_id": str(f.forecast_id),
                "symbol": f.symbol,
                "instrument_type": str(f.instrument_type.value)
                if hasattr(f.instrument_type, "value")
                else str(f.instrument_type),
                "session_label": f.session_label,
                "horizon": f.horizon,
                "status": f.status,
                "forecast_target": f.forecast_target,
                "probability": float(f.probability)
                if f.probability is not None
                else None,
                "review_date": f.review_date.isoformat() if f.review_date else None,
                "resolution_source": f.resolution_source,
                "resolution_detail": f.resolution_detail,
            }
        )

    return snapshot


def apply_migration_plan(
    backfill_targets: list[TradeForecast],
    superseded_target: TradeForecast,
    closed_no_claim_targets: list[TradeForecast],
) -> dict[str, Any]:
    """Apply the migration plan mutations to the target SQLAlchemy models in-place."""
    # 1. Backfill 12 items
    for f in backfill_targets:
        orig_target = dict(f.forecast_target or {})
        orig_dir = orig_target.get("direction")
        if orig_dir in ("at_or_above", "up"):
            new_dir = "up"
        elif orig_dir in ("at_or_below", "down"):
            new_dir = "down"
        else:
            raise ValueError(f"Unknown direction '{orig_dir}' for forecast id={f.id}")

        new_target = dict(orig_target)
        new_target["kind"] = "terminal_close"
        new_target["direction"] = new_dir
        new_target["outcome_rule_version"] = RULE_VERSION_TERMINAL_CLOSE

        f.forecast_target = new_target
        f.resolution_source = "legacy_rule_backfill"
        f.resolution_detail = {
            "migration": "legacy_rule_backfill_v1",
            "provenance_basis": "contemporaneous_internal_records",
            "provenance_detail": PROVENANCE_BACKFILL_DETAIL,
            "rule_version_applied": RULE_VERSION_TERMINAL_CLOSE,
            "original_forecast_target": orig_target,
        }

    # 2. Superseded 1 item (id 140)
    superseded_target.status = "closed_no_claim"
    superseded_target.resolution_source = "quarantine_legacy_superseded"
    superseded_target.resolution_detail = {
        "migration": "legacy_rule_superseded",
        "provenance_basis": "contemporaneous_internal_records",
        "provenance_detail": PROVENANCE_SUPERSEDED_DETAIL,
        "superseded_by": 143,
        "original_forecast_target": superseded_target.forecast_target,
    }

    # 3. Closed no claim 106 items
    for f in closed_no_claim_targets:
        f.status = "closed_no_claim"
        f.resolution_source = "quarantine_legacy_cleanup"
        f.resolution_detail = {
            "migration": "legacy_rule_closed_no_claim",
            "provenance_basis": "contemporaneous_internal_records",
            "provenance_detail": PROVENANCE_CLOSED_NO_CLAIM_DETAIL,
            "reason": "missing_outcome_rule_version",
            "original_forecast_target": f.forecast_target,
        }

    return {
        "backfill_count": len(backfill_targets),
        "superseded_count": 1,
        "closed_no_claim_count": len(closed_no_claim_targets),
        "total_migrated": len(backfill_targets) + 1 + len(closed_no_claim_targets),
    }


async def run_migration(
    db: AsyncSession,
    *,
    commit: bool = False,
    snapshot_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Execute the forecast legacy rule migration in dry-run or commit mode."""
    backfill, superseded, closed_no_claim = await fetch_migration_targets(db)

    if superseded is None:
        raise MigrationTargetCountMismatch("Superseded target ID 140 was not found!")

    snapshot_data = create_snapshot(backfill, superseded, closed_no_claim)

    summary = {
        "mode": "commit" if commit else "dry-run",
        "backfill_ids": [f.id for f in backfill],
        "superseded_id": superseded.id,
        "closed_no_claim_ids": [f.id for f in closed_no_claim],
        "backfill_count": len(backfill),
        "superseded_count": 1,
        "closed_no_claim_count": len(closed_no_claim),
        "total_targets": len(snapshot_data),
        "provenance_basis": "contemporaneous_internal_records",
    }

    if not commit:
        logger.info(
            "[DRY-RUN] Target selection validated: 12 backfill, 1 superseded, 106 closed_no_claim."
        )
        return summary

    # Save pre-change snapshot before commit
    out_dir = Path(snapshot_dir) if snapshot_dir else Path("data/migration_snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    snapshot_file = out_dir / f"forecast_legacy_migration_snapshot_{ts}.json"
    snapshot_file.write_text(
        json.dumps(snapshot_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    summary["snapshot_file"] = str(snapshot_file)

    logger.info("Saved pre-change snapshot to %s", snapshot_file)

    apply_migration_plan(backfill, superseded, closed_no_claim)

    await db.commit()
    logger.info("Successfully committed migration to database!")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Legacy forecast outcome_rule_version migration tool"
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply changes to the database (default: dry-run only)",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=str,
        default="data/migration_snapshots",
        help="Directory to store pre-change snapshot JSON when committing",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _main() -> None:
        async with AsyncSessionLocal() as db:
            try:
                res = await run_migration(
                    db,
                    commit=args.commit,
                    snapshot_dir=args.snapshot_dir,
                )
                print("\n=== MIGRATION REPORT ===")
                print(json.dumps(res, indent=2, ensure_ascii=False))
            except MigrationTargetCountMismatch as exc:
                print(f"\n❌ MIGRATION ABORTED: {exc}", file=sys.stderr)
                sys.exit(1)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
