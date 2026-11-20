"""Operator commands for execution mandates (hk 5441, design 5438 §4.3, S6).

Every command previews by default: it prints what it would write and writes
nothing. ``--commit`` writes. The actor must be a person; a system actor name is
refused.

    propose   a mandate for a go-path sleeve (shadow, or live under D2 / D17)
    revoke    end a mandate; lists the children reserved before the revocation
    exit      an operator exit: a one-hour permission to sell out a position,
              and the sell intent that uses it (S6)
    control   set the account kill row (I7 b)
    list      mandates, their state and the open exit handoffs

A **shadow** mandate is created by ``--commit`` alone (D12): a shadow intent
cannot send. A **live** mandate is bound to the account the operator confirmed
(D2, D17): the preview prints the credential fingerprint, a summary of the
balances read with those credentials, and the content hash; ``--commit`` needs
all three repeated back (``--confirm-account-ref``, ``--confirm-content-sha256``)
and ``--approval-ref`` naming the hk decision doc that records the hash. No
amount has a default: the operator sets every value, inside the D9 ceilings.

The database URL comes from ``--database-url-env NAME`` (recommended) or
``--database-url``; its value is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.execution_mandates import (
    ACCOUNT_CONTROL_MODES,
    ExecutionExitHandoff,
    ExecutionMandate,
    ExecutionMandateRevocation,
)
from app.schemas.execution_intent import (
    UPBIT_LIVE_ACCOUNT,
    DecimalInventoryConstraint,
    ExecutionIntentV2,
)
from app.services.execution_intent_gateway.account_control import (
    AccountControlError,
    PostgresAccountControlStore,
    set_account_mode,
)
from app.services.execution_intent_gateway.mandates import (
    MandateDraft,
    MandateError,
    create_mandate,
    create_operator_exit,
    draft_mandate,
    human_actor,
    revoke_mandate,
)
from app.services.execution_intent_gateway.upbit_decisions import AccountReads
from app.services.execution_intent_gateway.upbit_live_forwarder import live_enabled
from app.services.execution_intent_gateway.upbit_store import (
    IntentRefused,
    UpbitGatewayStore,
)
from app.services.kis_mock_runner.control import read_effective_kill_switch
from app.services.strategy_sleeves.account_binding import configured_upbit_account_ref

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXIT_OK = 0
EXIT_ERROR = 1
#: Stable namespace: one operator exit row has exactly one intent id.
_OPERATOR_EXIT_NAMESPACE = uuid.UUID("5e3d7f0a-5441-4c1e-9d55-0f1a6b2c3d4e")

BalanceReader = Callable[[], Awaitable[list[dict[str, str]]]]


async def read_account_summary() -> list[dict[str, str]]:
    """Currency, quantity and average buy price of every balance (B23).

    Read with the configured credentials, so the operator can see which account
    the fingerprint stands for before approving a live mandate.
    """
    import app.services.brokers.upbit.client as upbit_service

    coins = await upbit_service.fetch_my_coins()
    return [
        {
            "currency": str(coin.get("currency")),
            "balance": str(coin.get("balance")),
            "locked": str(coin.get("locked")),
            "avg_buy_price": str(coin.get("avg_buy_price")),
        }
        for coin in coins
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hk 5441 execution mandates")
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url", help="Explicit PostgreSQL URL")
    database.add_argument(
        "--database-url-env",
        metavar="NAME",
        help="Environment variable holding the PostgreSQL URL (never printed)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    propose = sub.add_parser("propose", help="Preview or create a mandate")
    propose.add_argument("--sleeve-key", required=True)
    propose.add_argument("--strategy", required=True)
    propose.add_argument("--strategy-code-sha", required=True)
    propose.add_argument("--mode", required=True, choices=["shadow", "live"])
    propose.add_argument(
        "--symbol", action="append", required=True, help="KRW-XXX; repeatable"
    )
    propose.add_argument("--per-intent-max-notional-krw", required=True)
    propose.add_argument("--max-open-intents", required=True)
    propose.add_argument("--price-band-bps", required=True)
    propose.add_argument("--max-intent-ttl-seconds", required=True)
    propose.add_argument("--expires-at", required=True, help="ISO 8601 with a timezone")
    propose.add_argument("--actor", required=True)
    propose.add_argument("--approval-ref", help="live: decision/YYYY-MM-DD/<slug>")
    propose.add_argument("--confirm-content-sha256", help="live: from the preview")
    propose.add_argument("--confirm-account-ref", help="live: from the preview")
    propose.add_argument("--commit", action="store_true")

    revoke = sub.add_parser("revoke", help="Preview or write a revocation")
    revoke.add_argument("--mandate-id", required=True)
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--actor", required=True)
    revoke.add_argument("--commit", action="store_true")

    exit_cmd = sub.add_parser("exit", help="Preview or create an operator exit")
    exit_cmd.add_argument("--mandate", required=True, help="mandate id")
    exit_cmd.add_argument("--symbol", required=True)
    exit_cmd.add_argument("--max-quantity", required=True)
    exit_cmd.add_argument("--limit-price-krw", required=True)
    exit_cmd.add_argument("--max-slippage-bps", required=True)
    exit_cmd.add_argument("--reason", required=True)
    exit_cmd.add_argument("--actor", required=True)
    exit_cmd.add_argument("--commit", action="store_true")

    control = sub.add_parser("control", help="Preview or set the account kill row")
    control.add_argument("--account", required=True, choices=[UPBIT_LIVE_ACCOUNT])
    control.add_argument("--mode", required=True, choices=list(ACCOUNT_CONTROL_MODES))
    control.add_argument("--reason", required=True)
    control.add_argument("--actor", required=True)
    control.add_argument("--commit", action="store_true")

    sub.add_parser("list", help="Mandates and open exit handoffs (read only)")
    return parser


def _validate_database_url(raw: str) -> str:
    url = make_url(raw)
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError("--database-url must be a complete PostgreSQL URL")
    return url.render_as_string(hide_password=False)


def _database_url(args: argparse.Namespace) -> str:
    env_name = getattr(args, "database_url_env", None)
    if env_name is None:
        try:
            return _validate_database_url(args.database_url)
        except Exception:
            raise ValueError(
                "--database-url must be a complete PostgreSQL URL"
            ) from None
    if not isinstance(env_name, str) or not _ENV_NAME.fullmatch(env_name):
        raise ValueError("--database-url-env must be an environment variable name")
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raise ValueError(f"environment variable {env_name} is not set or empty")
    try:
        return _validate_database_url(raw.strip())
    except Exception:
        raise ValueError(
            f"environment variable {env_name} does not hold a complete PostgreSQL URL"
        ) from None


def _mode(args: argparse.Namespace) -> str:
    return "commit" if args.commit else "preview"


def _parse_time(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise MandateError("--expires-at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise MandateError("--expires-at needs a timezone")
    return parsed


def _mandate_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise MandateError("mandate id must be a UUID") from exc


def _draft_dict(draft: MandateDraft) -> dict[str, Any]:
    return {
        "sleeve_key": draft.sleeve_key,
        "sleeve_id": draft.sleeve_id,
        "sleeve_version": draft.sleeve_version,
        "sleeve_state": draft.sleeve_state,
        "sleeve_budget_cap": str(draft.sleeve_budget_cap),
        "sleeve_loss_limit": str(draft.sleeve_loss_limit),
        "strategy": draft.strategy,
        "strategy_code_sha": draft.strategy_code_sha,
        "account": draft.account,
        "mode": draft.mode,
        "allowed_symbols": list(draft.allowed_symbols),
        "per_intent_max_notional_krw": str(draft.per_intent_max_notional_krw),
        "max_open_intents": draft.max_open_intents,
        "price_band_bps": draft.price_band_bps,
        "max_intent_ttl_seconds": int(draft.max_intent_ttl.total_seconds()),
        "expires_at": draft.expires_at.isoformat(),
        "loss_sell_mode": draft.loss_sell_mode,
        "account_ref": draft.account_ref,
        "content_sha256": draft.content_sha256,
    }


async def _propose(
    session: AsyncSession,
    args: argparse.Namespace,
    *,
    now: datetime,
    account_ref: Callable[[], str | None],
    balance_reader: BalanceReader,
) -> dict[str, Any]:
    human_actor(args.actor)
    live = args.mode == "live"
    draft = await draft_mandate(
        session,
        sleeve_key=args.sleeve_key,
        strategy=args.strategy,
        strategy_code_sha=args.strategy_code_sha,
        mode=args.mode,
        allowed_symbols=args.symbol,
        per_intent_max_notional_krw=args.per_intent_max_notional_krw,
        max_open_intents=args.max_open_intents,
        price_band_bps=args.price_band_bps,
        max_intent_ttl_seconds=args.max_intent_ttl_seconds,
        expires_at=_parse_time(args.expires_at),
        # A shadow mandate sends nothing, so it is bound to no account.
        account_ref=account_ref() if live else None,
        now=now,
    )
    payload: dict[str, Any] = {"mode": _mode(args), "mandate": _draft_dict(draft)}
    if live:
        # B23: the operator approves this account, not just this fingerprint.
        payload["account_summary"] = await balance_reader()
    if not args.commit:
        await session.rollback()
        return payload
    row = await create_mandate(
        session,
        draft,
        actor=args.actor,
        approval_ref=args.approval_ref,
        confirm_content_sha256=args.confirm_content_sha256,
        confirm_account_ref=args.confirm_account_ref,
    )
    payload["mandate_id"] = str(row.id)
    payload["approval_ref"] = row.approval_ref
    await session.commit()
    return payload


async def _revoke(session: AsyncSession, args: argparse.Namespace) -> dict[str, Any]:
    row, pending = await revoke_mandate(
        session,
        mandate_id=_mandate_id(args.mandate_id),
        reason=args.reason,
        actor=args.actor,
    )
    payload = {
        "mode": _mode(args),
        "mandate_id": str(row.mandate_id),
        "revoked_by": row.revoked_by,
        # B10: reserved before this revocation; cancel or review them.
        "children_reserved_before_revocation": [
            {
                "intent_id": str(child.intent_id),
                "idempotency_key": child.idempotency_key,
                "action": child.action,
                "state": child.state,
                "broker_order_id": child.broker_order_id,
            }
            for child in pending
        ],
    }
    await (session.commit() if args.commit else session.rollback())
    return payload


def _whole(raw: Any, name: str) -> int:
    text_value = str(raw).strip()
    if not text_value.isdigit():
        raise MandateError(f"{name} must be a whole number")
    return int(text_value)


def _exit_intent(
    exit_row: Any, mandate: Any, args: argparse.Namespace, *, now: datetime
) -> ExecutionIntentV2:
    """The sell intent of an operator exit whose lifetime starts at ``now``.

    ``now`` is the instant the intent is stored: the store passes the clock it
    read under its locks. For a preview it is the time of the preview, and the
    expiry shown is what a store at that instant would give.
    """
    quantity = format(Decimal(str(exit_row.max_quantity)).normalize(), "f")
    # The intent lives no longer than its exit row, and no longer than the
    # mandate lets any intent live: an operator exit keeps that term (S6), so a
    # mandate with a short TTL gets a shorter exit intent, not a refused one.
    expires_at = min(exit_row.expires_at, now + mandate.max_intent_ttl)
    return ExecutionIntentV2(
        schema_version=2,
        intent_id=uuid.uuid5(_OPERATOR_EXIT_NAMESPACE, str(exit_row.id)),
        strategy=mandate.strategy,
        account=UPBIT_LIVE_ACCOUNT,
        venue="upbit",
        execution_mode=mandate.mode,
        mandate_id=mandate.id,
        sleeve_id=mandate.sleeve_id,
        symbol=exit_row.symbol,
        side="sell",
        max_quantity_decimal=quantity,
        # B7: a sell has no notional cap; the field is a sanity bound only.
        max_notional_krw=1_000_000_000_000,
        strategy_code_sha=mandate.strategy_code_sha,
        limit_price_krw=_whole(args.limit_price_krw, "--limit-price-krw"),
        max_slippage_bps=_whole(args.max_slippage_bps, "--max-slippage-bps"),
        expires_at=expires_at,
        inventory=DecimalInventoryConstraint(
            position_at_authorization=quantity,
            min_position_after="0",
            max_position_after=quantity,
        ),
        policy_version="operator-exit/v1",
        authorization_ref=f"operator-exit:{exit_row.id}",
        decision_ref=f"operator-exit:{exit_row.id}",
        origin="operator_exit",
        operator_exit_id=exit_row.id,
    )


async def _exit(
    session: AsyncSession,
    args: argparse.Namespace,
    *,
    now: datetime,
    session_factory: Any,
    environment: Any,
) -> dict[str, Any]:
    mandate_id = _mandate_id(args.mandate)
    exit_row = await create_operator_exit(
        session,
        mandate_id=mandate_id,
        symbol=args.symbol,
        max_quantity=args.max_quantity,
        reason=args.reason,
        actor=args.actor,
        now=now,
    )
    mandate_row = await session.get(ExecutionMandate, mandate_id)
    if mandate_row is None:  # pragma: no cover - create_operator_exit checked it
        raise MandateError("mandate does not exist")
    # Plain copies: the commit below expires the two ORM rows, and the intent
    # is built after it, when the store has its locks.
    exit_terms = SimpleNamespace(
        id=exit_row.id,
        symbol=exit_row.symbol,
        max_quantity=exit_row.max_quantity,
        expires_at=exit_row.expires_at,
    )
    mandate = SimpleNamespace(
        id=mandate_row.id,
        strategy=mandate_row.strategy,
        mode=mandate_row.mode,
        sleeve_id=mandate_row.sleeve_id,
        strategy_code_sha=mandate_row.strategy_code_sha,
        max_intent_ttl=mandate_row.max_intent_ttl,
    )
    payload: dict[str, Any] = {
        "mode": _mode(args),
        "operator_exit": {
            "id": str(exit_terms.id),
            "mandate_id": str(mandate_id),
            "symbol": exit_terms.symbol,
            "max_quantity": str(exit_terms.max_quantity),
            "expires_at": exit_terms.expires_at.isoformat(),
        },
        # A preview: the expiry a store at this instant would give.
        "intent": _exit_intent(exit_terms, mandate, args, now=now).model_dump(
            mode="json"
        ),
    }
    if not args.commit:
        await session.rollback()
        return payload
    await session.commit()
    kill = await read_effective_kill_switch(
        PostgresAccountControlStore(UPBIT_LIVE_ACCOUNT, session_factory)
    )
    try:
        # The intent's lifetime starts when it is stored, not when this command
        # started: the commit above, the kill read and the wait for the store's
        # locks must not use up a short ``max_intent_ttl`` (S6).
        stored = await UpbitGatewayStore(
            session_factory
        ).store_intent_for_operator_exit(
            lambda stored_at: _exit_intent(exit_terms, mandate, args, now=stored_at),
            sleeve_id=mandate.sleeve_id,
            mandate_id=mandate.id,
            reads=AccountReads(
                kill_mode=kill.mode, live_enabled=live_enabled(environment)
            ),
        )
    except IntentRefused as exc:
        # The exit row stays (it expires within the hour); no intent exists.
        payload["intent_stored"] = False
        payload["refusal"] = exc.refusal.value
        return payload
    # What was stored, with the expiry it was stored with.
    payload["intent"] = stored.model_dump(mode="json")
    payload["intent_stored"] = True
    return payload


async def _control(session: AsyncSession, args: argparse.Namespace) -> dict[str, Any]:
    state = await set_account_mode(
        session,
        account=args.account,
        mode=args.mode,
        reason=args.reason,
        actor=args.actor,
    )
    await (session.commit() if args.commit else session.rollback())
    return {
        "mode": _mode(args),
        "account": args.account,
        "control_mode": state.mode.value,
        "reason": state.reason,
        "updated_by": state.updated_by,
    }


async def _list(session: AsyncSession, *, now: datetime) -> dict[str, Any]:
    revoked = set(
        (await session.scalars(select(ExecutionMandateRevocation.mandate_id))).all()
    )
    mandates = (
        await session.scalars(
            select(ExecutionMandate).order_by(ExecutionMandate.created_at)
        )
    ).all()
    handoffs = (
        await session.scalars(
            select(ExecutionExitHandoff)
            .where(ExecutionExitHandoff.state != "closed")
            .order_by(ExecutionExitHandoff.id)
        )
    ).all()
    # Plain values first: the rollback below expires the loaded rows, and
    # reading one afterwards would need a query this call can no longer make.
    listed = {
        "mandates": [
            {
                "mandate_id": str(row.id),
                "sleeve_id": row.sleeve_id,
                "strategy": row.strategy,
                "mode": row.mode,
                "expires_at": row.expires_at.isoformat(),
                "state": (
                    "revoked"
                    if row.id in revoked
                    else "expired"
                    if row.expires_at <= now
                    else "active"
                ),
                "content_sha256": row.content_sha256,
            }
            for row in mandates
        ],
        "open_exit_handoffs": [
            {
                "handoff_id": row.id,
                "sleeve_id": row.sleeve_id,
                "symbol": row.symbol,
                "state": row.state,
                "open_reason": row.open_reason,
                "fills_unmeasured": row.fills_unmeasured,
                "proposal_id": None
                if row.proposal_id is None
                else str(row.proposal_id),
            }
            for row in handoffs
        ],
    }
    await session.rollback()
    return listed


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Any | None = None,
    now: datetime | None = None,
    account_ref: Callable[[], str | None] = configured_upbit_account_ref,
    balance_reader: BalanceReader = read_account_summary,
    environment: Any | None = None,
) -> tuple[int, Any]:
    engine = None
    if session_factory is None:
        engine = create_async_engine(_database_url(args))
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    moment = now or datetime.now(UTC)
    try:
        async with session_factory() as session:
            if args.command == "propose":
                payload = await _propose(
                    session,
                    args,
                    now=moment,
                    account_ref=account_ref,
                    balance_reader=balance_reader,
                )
            elif args.command == "revoke":
                payload = await _revoke(session, args)
            elif args.command == "exit":
                payload = await _exit(
                    session,
                    args,
                    now=moment,
                    session_factory=session_factory,
                    environment=os.environ if environment is None else environment,
                )
            elif args.command == "control":
                payload = await _control(session, args)
            else:
                payload = await _list(session, now=moment)
            return EXIT_OK, payload
    finally:
        if engine is not None:
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = asyncio.run(run(args))
    except (MandateError, AccountControlError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - report the class, never the DSN
        print(
            json.dumps({"error": f"database_error:{exc.__class__.__name__}"}),
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
