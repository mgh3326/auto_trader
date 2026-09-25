#!/usr/bin/env python
"""NHPLUG (NH namuh) Stage 2 MOCK-account order smoke CLI (#711).

Operator-only.  Every network mode needs ``NHPLUG_MOCK_ENABLED=true`` in the
process environment and the dedicated three-key env file used by the Stage 1
smoke (``NHPLUG_APP_KEY``, ``NHPLUG_APP_SECRET``, ``NHPLUG_MOCK_ACCOUNT_NO``).
Order modes additionally need ``--confirm-mock-order``; read modes need
``--confirm-read``.  Ledger modes use the application database resolved by the
normal settings (``DATABASE_URL``); the CLI refuses a ``prod`` ``ENV_FILE``.

Modes:

- ``preflight``   offline: env file shape, gate, scope constants (0 network)
- ``positions``   read cash + positions
- ``open-orders`` two-source open-order read (present/none_confirmed/unknown)
- ``history``     daily order/fill listing
- ``place``       one confirmed KRX limit order (ledger row first)
- ``modify``      one confirmed limit-price modification
- ``cancel``      one confirmed cancel (full remainder by default)
- ``reconcile``   ledger reconcile (``--apply`` writes; default plans only)
- ``roundtrip``   order -> query -> modify -> query -> cancel -> query ->
                  reconcile -> empty-listing check, in one process with one
                  OAuth token; any unexpected state triggers a best-effort
                  cancel of the live test order and exits 2.

Output is one JSON object per line.  Credential values, tokens, account
numbers, customer names, and raw broker bodies are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.client import (
    ALLOWED_MUTATION_PATHS,
    ALLOWED_READONLY_PATHS,
    NHPlugMockClient,
)
from app.services.nhplug_mock import operations
from scripts.nhplug_mock_smoke import (
    DEFAULT_ENV_FILE,
    REQUIRED_ENV_KEYS,
    SmokeConfigurationError,
    _load_minimal_env,
)

ORDER_MODES = frozenset({"place", "modify", "cancel", "roundtrip"})
LEDGER_MODES = frozenset(
    {"place", "modify", "cancel", "reconcile", "open-orders", "roundtrip"}
)
READ_MODES = frozenset({"positions", "open-orders", "history", "reconcile"})
DEFAULT_SETTLE_SECONDS = 3.0


class RoundtripAbort(RuntimeError):
    """An unexpected broker state; carries a value-free reason."""


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _step(name: str, result: dict[str, Any]) -> dict[str, Any]:
    _emit({"step": name, **result})
    return result


async def _ledger_scope(
    body: Callable[[Any], Awaitable[int]],
) -> int:
    from app.core.db import AsyncSessionLocal
    from app.services.nhplug_mock.ledger_service import NHPlugMockLedgerService

    async with AsyncSessionLocal() as session:
        return await body(NHPlugMockLedgerService(session))


def _open_ids(result: dict[str, Any]) -> set[str]:
    return {str(row.get("order_no")) for row in result.get("open_orders", [])}


async def _roundtrip(
    args: argparse.Namespace,
    client: NHPlugMockClient,
    ledger: Any,
    order_date: str,
) -> int:
    async def factory() -> NHPlugMockClient:
        return client

    settle = float(args.settle_seconds)
    live_order_id: str | None = None
    try:
        _step("1_positions", await operations.get_positions(client))
        before = _step(
            "2_open_orders_before",
            await operations.get_open_orders(
                client, order_date=order_date, ledger=ledger
            ),
        )
        if before["open_orders_state"] == "unknown":
            raise RoundtripAbort("open-order listing is unknown before the test")

        placed = _step(
            "3_place_limit_buy",
            await operations.place_limit_order(
                client_factory=factory,
                ledger=ledger,
                side="buy",
                symbol=args.symbol,
                quantity=args.quantity,
                price=args.price,
                dry_run=False,
                confirm=True,
                strategy="nhplug_stage2_smoke",
                reason="#711 mock round-trip smoke",
                order_date=order_date,
            ),
        )
        if placed.get("status") != "accepted":
            if placed.get("status") == "acceptance_uncertain":
                raise RoundtripAbort("place acceptance uncertain")
            raise RoundtripAbort("place was not accepted")
        live_order_id = str(placed["broker_order_id"])

        await asyncio.sleep(settle)
        after_place = _step(
            "4_open_orders_after_place",
            await operations.get_open_orders(
                client, order_date=order_date, ledger=ledger
            ),
        )
        if after_place[
            "open_orders_state"
        ] != "present" or live_order_id not in _open_ids(after_place):
            raise RoundtripAbort(
                "placed order is not visible as open (order-number or listing mismatch)"
            )

        modified = _step(
            "5_modify_limit_price",
            await operations.modify_limit_order(
                client_factory=factory,
                ledger=ledger,
                order_id=live_order_id,
                symbol=args.symbol,
                new_price=args.modify_price,
                dry_run=False,
                confirm=True,
                order_date=order_date,
            ),
        )
        if modified.get("status") != "accepted":
            raise RoundtripAbort("modify was not accepted")
        live_order_id = str(modified["broker_order_id"])

        await asyncio.sleep(settle)
        after_modify = _step(
            "6_open_orders_after_modify",
            await operations.get_open_orders(
                client, order_date=order_date, ledger=ledger
            ),
        )
        if live_order_id not in _open_ids(after_modify):
            raise RoundtripAbort("modified order is not visible as open")

        cancelled = _step(
            "7_cancel_full",
            await operations.cancel_order(
                client_factory=factory,
                ledger=ledger,
                order_id=live_order_id,
                symbol=args.symbol,
                dry_run=False,
                confirm=True,
                order_date=order_date,
            ),
        )
        if cancelled.get("status") != "accepted":
            raise RoundtripAbort("cancel was not accepted")

        await asyncio.sleep(settle)
        after_cancel = _step(
            "8_open_orders_after_cancel",
            await operations.get_open_orders(
                client, order_date=order_date, ledger=ledger
            ),
        )
        if live_order_id in _open_ids(after_cancel):
            raise RoundtripAbort("cancelled order is still listed as open")
        live_order_id = None

        _step(
            "9_reconcile_apply",
            await operations.reconcile_orders(
                client, ledger, order_date=order_date, dry_run=False
            ),
        )
        final = _step(
            "10_empty_listing_check",
            await operations.get_open_orders(
                client, order_date=order_date, ledger=ledger
            ),
        )
        _emit(
            {
                "step": "summary",
                "status": "ok",
                "final_open_orders_state": final["open_orders_state"],
                "empty_listing_is_two_source_confirmed": final["open_orders_state"]
                in {"none_confirmed", "present"}
                and all(source["complete"] for source in final["sources"]),
            }
        )
        return 0
    except BaseException as error:
        reason = (
            str(error) if isinstance(error, RoundtripAbort) else type(error).__name__
        )
        cleanup: dict[str, Any] = {"attempted": False}
        if live_order_id is not None:
            cleanup["attempted"] = True
            try:
                result = await operations.cancel_order(
                    client_factory=factory,
                    ledger=ledger,
                    order_id=live_order_id,
                    symbol=args.symbol,
                    dry_run=False,
                    confirm=True,
                    order_date=order_date,
                )
                cleanup["status"] = result.get("status")
                cleanup["error_code"] = result.get("error_code")
            except BaseException as cleanup_error:  # noqa: BLE001
                cleanup["status"] = "cleanup_failed"
                cleanup["error_type"] = type(cleanup_error).__name__
        _emit(
            {
                "step": "abort",
                "status": "aborted",
                "reason": reason,
                "live_test_order_id": live_order_id,
                "cleanup_cancel": cleanup,
                "operator_action": (
                    "verify in the NH namuh MOCK app/HTS that no test order remains "
                    "open; cancel manually if one does, then run --mode reconcile --apply"
                ),
            }
        )
        return 2


async def run(args: argparse.Namespace) -> int:
    try:
        credentials_raw = _load_minimal_env(Path(args.env_file))
        if os.getenv("NHPLUG_MOCK_ENABLED", "").strip().lower() != "true":
            raise SmokeConfigurationError(
                "NHPLUG_MOCK_ENABLED=true is required in the process environment"
            )
        if args.mode == "preflight":
            _emit(
                {
                    "mode": "preflight",
                    "status": "ready",
                    "network_calls": 0,
                    "required_env_keys": list(REQUIRED_ENV_KEYS),
                    "readonly_path_count": len(ALLOWED_READONLY_PATHS),
                    "mutation_path_count": len(ALLOWED_MUTATION_PATHS),
                    "order_type": "limit_only",
                    "venue": "KRX",
                    "confirm_mock_order_required_for_orders": True,
                }
            )
            return 0
        if args.mode in ORDER_MODES and not args.confirm_mock_order:
            raise SmokeConfigurationError(
                "--confirm-mock-order is required for order modes"
            )
        if args.mode in READ_MODES and not args.confirm_read:
            raise SmokeConfigurationError("--confirm-read is required for read modes")
        if args.mode in {"place", "roundtrip"} and args.price is None:
            raise SmokeConfigurationError("--price (a resting limit price) is required")
        if args.mode == "roundtrip" and args.modify_price is None:
            raise SmokeConfigurationError("--modify-price is required for roundtrip")
        if args.mode in {"modify", "cancel"} and not args.order_id:
            raise SmokeConfigurationError("--order-id is required")
        if args.mode == "modify" and args.modify_price is None:
            raise SmokeConfigurationError("--modify-price is required for modify")

        credentials = operations.NHPlugMockCredentials(
            app_key=credentials_raw.app_key,
            app_secret=credentials_raw.app_secret,
            account_no=credentials_raw.account_no,
        )
        auth = NHPlugAuthClient(
            app_key=credentials.app_key, app_secret=credentials.app_secret
        )
        client = await operations.open_verified_client(
            credentials, token_provider=auth.get_access_token
        )
        order_date = args.order_date or operations.today_order_date()
        _emit(
            {
                "mode": args.mode,
                "step": "0_account_verified",
                "acct_type": "03",
                "order_date": order_date,
            }
        )

        async def factory() -> NHPlugMockClient:
            return client

        if args.mode == "positions":
            _step("positions", await operations.get_positions(client))
            return 0
        if args.mode == "history":
            result = await operations.get_order_history(
                client, order_date=order_date, scope=args.scope
            )
            _step("history", result)
            return 0 if result["success"] else 2

        async def with_ledger(ledger: Any) -> int:
            if args.mode == "open-orders":
                result = await operations.get_open_orders(
                    client, order_date=order_date, ledger=ledger
                )
                _step("open_orders", result)
                return 0 if result["success"] else 2
            if args.mode == "reconcile":
                result = await operations.reconcile_orders(
                    client, ledger, order_date=order_date, dry_run=not args.apply
                )
                _step("reconcile", result)
                return 0 if result["success"] else 2
            if args.mode == "place":
                result = await operations.place_limit_order(
                    client_factory=factory,
                    ledger=ledger,
                    side=args.side,
                    symbol=args.symbol,
                    quantity=args.quantity,
                    price=args.price,
                    dry_run=False,
                    confirm=True,
                    strategy="nhplug_stage2_smoke",
                    order_date=order_date,
                )
                _step("place", result)
                return 0 if result.get("status") == "accepted" else 2
            if args.mode == "modify":
                result = await operations.modify_limit_order(
                    client_factory=factory,
                    ledger=ledger,
                    order_id=args.order_id,
                    symbol=args.symbol,
                    new_price=args.modify_price,
                    dry_run=False,
                    confirm=True,
                    order_date=order_date,
                )
                _step("modify", result)
                return 0 if result.get("status") == "accepted" else 2
            if args.mode == "cancel":
                result = await operations.cancel_order(
                    client_factory=factory,
                    ledger=ledger,
                    order_id=args.order_id,
                    symbol=args.symbol,
                    dry_run=False,
                    confirm=True,
                    order_date=order_date,
                )
                _step("cancel", result)
                return 0 if result.get("status") == "accepted" else 2
            return await _roundtrip(args, client, ledger, order_date)

        return await _ledger_scope(with_ledger)
    except Exception as error:  # noqa: BLE001 - CLI returns a value-free failure
        failure: dict[str, Any] = {
            "mode": args.mode,
            "status": "failed",
            "error_type": type(error).__name__,
        }
        if isinstance(error, SmokeConfigurationError):
            failure["reason"] = str(error)
        _emit(failure)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument(
        "--mode",
        choices=(
            "preflight",
            "positions",
            "open-orders",
            "history",
            "place",
            "modify",
            "cancel",
            "reconcile",
            "roundtrip",
        ),
        default="preflight",
    )
    parser.add_argument("--confirm-read", action="store_true")
    parser.add_argument("--confirm-mock-order", action="store_true")
    parser.add_argument("--apply", action="store_true", help="reconcile writes")
    parser.add_argument("--symbol", default="005930")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--price", type=int, default=None)
    parser.add_argument("--modify-price", type=int, default=None)
    parser.add_argument("--order-id", default=None)
    parser.add_argument(
        "--order-date", default=None, help="YYYYMMDD, default KST today"
    )
    parser.add_argument("--scope", choices=("all", "filled", "open"), default="all")
    parser.add_argument("--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
