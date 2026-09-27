#!/usr/bin/env python3
"""Sync Toss Open API symbol master metadata and market-cap valuation rows.

Defaults to dry-run. Pass --commit only after reviewing the printed coverage packet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger(__name__)

# Internal runtime budget. Callers kill the subprocess at 900s
# (Prefect subprocess timeout, at-job module timeout); a killed process cannot
# log or alert, so the sync aborts itself first and reports to the operator.
_DEFAULT_TIMEOUT_SECONDS = 600
_ALERT_SEND_TIMEOUT_SECONDS = 15.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first Toss symbol master sync (ROB-534)."
    )
    parser.add_argument("--market", choices=["kr", "us"], required=True)
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to one symbol. Can be repeated.",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--all", action="store_true", help="Process all active universe symbols."
    )
    parser.add_argument(
        "--no-market-cap",
        action="store_true",
        help="Update master fields only; skip prices/market cap.",
    )
    parser.add_argument(
        "--commit", action="store_true", help="Write changes. Default is dry-run."
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=(
            "Internal runtime budget; 0 disables. Must stay below the 900s "
            "outer timeout so a stall still alerts before the caller kills us."
        ),
    )
    args = parser.parse_args(argv)
    if args.all and (args.symbol or args.limit != 20):
        parser.error("--all is mutually exclusive with --symbol and explicit --limit")
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    return args


def _print_result(result) -> None:
    print(
        f"\nToss symbol master {result.market.upper()} "
        f"(dry_run={not result.commit}, batches={result.batches})"
    )
    print("coverage:")
    print(f"  requested: {result.symbols_requested}")
    print(f"  stocks_matched: {result.stocks_matched}")
    print(f"  stocks_missing: {result.stocks_missing}")
    print(f"  master_updates: {result.master_updates}")
    print(f"  market_cap_payloads: {result.market_cap_payloads}")
    print(f"  market_cap_nonnull: {result.market_cap_nonnull}")
    print(
        f"  market_cap_skipped_existing: {result.market_cap_skipped_existing} "
        "(gap-fill: other source already covers the key)"
    )
    for warning in result.warnings:
        print(f"  warning: {warning}")
    if result.samples:
        print("samples:")
        for sample in result.samples:
            print(f"  {sample}")
    if not result.commit:
        print("\n--dry-run: no rows written.\n")
    else:
        print("\ncommitted Toss symbol master updates.\n")


async def _alert_failure(*, market: str, commit: bool, reason: str) -> None:
    """Best-effort operator alert for a failed sync; must never raise.

    Reuses the process-local TradeNotifier configured from the same env the
    container already receives, so delivery does not depend on the Prefect
    worker's environment. Only the failure reason string is sent — exception
    messages can embed DSNs or other secrets and are never forwarded.
    """
    try:
        from app.monitoring.trade_notifier import get_trade_notifier
        from app.monitoring.trade_notifier import runtime as notifier_runtime

        configured = notifier_runtime.configure_trade_notifier_from_settings(
            log_context="Toss symbol master failure alert"
        )
        if not configured:
            logger.warning(
                "toss symbol master %s failed (%s) but no operator alert "
                "channel is configured",
                market,
                reason,
            )
            return
        notifier = get_trade_notifier()
        mode = "commit" if commit else "dry-run"
        message = (
            "Toss symbol master sync FAILED\n"
            f"market: {market.upper()} ({mode})\n"
            f"error: {reason}\n"
        )
        try:
            delivered = await asyncio.wait_for(
                notifier.notify_agent_message(message),
                timeout=_ALERT_SEND_TIMEOUT_SECONDS,
            )
            if not delivered:
                logger.warning("operator alert send returned false for %s", market)
        finally:
            await notifier.shutdown()
    except Exception:
        logger.warning("operator alert send failed", exc_info=True)


async def run(args: argparse.Namespace) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.toss.client import TossReadClient
    from app.services.toss_symbol_master_service import (
        TossSymbolMasterSyncRequest,
        sync_toss_symbol_master,
    )

    client = TossReadClient.from_settings()
    budget_exceeded = False
    try:
        try:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    sync = sync_toss_symbol_master(
                        session,
                        client=client,
                        request=TossSymbolMasterSyncRequest(
                            market=args.market,
                            symbols=tuple(args.symbol),
                            all_symbols=args.all,
                            limit=args.limit,
                            commit=args.commit,
                            include_market_cap=not args.no_market_cap,
                        ),
                    )
                    if args.timeout_seconds > 0:
                        try:
                            result = await asyncio.wait_for(
                                sync, timeout=args.timeout_seconds
                            )
                        except TimeoutError:
                            budget_exceeded = True
                            raise
                    else:
                        result = await sync
            _print_result(result)
        except Exception as exc:
            reason = (
                f"exceeded runtime budget {args.timeout_seconds}s"
                if budget_exceeded
                else type(exc).__name__
            )
            await _alert_failure(market=args.market, commit=args.commit, reason=reason)
            raise
    finally:
        await client.aclose()
    return 0


async def main() -> int:
    args = parse_args()
    from app.core.cli import setup_logging_and_sentry

    setup_logging_and_sentry(service_name="sync-toss-symbol-master")
    return await run(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
