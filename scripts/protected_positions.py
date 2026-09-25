#!/usr/bin/env python3
"""Read protected-position declarations from an explicitly selected database.

This intentionally has only list, show, and history commands.  It has no
broker client, no mutation command, and no implicit DATABASE_URL fallback;
an operator must name the database URL on every invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.protected_position_settings import read_protected_position_history
from app.services.protected_quantity_service import (
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
    normalize_protection_key,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only protected-position declaration inspection"
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="Explicit PostgreSQL database URL; no environment fallback is used",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format, default json is stable and machine-readable",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list", help="List declaration heads")
    list_parser.add_argument("--scope", dest="account_scope")
    for command in ("show", "history"):
        command_parser = commands.add_parser(
            command,
            help=f"{command.title()} one normalized declaration key",
        )
        command_parser.add_argument("account_scope")
        command_parser.add_argument("market")
        command_parser.add_argument("symbol")
    return parser


def _validate_database_url(raw: str) -> str:
    url = make_url(raw)
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError("--database-url must be a complete PostgreSQL URL")
    return url.render_as_string(hide_password=False)


def _head_payload(head: Any) -> dict[str, Any]:
    return {
        "account_scope": head.key.account_scope,
        "market": head.key.market,
        "symbol": head.key.symbol,
        "protected_quantity": format(head.protected_quantity, "f"),
        "revision": head.revision,
        "last_confirmed_broker_held": format(
            head.last_confirmed_broker_held,
            "f",
        ),
        "last_confirmed_at": head.last_confirmed_at.isoformat(),
        "updated_by_user_id": head.updated_by_user_id,
        "updated_at": head.updated_at.isoformat(),
    }


async def read_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Read the selected durable rows without calling a broker API."""

    engine = create_async_engine(_validate_database_url(args.database_url))
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as session:
            service = ProtectedQuantityService(session)
            if args.command == "list":
                heads = await service.list(account_scope=args.account_scope)
                return 0, {"positions": [_head_payload(head) for head in heads]}

            key = normalize_protection_key(
                account_scope=args.account_scope,
                market=args.market,
                symbol=args.symbol,
            )
            head = await service.get(key=key)
            if head is None:
                return 2, {
                    "error": "not_found",
                    "account_scope": key.account_scope,
                    "market": key.market,
                    "symbol": key.symbol,
                }
            if args.command == "show":
                return 0, {"position": _head_payload(head)}
            return 0, {
                "position": _head_payload(head),
                "history": await read_protected_position_history(session, key=key),
            }
    finally:
        await engine.dispose()


def _render_text(payload: dict[str, Any]) -> str:
    if "error" in payload:
        return f"error: {payload['error']}"
    positions = payload.get("positions")
    if isinstance(positions, list):
        if not positions:
            return "no protected positions"
        return "\n".join(
            " ".join(
                (
                    str(position["account_scope"]),
                    str(position["market"]),
                    str(position["symbol"]),
                    f"P={position['protected_quantity']}",
                    f"revision={position['revision']}",
                )
            )
            for position in positions
        )
    position = payload.get("position")
    if isinstance(position, dict):
        lines = [
            " ".join(
                (
                    str(position["account_scope"]),
                    str(position["market"]),
                    str(position["symbol"]),
                    f"P={position['protected_quantity']}",
                    f"revision={position['revision']}",
                )
            )
        ]
        history = payload.get("history")
        if isinstance(history, list):
            lines.extend(
                f"revision={item['revision']} action={item['action']} "
                f"quantity={item['new_quantity']}"
                for item in history
            )
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        exit_code, payload = asyncio.run(read_command(args))
    except (ProtectedQuantityValidationError, ValueError) as exc:
        print(
            json.dumps(
                {"error": "invalid_request", "message": str(exc)}, sort_keys=True
            )
        )
        return 2
    except Exception:
        print(json.dumps({"error": "protected_positions_unavailable"}, sort_keys=True))
        return 1
    if args.format == "text":
        print(_render_text(payload))
    else:
        print(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
