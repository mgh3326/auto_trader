#!/usr/bin/env python3
"""Operator CLI for the hk 3795 mandate (ticket T2).

Subcommands:

``request-terms``    snapshot a clause (and the sleeve values of its version) as
                     an approval request. The approval itself is a Telegram card
                     (T3b); this command never records an approval.
``request-profile``  snapshot an activation profile as an approval request.
``register-plan``    register a split-buy plan in state ``proposed``.
``event``            append one operator plan event. The closed list is
                     ``OPERATOR_EVENTS``; ``approved``, ``rejected``, fences,
                     fills and ``released`` are not reachable from this command.
``status``           read-only: the folded state of one plan, or of a sleeve's plans.

Every operator write needs ``--actor operator:<name>``. A system actor (``system:``)
or any other value is refused before the database is touched.

Writes happen only with ``--commit``. Without it the command runs the same store
checks, then rolls back, so a refusal in preview is the refusal a commit gets.

The database is named explicitly: ``--database-url-env NAME`` reads the URL from
that one environment variable (preferred) or ``--database-url URL``. The URL value
is never printed.

Inputs are JSON files with closed keys. Every amount is a decimal string written
by the operator. Broker reads are not made here: holdings evidence, the account
references and the clause's account are operator input, and the store checks their
shape. The one-share price is the last complete daily close from ``daily_candles``.

Runbook: ``docs/runbooks/mandate-cli.md``.

Exit codes: 0 ok, 1 input, refusal or database error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.mandate import MandatePlan, MandatePlanTranche
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.mandate.fold import PlanState
from app.services.strategy_sleeves import store as sleeve_store
from app.services.strategy_sleeves.marks import latest_close_marks
from app.services.strategy_sleeves.store import PreRegistration
from app.services.trading_policy_service import load_trading_policy

OPERATOR_PREFIX = "operator:"
ACTOR_MAX = 128
#: The only plan events an operator may append from this CLI (§8 T2).
OPERATOR_EVENTS: tuple[str, ...] = (
    "suspended",
    "resumed",
    "retired",
    "tranche_closed_partial",
    "thesis_broken",
    "thesis_evidence_refreshed",
    "holdings_disposed",
)
#: Flags each operator event may carry. Anything else is refused, not ignored.
EVENT_FLAGS: dict[str, frozenset[str]] = {
    "suspended": frozenset(),
    "resumed": frozenset({"effective_session"}),
    "retired": frozenset(),
    "tranche_closed_partial": frozenset({"tranche_index"}),
    "thesis_broken": frozenset({"retrospective_id", "approval_issue_id"}),
    "thesis_evidence_refreshed": frozenset({"retrospective_id", "approval_issue_id"}),
    "holdings_disposed": frozenset(),
}
#: Flags an event cannot be recorded without.
EVENT_REQUIRED: dict[str, frozenset[str]] = {
    "suspended": frozenset(),
    "resumed": frozenset({"effective_session"}),
    "retired": frozenset(),
    "tranche_closed_partial": frozenset({"tranche_index"}),
    "thesis_broken": frozenset({"retrospective_id", "approval_issue_id"}),
    "thesis_evidence_refreshed": frozenset({"retrospective_id", "approval_issue_id"}),
    "holdings_disposed": frozenset(),
}
TERMS_KEYS = frozenset(
    {
        "basket_key",
        "account_ref",
        "universe_rule",
        "allowed_symbols",
        "entry_max_premium_bps",
        "per_order_cap",
        "daily_cap",
        "sector_cap_pct",
        "basket_drawdown_freeze_pct",
        "account_drawdown_freeze_pct",
        "tranche_rule",
        "expires_at",
        "budget_cap",
        "loss_limit",
        "prereg",
        "loss_sell_mode",
    }
)
TERMS_REQUIRED = TERMS_KEYS - {"loss_sell_mode"}
PREREG_KEYS = frozenset(
    {"metric", "min_effect_pct", "horizon_sessions", "max_duration_days"}
)
PLAN_KEYS = frozenset(
    {
        "sleeve_key",
        "symbol",
        "planned_amount",
        "adopted_amount",
        "adopted_evidence",
        "account_ref",
        "scope_account_refs",
        "thesis",
        "invalidation_conditions",
        "sector_key",
    }
)
#: The policy's market keys for the global auto-approval caps.
POLICY_MARKET = {"equity_kr": "kr", "equity_us": "us", "crypto": "crypto"}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXIT_OK = 0
EXIT_ERROR = 1


class MandateCliError(ValueError):
    """An input the CLI refuses before the store is asked anything."""


def _operator_actor(value: str | None) -> str:
    who = (value or "").strip()
    if (
        not who.startswith(OPERATOR_PREFIX)
        or len(who) <= len(OPERATOR_PREFIX)
        or len(who) > ACTOR_MAX
    ):
        raise MandateCliError(
            "actor는 operator:<이름> 형식이어야 합니다(system 등 다른 actor는 거부)"
        )
    return who


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("정수여야 합니다") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수여야 합니다")
    return value


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("YYYY-MM-DD 형식이어야 합니다") from exc


def _aware_datetime(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "시간대가 붙은 ISO 8601 시각이어야 합니다"
        ) from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("시각에 시간대가 필요합니다")
    return value


def _read_input(path: str, allowed: frozenset[str], required: frozenset[str]) -> dict:
    """A JSON object with closed keys. Numbers arrive as decimal strings."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), parse_float=str)
    except (OSError, ValueError) as exc:
        raise MandateCliError(
            f"입력 {path!r}을(를) 읽을 수 없거나 JSON이 아닙니다"
        ) from exc
    if not isinstance(raw, dict):
        raise MandateCliError("입력은 JSON 객체여야 합니다")
    unknown = set(raw) - allowed
    if unknown:
        raise MandateCliError(f"모르는 입력 키 {sorted(unknown)}")
    missing = required - set(raw)
    if missing:
        raise MandateCliError(f"빠진 입력 키 {sorted(missing)}")
    return raw


