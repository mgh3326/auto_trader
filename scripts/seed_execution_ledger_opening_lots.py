# scripts/seed_execution_ledger_opening_lots.py
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.opening_lots import (
    build_opening_lot_plan,
    filter_opening_lot_candidates,
    load_opening_lot_candidates,
)
from app.services.execution_ledger.repository import ExecutionLedgerRepository


def _sample_seed_row(upsert, *, status: str) -> dict:  # noqa: ANN001
    return {
        "status": status,
        "broker": upsert.broker,
        "account_mode": upsert.account_mode,
        "venue": upsert.venue,
        "instrument_type": upsert.instrument_type,
        "symbol": upsert.symbol,
        "raw_symbol": upsert.raw_symbol,
        "currency": upsert.currency,
        "side": upsert.side,
        "filled_qty": upsert.filled_qty,
        "filled_price": upsert.filled_price,
        "broker_order_id": upsert.broker_order_id,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed manual_import opening lots into execution_ledger."
    )
    parser.add_argument("--broker", choices=["kis", "upbit"], action="append")
    parser.add_argument("--cutover", required=True, help="UTC cutover date YYYY-MM-DD")
    parser.add_argument(
        "--symbol",
        action="append",
        metavar="SYMBOL",
        help=(
            "Restrict seeding to SYMBOL; repeatable and accepts comma lists. "
            "Matching is exact equality after normalization "
            "(uppercase + DB separator form): KR codes keep their 6 digits "
            "(005930), US accepts BRK.B/BRK-B/BRK/B, crypto accepts the "
            "currency code (BTC) or its KRW- market key (KRW-BTC)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--commit", action="store_true")
    return parser.parse_args(argv)


def parse_cutover(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


async def _run(
    *,
    brokers: list[str],
    cutover: datetime,
    dry_run: bool,
    symbols: list[str] | None = None,
) -> int:
    if not dry_run and not settings.EXECUTION_LEDGER_COMMIT_ENABLED:
        raise RuntimeError(
            "EXECUTION_LEDGER_COMMIT_ENABLED is false; commit mode is disabled"
        )
    async with AsyncSessionLocal() as db:
        repo = ExecutionLedgerRepository(db)
        candidates = await load_opening_lot_candidates(brokers)
        symbol_filter = None
        if symbols is not None:
            symbol_filter = filter_opening_lot_candidates(candidates, symbols)
            candidates = symbol_filter.candidates
        ledger_net = await repo.net_quantity_by_match_key_since(cutover=cutover)
        plan = build_opening_lot_plan(
            candidates=candidates,
            ledger_net_by_key=ledger_net,
            cutover=cutover,
        )
        committed = 0
        committed_insert = 0
        committed_update = 0
        would_insert = 0
        would_update = 0
        unchanged = 0
        sample_seed_rows = []
        for upsert in plan.upserts:
            status = await repo.classify_fill(upsert)
            if status == "inserted":
                would_insert += 1
            elif status == "updated":
                would_update += 1
            else:
                unchanged += 1
            if len(sample_seed_rows) < 10:
                sample_seed_rows.append(_sample_seed_row(upsert, status=status))
            if not dry_run and status != "unchanged":
                committed_status, _row_id = await repo.upsert_fill(upsert)
                committed += 1
                if committed_status == "inserted":
                    committed_insert += 1
                elif committed_status == "updated":
                    committed_update += 1
        if dry_run:
            await db.rollback()
        else:
            await db.commit()
        skipped_rows = [asdict(skip) for skip in plan.skipped]
        if symbol_filter is not None:
            skipped_rows += [
                {"symbol": symbol, "reason": "no_matching_candidate"}
                for symbol in symbol_filter.unmatched
            ]
        payload = {
            "dry_run": dry_run,
            "would_seed": len(plan.upserts),
            "would_insert": would_insert,
            "would_update": would_update,
            "unchanged": unchanged,
            "committed": committed,
            "committed_insert": committed_insert,
            "committed_update": committed_update,
            "sample_seed_rows": sample_seed_rows,
            "skipped": skipped_rows,
        }
        if symbol_filter is not None:
            payload["symbol_filter"] = {
                "requested": symbol_filter.requested,
                "matched": symbol_filter.matched,
                "unmatched": [
                    {"symbol": symbol, "reason": "no_matching_candidate"}
                    for symbol in symbol_filter.unmatched
                ],
            }
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )
    if symbol_filter is not None and not symbol_filter.matched:
        print(
            "no --symbol value matched an opening-lot candidate; nothing to seed",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    args = parse_args()
    brokers = args.broker or ["kis", "upbit"]
    return asyncio.run(
        _run(
            brokers=brokers,
            cutover=parse_cutover(args.cutover),
            dry_run=not bool(args.commit),
            symbols=args.symbol,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
