from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from app.services.brokers.toss.client import TossReadClient


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


async def run_preflight(symbols: Sequence[str]) -> int:
    client = TossReadClient.from_settings()
    try:
        accounts = await client.accounts()
        holdings = await client.holdings()
        prices = await client.prices(list(symbols))
    finally:
        await client.aclose()
    print(
        "Toss preflight ok: "
        f"accounts={len(accounts)} holdings={len(holdings.items)} prices={len(prices)}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toss Open API read-only smoke")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--symbol", action="append", default=["005930"])
    args = parser.parse_args(argv)

    if not args.preflight:
        print("Toss live smoke disabled: pass --preflight to run read-only checks")
        return 0
    if not _truthy(os.environ.get("TOSS_API_ENABLED")):
        print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
        return 0
    return asyncio.run(run_preflight(args.symbol))


if __name__ == "__main__":
    raise SystemExit(main())
