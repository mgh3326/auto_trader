#!/usr/bin/env python3
"""Operator CLI for the hk 3795 morning approval batch (ticket T3a).

Subcommands:

``create``   make the basket's batch for its next session, with a card for every
             plan that needs an answer, and send the summary and the cards.
``resend``   re-send the cards of a batch that are still unanswered. A re-send is not
             a new batch: each card gets a new nonce and the older card stops
             answering.
``status``   read-only: the batch, its plans, the newest card of each and the answer.

Nothing is scheduled: a batch exists only on the day an operator runs ``create``
(Q8 = A). Every write needs ``--actor operator:<name>``.

Writes and Telegram sends happen only with ``--commit``. Without it the same store
checks run and roll back, and nothing is sent. With it the rows commit first and
the messages go out after, so a delivered card always names a stored nonce digest;
a failed send leaves the row and is repaired with ``resend`` (exit code 2).

The database is named explicitly (``--database-url-env NAME`` or ``--database-url``);
the URL value is never printed. Nonces are never printed either: they exist only
inside the card's button data.

Runbook: ``docs/runbooks/mandate-batch-card.md``.

Exit codes: 0 ok, 1 input, refusal or database error, 2 committed but a send failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.mandate import MandatePlan, MandateTerms
from app.services.mandate import batch_cards as cards
from app.services.mandate import batch_rules as rules
from app.services.mandate import batches as bt
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from scripts.mandate import (
    EXIT_ERROR,
    EXIT_OK,
    MandateCliError,
    _database_url,
    _operator_actor,
    _positive_int,
)

EXIT_SEND_FAILED = 2
#: ``(text, inline_keyboard | None) -> (ok, failure_code)``
Sender = Callable[[str, dict | None], Awaitable[tuple[bool, str | None]]]


def default_sender(client: httpx.AsyncClient) -> Sender:
    """Send to the first chat on the approval allowlist with the approval bot."""

    async def send(text: str, keyboard: dict | None) -> tuple[bool, str | None]:
        from app.monitoring.trade_notifier.transports import send_telegram_message

        token = settings.ORDER_PROPOSALS_TELEGRAM_BOT_TOKEN
        chats = settings.order_proposals_telegram_chat_allowlist
        if not token or not chats:
            return False, "telegram_not_configured"
        result = await send_telegram_message(
            http_client=client,
            bot_token=token,
            chat_id=chats[0],
            text=text,
            parse_mode=None,
            reply_markup=keyboard,
        )
        return bool(result.ok), None if result.ok else result.failure_code

    return send


async def _card_facts(
    session: AsyncSession,
    batch: Any,
    member: Any,
) -> cards.CardFacts:
    plan = await session.get(MandatePlan, member.plan_id)
    terms = await session.get(MandateTerms, member.terms_id)
    assert plan is not None and terms is not None
    return cards.CardFacts(
        kind=member.kind,
        market=batch.market,
        basket_key=batch.basket_key,
        symbol=plan.symbol,
        planned_amount=plan.planned_amount,
        adopted_amount=plan.adopted_amount,
        sector_key=plan.sector_key,
        thesis=plan.thesis,
        invalidation_conditions=tuple(plan.invalidation_conditions),
        terms_id=terms.id,
        terms_sha256=member.terms_sha256,
        plan_sha256=member.plan_sha256,
        target_session=batch.target_session,
        approval_deadline_at=batch.approval_deadline_at,
        expires_at=batch.expires_at,
        card_no=member.card_no,
        open_order_count=await bt.open_order_count(session, plan.id),
    )


async def _armed_tranches(
    session: AsyncSession, realm: str, basket_key: str
) -> tuple[tuple[str, int], ...]:
    """Tranches armed but without an open proposal, of the basket's live plans (§5.1 (c))."""
    found: list[tuple[str, int]] = []
    modes = [m for m, r in v.REALM_BY_ACCOUNT_MODE.items() if r == realm]
    from app.models.strategy_sleeves import StrategySleeve

    sleeves = (
        (
            await session.execute(
                select(StrategySleeve.id).where(StrategySleeve.account_mode.in_(modes))
            )
        )
        .scalars()
        .all()
    )
    for sleeve_id in sleeves:
        _, terms = await ms.current_terms(session, sleeve_id)
        if terms is None or terms.basket_key != basket_key:
            continue
        plans = (
            (
                await session.execute(
                    select(MandatePlan).where(
                        MandatePlan.sleeve_id == sleeve_id, ms._unreleased()
                    )
                )
            )
            .scalars()
            .all()
        )
        for plan in plans:
            state = await ms.plan_state(session, plan.id)
            if state.authority not in v.SEND_AUTHORITY:
                continue
            found += [
                (plan.symbol, t.index)
                for t in state.tranches
                if t.armed and not t.completed and not t.open_proposals
            ]
    return tuple(sorted(found))


