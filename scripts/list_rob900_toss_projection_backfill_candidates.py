"""ROB-900 evidence-first, read-only Toss proposal-link backfill list.

This tool never updates either ledger or proposal tables.  It prints only
unlinked terminal Toss rows with exactly one conservative, reviewable proposal
rung suggestion; ambiguous and weak matches are counted but not emitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.core.db import AsyncSessionLocal


def _market_for_toss(market: str) -> str | None:
    return {"kr": "equity_kr", "us": "equity_us"}.get(market)


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _within_seconds(left: datetime, right: datetime, seconds: int) -> int | None:
    if left.tzinfo is None or right.tzinfo is None:
        return None
    delta = abs(int((left - right).total_seconds()))
    return delta if delta <= seconds else None


def _unique_suggestion(
    ledger: dict[str, Any], rungs: list[dict[str, Any]], *, window_seconds: int
) -> dict[str, Any] | None:
    """Return one exact-intent/time-near rung, otherwise fail closed."""
    proposal_market = _market_for_toss(str(ledger["market"]))
    ledger_quantity = _as_decimal(ledger["filled_qty"] or ledger["quantity"])
    created_at = ledger["created_at"]
    if (
        proposal_market is None
        or ledger_quantity is None
        or not isinstance(created_at, datetime)
    ):
        return None

    matches: list[tuple[dict[str, Any], int]] = []
    for rung in rungs:
        if (
            rung["symbol"] != ledger["symbol"]
            or rung["market"] != proposal_market
            or rung["side"] != ledger["side"]
            or _as_decimal(rung["quantity"]) != ledger_quantity
        ):
            continue
        seconds_apart = _within_seconds(
            created_at, rung["rung_updated_at"], window_seconds
        )
        if seconds_apart is not None:
            matches.append((rung, seconds_apart))

    if len(matches) != 1:
        return None
    rung, seconds_apart = matches[0]
    return {
        "ledger_id": ledger["id"],
        "broker_order_id": ledger["broker_order_id"],
        "ledger_status": ledger["status"],
        "symbol": ledger["symbol"],
        "market": ledger["market"],
        "side": ledger["side"],
        "ledger_quantity": str(ledger_quantity),
        "ledger_created_at": created_at.isoformat(),
        "proposal_id": str(rung["proposal_id"]),
        "proposal_rung_id": rung["rung_id"],
        "proposal_rung_index": rung["rung_index"],
        "proposal_state": rung["state"],
        "rung_updated_at": rung["rung_updated_at"].isoformat(),
        "seconds_apart": seconds_apart,
        "match_basis": [
            "same_symbol",
            "same_market",
            "same_side",
            "exact_quantity",
            "near_resting_timestamp",
        ],
        "auto_backfill_eligible": False,
        "operator_action": "review evidence; no mutation is performed by this tool",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROB-900 read-only Toss proposal-link backfill candidate list"
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--window-seconds", type=int, default=300)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if args.limit <= 0 or args.window_seconds <= 0:
        raise ValueError("--limit and --window-seconds must be positive")
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        ledger_rows = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT l.id, l.broker_order_id, l.status, l.market, l.symbol,
                               l.side, l.quantity, l.filled_qty, l.created_at
                        FROM review.toss_live_order_ledger l
                        WHERE l.status IN ('filled', 'cancelled', 'rejected')
                          AND NOT EXISTS (
                              SELECT 1
                              FROM review.order_proposal_rungs r
                              WHERE (l.correlation_id IS NOT NULL
                                     AND r.correlation_id = l.correlation_id)
                                 OR (l.broker_order_id IS NOT NULL
                                     AND r.broker_order_id = l.broker_order_id)
                          )
                        ORDER BY l.id ASC
                        LIMIT :limit
                        """
                    ),
                    {"limit": args.limit},
                )
            ).mappings()
        )
        rung_rows = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT p.proposal_id, p.symbol, p.market, r.id AS rung_id,
                               r.rung_index, r.side, r.quantity, r.state,
                               r.updated_at AS rung_updated_at
                        FROM review.order_proposals p
                        JOIN review.order_proposal_rungs r ON r.proposal_pk = p.id
                        WHERE p.account_mode = 'toss_live'
                          AND r.state IN ('acked', 'resting', 'partially_filled', 'unverified')
                        """
                    )
                )
            ).mappings()
        )
        suggestions = [
            suggestion
            for row in ledger_rows
            if (
                suggestion := _unique_suggestion(
                    dict(row),
                    [dict(rung) for rung in rung_rows],
                    window_seconds=args.window_seconds,
                )
            )
            is not None
        ]
        await session.rollback()

    print(
        json.dumps(
            {
                "dry_run": True,
                "read_only": True,
                "terminal_unlinked_rows_scanned": len(ledger_rows),
                "unique_review_suggestions": len(suggestions),
                "excluded_no_or_ambiguous_match": len(ledger_rows) - len(suggestions),
                "suggestions": suggestions,
            },
            default=str,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
