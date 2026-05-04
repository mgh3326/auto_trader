#!/usr/bin/env python3
"""CLI operator wrapper for the weekend crypto Alpaca Paper cycle runner (ROB-94).

Usage (dry-run, safe by default):
    uv run python scripts/run_weekend_crypto_paper_cycle.py --dry-run \
        --max-candidates 1 --symbols BTC/USD --print-trace

Usage (execute, requires explicit operator token):
    export WEEKEND_CRYPTO_CYCLE_OPERATOR_TOKEN='[REDACTED]'
    uv run python scripts/run_weekend_crypto_paper_cycle.py --execute \
        --max-candidates 1 --symbols BTC/USD --print-trace

Never logs secrets, tokens, or raw broker payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weekend crypto Alpaca Paper cycle runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        dest="dry_run",
        help="Dry-run mode (default): plan/preview/validate without broker mutation",
    )
    mode.add_argument(
        "--execute",
        action="store_false",
        dest="dry_run",
        help=(
            "Execute mode: requires WEEKEND_CRYPTO_CYCLE_OPERATOR_TOKEN env var. "
            "Will attempt Alpaca Paper crypto order submission."
        ),
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=1,
        metavar="N",
        help="Max candidates to process (1-3, default: 1)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        metavar="SYMBOL",
        help="Execution symbol filter e.g. BTC/USD ETH/USD",
    )
    parser.add_argument(
        "--print-trace",
        action="store_true",
        help="Print full per-candidate stage traces in output",
    )
    parser.add_argument(
        "--approval-tokens",
        metavar="JSON",
        help=(
            "JSON dict of per-candidate approval tokens for execute mode. "
            'E.g. \'{"<uuid>": "<buy_token>", "<uuid>:sell": "<sell_token>"}\'. '
            "Prefer a clean ephemeral shell; never include real secrets in shell history."
        ),
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    from app.services.weekend_crypto_paper_cycle_runner import (
        CycleGateError,
        WeekendCryptoPaperCycleRunner,
    )

    dry_run: bool = args.dry_run
    operator_token: str | None = None
    approval_tokens: dict[str, str] | None = None

    if not dry_run:
        operator_token = os.environ.get("WEEKEND_CRYPTO_CYCLE_OPERATOR_TOKEN")
        if not operator_token:
            print(
                "ERROR: --execute requires WEEKEND_CRYPTO_CYCLE_OPERATOR_TOKEN env var.",
                file=sys.stderr,
            )
            return 1
        if args.approval_tokens:
            try:
                approval_tokens = json.loads(args.approval_tokens)
            except json.JSONDecodeError as exc:
                print(
                    f"ERROR: --approval-tokens is not valid JSON: {exc}",
                    file=sys.stderr,
                )
                return 1

    runner = WeekendCryptoPaperCycleRunner()
    try:
        report = await runner.run_cycle(
            dry_run=dry_run,
            confirm=not dry_run,
            max_candidates=args.max_candidates,
            symbols=args.symbols,
            approval_tokens=approval_tokens,
            operator_token=operator_token,
        )
    except CycleGateError as exc:
        print(f"GATE REFUSED: {exc}", file=sys.stderr)
        return 2

    report_dict = report.to_dict()
    if not args.print_trace:
        report_dict.pop("traces", None)

    print(json.dumps(report_dict, indent=2, default=str))
    return 0 if report.status in {"ok", "dry_run_ok"} else 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
