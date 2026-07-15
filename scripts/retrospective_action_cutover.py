#!/usr/bin/env python
"""ROB-878 child-2 — canonical cutover command.

Usage:
    uv run python scripts/retrospective_action_cutover.py [--if-shadow]

This command atomically switches the retrospective action control mode from
``shadow`` to ``canonical``. It must be run AFTER all old-version processes
have been drained and only new-version code is serving traffic.

Steps (all in one transaction):
    1. Transaction-scoped advisory lock.
    2. LOCK TABLE parent → control → actions (SHARE ROW EXCLUSIVE).
    3. Delete stale shadow children.
    4. Rebuild children from frozen parent JSONB (deterministic backfill).
    5. Verify full field/count/ordinal parity.
    6. Switch control mode to canonical, record cutover_at/count.

If parity fails, the entire transaction (including the mode switch) rolls
back and the system remains in shadow mode.

``--if-shadow`` makes the command idempotent: if the mode is already
canonical, it reports the existing cutover state and exits 0 without
rebuilding.

Rollback / roll-forward procedure:
    After a successful cutover, the control mode is canonical and the
    write-fence trigger rejects direct parent JSONB writes from old code.
    If a later failure requires returning traffic to old code:
        - Old code can still READ parent JSONB (the projection is maintained).
        - Old code CANNOT write next_actions (the trigger blocks it).
        - Recovery is mutation-disable + roll-forward to new code.
        - Schema downgrade is NOT a recovery path after cutover.
    A failed cutover (parity error) leaves mode=shadow and all children
    untouched; the deploy may safely roll back to the previous release.

Exit codes:
    0 — cutover succeeded (or idempotent no-op with --if-shadow)
    10 — safe pre-commit failure; mode remains shadow and rollback is safe
    20 — committed or commit-unknown failure; mutation-disable + roll-forward
    2 — invalid arguments
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.db import engine
from app.services.trade_journal.retrospective_action_repository import (
    ActionControlError,
    CutoverLockError,
    CutoverParityError,
    run_cutover,
)

EXIT_OK = 0
EXIT_SAFE_PRECOMMIT = 10
EXIT_COMMITTED_OR_UNKNOWN = 20


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Canonical cutover for retrospective action ledger (ROB-878).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Recovery and exit states:
  exit 10  Safe rollback. The transaction did not commit and mode remains shadow.
  exit 20  Commit succeeded or its outcome is unknown. Disable retrospective
           action mutation, roll-forward the new release, and do not downgrade
           the schema.
""",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _build_parser()
    parser.add_argument(
        "--if-shadow",
        action="store_true",
        help="Idempotent mode: no-op if already canonical.",
    )
    return parser.parse_args(argv)


async def _run(if_shadow: bool) -> dict[str, Any]:
    """Execute the cutover in a single transaction."""
    async with engine.begin() as conn:
        return await run_cutover(conn, if_shadow=if_shadow)


