"""ROB-307 PR1 — observe-only Binance Demo scalping signal CLI.

One-shot, **read-only, observe-only**. Reads Demo-host market data
(``demo-api`` / ``demo-fapi``, unsigned) + ledger-backed durable state,
runs the deterministic trend micro-breakout signal + risk envelope, and
prints observe-only evidence records. It **never** places, previews, or
tests an order — no signing, no credentials, no execution client is
imported (enforced by the demo_scalping import guard, which also scans
this script).

Default-disabled: with ``BINANCE_DEMO_SCALPING_ENABLED`` unset/false the
CLI logs a line and exits 0 with zero side effects.

Exit codes: 0 clean / disabled, 1 operator misconfiguration, 2 runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.brokers.binance.demo_scalping.contract import (
    DEFAULT_ALLOWLIST,
    LedgerSnapshot,
    Product,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.ledger_state import (
    load_ledger_snapshot,
)
from app.services.brokers.binance.demo_scalping.market_data import (
    DemoScalpingMarketData,
)
from app.services.brokers.binance.demo_scalping.runner import (
    ObserveOnlyRecord,
    evaluate_symbol,
)

logger = logging.getLogger("rob307.demo_scalping_signal")

_ENABLED_ENV = "BINANCE_DEMO_SCALPING_ENABLED"
_VALID_PRODUCTS = ("spot", "usdm_futures")

SnapshotLoader = Callable[..., Awaitable[LedgerSnapshot]]


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _evidence(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-307 observe-only Binance Demo scalping signal. Default "
            "behavior is disabled (zero side effects). Set "
            f"{_ENABLED_ENV}=true to opt in. NEVER places orders."
        )
    )
    parser.add_argument(
        "--symbols",
        default=",".join(sorted(DEFAULT_ALLOWLIST)),
        help="Comma-separated symbols (default: the allowlist).",
    )
    parser.add_argument(
        "--products",
        default="spot",
        help="Comma-separated products from {spot,usdm_futures} (default: spot).",
    )
    parser.add_argument("--interval", default="1m", help="Kline interval (default 1m).")
    parser.add_argument(
        "--limit", type=int, default=50, help="Klines to fetch (default 50)."
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


async def observe_symbols(
    *,
    market_data: Any,
    ledger_service: Any,
    products: list[Product],
    symbols: list[str],
    now: dt.datetime,
    limits: ScalpingRiskLimits | None = None,
    interval: str = "1m",
    limit: int = 50,
    snapshot_loader: SnapshotLoader = load_ledger_snapshot,
) -> list[ObserveOnlyRecord]:
    """Evaluate every ``(product, symbol)`` pair into observe-only records."""
    limits = limits or ScalpingRiskLimits()
    records: list[ObserveOnlyRecord] = []
    for product in products:
        for symbol in symbols:
            snapshot = await snapshot_loader(
                ledger_service, product=product, symbol=symbol, now=now
            )
            record = await evaluate_symbol(
                product=product,
                symbol=symbol,
                market_data=market_data,
                ledger_snapshot=snapshot,
                now=now,
                limits=limits,
                interval=interval,
                limit=limit,
            )
            records.append(record)
    return records


async def _run(args: argparse.Namespace) -> int:
    if not _truthy(os.environ.get(_ENABLED_ENV)):
        logger.info(
            "demo scalping signal disabled — set %s=true to opt in", _ENABLED_ENV
        )
        return 0

    symbols = _split_csv(args.symbols)
    products = _split_csv(args.products)
    if not symbols or not products:
        logger.error("at least one symbol and one product are required")
        return 1
    invalid = [p for p in products if p not in _VALID_PRODUCTS]
    if invalid:
        logger.error("invalid product(s) %s; valid: %s", invalid, _VALID_PRODUCTS)
        return 1
    not_allowed = [s for s in symbols if s not in DEFAULT_ALLOWLIST]
    if not_allowed:
        logger.warning(
            "symbol(s) outside allowlist %s will be reported but blocked by risk",
            not_allowed,
        )

    now = dt.datetime.now(dt.UTC)
    # Imported lazily so the disabled path triggers zero DB engine setup.
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService

    market_data = DemoScalpingMarketData()
    try:
        async with AsyncSessionLocal() as session:
            service = BinanceDemoLedgerService(session)
            records = await observe_symbols(
                market_data=market_data,
                ledger_service=service,
                products=products,  # type: ignore[arg-type]
                symbols=symbols,
                now=now,
                interval=args.interval,
                limit=args.limit,
            )
    finally:
        await market_data.aclose()

    for record in records:
        _evidence(record.to_evidence_dict())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        logger.error("demo scalping signal failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
