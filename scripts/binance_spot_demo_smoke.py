"""ROB-296 — Binance Spot Demo Mode smoke CLI (default-disabled).

Parallel to ``scripts.binance_testnet_scalper_smoke`` but targets the
Spot Demo endpoint (``https://demo-api.binance.com``) and is read-only
by design: this PR does NOT include order submission for Spot Demo (see
``BinanceSpotDemoOrderSubmitNotImplemented``).

Hard invariant: default behavior is fail-closed.

    uv run python -m scripts.binance_spot_demo_smoke
    # ⇒ exits 0, single log line:
    #    "spot demo disabled — set BINANCE_SPOT_DEMO_ENABLED=true to opt in"
    # zero HTTP, zero DB writes, zero Sentry events.

Opt-in (dry-run plan only; no HTTP):

    BINANCE_SPOT_DEMO_ENABLED=true \\
      uv run python -m scripts.binance_spot_demo_smoke --plan-only

Opt-in with credentials (read-only GET /api/v3/account preflight):

    BINANCE_SPOT_DEMO_ENABLED=true \\
      BINANCE_SPOT_DEMO_API_KEY=... \\
      BINANCE_SPOT_DEMO_API_SECRET=... \\
      uv run python -m scripts.binance_spot_demo_smoke --preflight

The ``--confirm`` flag is intentionally unsupported in this PR; the CLI
raises ``BinanceSpotDemoOrderSubmitNotImplemented`` with the exact
follow-up step.

Exit codes:
  0 — clean run (or default-disabled exit)
  1 — operator misconfiguration (missing env, missing credentials,
      ``--confirm`` requested)
  2 — runtime failure (HTTP error, server auth rejection, etc.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from decimal import Decimal

from app.services.brokers.binance.spot_demo import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoMissingCredentials,
    BinanceSpotDemoOrderSubmitNotImplemented,
    BinanceSpotDemoUnsupportedAuth,
    SpotDemoPreflightClient,
    plan_spot_demo_order,
)

logger = logging.getLogger("scripts.binance_spot_demo_smoke")


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-296 smoke CLI for the Binance Spot Demo Mode adapter. "
            "Default behavior is disabled (zero side effects). Set "
            "BINANCE_SPOT_DEMO_ENABLED=true + credentials to opt in. "
            "Read-only preflight is supported; order submission is NOT "
            "in this PR — use --confirm to see the operator follow-up "
            "instructions."
        )
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        default=False,
        help=(
            "Emit a source-labeled planned-order template without any "
            "HTTP. Safe to run with no credentials when the env gate is "
            "on."
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        default=False,
        help=(
            "Run a read-only GET /api/v3/account preflight against the "
            "Spot Demo endpoint. Requires env gate + credentials. Emits "
            "redacted evidence JSON."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help=(
            "Dry-run mode (default and the only supported mode in this "
            "PR). No order submission."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "Operator-only flag. Order submission is NOT implemented "
            "in this PR; passing this raises with follow-up instructions."
        ),
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="Symbol for the planned-order template (default: BTCUSDT).",
    )
    parser.add_argument(
        "--side",
        default="BUY",
        choices=["BUY", "SELL"],
        help="Side for the planned-order template (default: BUY).",
    )
    parser.add_argument(
        "--quantity",
        type=Decimal,
        default=Decimal("0.0001"),
        help="Quantity for the planned-order template (default: 0.0001).",
    )
    parser.add_argument(
        "--price",
        type=Decimal,
        default=None,
        help="Price for LIMIT orders. Omit for MARKET.",
    )
    parser.add_argument(
        "--order-type",
        default="MARKET",
        choices=["MARKET", "LIMIT"],
        help="Order type for the planned-order template (default: MARKET).",
    )
    parser.add_argument(
        "--max-notional-usdt",
        type=Decimal,
        default=None,
        help=(
            "Override the per-order notional cap. Default reads "
            "BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT (default 10)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return parser.parse_args(argv)


def _resolve_notional_cap(arg_value: Decimal | None) -> Decimal:
    if arg_value is not None:
        return arg_value
    raw = os.environ.get("BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT", "10")
    try:
        return Decimal(raw)
    except Exception:
        logger.warning(
            "BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT=%r is not a valid Decimal; "
            "falling back to 10",
            raw,
        )
        return Decimal("10")


def _emit_evidence(payload: dict[str, object]) -> None:
    """Stdout-stream a single source-labeled evidence JSON line."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


async def _run(args: argparse.Namespace) -> int:
    # Default-disabled gate — must match the testnet smoke CLI's UX.
    if not _truthy(os.environ.get("BINANCE_SPOT_DEMO_ENABLED")):
        logger.info(
            "spot demo disabled — set BINANCE_SPOT_DEMO_ENABLED=true to opt in"
        )
        return 0

    # --confirm: operator-only; not implemented in this PR.
    if args.confirm:
        raise BinanceSpotDemoOrderSubmitNotImplemented(
            "--confirm requires Spot Demo order submission, which is NOT "
            "included in the ROB-296 first PR. Follow-up: implement a Spot "
            "Demo execution client (mirror app/services/brokers/binance/"
            "testnet/execution_client.py under spot_demo/, decide ledger "
            "policy, and land behind operator approval). Until then, do not "
            "pass --confirm."
        )

    cap = _resolve_notional_cap(args.max_notional_usdt)

    if args.plan_only and not args.preflight:
        plan = plan_spot_demo_order(
            symbol=args.symbol,
            side=args.side,
            order_type=args.order_type,
            quantity=args.quantity,
            price=args.price,
            notional_cap_usdt=cap,
        )
        _emit_evidence(
            {
                "event": "spot_demo_plan",
                "plan": plan.to_evidence_dict(),
            }
        )
        return 0

    if args.preflight:
        try:
            client = SpotDemoPreflightClient.from_env()
        except BinanceSpotDemoMissingCredentials as exc:
            logger.error("preflight refused: %s", exc)
            return 1
        try:
            result = await client.preflight_account()
        finally:
            await client.aclose()
        _emit_evidence(
            {
                "event": "spot_demo_preflight",
                "preflight": result.to_evidence_dict(),
            }
        )
        # Optionally chain a plan emission so operators can compare in one run.
        if args.plan_only:
            plan = plan_spot_demo_order(
                symbol=args.symbol,
                side=args.side,
                order_type=args.order_type,
                quantity=args.quantity,
                price=args.price,
                notional_cap_usdt=cap,
            )
            _emit_evidence(
                {
                    "event": "spot_demo_plan",
                    "plan": plan.to_evidence_dict(),
                }
            )
        return 0

    # No action flag supplied while enabled — print guidance and exit 0.
    logger.info(
        "spot demo enabled but no action requested. Pass --plan-only for a "
        "no-HTTP planning template, or --preflight for a read-only "
        "GET /api/v3/account against demo-api.binance.com."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except BinanceSpotDemoDisabled as exc:
        logger.error("spot demo disabled: %s", exc)
        return 1
    except BinanceSpotDemoMissingCredentials as exc:
        logger.error("spot demo credentials missing: %s", exc)
        return 1
    except BinanceSpotDemoOrderSubmitNotImplemented as exc:
        logger.error("spot demo order submit not implemented: %s", exc)
        return 1
    except BinanceSpotDemoUnsupportedAuth as exc:
        logger.error("spot demo unsupported auth: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
