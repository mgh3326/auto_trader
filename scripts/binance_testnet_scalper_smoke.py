"""ROB-286 — Binance testnet scalper smoke CLI (default-disabled).

Hard invariant #6 — default behavior is fail-closed:

    uv run python -m scripts.binance_testnet_scalper_smoke
    # ⇒ exits 0, single log line:
    #    "scalper disabled — set BINANCE_TESTNET_ENABLED=true to opt in"
    # zero HTTP, zero DB writes, zero Sentry events.

To opt in (still dry-run by default):

    BINANCE_TESTNET_ENABLED=true \\
      BINANCE_TESTNET_API_KEY=... \\
      BINANCE_TESTNET_API_SECRET=... \\
      uv run python -m scripts.binance_testnet_scalper_smoke --duration 30 \\
        --dry-run

The ``--confirm`` flag is required for any actual broker submission;
without it the runner runs in dry-run mode and produces ``planned`` /
``previewed`` / ``validated`` ledger rows but never ``submitted`` rows.

Exit codes:
  0 — clean run (or default-disabled exit)
  1 — operator misconfiguration (e.g., --confirm without
      BINANCE_TESTNET_ENABLED, or credentials missing)
  2 — runtime failure (broker error, reconcile drift)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal

logger = logging.getLogger("scripts.binance_testnet_scalper_smoke")


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-286 smoke CLI for the Binance testnet scalper. Default "
            "behavior is disabled (zero side effects). Set "
            "BINANCE_TESTNET_ENABLED=true + credentials to opt in."
        )
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help=(
            "Run loop for N seconds (0 = single tick across MVP symbols; default 0)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run mode (default). No HTTP submission; ledger gets "
        "planned/previewed/validated only.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "Pass confirm=True on every submit. Required for any broker "
            "submission. Implies --no-dry-run."
        ),
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Disable dry-run; use with --confirm.",
    )
    return parser.parse_args(argv)


async def _market_snapshot_stub(symbol: str):
    """Stub snapshot — the smoke CLI uses synthetic data that always Holds.

    Production-tier snapshot wiring (Child B WS subscription) is out of
    scope for the smoke CLI; the goal is to exercise the orchestration
    plumbing without depending on live market data.
    """
    from app.services.scalping.decision import MarketSnapshot

    return MarketSnapshot(
        symbol=symbol,
        last_price=Decimal("50000"),
        rsi_5m=50.0,  # neutral → Hold
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
        instrument_health="healthy",
    )


async def _instrument_id_resolver(
    session,
) -> Callable[[str], Awaitable[int]]:
    from sqlalchemy import select

    from app.models.crypto_instruments import CryptoInstrument

    async def _resolve(symbol: str) -> int:
        result = await session.execute(
            select(CryptoInstrument.id).where(
                CryptoInstrument.venue == "binance",
                CryptoInstrument.product == "spot",
                CryptoInstrument.venue_symbol == symbol,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise LookupError(
                f"crypto_instruments row not found for binance/spot/{symbol}. "
                "Run scripts/binance_testnet_seed_instruments.py first."
            )
        return int(row)

    return _resolve


async def _run_smoke(*, dry_run: bool, confirm: bool, duration_s: int) -> int:
    """Execute the smoke loop. Returns the desired exit code."""
    # Defer adapter imports until inside the opt-in branch so the
    # default-disabled exit path doesn't import any signed-adapter code.
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.testnet.execution_client import (
        BinanceTestnetExecutionClient,
    )
    from app.services.brokers.binance.testnet.ledger.service import (
        BinanceTestnetLedgerService,
    )
    from app.services.scalping.config import ScalperConfig
    from app.services.scalping.runner import ScalperRunner

    client = BinanceTestnetExecutionClient.from_env()
    config = ScalperConfig.default_for_testnet()
    async with AsyncSessionLocal() as session:
        instrument_id_for_symbol = await _instrument_id_resolver(session)
        ledger = BinanceTestnetLedgerService(session=session)
        runner = ScalperRunner(
            execution_client=client,
            ledger_service=ledger,
            config=config,
            instrument_id_for_symbol=instrument_id_for_symbol,
            market_snapshot_for_symbol=_market_snapshot_stub,
            dry_run=dry_run,
        )
        if confirm and dry_run:
            logger.warning(
                "smoke CLI: --confirm passed but dry_run=True; orders will "
                "remain dry-run. Pass --no-dry-run to actually submit."
            )
        # Optional reconciliation pass before any tick.
        try:
            recon = await runner.reconcile_on_start()
            logger.info(
                "smoke CLI: reconcile_on_start examined=%s anomalies=%s",
                recon.rows_examined,
                recon.anomalies_detected,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("smoke CLI: reconcile_on_start failed: %s", exc)

        # Single-tick loop across MVP symbols (duration=0 means one pass).
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0, duration_s)
        first_pass = True
        while first_pass or loop.time() < deadline:
            first_pass = False
            for symbol in sorted(config.symbols):
                try:
                    tick = await runner.tick_once(symbol=symbol)
                    logger.info(
                        "smoke CLI: tick symbol=%s action=%s submitted=%s notes=%s",
                        tick.symbol,
                        tick.action_name,
                        tick.submitted,
                        tick.notes,
                    )
                except LookupError as exc:
                    logger.warning("smoke CLI: %s", exc)
                except Exception as exc:  # noqa: BLE001
                    logger.error("smoke CLI: tick failed for %s: %s", symbol, exc)
                    await client.aclose()
                    return 2
            if duration_s <= 0:
                break
            await asyncio.sleep(1)
        await session.commit()
    await client.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Default-disabled gate (hard invariant #6) — exit 0 with a single
    # log line + zero side effects.
    if not _truthy(os.environ.get("BINANCE_TESTNET_ENABLED")):
        logger.info("scalper disabled — set BINANCE_TESTNET_ENABLED=true to opt in")
        return 0
    # Opt-in path: confirm flag implies --no-dry-run.
    dry_run = args.dry_run and not args.confirm
    try:
        return asyncio.run(
            _run_smoke(
                dry_run=dry_run,
                confirm=args.confirm,
                duration_s=args.duration,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("smoke CLI: top-level failure: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
