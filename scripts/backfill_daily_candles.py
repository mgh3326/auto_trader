"""Initial backfill CLI for the daily candle store.

Use after applying the daily candle migrations and before enabling the
cron jobs, to populate the tables with horizon_bars of history for a
specific symbol set.

Examples:
    uv run python scripts/backfill_daily_candles.py \
        --market us --symbols AAPL,MSFT,NVDA --horizon-bars 500

    uv run python scripts/backfill_daily_candles.py \
        --market kr --symbols 005930,000660 --horizon-bars 400
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.services.daily_candles.constants import (
    DAILY_CANDLE_BACKFILL_BARS_CRYPTO,
    DAILY_CANDLE_BACKFILL_BARS_KR,
    DAILY_CANDLE_BACKFILL_BARS_US,
)
from app.services.daily_candles.repository import MarketKey
from app.services.daily_candles.sync_service import (
    SyncTarget,
    _build_default_service,
)

logger = logging.getLogger(__name__)

_MARKET_DEFAULTS = {
    "kr": (MarketKey.KR, DAILY_CANDLE_BACKFILL_BARS_KR, "KRX"),
    "us": (MarketKey.US, DAILY_CANDLE_BACKFILL_BARS_US, "NASD"),
    "crypto": (MarketKey.CRYPTO, DAILY_CANDLE_BACKFILL_BARS_CRYPTO, "KRW"),
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=list(_MARKET_DEFAULTS), required=True)
    parser.add_argument(
        "--symbols",
        required=True,
        help="comma-separated symbols (DB-canonical form, e.g. BRK.B not BRK-B)",
    )
    parser.add_argument("--horizon-bars", type=int, default=None)
    parser.add_argument(
        "--partition",
        default=None,
        help="exchange (US) / venue (KR) / market (crypto). Defaults: NASD / KRX / KRW.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    market_key, default_bars, default_partition = _MARKET_DEFAULTS[args.market]
    horizon = args.horizon_bars if args.horizon_bars is not None else default_bars
    partition = args.partition or default_partition
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    svc = await _build_default_service()
    try:
        for symbol in symbols:
            target = SyncTarget(market=market_key, symbol=symbol, partition=partition)
            if args.dry_run:
                logger.info("DRY RUN - would sync %s", target)
                continue
            result = await svc.sync_one(target=target, horizon_bars=horizon)
            logger.info(
                "backfill done symbol=%s upserted=%d fallback=%s",
                symbol,
                result.rows_upserted,
                result.fallback_used,
            )
    finally:
        await svc.close()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _build_parser().parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