def _card_row(card: bt.IssuedCard, facts: cards.CardFacts | None) -> dict[str, Any]:
    return {
        "plan_id": card.member.plan_id,
        "card_no": card.member.card_no,
        "kind": card.member.kind,
        "terms_id": card.member.terms_id,
        "symbol": None if facts is None else facts.symbol,
    }


@dataclass(frozen=True)
class Outgoing:
    """A card to send, copied out of the ORM before the commit expires it."""

    member_id: int
    plan_id: int
    card_no: int
    nonce: str
    facts: cards.CardFacts


def _outgoing(pairs: list[tuple[bt.IssuedCard, cards.CardFacts]]) -> list[Outgoing]:
    return [
        Outgoing(c.member.id, c.member.plan_id, c.member.card_no, c.nonce, f)
        for c, f in pairs
    ]


async def _deliver_cards(
    sender: Sender, outgoing: list[Outgoing]
) -> list[dict[str, Any]]:
    """One message per card. A failed send never stops the others."""
    results: list[dict[str, Any]] = []
    for card in outgoing:
        ok, failure = await sender(
            cards.render_card(card.facts),
            rules.inline_keyboard(card.member_id, card.nonce),
        )
        results.append(
            {
                "message": "card",
                "plan_id": card.plan_id,
                "card_no": card.card_no,
                "sent": ok,
                "failure_code": failure,
            }
        )
    return results


async def _create(
    session: AsyncSession,
    args: argparse.Namespace,
    actor: str,
    now: datetime,
    sender: Sender | None,
    calendar: rules.SessionCalendar | None,
) -> tuple[int, dict[str, Any]]:
    issued = await bt.create_batch(
        session,
        realm=args.realm,
        basket_key=args.basket_key,
        actor=actor,
        now=now,
        calendar=calendar,
    )
    batch = issued.batch
    facts = [(c, await _card_facts(session, batch, c.member)) for c in issued.cards]
    armed = await _armed_tranches(session, args.realm, args.basket_key)
    summary = cards.render_summary(
        cards.SummaryFacts(
            market=batch.market,
            basket_key=batch.basket_key,
            target_session=batch.target_session,
            approval_deadline_at=batch.approval_deadline_at,
            expires_at=batch.expires_at,
            armed_tranches=armed,
            member_count=len(issued.cards),
        )
    )
    body: dict[str, Any] = {
        "batch": {
            "id": batch.id,
            "realm": batch.realm,
            "basket_key": batch.basket_key,
            "market": batch.market,
            "target_session": batch.target_session.isoformat(),
            "approval_deadline_at": batch.approval_deadline_at.isoformat(),
            "expires_at": batch.expires_at.isoformat(),
        },
        "cards": [_card_row(c, f) for c, f in facts],
        "skipped": [
            {"plan_id": s.plan_id, "symbol": s.symbol, "reason": s.reason}
            for s in issued.skipped
        ],
    }
    if not args.commit:
        await session.rollback()
        body["batch"]["id"] = None
        return EXIT_OK, {"mode": "preview", **body}
    outgoing = _outgoing(facts)
    await session.commit()  # rows first: a delivered card always names a stored digest
    assert sender is not None
    ok, failure = await sender(summary, None)
    sent = [{"message": "summary", "sent": ok, "failure_code": failure}]
    sent += await _deliver_cards(sender, outgoing)
    body["sent"] = sent
    code = EXIT_OK if all(r["sent"] for r in sent) else EXIT_SEND_FAILED
    return code, {"mode": "commit", **body}


