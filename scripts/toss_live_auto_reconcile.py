"""Scheduleless Toss reconciliation; preview unless explicitly applied.

Exit codes:
    0  - run completed, no row needs a person
    2  - activation gate refused, no run
    3  - run completed, but rows need a person (deferred fill booking,
         manual-review or unconfirmed rows, dry-run would_* previews)
    4  - run failed or is incomplete (kernel error, truncated scan, sweep or
         projection repair failure, batch build error, or a row with error)
"""

import argparse
import asyncio
import json

from app.core.config import settings
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl


def _failure_conditions(result: dict) -> list[str]:
    conditions: list[str] = []
    if not result.get("success"):
        conditions.append("success_false")
    if result.get("error") is not None:
        conditions.append("kernel_error")
    if (result.get("candidate_scan") or {}).get("truncated"):
        conditions.append("candidate_scan_truncated")
    if (result.get("proposal_rung_sweep") or {}).get("error") is not None:
        conditions.append("proposal_rung_sweep_error")
    if (result.get("proposal_projection_repair") or {}).get("failed", 0) > 0:
        conditions.append("projection_repair_failed")
    if result.get("batch_build_error") is not None:
        conditions.append("batch_build_error")
    if any(row.get("error") is not None for row in result.get("reconciled", [])):
        conditions.append("row_error")
    return conditions


def _clean(row: dict, *, apply: bool) -> bool:
    action = row.get("action", "")
    if row.get("requires_manual_review") or row.get("reason_code") == "not_found":
        return False
    return action in {"noop_pending", "noop_terminal"} or (
        apply and (action.startswith("marked_") or action == "booked")
    )


async def _run(*, apply: bool, terminal_only: bool) -> int:
    result = await toss_reconcile_orders_impl(
        dry_run=not apply, scope="terminal_only" if terminal_only else "full"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    conditions = _failure_conditions(result)
    if conditions:
        print(f"실패: 대사 실행이 완료되지 않았다 (조건: {', '.join(conditions)})")
        return 4
    if not all(_clean(row, apply=apply) for row in result.get("reconciled", [])):
        print("WARNING: reconciliation requires attention")
        return 3
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Toss live order reconciliation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--apply-terminal-only", action="store_true")
    args = parser.parse_args()
    if (
        args.apply_terminal_only
        and settings.TOSS_TERMINAL_AUTO_RECONCILE_ENABLED is not True
    ):
        print("--apply-terminal-only requires TOSS_TERMINAL_AUTO_RECONCILE_ENABLED")
        return 2
    if args.apply and not (
        settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED
        and settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED
    ):
        print(
            "--apply requires TOSS_LIVE_AUTO_RECONCILE_ENABLED and TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED"
        )
        return 2
    return asyncio.run(
        _run(
            apply=args.apply or args.apply_terminal_only,
            terminal_only=args.apply_terminal_only,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
