from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from collections.abc import Sequence
from typing import Any

from app.mcp_server.tooling.orders_toss_variants import (
    _toss_place_order_impl,
    toss_cancel_order,
    toss_place_order,
)
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl
from app.services.brokers.toss.client import TossReadClient


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def _print_event(event: str, payload: dict[str, Any]) -> None:
    safe = {"event": event, **payload}
    print(json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str))


def _validate_order_args(args: argparse.Namespace, mode_name: str) -> list[str]:
    errors: list[str] = []
    if args.market is None:
        errors.append(f"--market is required for {mode_name}")
    if not args.symbol:
        errors.append(f"--symbol is required for {mode_name}")
    elif len(args.symbol) != 1:
        errors.append(f"exactly one --symbol is required for {mode_name}")
    if not args.quantity:
        errors.append(f"--quantity is required for {mode_name}")
    if not args.price:
        errors.append(f"--price is required for {mode_name}")
    return errors


def _has_reconcile_anomaly(result: dict[str, Any]) -> bool:
    counts = result.get("counts")
    if not isinstance(counts, dict):
        return True
    return bool(counts.get("anomaly"))


async def _place_order_for_smoke(**kwargs: Any) -> dict[str, Any]:
    return await _toss_place_order_impl(**kwargs)


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


async def run_order_test(
    *,
    market: str,
    symbol: str,
    quantity: str,
    price: str,
    time_in_force: str,
) -> int:
    result = await toss_place_order(
        symbol=symbol,
        side="buy",
        order_type="limit",
        quantity=quantity,
        price=price,
        market=market,  # type: ignore[arg-type]
        time_in_force=time_in_force,  # type: ignore[arg-type]
        dry_run=True,
        confirm=False,
        reason="ROB-539 Toss live smoke order-test",
        account_mode="toss_live",
    )
    _print_event("toss_order_test_preview", result)
    return 0 if bool(result.get("success")) else 1


async def run_confirm(
    *,
    market: str,
    symbol: str,
    quantity: str,
    price: str,
    time_in_force: str,
) -> int:
    client_order_id = uuid.uuid4().hex
    opened_order_ids: list[str] = []
    exit_code = 0

    async def place_once(step: str) -> dict[str, Any]:
        result = await _place_order_for_smoke(
            symbol=symbol,
            side="buy",
            order_type="limit",
            quantity=quantity,
            price=price,
            order_amount=None,
            market=market,
            time_in_force=time_in_force,
            dry_run=False,
            confirm=True,
            confirm_high_value_order=False,
            reason="ROB-539 Toss live smoke confirm",
            exit_reason=None,
            thesis=None,
            strategy=None,
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes="ROB-539 live smoke: 1-share limit buy, immediate cancel",
            indicators_snapshot=None,
            report_item_uuid=None,
            account_mode="toss_live",
            account_type=None,
            client_order_id_override=client_order_id,
        )
        _print_event(step, result)
        # ROB-545: track any returned order_id, including from a failed/anomaly
        # response (e.g. the broker accepted the order but a UNIQUE/idempotency
        # conflict or DB write failed). A returned order_id means a live order
        # may exist, so finally-cancel must attempt to cancel it.
        order_id = result.get("order_id")
        if isinstance(order_id, str) and order_id not in opened_order_ids:
            opened_order_ids.append(order_id)
        return result

    try:
        first = await place_once("toss_confirm_place")
        if not bool(first.get("success")):
            return 1

        retry = await place_once("toss_confirm_idempotency_retry")
        if not bool(retry.get("success")):
            exit_code = 2
        elif retry.get("order_id") != first.get("order_id"):
            _print_event(
                "toss_confirm_idempotency_anomaly",
                {
                    "success": False,
                    "first_order_id": first.get("order_id"),
                    "retry_order_id": retry.get("order_id"),
                    "message": "Same clientOrderId returned a different order id.",
                },
            )
            exit_code = 2
    except Exception as exc:
        _print_event(
            "toss_confirm_exception",
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        exit_code = 2
    finally:
        for order_id in list(opened_order_ids):
            try:
                cancel = await toss_cancel_order(
                    order_id=order_id,
                    dry_run=False,
                    confirm=True,
                    account_mode="toss_live",
                )
                _print_event("toss_confirm_cancel", cancel)
                if not bool(cancel.get("success")):
                    exit_code = 2
            except Exception as exc:
                _print_event(
                    "toss_confirm_cancel_exception",
                    {
                        "success": False,
                        "order_id": order_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                exit_code = 2

        for order_id in list(opened_order_ids):
            try:
                preview = await toss_reconcile_orders_impl(
                    order_id=order_id,
                    symbol=symbol,
                    market=market,
                    dry_run=True,
                    limit=10,
                )
                _print_event("toss_confirm_reconcile_preview", preview)
                if _has_reconcile_anomaly(preview):
                    exit_code = 2
                    continue

                applied = await toss_reconcile_orders_impl(
                    order_id=order_id,
                    symbol=symbol,
                    market=market,
                    dry_run=False,
                    limit=10,
                )
                _print_event("toss_confirm_reconcile_apply", applied)
                if _has_reconcile_anomaly(applied):
                    exit_code = 2
            except Exception as exc:
                _print_event(
                    "toss_confirm_reconcile_exception",
                    {
                        "success": False,
                        "order_id": order_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                exit_code = 2

    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toss Open API live smoke")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--order-test", action="store_true")
    mode.add_argument("--confirm", action="store_true")
    parser.add_argument("--market", choices=["kr", "us"])
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--quantity")
    parser.add_argument("--price")
    parser.add_argument("--time-in-force", default="DAY", choices=["DAY", "CLS"])
    args = parser.parse_args(argv)

    if not (args.preflight or args.order_test or args.confirm):
        print("Toss live smoke disabled: pass --preflight, --order-test, or --confirm")
        return 0

    if args.preflight:
        if not _truthy(os.environ.get("TOSS_API_ENABLED")):
            print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
            return 0
        return asyncio.run(run_preflight(args.symbol or ["005930"]))

    if args.order_test:
        if not _truthy(os.environ.get("TOSS_API_ENABLED")):
            print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
            return 0
        errors = _validate_order_args(args, "--order-test")
        if errors:
            for error in errors:
                print(error)
            return 2
        return asyncio.run(
            run_order_test(
                market=args.market,
                symbol=args.symbol[0],
                quantity=args.quantity,
                price=args.price,
                time_in_force=args.time_in_force,
            )
        )

    if args.confirm:
        if not _truthy(os.environ.get("TOSS_API_ENABLED")):
            print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
            return 0
        if not _truthy(os.environ.get("TOSS_LIVE_ORDER_MUTATIONS_ENABLED")):
            print(
                "Toss live smoke disabled: "
                "TOSS_LIVE_ORDER_MUTATIONS_ENABLED is not truthy"
            )
            return 0
        errors = _validate_order_args(args, "--confirm")
        if errors:
            for error in errors:
                print(error)
            return 2
        return asyncio.run(
            run_confirm(
                market=args.market,
                symbol=args.symbol[0],
                quantity=args.quantity,
                price=args.price,
                time_in_force=args.time_in_force,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
