"""ROB-286 — Binance testnet instrument seeder (idempotent).

Open item #8 lean adopted: ship a tiny one-shot CLI that ensures the
MVP triplet (BTCUSDT / ETHUSDT / SOLUSDT) exists as
``crypto_instruments`` rows with ``(venue='binance', product='spot')``.
Idempotent: re-running the script is safe (skips rows that already exist).

Usage:

    uv run python -m scripts.binance_testnet_seed_instruments
    uv run python -m scripts.binance_testnet_seed_instruments --dry-run

Side effects:
  * INSERTs into ``crypto_instruments`` (existing rows untouched).
  * No broker calls, no ledger writes, no notifications.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.crypto_instruments import CryptoInstrument

logger = logging.getLogger("scripts.binance_testnet_seed_instruments")

# MVP instruments (matches ScalperConfig.symbols).
MVP_INSTRUMENTS: tuple[dict[str, str], ...] = (
    {
        "venue_symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
    },
    {
        "venue_symbol": "ETHUSDT",
        "base_asset": "ETH",
        "quote_asset": "USDT",
    },
    {
        "venue_symbol": "SOLUSDT",
        "base_asset": "SOL",
        "quote_asset": "USDT",
    },
)


async def _seed_one(
    *,
    session: AsyncSession,
    venue_symbol: str,
    base_asset: str,
    quote_asset: str,
    dry_run: bool,
) -> str:
    """Insert one ``crypto_instruments`` row if absent.

    Returns ``"inserted"`` if a new row was added, ``"skipped"`` if a row
    with the same (venue, product, venue_symbol) tuple already existed.
    """
    existing = await session.execute(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == venue_symbol,
        )
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("seeder: skip %s (binance/spot already present)", venue_symbol)
        return "skipped"
    if dry_run:
        logger.info(
            "seeder: would insert %s (binance/spot) — dry-run; no write",
            venue_symbol,
        )
        return "would_insert"
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=venue_symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        status="active",
    )
    session.add(inst)
    await session.flush()
    logger.info("seeder: inserted %s (binance/spot)", venue_symbol)
    return "inserted"


async def seed_instruments(
    *,
    instruments: Iterable[dict[str, str]] = MVP_INSTRUMENTS,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the seed; returns a summary dict ``{action: count}``."""
    counts: dict[str, int] = {"inserted": 0, "skipped": 0, "would_insert": 0}
    async with AsyncSessionLocal() as session:
        async with session.begin():
            for spec in instruments:
                outcome = await _seed_one(
                    session=session,
                    venue_symbol=spec["venue_symbol"],
                    base_asset=spec["base_asset"],
                    quote_asset=spec["quote_asset"],
                    dry_run=dry_run,
                )
                counts[outcome] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-286 — Seed Binance testnet MVP instruments (BTCUSDT/ETHUSDT/"
            "SOLUSDT) into crypto_instruments. Idempotent."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without writing to the database.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    counts = asyncio.run(seed_instruments(dry_run=args.dry_run))
    logger.info("seeder summary: %s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