def _terms_input(raw: dict) -> ms.TermsInput:
    prereg = raw["prereg"]
    if not isinstance(prereg, dict) or set(prereg) != PREREG_KEYS:
        raise MandateCliError(
            "prereg에는 metric, min_effect_pct, horizon_sessions, "
            "max_duration_days 넷만 필요합니다"
        )
    expires_at = raw["expires_at"]
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except ValueError as exc:
        raise MandateCliError("expires_at은 ISO 8601 시각이어야 합니다") from exc
    if expires.tzinfo is None:
        raise MandateCliError("expires_at에 시간대가 필요합니다")
    return ms.TermsInput(
        basket_key=raw["basket_key"],
        account_ref=raw["account_ref"],
        universe_rule=raw["universe_rule"],
        allowed_symbols=list(raw["allowed_symbols"]),
        entry_max_premium_bps=raw["entry_max_premium_bps"],
        per_order_cap=raw["per_order_cap"],
        daily_cap=raw["daily_cap"],
        sector_cap_pct=raw["sector_cap_pct"],
        basket_drawdown_freeze_pct=raw["basket_drawdown_freeze_pct"],
        account_drawdown_freeze_pct=raw["account_drawdown_freeze_pct"],
        tranche_rule=raw["tranche_rule"],
        expires_at=expires,
        budget_cap=raw["budget_cap"],
        loss_limit=raw["loss_limit"],
        prereg=PreRegistration(
            prereg["metric"],
            prereg["min_effect_pct"],
            prereg["horizon_sessions"],
            prereg["max_duration_days"],
        ),
        loss_sell_mode=raw.get("loss_sell_mode", "human_card"),
    )


def global_caps(market: str) -> ms.GlobalCaps:
    """The per-order and daily caps a clause may not exceed, from the policy."""
    auto = load_trading_policy().order_proposals.auto_approve
    key = POLICY_MARKET[market]
    return ms.GlobalCaps(
        per_order_cap=Decimal(str(auto.per_order_cap[key])),
        daily_cap=Decimal(str(auto.daily_cap[key])),
    )


