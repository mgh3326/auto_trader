#!/usr/bin/env python
"""ROB-1284 — phantom-resting rung convergence sweep (dry-run by default).

Answers three questions with evidence, in this order:

1. **How many rungs are actually broker-live?** (``--census``) — untruncated,
   rung-state-driven, plus the truncation proof: the same population counted
   through progressively larger proposal-recency pages, so the point where the
   number stops growing is visible rather than assumed.
2. **What does the broker evidence say about each one?** (default) — every
   candidate classified ``TRANSITION`` / ``NO_EVIDENCE`` / ``CONFLICT`` with the
   fields a reviewer needs to audit the call.
3. **Apply it** (``--apply --confirm``) — transitions only ``TRANSITION`` rows.

Safety:
  * dry-run is the default; ``--apply`` alone is refused without ``--confirm``.
  * no broker call, no broker mutation — evidence is the committed ledger rows.
  * ``NO_EVIDENCE`` and ``CONFLICT`` are reported, never transitioned.

Usage:
    ENV_FILE=.env.prod uv run python scripts/rob1284_resting_rung_sweep.py --census
    ENV_FILE=.env.prod uv run python scripts/rob1284_resting_rung_sweep.py
    ENV_FILE=.env.prod uv run python scripts/rob1284_resting_rung_sweep.py --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.db import AsyncSessionLocal  # noqa: E402
from app.models.order_proposals import OrderProposal, OrderProposalRung  # noqa: E402
from app.services.order_proposals.resting_sweep import RungVerdict  # noqa: E402
from app.services.order_proposals.resting_sweep_service import (  # noqa: E402
    RestingRungSweepService,
)
from app.services.order_proposals.state_machine import (  # noqa: E402
    EVIDENCE_ACCEPTING_RUNG_STATES,
)

_PAGES = (50, 100, 200, 500, 1000, 5000)


async def _census(db) -> dict:
    """Prove the true N and prove that no page size or window is cutting it."""
    live = OrderProposalRung.state.in_(EVIDENCE_ACCEPTING_RUNG_STATES)

    true_n = int(
        (
            await db.execute(
                select(func.count()).select_from(OrderProposalRung).where(live)
            )
        ).scalar_one()
    )
    total_groups = int(
        (await db.execute(select(func.count()).select_from(OrderProposal))).scalar_one()
    )
    bounds = (
        await db.execute(
            select(
                func.min(OrderProposal.created_at), func.max(OrderProposal.created_at)
            )
        )
    ).one()

    # Truncation proof: count the SAME population inside the newest-N-proposals
    # page that every recency-paged surface uses. Where this plateaus at true_n
    # is where the page stops hiding rows.
    by_page: list[dict] = []
    for page in _PAGES:
        sub = select(OrderProposal.id).order_by(OrderProposal.id.desc()).limit(page)
        visible = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(OrderProposalRung)
                    .where(live, OrderProposalRung.proposal_pk.in_(sub))
                )
            ).scalar_one()
        )
        by_page.append(
            {"page_limit": page, "visible": visible, "hidden": true_n - visible}
        )

    # Window proof: widen the created_at floor; a stable count means the window
    # is not the thing bounding the answer.
    by_window: list[dict] = []
    for since in ("2026-08-01", "2026-07-11", "2026-07-01", "2026-01-01", "2000-01-01"):
        floor = datetime.datetime.fromisoformat(since).replace(tzinfo=datetime.UTC)
        n = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(OrderProposalRung)
                    .join(
                        OrderProposal, OrderProposal.id == OrderProposalRung.proposal_pk
                    )
                    .where(live, OrderProposal.created_at >= floor)
                )
            ).scalar_one()
        )
        by_window.append({"since": since, "n": n})

    return {
        "true_n": true_n,
        "rung_states_counted": sorted(EVIDENCE_ACCEPTING_RUNG_STATES),
        "total_proposal_groups": total_groups,
        "proposals_created_between": [
            bounds[0].isoformat() if bounds[0] else None,
            bounds[1].isoformat() if bounds[1] else None,
        ],
        "truncation_proof_by_page": by_page,
        "truncation_proof_by_window": by_window,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="ROB-1284 resting rung sweep")
    ap.add_argument("--census", action="store_true", help="only the TRUE_N census")
    ap.add_argument("--apply", action="store_true", help="write (needs --confirm)")
    ap.add_argument("--confirm", action="store_true", help="second gate for --apply")
    ap.add_argument("--json", type=str, default=None, help="write full payload here")
    ap.add_argument("--show", type=int, default=25, help="rows to print (0 = all)")
    args = ap.parse_args()

    if args.apply and not args.confirm:
        print("REFUSED: --apply requires --confirm (ROB-1284 double gate)")
        return 2

    now = datetime.datetime.now(datetime.UTC)
    async with AsyncSessionLocal() as db:
        census = await _census(db)
        print("=== CENSUS (untruncated) ===")
        print(json.dumps(census, indent=2, ensure_ascii=False))
        if args.census:
            return 0

        service = RestingRungSweepService(db)
        result = await service.apply(
            now=now, dry_run=not args.apply, confirm=args.confirm
        )
        if args.apply:
            await db.commit()

    print("\n=== SWEEP SUMMARY ===")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    print(
        f"\ndry_run={result['dry_run']}  applied={result['applied']}  failed={result['failed']}"
    )

    rows = result["rows"]
    if census["true_n"] != len(rows):
        print(
            f"\n!! census true_n={census['true_n']} but sweep classified {len(rows)} "
            "— investigate before trusting this run"
        )
    print("\n=== TRANSITION candidates (the only rows --apply would touch) ===")
    trans = [r for r in rows if r["verdict"] == RungVerdict.TRANSITION.value]
    if not trans:
        print("  (none — no rung has committed terminal broker evidence)")
    shown = trans if args.show == 0 else trans[: args.show]
    for r in shown:
        print(
            f"  {r['proposal_id'][:8]}#{r['rung_index']} {r['symbol']:<10} "
            f"{r['account_mode']:<10} {r['side']:<4} {r['rung_state']:>16} -> "
            f"{r['target_state']:<10} broker_id={r['broker_order_id']} "
            f"remaining={r['remaining_qty']} ledger={r['ledger_rows']}"
        )
    if args.show and len(trans) > args.show:
        print(f"  ... {len(trans) - args.show} more (use --show 0)")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"census": census, **result}, indent=2, ensure_ascii=False, default=str
            )
        )
        print(f"\nfull payload -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
