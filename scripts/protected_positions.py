#!/usr/bin/env python3
"""Inspect and (desk only) change protected-position declarations.

Read commands (list, show, history) use only the explicitly named database:
no broker client and no settings import, and no implicit DATABASE_URL
fallback.

Write commands (declare, increase, decrease, release; #943) are the reviewed
replacement for ad hoc declaration scripts.  They call
``ProtectedQuantityService.save`` with ``origin='operator_cli'``, the fixed
owner actor, and the fresh broker observation provider that ``save`` invokes
only after its per-key lock.  Every write requires an exact ``--confirm-symbol``
and is a dry-run preview unless ``--commit`` is given.  The broker read needs
the application's broker credentials; the database is still only the explicit
``--database-url``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.protected_position_history import read_protected_position_history
from app.services.protected_quantity_service import (
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
    normalize_protection_key,
)

WRITE_COMMANDS = ("declare", "increase", "decrease", "release")
LEVER_COMMAND = "auto-reconcile"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Protected-position declarations: read-only inspection, and "
            "desk-only dry-run-by-default writes"
        )
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
    for command in WRITE_COMMANDS:
        write_parser = commands.add_parser(
            command,
            help=f"{command.title()} one declaration (dry-run unless --commit)",
        )
        write_parser.add_argument("account_scope")
        write_parser.add_argument("market")
        write_parser.add_argument("symbol")
        if command != "release":
            write_parser.add_argument(
                "--quantity",
                required=True,
                help="New protected quantity as a Decimal string",
            )
        if command != "declare":
            write_parser.add_argument(
                "--expected-revision",
                type=int,
                required=True,
                help="Current head revision (optimistic concurrency token)",
            )
        write_parser.add_argument("--reason", required=True)
        write_parser.add_argument(
            "--confirm-symbol",
            required=True,
            help="Re-type the symbol; it must normalize to the same key",
        )
        write_parser.add_argument(
            "--idempotency-key",
            help="Reuse to retry the same request safely; generated if omitted",
        )
        write_parser.add_argument(
            "--commit",
            action="store_true",
            help="Write the revision; without it only a preview is printed",
        )
    lever_parser = commands.add_parser(
        LEVER_COMMAND,
        help=(
            "#943 lever: lower every active declared P above the fresh holding "
            "(dry-run unless --commit; no-op while the kill switch is off)"
        ),
    )
    lever_parser.add_argument("--scope", dest="account_scope")
    lever_parser.add_argument(
        "--commit",
        action="store_true",
        help="Write the lowering revisions; without it only a preview is printed",
    )
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


def _default_provider_factory(key: Any) -> Any:
    async def observe() -> Any:
        from app.services.protected_position_settings import (
            fresh_broker_observation,
        )

        return await fresh_broker_observation(key=key)

    return observe


def _default_actor() -> int:
    from app.services.protected_position_auto_follow import (
        protection_owner_user_id,
    )

    return protection_owner_user_id()


def _write_mismatch(command: str, message: str) -> tuple[int, dict[str, Any]]:
    return 2, {"error": "command_mismatch", "command": command, "message": message}


def _check_command(
    command: str, *, head: Any, expected_revision: int | None, new_quantity: Decimal
) -> tuple[int, dict[str, Any]] | None:
    """Refuse a write whose command word disagrees with the resulting action."""

    if command == "declare":
        if head is not None:
            return _write_mismatch(
                command, "a declaration already exists; use increase/decrease/release"
            )
        if new_quantity <= 0:
            return _write_mismatch(command, "declare requires a positive quantity")
        return None
    if head is None:
        return 2, {"error": "not_found", "command": command}
    if expected_revision != head.revision:
        return 3, {
            "error": "stale_form",
            "current_revision": head.revision,
            "current_protected_quantity": format(head.protected_quantity, "f"),
        }
    current = head.protected_quantity
    if command == "increase" and not new_quantity > current:
        return _write_mismatch(command, "increase requires quantity above current P")
    if command == "decrease" and not (0 < new_quantity < current):
        return _write_mismatch(
            command, "decrease requires 0 < quantity < current P; use release for 0"
        )
    if command == "release" and current == 0:
        return _write_mismatch(command, "the declaration is already released")
    return None


async def write_command(
    args: argparse.Namespace,
    *,
    provider_factory: Any | None = None,
    actor_resolver: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    """Preview (default) or commit one declaration revision via the service."""

    from app.services.protected_position_settings import (
        BrokerObservationUnavailable,
        protection_change_preview,
    )
    from app.services.protected_quantity_service import (
        ProtectedQuantityConflictError,
        parse_operator_quantity,
    )

    command = args.command
    key = normalize_protection_key(
        account_scope=args.account_scope, market=args.market, symbol=args.symbol
    )
    confirmed = normalize_protection_key(
        account_scope=args.account_scope,
        market=args.market,
        symbol=args.confirm_symbol,
    )
    if confirmed.symbol != key.symbol:
        return 2, {
            "error": "symbol_confirmation_mismatch",
            "symbol": key.symbol,
            "confirm_symbol": confirmed.symbol,
        }
    raw_quantity = "0" if command == "release" else args.quantity
    new_quantity = parse_operator_quantity(raw_quantity, field="quantity")
    expected_revision = getattr(args, "expected_revision", None)
    if not isinstance(args.reason, str) or not args.reason.strip():
        raise ProtectedQuantityValidationError("reason is required")
    factory = provider_factory or _default_provider_factory
    idempotency_key = args.idempotency_key or f"operator_cli:{uuid4()}"

    engine = create_async_engine(_validate_database_url(args.database_url))
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as session:
            service = ProtectedQuantityService(session)
            head = await service.get(key=key)
            refused = _check_command(
                command,
                head=head,
                expected_revision=expected_revision,
                new_quantity=new_quantity,
            )
            if refused is not None:
                return refused
            if not args.commit:
                try:
                    observation = await factory(key)()
                except BrokerObservationUnavailable as exc:
                    return 1, {"error": "broker_read_failed", "message": str(exc)}
                return 0, {
                    "dry_run": True,
                    "command": command,
                    "expected_revision": expected_revision,
                    "idempotency_key": idempotency_key,
                    "preview": protection_change_preview(
                        key=key,
                        previous_quantity=(
                            head.protected_quantity if head else Decimal("0")
                        ),
                        new_quantity=new_quantity,
                        observation=observation,
                    ),
                    "exceeds_broker_held": new_quantity > observation.held,
                }
            actor_user_id = (actor_resolver or _default_actor)()
            try:
                result = await service.save(
                    account_scope=key.account_scope,
                    market=key.market,
                    symbol=key.symbol,
                    protected_quantity=raw_quantity,
                    expected_revision=expected_revision,
                    reason=args.reason,
                    idempotency_key=idempotency_key,
                    actor_user_id=actor_user_id,
                    origin="operator_cli",
                    observation_provider=factory(key),
                    confirm_protection_change=True,
                    confirm_symbol=args.confirm_symbol,
                )
            except BrokerObservationUnavailable as exc:
                return 1, {"error": "broker_read_failed", "message": str(exc)}
            except ProtectedQuantityConflictError as exc:
                return 3, {"error": exc.error, "message": str(exc), **exc.context}
            return 0, {
                "dry_run": False,
                "command": command,
                "action": result.action,
                "revision": result.revision,
                "idempotent_replay": result.idempotent_replay,
                "idempotency_key": idempotency_key,
                "position": _head_payload(result.head),
            }
    finally:
        await engine.dispose()


async def lever_command(
    args: argparse.Namespace,
    *,
    provider_factory: Any | None = None,
    notify: Any | None = None,
    settings_obj: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run the scheduleless #943 reconcile lever against the named database."""

    from app.services.protected_position_auto_follow import (
        reconcile_declared_positions,
    )

    engine = create_async_engine(_validate_database_url(args.database_url))
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        payload = await reconcile_declared_positions(
            account_scope=args.account_scope,
            dry_run=not args.commit,
            session_factory=session_factory,
            provider_factory=provider_factory,
            notify=notify,
            settings_obj=settings_obj,
        )
    finally:
        await engine.dispose()
    if payload["status"] == "disabled":
        return 2, {"error": "auto_follow_disabled", **payload}
    failed = any(item["status"] == "error" for item in payload["outcomes"])
    return (1 if failed else 0), payload


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in WRITE_COMMANDS:
        runner = write_command
    elif args.command == LEVER_COMMAND:
        runner = lever_command
    else:
        runner = read_command
    try:
        exit_code, payload = asyncio.run(runner(args))
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
