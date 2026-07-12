"""ROB-307 PR2 — one-shot Binance Demo scalping executor CLI.

Consumes one manual order-intent (``--product/--symbol/--side``) and runs
a complete small Demo lifecycle to flat / open-orders-0 via the
``DemoScalpingExecutor``. Real broker mutation happens **only** with
``--confirm``; the default is a dry-run (sizing + risk re-check, zero
orders).

Default-disabled: requires ``BINANCE_DEMO_SCALPING_ENABLED=true`` (and the
execution client's own ``BINANCE_SPOT_DEMO_ENABLED`` /
``BINANCE_FUTURES_DEMO_ENABLED`` gate for ``--confirm``). Demo hosts only;
no live/testnet path.

Exit codes: 0 reconciled / dry-run / disabled, 1 blocked / operator
misconfig, 2 anomaly / runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.demo_scalping.contract import (
    Product,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent

logger = logging.getLogger("rob307.demo_scalping_execute")

_ENABLED_ENV = "BINANCE_DEMO_SCALPING_ENABLED"
_VALID_PRODUCTS = ("spot", "usdm_futures")
_EXIT_BY_STATUS = {
    "reconciled": 0,
    "dry_run": 0,
    "blocked": 1,
    "anomaly": 2,
}


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _evidence(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def build_manual_intent(
    *,
    product: Product,
    symbol: str,
    side: str,
    now: dt.datetime,
    limits: ScalpingRiskLimits,
) -> OrderIntent:
    """A manual one-shot intent (no signal); notional pinned to the cap."""
    now_ms = int(now.timestamp() * 1000)
    return OrderIntent(
        product=product,
        symbol=symbol,
        side=side,
        order_type="MARKET",
        target_notional_usdt=limits.max_notional_usdt,
        entry_reference_price=None,
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0"),
        reason_codes=("manual_executor",),
        source_candle_close_time_ms=now_ms,
        evaluated_at_ms=now_ms,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-307 one-shot Binance Demo scalping executor. Default is "
            f"disabled (set {_ENABLED_ENV}=true) and dry-run (pass --confirm "
            "for real Demo orders). Demo hosts only; ends flat/open-orders-0."
        )
    )
    parser.add_argument("--product", required=True, choices=_VALID_PRODUCTS)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", default="BUY", choices=("BUY", "SELL"))
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Place real Demo orders. Without it, dry-run (no mutation).",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help=(
            "Use the bounded app-managed TP/SL monitor (open → poll → "
            "MARKET-close on TP/SL or failsafe at window end) instead of the "
            "default immediate open+close-flat. Always ends flat in-run."
        ),
    )
    parser.add_argument(
        "--max-poll-count",
        type=int,
        default=30,
        help="Monitor bound: max price polls before failsafe close (default 30).",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if not _truthy(os.environ.get(_ENABLED_ENV)):
        logger.info(
            "demo scalping executor disabled — set %s=true to opt in", _ENABLED_ENV
        )
        return 0

    now = dt.datetime.now(dt.UTC)
    limits = ScalpingRiskLimits()

    # Lazy imports so the disabled path triggers zero engine/credential setup.
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo_scalping.market_data import (
        DemoScalpingMarketData,
        MarketConditionsUnavailable,
        build_market_conditions,
    )
    from app.services.brokers.binance.demo_scalping_exec.executor import (
        DemoScalpingExecutor,
        ExecutionResult,
    )
    from app.services.brokers.binance.demo_scalping_exec.reference import (
        DemoReferenceData,
    )

    if args.product == "spot":
        from app.services.brokers.binance.spot_demo.execution_client import (
            BinanceSpotDemoExecutionClient,
        )

        client = BinanceSpotDemoExecutionClient.from_env()
    else:
        from app.services.brokers.binance.futures_demo.execution_client import (
            BinanceFuturesDemoExecutionClient,
        )

        client = BinanceFuturesDemoExecutionClient.from_env()

    reference = DemoReferenceData()
    # ROB-841: the executor now fails closed without a server-derived market
    # snapshot, so build one for BOTH the immediate and monitored paths.
    market_data = DemoScalpingMarketData()
    # Built up-front (pure; no DB/broker) so an unavailable-market blocked
    # result can carry the correct product/symbol/side in its evidence.
    intent = build_manual_intent(
        product=args.product,
        symbol=args.symbol,
        side=args.side,
        now=now,
        limits=limits,
    )
    try:
        try:
            # ROB-841: the builder samples its own clock after both observations
            # complete, so fetch latency counts toward staleness.
            market = await build_market_conditions(
                market_data,
                product=args.product,
                symbol=args.symbol,
            )
        except MarketConditionsUnavailable as exc:
            # CLI boundary: an untrustworthy server market snapshot is a
            # blocked outcome (exit 1), NOT a runtime failure (exit 2). We
            # return before opening a DB session or constructing the executor,
            # so no broker submit / ledger touch occurs. Any OTHER exception
            # keeps propagating to the top-level runtime guard (exit 2).
            logger.warning(
                "demo scalping market conditions unavailable: %s", exc.reason
            )
            result = ExecutionResult(
                intent=intent,
                status="blocked",
                reason_codes=("market_conditions_unavailable",),
            )
            _evidence({"event": "demo_scalping_execute", **result.to_evidence_dict()})
            return _EXIT_BY_STATUS["blocked"]
        async with AsyncSessionLocal() as session:
            executor = DemoScalpingExecutor(
                product=args.product,
                client=client,
                session=session,
                reference=reference,
                now=now,
                limits=limits,
                market_data=market_data,
            )
            if args.monitor:
                result = await executor.execute_monitored(
                    intent,
                    confirm=args.confirm,
                    market=market,
                    max_poll_count=args.max_poll_count,
                )
            else:
                result = await executor.execute(
                    intent, confirm=args.confirm, market=market
                )
            await session.commit()
    finally:
        await reference.aclose()
        await market_data.aclose()
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()

    _evidence({"event": "demo_scalping_execute", **result.to_evidence_dict()})
    return _EXIT_BY_STATUS.get(result.status, 2)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        logger.error("demo scalping executor failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