async def _health_check(conn: AsyncConnection) -> dict[str, Any]:
    """Post-cutover canonical health/parity check (read-only)."""
    mode_result = await conn.execute(
        text(
            "SELECT mode, cutover_at, cutover_action_count "
            "FROM review.trade_retrospective_action_control WHERE id = 1"
        )
    )
    ctrl = mode_result.fetchone()
    if ctrl is None:
        return {"healthy": False, "reason": "control row missing"}
    if ctrl.mode != "canonical":
        return {"healthy": False, "reason": f"mode is {ctrl.mode}, not canonical"}
    if ctrl.cutover_at is None or ctrl.cutover_action_count is None:
        return {"healthy": False, "reason": "canonical cutover metadata is missing"}

    # Quick count parity
    count_result = await conn.execute(
        text(
            """
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN jsonb_typeof(next_actions) = 'array'
                        THEN jsonb_array_length(next_actions)
                        ELSE 0
                    END
                ), 0) AS parent_count,
                (SELECT count(*) FROM review.trade_retrospective_actions) AS child_count
            FROM review.trade_retrospectives
            """
        )
    )
    counts = count_result.one()
    if counts.parent_count != counts.child_count:
        return {
            "healthy": False,
            "reason": f"count mismatch: parent={counts.parent_count}, "
            f"child={counts.child_count}",
        }
    parity_result = await conn.execute(
        text(
            """
            WITH parent_actions AS (
                SELECT
                    t.id AS retrospective_id,
                    (elem.ordinality - 1)::integer AS position,
                    elem.value AS payload
                FROM review.trade_retrospectives t
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(t.next_actions) = 'array'
                            THEN t.next_actions
                        ELSE '[]'::jsonb
                    END
                ) WITH ORDINALITY AS elem(value, ordinality)
            )
            SELECT COALESCE(p.retrospective_id, a.retrospective_id) AS retrospective_id,
                   COALESCE(p.position, a.position) AS position
            FROM parent_actions p
            FULL JOIN review.trade_retrospective_actions a
              ON a.retrospective_id = p.retrospective_id
             AND a.position = p.position
            WHERE p.retrospective_id IS NULL
               OR a.id IS NULL
               OR btrim(p.payload->>'action') IS DISTINCT FROM a.action
               OR (p.payload->>'owner') IS DISTINCT FROM a.owner
               OR (p.payload->>'issue_id') IS DISTINCT FROM a.issue_id
               OR CASE
                    WHEN btrim(COALESCE(p.payload->>'status', '')) = '' THEN 'open'
                    ELSE p.payload->>'status'
                  END IS DISTINCT FROM CASE
                    WHEN a.status IN ('obsolete', 'expired') THEN 'done'
                    ELSE a.status
                  END
               OR NULLIF(btrim(COALESCE(p.payload->>'due_kst_date', '')), '')
                    IS DISTINCT FROM a.due_kst_date::text
            LIMIT 1
            """
        )
    )
    mismatch = parity_result.fetchone()
    if mismatch is not None:
        return {
            "healthy": False,
            "reason": "field/ordinal mismatch: retrospective "
            f"{mismatch.retrospective_id} action[{mismatch.position}]",
        }

    return {
        "healthy": True,
        "mode": ctrl.mode,
        "cutover_at": ctrl.cutover_at.isoformat() if ctrl.cutover_at else None,
        "action_count": counts.child_count,
        "cutover_action_count": ctrl.cutover_action_count,
    }


async def _async_main(if_shadow: bool) -> int:
    try:
        result = await _run(if_shadow)
    except CutoverParityError as exc:
        print(f"CUTOVER FAILED (parity): {exc}", file=sys.stderr)
        print(
            "Mode remains shadow. The deploy may safely roll back.",
            file=sys.stderr,
        )
        return EXIT_SAFE_PRECOMMIT
    except CutoverLockError as exc:
        print(f"CUTOVER FAILED (lock): {exc}", file=sys.stderr)
        print("Mode remains shadow. The deploy may safely roll back.", file=sys.stderr)
        return EXIT_SAFE_PRECOMMIT
    except ActionControlError as exc:
        print(f"CUTOVER FAILED (control): {exc}", file=sys.stderr)
        print(
            "Recovery state is uncertain. Disable mutation and inspect the control row.",
            file=sys.stderr,
        )
        return EXIT_COMMITTED_OR_UNKNOWN
    except Exception as exc:
        print(f"CUTOVER FAILED (unexpected): {exc}", file=sys.stderr)
        return EXIT_COMMITTED_OR_UNKNOWN

    idempotent = result.get("idempotent", False)
    if idempotent:
        print(
            f"Cutover: already canonical (idempotent). "
            f"action_count={result['action_count']}"
        )
    else:
        print(f"Cutover: shadow → canonical. action_count={result['action_count']}")

    # Post-cutover health check
    try:
        async with engine.connect() as conn:
            health = await _health_check(conn)
        if health.get("healthy"):
            print(f"Health: OK mode={health['mode']} count={health['action_count']}")
        else:
            print(f"Health: DEGRADED — {health.get('reason')}", file=sys.stderr)
            print(
                "Recovery: mutation-disable + roll-forward. "
                "Do NOT use schema downgrade.",
                file=sys.stderr,
            )
            return EXIT_COMMITTED_OR_UNKNOWN
    except Exception as exc:
        print(f"Health check error: {exc}", file=sys.stderr)
        print(
            "Recovery: mutation-disable + roll-forward. Do NOT use schema downgrade.",
            file=sys.stderr,
        )
        return EXIT_COMMITTED_OR_UNKNOWN

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_async_main(args.if_shadow))


if __name__ == "__main__":
    sys.exit(main())