async def _resend(
    session: AsyncSession,
    args: argparse.Namespace,
    actor: str,
    now: datetime,
    sender: Sender | None,
) -> tuple[int, dict[str, Any]]:
    batch = await bt.get_batch(session, args.batch_id)
    if batch is None:
        raise MandateCliError(f"batch {args.batch_id}가 없습니다")
    issued = await bt.reissue_cards(
        session, batch_id=args.batch_id, actor=actor, now=now
    )
    facts = [(c, await _card_facts(session, batch, c.member)) for c in issued]
    body: dict[str, Any] = {
        "batch_id": batch.id,
        "cards": [_card_row(c, f) for c, f in facts],
    }
    if not args.commit:
        await session.rollback()
        return EXIT_OK, {"mode": "preview", **body}
    outgoing = _outgoing(facts)
    await session.commit()
    assert sender is not None
    results = await _deliver_cards(sender, outgoing)
    body["sent"] = results
    code = EXIT_OK if all(r["sent"] for r in results) else EXIT_SEND_FAILED
    return code, {"mode": "commit", **body}


async def _status(session: AsyncSession, args: argparse.Namespace) -> dict[str, Any]:
    batch = await bt.get_batch(session, args.batch_id)
    if batch is None:
        raise MandateCliError(f"batch {args.batch_id}가 없습니다")
    plans: list[dict[str, Any]] = []
    for first in await bt.members_of_batch(session, batch.id):
        latest = await bt.latest_card(session, batch.id, first.plan_id)
        answer = await bt.answer_event(session, batch.id, first.plan_id)
        plan = await session.get(MandatePlan, first.plan_id)
        plans.append(
            {
                "plan_id": first.plan_id,
                "symbol": None if plan is None else plan.symbol,
                "kind": first.kind,
                "terms_id": first.terms_id,
                "latest_card_no": None if latest is None else latest.card_no,
                "answer": None
                if answer is None
                else {
                    "event": answer.event,
                    "seq": answer.seq,
                    "actor": answer.actor,
                    "effective_session": None
                    if answer.effective_session is None
                    else answer.effective_session.isoformat(),
                },
            }
        )
    await session.rollback()
    return {
        "mode": "read",
        "batch": {
            "id": batch.id,
            "realm": batch.realm,
            "basket_key": batch.basket_key,
            "market": batch.market,
            "target_session": batch.target_session.isoformat(),
            "approval_deadline_at": batch.approval_deadline_at.isoformat(),
            "expires_at": batch.expires_at.isoformat(),
        },
        "plans": plans,
    }


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Any] | None = None,
    sender: Sender | None = None,
    now: datetime | None = None,
    calendar: rules.SessionCalendar | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one command. Tests inject the session factory, sender, clock and calendar."""
    if args.command != "status":
        _operator_actor(args.actor)  # refused before any engine or session exists
    engine = None
    client: httpx.AsyncClient | None = None
    if session_factory is None:
        engine = create_async_engine(_database_url(args))
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    try:
        if args.command != "status" and args.commit and sender is None:
            client = httpx.AsyncClient(timeout=10.0, follow_redirects=False)
            sender = default_sender(client)
        async with session_factory() as session:
            moment = now or datetime.now(UTC)
            if args.command == "status":
                return EXIT_OK, await _status(session, args)
            actor = _operator_actor(args.actor)
            if args.command == "create":
                return await _create(session, args, actor, moment, sender, calendar)
            if args.command == "resend":
                return await _resend(session, args, actor, moment, sender)
            raise MandateCliError(f"알 수 없는 명령 {args.command}")
    finally:
        if client is not None:
            await client.aclose()
        if engine is not None:
            await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hk 3795 위임장 아침 묶음 승인 CLI")
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url", help="명시적 PostgreSQL URL")
    database.add_argument(
        "--database-url-env",
        metavar="NAME",
        help="PostgreSQL URL을 담은 환경 변수 이름(값은 출력하지 않음)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser(
        "create", help="바구니의 다음 거래일 배치를 만들고 카드를 발송"
    )
    create.add_argument("--realm", required=True, choices=list(v.REALMS))
    create.add_argument("--basket-key", required=True)
    create.add_argument("--actor", required=True, help="operator:<이름>")
    create.add_argument("--commit", action="store_true")

    resend = sub.add_parser("resend", help="응답 없는 카드를 새 nonce로 재발송")
    resend.add_argument("--batch-id", type=_positive_int, required=True)
    resend.add_argument("--actor", required=True, help="operator:<이름>")
    resend.add_argument("--commit", action="store_true")

    status = sub.add_parser("status", help="읽기 전용 배치 상태")
    status.add_argument("--batch-id", type=_positive_int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload = asyncio.run(run(args))
    except (MandateCliError, ms.MandateError, ValueError) as exc:
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
