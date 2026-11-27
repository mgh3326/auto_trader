"""Manual dry-run: print Upbit per-currency deposit/withdraw wallet status.

Read-only. Shows the closed hold reason for every held coin (nonzero
``/v1/accounts`` balance) and for the top-``--top-n`` KRW markets by 24h trade
value. Wallet status rows are per network; each currency line lists its
networks. Upbit warns this data may lag the real service status by minutes —
it is a reference signal, not an order input.

Usage::

    uv run python -m scripts.upbit_wallet_status [--top-n 20]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.services import upbit_wallet_status as wallet_status
from app.services.minute_candles.upbit_public import (
    UpbitMinutePublicData,
    UpbitPublicError,
)


def _market_currency(market: str) -> str:
    return market.removeprefix("KRW-")


async def run(top_n: int) -> int:
    rows = await wallet_status.fetch_wallet_status()
    held = await wallet_status.fetch_accounts()

    public = UpbitMinutePublicData()
    markets = public.markets()
    top_markets = public.top_by_trade_value(markets, top_n)

    sources: dict[str, set[str]] = {}
    for code in held:
        sources.setdefault(code, set()).add("held")
    for market in top_markets:
        sources.setdefault(_market_currency(market), set()).add(f"top{top_n}")

    statuses = wallet_status.currency_statuses(rows)

    print(f"upbit wallet status — {len(rows)} network rows, {len(statuses)} currencies")
    print(f"{'currency':<14} {'source':<12} {'hold_reason':<28} networks")
    for code in sorted(sources):
        status = wallet_status.status_for_currency(statuses, code)
        reason = status.hold_reason or "-"
        networks = "; ".join(
            f"{row.net_type or '?'}:{row.wallet_state}" for row in status.rows
        )
        print(
            f"{code:<14} {'+'.join(sorted(sources[code])):<12} {reason:<28} {networks}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="KRW markets by 24h trade value to include (default: 20)",
    )
    args = parser.parse_args(argv)
    if args.top_n < 1:
        parser.error("--top-n must be >= 1")
    try:
        return asyncio.run(run(args.top_n))
    except (wallet_status.WalletStatusError, UpbitPublicError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