def event_fields(
    event: str, state: PlanState, args: argparse.Namespace
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The fields and evidence of one operator event, derived from the folded state.

    Pure. ``resume_target``, the tranche's first fill and the thesis sequence are
    read from the state, not typed by the operator, so a stale or mistyped value
    cannot be recorded.
    """
    given = {
        name
        for name in (
            "effective_session",
            "tranche_index",
            "retrospective_id",
            "approval_issue_id",
        )
        if getattr(args, name) is not None
    }
    extra = given - EVENT_FLAGS[event]
    if extra:
        raise MandateCliError(f"{event}은(는) {sorted(extra)} 플래그를 받지 않습니다")
    missing = EVENT_REQUIRED[event] - given
    if missing:
        raise MandateCliError(f"{event}에는 {sorted(missing)}이(가) 필요합니다")

    evidence: dict[str, Any] = {"reason_code": args.reason}
    fields: dict[str, Any] = {}
    if event == "resumed":
        fields["effective_session"] = args.effective_session
        fields["resume_target"] = "active" if state.tranche(1).completed else "approved"
    elif event == "tranche_closed_partial":
        fields["tranche_index"] = args.tranche_index
        if args.tranche_index == 1:
            tranche = state.tranche(1)
            fields["first_fill_amount"] = tranche.filled_amount
            fields["first_fill_quantity"] = tranche.filled_quantity
    elif event in ("thesis_broken", "thesis_evidence_refreshed"):
        evidence["retrospective_id"] = args.retrospective_id
        evidence["approval_issue_id"] = args.approval_issue_id
        if event == "thesis_evidence_refreshed":
            if not state.thesis_broken_seqs:
                raise MandateCliError("갱신할 thesis_broken 이벤트가 없습니다")
            fields["thesis_seq"] = state.thesis_broken_seqs[-1]
    elif event == "holdings_disposed":
        evidence["holdings_disposition"] = "kept_outside_basket"
    return fields, evidence


async def _finish(session: AsyncSession, commit: bool) -> None:
    if commit:
        await session.commit()
    else:
        await session.rollback()


async def _sleeve_or_refuse(session: AsyncSession, sleeve_key: str) -> Any:
    sleeve = await sleeve_store.get_sleeve_by_key(session, sleeve_key)
    if sleeve is None:
        raise MandateCliError(f"sleeve {sleeve_key!r}가 없습니다")
    return sleeve


async def _request_terms(
    session: AsyncSession, args: argparse.Namespace, actor: str
) -> dict[str, Any]:
    raw = _read_input(args.input, TERMS_KEYS, TERMS_REQUIRED)
    sleeve = await _sleeve_or_refuse(session, args.sleeve_key)
    request = await ms.create_terms_request(
        session,
        sleeve_id=sleeve.id,
        data=_terms_input(raw),
        global_caps=global_caps(sleeve.market),
        approval_ref=args.approval_ref,
        requested_by=actor,
    )
    return {
        "subject_kind": request.subject_kind,
        "request_id": request.id,
        "sleeve_key": sleeve.sleeve_key,
        "content_sha256": request.content_sha256,
        "approval_ref": request.approval_ref,
        "requested_by": request.requested_by,
    }


async def _request_profile(
    session: AsyncSession, args: argparse.Namespace, actor: str
) -> dict[str, Any]:
    sleeve = await _sleeve_or_refuse(session, args.sleeve_key)
    request = await ms.create_profile_request(
        session,
        sleeve_id=sleeve.id,
        terms_id=args.terms_id,
        max_tranche_index=args.max_tranche_index,
        effective_session=args.effective_session,
        expires_at=args.expires_at,
        approval_ref=args.approval_ref,
        requested_by=actor,
    )
    return {
        "subject_kind": request.subject_kind,
        "request_id": request.id,
        "sleeve_key": sleeve.sleeve_key,
        "content_sha256": request.content_sha256,
        "approval_ref": request.approval_ref,
        "requested_by": request.requested_by,
    }


async def _register_plan(
    session: AsyncSession, args: argparse.Namespace, actor: str, now: datetime
) -> dict[str, Any]:
    raw = _read_input(args.input, PLAN_KEYS, PLAN_KEYS)
    sleeve = await _sleeve_or_refuse(session, raw["sleeve_key"])
    key = sleeve_store.sleeve_symbol(raw["symbol"], sleeve.market)
    marks = await latest_close_marks(session, sleeve.market, [key], now)
    price = marks.get(key)
    if price is None:
        raise ms.MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            f"{key}에 1주 크기 계산용 완결 일봉 종가가 없습니다",
        )
    registered = await ms.register_plan(
        session,
        ms.PlanInput(
            sleeve_id=sleeve.id,
            symbol=raw["symbol"],
            planned_amount=raw["planned_amount"],
            adopted_amount=raw["adopted_amount"],
            adopted_evidence=raw["adopted_evidence"],
            account_ref=raw["account_ref"],
            scope_account_refs=list(raw["scope_account_refs"]),
            thesis=raw["thesis"],
            invalidation_conditions=list(raw["invalidation_conditions"]),
            sector_key=raw["sector_key"],
            created_by=actor,
            one_share_price=price,
        ),
        now=now,
    )
    return {
        "plan_id": registered.plan.id,
        "sleeve_key": sleeve.sleeve_key,
        "symbol": registered.plan.symbol,
        "authority": "proposed",
        "planned_amount": str(registered.plan.planned_amount),
        "adopted_amount": str(registered.plan.adopted_amount),
        "tranches": [
            {"tranche_index": t.tranche_index, "amount": str(t.amount)}
            for t in registered.tranches
        ],
        "reservation": {
            "realm": registered.reservation.realm,
            "market": registered.reservation.market,
            "symbol": registered.reservation.symbol,
        },
    }


async def _event(
    session: AsyncSession, args: argparse.Namespace, actor: str
) -> dict[str, Any]:
    plan = await ms.get_plan(session, args.plan_id)
    if plan is None:
        raise MandateCliError(f"계획 {args.plan_id}가 없습니다")
    state = await ms.plan_state(session, args.plan_id)
    fields, evidence = event_fields(args.event, state, args)
    row = await ms.append_plan_event(
        session,
        plan_id=args.plan_id,
        event=args.event,
        actor=actor,
        evidence=evidence,
        **fields,
    )
    return {
        "plan_id": args.plan_id,
        "event": row.event,
        "seq": row.seq,
        "actor": row.actor,
    }


async def _plan_detail(session: AsyncSession, plan: MandatePlan) -> dict[str, Any]:
    state = await ms.plan_state(session, plan.id)
    tranches = (
        (
            await session.execute(
                select(MandatePlanTranche)
                .where(MandatePlanTranche.plan_id == plan.id)
                .order_by(MandatePlanTranche.tranche_index)
            )
        )
        .scalars()
        .all()
    )
    events = await ms.list_plan_events(session, plan.id)
    return {
        "plan_id": plan.id,
        "symbol": plan.symbol,
        "account_ref": plan.account_ref,
        "scope_account_refs": list(plan.scope_account_refs),
        "planned_amount": str(plan.planned_amount),
        "adopted_amount": str(plan.adopted_amount),
        "sector_key": plan.sector_key,
        "authority": state.authority,
        "cleanup": state.cleanup,
        "released": state.released,
        "last_seq": state.last_seq,
        "unclosed_fences": list(state.unclosed_fences),
        "thesis_broken_seqs": list(state.thesis_broken_seqs),
        "tranches": [
            {
                "tranche_index": row.tranche_index,
                "amount": str(row.amount),
                "completed": state.tranche(row.tranche_index).completed,
                "filled_amount": str(state.tranche(row.tranche_index).filled_amount),
                "filled_quantity": str(
                    state.tranche(row.tranche_index).filled_quantity
                ),
                "open_proposals": len(state.tranche(row.tranche_index).open_proposals),
            }
            for row in tranches
        ],
        "events": [{"seq": e.seq, "event": e.event, "actor": e.actor} for e in events],
    }


async def _status(session: AsyncSession, args: argparse.Namespace) -> dict[str, Any]:
    if args.plan_id is not None:
        plan = await ms.get_plan(session, args.plan_id)
        if plan is None:
            raise MandateCliError(f"계획 {args.plan_id}가 없습니다")
        return await _plan_detail(session, plan)
    sleeve = await _sleeve_or_refuse(session, args.sleeve_key)
    plans = (
        (
            await session.execute(
                select(MandatePlan)
                .where(MandatePlan.sleeve_id == sleeve.id)
                .order_by(MandatePlan.id)
            )
        )
        .scalars()
        .all()
    )
    rows = []
    for plan in plans:
        state = await ms.plan_state(session, plan.id)
        rows.append(
            {
                "plan_id": plan.id,
                "symbol": plan.symbol,
                "authority": state.authority,
                "cleanup": state.cleanup,
                "released": state.released,
            }
        )
    return {"sleeve_key": sleeve.sleeve_key, "plans": rows}


async def _dispatch(
    session: AsyncSession, args: argparse.Namespace, now: datetime
) -> dict[str, Any]:
    if args.command == "status":
        payload = await _status(session, args)
        await session.rollback()
        return {"mode": "read", **payload}
    actor = _operator_actor(args.actor)
    if args.command == "request-terms":
        body = await _request_terms(session, args, actor)
    elif args.command == "request-profile":
        body = await _request_profile(session, args, actor)
    elif args.command == "register-plan":
        body = await _register_plan(session, args, actor, now)
    elif args.command == "event":
        body = await _event(session, args, actor)
    else:
        raise MandateCliError(f"알 수 없는 명령 {args.command}")
    await _finish(session, args.commit)
    return {"mode": "commit" if args.commit else "preview", **body}


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


async def run(
    args: argparse.Namespace,
    *,
    session_factory: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one command. ``session_factory`` is injected by tests; the CLI builds it."""
    if args.command != "status":
        # refuse before any engine or session exists
        _operator_actor(args.actor)
    engine = None
    if session_factory is None:
        engine = create_async_engine(_database_url(args))
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    try:
        async with session_factory() as session:
            return EXIT_OK, await _dispatch(session, args, now or datetime.now(UTC))
    finally:
        if engine is not None:
            await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hk 3795 위임장 운영 CLI")
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url", help="명시적 PostgreSQL URL")
    database.add_argument(
        "--database-url-env",
        metavar="NAME",
        help="PostgreSQL URL을 담은 환경 변수 이름(값은 출력하지 않음)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    terms = sub.add_parser(
        "request-terms", help="조항 승인 요청 스냅샷(승인은 Telegram 카드에서만 기록)"
    )
    terms.add_argument("--sleeve-key", required=True)
    terms.add_argument("--input", required=True, help="JSON 조항(닫힌 키)")
    terms.add_argument("--approval-ref", required=True)
    terms.add_argument("--actor", required=True, help="operator:<이름>")
    terms.add_argument("--commit", action="store_true")

    profile = sub.add_parser("request-profile", help="활성화 프로필 승인 요청 스냅샷")
    profile.add_argument("--sleeve-key", required=True)
    profile.add_argument("--terms-id", type=_positive_int, required=True)
    profile.add_argument("--max-tranche-index", type=int, required=True)
    profile.add_argument("--effective-session", type=_iso_date, required=True)
    profile.add_argument("--expires-at", type=_aware_datetime, required=True)
    profile.add_argument("--approval-ref", required=True)
    profile.add_argument("--actor", required=True, help="operator:<이름>")
    profile.add_argument("--commit", action="store_true")

    register = sub.add_parser("register-plan", help="분할 매수 계획을 proposed로 등록")
    register.add_argument("--input", required=True, help="JSON 계획(닫힌 키)")
    register.add_argument("--actor", required=True, help="operator:<이름>")
    register.add_argument("--commit", action="store_true")

    event = sub.add_parser("event", help="운영자 계획 이벤트 1건 추가")
    event.add_argument("--plan-id", type=_positive_int, required=True)
    event.add_argument("--event", required=True, choices=list(OPERATOR_EVENTS))
    event.add_argument("--reason", required=True, help="사유(reason_code로 기록, 필수)")
    event.add_argument("--actor", required=True, help="operator:<이름>")
    event.add_argument("--effective-session", type=_iso_date)
    event.add_argument(
        "--tranche-index", type=int, choices=range(1, v.MAX_TRANCHES + 1)
    )
    event.add_argument("--retrospective-id", type=_positive_int)
    event.add_argument("--approval-issue-id")
    event.add_argument("--commit", action="store_true")

    status = sub.add_parser("status", help="읽기 전용 계획 상태")
    target = status.add_mutually_exclusive_group(required=True)
    target.add_argument("--plan-id", type=_positive_int)
    target.add_argument("--sleeve-key")
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
