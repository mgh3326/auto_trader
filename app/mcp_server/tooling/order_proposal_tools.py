"""ROB-816 read/create/void MCP tools for order_proposals.

READ + CREATE + VOID ONLY. There is deliberately no approve/submit tool —
approval is Telegram-only (PR 2). ``order_proposal_create`` persists a proposal
row. Broker mutation remains behind Telegram dispatch; with ROB-871's separate
default-off gate, a narrowly eligible resting order may submit only after the
row has committed and the existing fresh revalidation path passes.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.broker_gateway import (
    fetch_operator_void_evidence,
    fetch_target_order,
)
from app.services.order_proposals.buying_power import (
    build_create_advisory,
    currency_for_market,
    default_buying_power_reader,
)
from app.services.order_proposals.dispatch import dispatch_proposal
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalNotFound,
    OrderProposalUnsupportedTargetAction,
)
from app.services.order_proposals.service import RungInput, check_action_capability
from app.services.order_proposals.telegram_callback import _safe_edit_message

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

ORDER_PROPOSAL_TOOL_NAMES: set[str] = {
    "order_proposal_create",
    "order_proposal_get",
    "order_proposal_list",
    "order_proposal_void",
    "order_proposal_expire_sweep",
    "order_proposal_list_expired_defensive",
}

_MARKET_ALIASES = {"kr": "equity_kr", "us": "equity_us"}


def _normalize_order_proposal_market(market: str) -> str:
    return _MARKET_ALIASES.get(market, market)


def _dec(v: str | None) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal {v!r}") from exc


def _group_dict(g: Any) -> dict[str, Any]:
    return {
        "proposal_id": str(g.proposal_id),
        "root_proposal_id": str(g.root_proposal_id),
        "revision": g.revision,
        "symbol": g.symbol,
        "market": g.market,
        "account_mode": g.account_mode,
        "side": g.side,
        "order_type": g.order_type,
        "action": g.action or "place",
        "target_broker_order_id": g.target_broker_order_id,
        "proposer": g.proposer,
        "lifecycle_state": g.lifecycle_state,
        "thesis": g.thesis,
        "strategy": g.strategy,
        "exit_intent": g.exit_intent,
        "exit_reason": g.exit_reason,
        "retrospective_id": g.retrospective_id,
        "approval_issue_id": g.approval_issue_id,
        "supersedes_proposal_id": (
            str(g.supersedes_proposal_id) if g.supersedes_proposal_id else None
        ),
        "superseded_by_proposal_id": (
            str(g.superseded_by_proposal_id) if g.superseded_by_proposal_id else None
        ),
        "valid_until": g.valid_until.isoformat() if g.valid_until else None,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


def _rung_dict(r: Any) -> dict[str, Any]:
    return {
        "rung_index": r.rung_index,
        "side": r.side,
        "quantity": str(r.quantity),
        "limit_price": str(r.limit_price) if r.limit_price is not None else None,
        "notional": str(r.notional) if r.notional is not None else None,
        "state": r.state,
        "broker_order_id": r.broker_order_id,
        "correlation_id": r.correlation_id,
    }


def _get_trade_notifier() -> Any:
    from app.monitoring.trade_notifier.notifier import get_trade_notifier

    return get_trade_notifier()


def _escape_telegram_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    return re.sub(r"([_*`\[])", r"\\\1", escaped)


async def _fetch_void_evidence(*, group: Any, rungs: list[Any], now: datetime) -> Any:
    return await fetch_operator_void_evidence(
        account_mode=group.account_mode,
        market=group.market,
        symbol=group.symbol,
        rungs=rungs,
        now=now,
        valid_until=group.valid_until,
    )


async def _edit_voided_approval_message(
    *, chat_id: Any, message_id: Any, void_reason: str
) -> None:
    if chat_id is None or message_id is None:
        return
    try:
        edited = await _get_trade_notifier().edit_message(
            str(chat_id),
            int(message_id),
            f"🗑️ 제안 무효화됨\n사유: {_escape_telegram_markdown(void_reason)}",
            reply_markup={"inline_keyboard": []},
        )
        if edited is False:
            logger.error(
                "order_proposal_void: telegram message edit returned false "
                "for message_id=%s",
                message_id,
            )
    except Exception:  # noqa: BLE001 - DB void is already committed
        logger.exception(
            "order_proposal_void: telegram message edit failed for message_id=%s",
            message_id,
        )


async def _edit_expired_approval_message(
    *, chat_id: Any, message_id: Any, symbol: str
) -> None:
    """Mirror ``_edit_voided_approval_message`` for sweep-expired proposals.

    The DB expiry is already committed by the time this runs (called after
    ``sweep_expired``'s session commits) -- a Telegram failure here must never
    surface as a sweep failure, only be logged (ROB-897).
    """
    if chat_id is None or message_id is None:
        return
    try:
        edited = await _get_trade_notifier().edit_message(
            str(chat_id),
            int(message_id),
            f"⏰ 제안 만료됨\n종목: {_escape_telegram_markdown(symbol)}",
            reply_markup={"inline_keyboard": []},
        )
        if edited is False:
            logger.error(
                "order_proposal_expire_sweep: telegram message edit returned "
                "false for message_id=%s",
                message_id,
            )
    except Exception:  # noqa: BLE001 - DB expiry is already committed
        logger.exception(
            "order_proposal_expire_sweep: telegram message edit failed for "
            "message_id=%s",
            message_id,
        )


async def _edit_superseded_approval_message(
    *, chat_id: Any, message_id: Any, replacement_proposal_id: uuid.UUID
) -> None:
    if chat_id is None or message_id is None:
        return
    try:
        notifier = _get_trade_notifier()
    except Exception:  # noqa: BLE001 - DB supersede is already committed
        logger.exception(
            "order_proposal_create: superseded telegram message setup failed "
            "for message_id=%s",
            message_id,
        )
        return
    await _safe_edit_message(
        notifier,
        str(chat_id),
        int(message_id),
        f"🔁 → {str(replacement_proposal_id)[:8]}로 대체됨",
        reply_markup={"inline_keyboard": []},
    )


async def order_proposal_create(
    symbol: str,
    market: str,
    account_mode: str,
    side: str,
    order_type: str,
    proposer: str,
    rungs: list[dict],
    thesis: str | None = None,
    strategy: str | None = None,
    rationale: dict | None = None,
    broker_account_id: str | None = None,
    lot_context: dict | None = None,
    valid_until: str | None = None,
    supersedes_proposal_id: str | None = None,
    exit_intent: str | None = None,
    exit_reason: str | None = None,
    retrospective_id: int | None = None,
    approval_issue_id: str | None = None,
    action: str = "place",
    target_broker_order_id: str | None = None,
) -> dict[str, Any]:
    """Create a place, replace, or cancel proposal without broker mutation.

    ``loss_cut`` keeps retrospective/price/hash guards and is approved only by
    Telegram's two-click confirmation. ``approval_issue_id`` is an optional
    free-text audit note; no external issue tracker is queried.

    Args:
        market: Canonical market in {equity_kr, equity_us, crypto}; aliases
                kr→equity_kr and us→equity_us are accepted. Supported place
                combinations are kis_live/toss_live with equity_kr or equity_us,
                and upbit with crypto.
        rungs: list of {"rung_index": int, "side": str, "quantity": str,
               "limit_price": str|None, "notional": str|None}.
        supersedes_proposal_id: if this proposal replaces an existing one (price/qty
               change), the original is marked superseded and lineage is linked.
        action: ``place`` (default), ``replace``, or ``cancel``. Replace/cancel
                support the same account_mode/market combinations as place
                (kis_live/toss_live equity_kr|equity_us, upbit crypto) and
                perform a read-only target-order preflight before persistence.
                An unsupported combination returns success=False with a
                structured supported_matrix (per action) instead of a bare
                error string.
        target_broker_order_id: required broker order ID for replace/cancel.
    """
    try:
        market = _normalize_order_proposal_market(market)
        rung_inputs = [
            RungInput(
                int(r["rung_index"]),
                str(r["side"]),
                _dec(r["quantity"]),
                _dec(r.get("limit_price")),
                _dec(r.get("notional")),
            )
            for r in rungs
        ]
        vu = datetime.fromisoformat(valid_until) if valid_until else None
        sup = uuid.UUID(supersedes_proposal_id) if supersedes_proposal_id else None
        normalized_action = action or "place"
        target_snapshot = None
        if normalized_action in {"replace", "cancel"}:
            if not target_broker_order_id:
                raise ValueError(f"{normalized_action} requires target_broker_order_id")
            check_action_capability(
                action=normalized_action, account_mode=account_mode, market=market
            )
            target_snapshot = await fetch_target_order(
                order_id=target_broker_order_id,
                symbol=symbol,
                market=market,
                account_mode=account_mode,
                now=now_kst(),
            )
        superseded_message: tuple[Any, Any] | None = None
        async with AsyncSessionLocal() as session:
            svc = OrderProposalsService(session)
            group = await svc.create_proposal(
                symbol=symbol,
                market=market,
                account_mode=account_mode,
                side=side,
                order_type=order_type,
                proposer=proposer,
                rungs=rung_inputs,
                thesis=thesis,
                strategy=strategy,
                rationale=rationale,
                broker_account_id=broker_account_id,
                lot_context=lot_context,
                valid_until=vu,
                exit_intent=exit_intent,
                exit_reason=exit_reason,
                retrospective_id=retrospective_id,
                approval_issue_id=approval_issue_id,
                supersedes_proposal_id=sup,
                action=normalized_action,
                target_broker_order_id=target_broker_order_id,
                target_order_snapshot=(
                    target_snapshot.to_payload()
                    if target_snapshot is not None
                    else None
                ),
            )
            _, saved_rungs = await svc.get_proposal(group.proposal_id)
            if sup is not None:
                superseded_group, _ = await svc.get_proposal(sup)
                superseded_source_asof = superseded_group.source_asof or {}
                superseded_message = (
                    superseded_source_asof.get("approval_chat_id"),
                    superseded_source_asof.get("approval_message_id"),
                )
            await session.commit()
            proposal_id = group.proposal_id
            result = {
                "success": True,
                "proposal_id": str(proposal_id),
                "lifecycle_state": group.lifecycle_state,
                "action": group.action or "place",
                "target_broker_order_id": group.target_broker_order_id,
                "valid_until": group.valid_until.isoformat()
                if group.valid_until
                else None,
                "rungs": [_rung_dict(r) for r in saved_rungs],
            }

        if superseded_message is not None:
            await _edit_superseded_approval_message(
                chat_id=superseded_message[0],
                message_id=superseded_message[1],
                replacement_proposal_id=proposal_id,
            )

        if (
            normalized_action == "place"
            and account_mode == "toss_live"
            and side == "buy"
        ):
            try:
                async with AsyncSessionLocal() as advisory_session:
                    advisory = await build_create_advisory(
                        advisory_session,
                        account_mode=account_mode,
                        broker_account_id=broker_account_id,
                        currency=currency_for_market(market),
                        now=now_kst(),
                        buying_power_reader=default_buying_power_reader,
                    )
                result["buying_power_advisory"] = [advisory]
                warning = advisory.get("warning")
                if warning:
                    result["warnings"] = [warning]
            except Exception:  # noqa: BLE001 - advisory never blocks create
                logger.exception(
                    "order_proposal_create: buying-power advisory failed for "
                    "proposal_id=%s",
                    proposal_id,
                )

        # Best-effort Telegram dispatch (ROB-816 PR 2). The proposal's own
        # session above is already closed/committed by this point --
        # `dispatch_proposal` opens a genuinely separate session, so
        # this is intentional, not a nested-session bug. A dispatch failure
        # (Telegram down, notifier misconfigured, etc.) must never fail this
        # tool's contract -- the proposal has already persisted successfully.
        if (
            settings.ORDER_PROPOSALS_TELEGRAM_ENABLED
            and settings.order_proposals_telegram_chat_allowlist
        ):
            try:
                from app.monitoring.trade_notifier.notifier import (
                    get_trade_notifier,
                )

                await dispatch_proposal(
                    proposal_id,
                    notifier=get_trade_notifier(),
                    now=now_kst(),
                )
            except Exception:  # noqa: BLE001 - best-effort, never fail create
                logger.exception(
                    "order_proposal_create: telegram approval dispatch failed "
                    "for proposal_id=%s",
                    proposal_id,
                )

        return result
    except OrderProposalUnsupportedTargetAction as exc:
        return {
            "success": False,
            "error": str(exc),
            "supported_matrix": exc.supported_matrix,
            "requested": exc.requested,
        }
    except (ValueError, OrderProposalError) as exc:
        return {"success": False, "error": str(exc)}


async def order_proposal_get(proposal_id: str) -> dict[str, Any]:
    """Fetch a proposal + its rungs (read-only)."""
    try:
        pid = uuid.UUID(proposal_id)
    except ValueError:
        return {"success": False, "error": f"invalid proposal_id {proposal_id!r}"}
    async with AsyncSessionLocal() as session:
        svc = OrderProposalsService(session)
        try:
            group, rungs = await svc.get_proposal(pid)
        except OrderProposalNotFound:
            return {"success": False, "error": "not_found"}
        return {
            "success": True,
            "proposal": _group_dict(group),
            "rungs": [_rung_dict(r) for r in rungs],
        }


async def order_proposal_list(
    limit: int = 50,
    symbol: str | None = None,
    lifecycle_state: str | None = None,
) -> dict[str, Any]:
    """List recent proposals (read-only). limit is clamped to 1..200."""
    limit = max(1, min(int(limit), 200))
    async with AsyncSessionLocal() as session:
        svc = OrderProposalsService(session)
        rows = await svc.list_recent(
            limit=limit, symbol=symbol, lifecycle_state=lifecycle_state
        )
        return {
            "success": True,
            "count": len(rows),
            "proposals": [
                {**_group_dict(g), "rungs": [_rung_dict(r) for r in rs]}
                for g, rs in rows
            ],
        }


async def order_proposal_void(proposal_id: str, reason: str) -> dict[str, Any]:
    try:
        pid = uuid.UUID(proposal_id)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("void reason is required")
        async with AsyncSessionLocal() as session:
            service = OrderProposalsService(session)
            now = now_kst()
            await service.void_proposal(
                pid,
                reason=normalized_reason,
                now=now,
                broker_evidence=_fetch_void_evidence,
            )
            group, rungs = await service.get_proposal(pid)
            source_asof = group.source_asof or {}
            approval_chat_id = source_asof.get("approval_chat_id")
            approval_message_id = source_asof.get("approval_message_id")
            await session.commit()
            result = {
                "success": True,
                "proposal_id": proposal_id,
                "lifecycle_state": group.lifecycle_state,
                "void_reason": group.void_reason,
                "rungs": [_rung_dict(rung) for rung in rungs],
            }
        await _edit_voided_approval_message(
            chat_id=approval_chat_id,
            message_id=approval_message_id,
            void_reason=result["void_reason"],
        )
        return result
    except (ValueError, OrderProposalError) as exc:
        return {"success": False, "error": str(exc)}


async def run_order_proposal_expire_sweep(*, now: datetime) -> dict[str, Any]:
    """Execute the DB expiry sweep and clean up its Telegram messages.

    Shared by ``order_proposal_expire_sweep(dry_run=False)`` and the TaskIQ
    task (``app/tasks/order_proposal_expiry_tasks.py``) -- mirrors the
    toss_manual_activity pattern of a single non-MCP entry point both call.
    """
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        candidates_before = await service.list_expiry_candidates(now=now)
        swept = await service.sweep_expired(now=now)
        await session.commit()
    for result in swept:
        await _edit_expired_approval_message(
            chat_id=result.chat_id,
            message_id=result.message_id,
            symbol=result.symbol,
        )
    return {
        "success": True,
        "swept_count": len(swept),
        "swept_proposal_ids": [str(result.proposal_id) for result in swept],
        "skipped_count": len(candidates_before) - len(swept),
    }


async def order_proposal_expire_sweep(dry_run: bool = True) -> dict[str, Any]:
    """List (dry_run=True) or execute (dry_run=False) the valid_until expiry sweep.

    Structural fix for ROB-897 cause (1): ``expire_if_needed`` previously only
    ran from the Telegram approval callback, so proposals nobody tapped stayed
    ``proposed``/``needs_reconfirm`` forever after ``valid_until`` passed. This
    is the manual operator lever -- run with dry_run=True first to review what
    would expire; recurring automation is a separate, later decision (see
    ``app/tasks/order_proposal_expiry_tasks.py``). NOT a broker mutation.
    """
    try:
        now = now_kst()
        if dry_run:
            async with AsyncSessionLocal() as session:
                service = OrderProposalsService(session)
                candidates = await service.list_expiry_candidates(now=now)
                return {
                    "success": True,
                    "dry_run": True,
                    "count": len(candidates),
                    "candidates": [
                        {
                            "proposal_id": str(group.proposal_id),
                            "symbol": group.symbol,
                            "lifecycle_state": group.lifecycle_state,
                            "valid_until": group.valid_until.isoformat()
                            if group.valid_until
                            else None,
                            "rung_states": [rung.state for rung in rungs],
                        }
                        for group, rungs in candidates
                    ],
                }

        result = await run_order_proposal_expire_sweep(now=now)
        return {**result, "dry_run": False}
    except (ValueError, OrderProposalError) as exc:
        return {"success": False, "error": str(exc)}


async def order_proposal_list_expired_defensive(
    hours: int = 24, market: str | None = None
) -> dict[str, Any]:
    """List recently expired/voided defensive (loss_cut/defensive_trim) proposals.

    ROB-929: 07-15 US 방어 제안 6건이 미응답 만료되고 다음 세션이 같은 판단을
    처음부터 재구축했다. This forces that handoff -- a session-start check can
    call this and, for anything returned, re-judge at the current price instead
    of silently letting the same setup die again. Noise suppression: a group
    already superseded, or sharing a symbol+side with a still-active
    (non-terminal) proposal, is dropped. Read-only; not a broker mutation and
    never sends anything.
    """
    try:
        now = now_kst()
        bounded_hours = max(1, min(int(hours), 24 * 30))
        async with AsyncSessionLocal() as session:
            service = OrderProposalsService(session)
            items = await service.list_expired_defensive_handoff(
                now=now, hours=bounded_hours, market=market
            )
        return {
            "success": True,
            "hours": bounded_hours,
            "market": market,
            "count": len(items),
            "proposals": [
                {
                    "proposal_id": str(item.proposal_id),
                    "symbol": item.symbol,
                    "side": item.side,
                    "market": item.market,
                    "exit_intent": item.exit_intent,
                    "lifecycle_state": item.lifecycle_state,
                    "limit_price": (
                        str(item.limit_price) if item.limit_price is not None else None
                    ),
                    "valid_until": (
                        item.valid_until.isoformat() if item.valid_until else None
                    ),
                    "expired_or_voided_at": item.expired_or_voided_at.isoformat(),
                    "needs_reassessment": item.needs_reassessment,
                }
                for item in items
            ],
        }
    except (ValueError, OrderProposalError) as exc:
        return {"success": False, "error": str(exc)}


def register_order_proposal_tools(mcp: FastMCP) -> None:
    """Register the order_proposals read/create/void MCP tools.

    Deliberately excludes any approve/submit tool — approval is Telegram-only
    (ROB-816 PR 2).
    """
    _ = mcp.tool(
        name="order_proposal_create",
        description=(
            "Create a place, replace, or cancel order proposal (SOT ledger row). "
            "Replace/cancel read target-order evidence before persistence, but never "
            "mutate a broker. Approval/submission happens via Telegram (PR 2), not "
            "through this tool. loss_cut requires a two-click confirmation with a "
            "single-use nonce and full second-click revalidation; approval_issue_id "
            "is an optional audit note and is never externally queried."
        ),
    )(order_proposal_create)
    _ = mcp.tool(
        name="order_proposal_get",
        description="Read-only fetch of a proposal and its rungs by proposal_id.",
    )(order_proposal_get)
    _ = mcp.tool(
        name="order_proposal_list",
        description=(
            "Read-only list of recent order proposals, optionally filtered by "
            "symbol and/or lifecycle_state."
        ),
    )(order_proposal_list)
    _ = mcp.tool(
        name="order_proposal_void",
        description=(
            "Void an order proposal with a required operator reason. Unverified "
            "rungs require a five-minute settlement grace plus a fresh, conclusive "
            "broker-absence lookup and become voided_local_stale; found or "
            "inconclusive broker evidence fails closed. NOT a broker mutation."
        ),
    )(order_proposal_void)
    _ = mcp.tool(
        name="order_proposal_expire_sweep",
        description=(
            "List (dry_run=True, default) or expire (dry_run=False) all "
            "non-terminal proposals whose valid_until has passed. A group with "
            "any rung outside the voidable states (e.g. submitting/resting/"
            "filled) is skipped, not force-expired. NOT a broker mutation; "
            "cleans up the Telegram approval message for each expired group."
        ),
    )(order_proposal_expire_sweep)
    _ = mcp.tool(
        name="order_proposal_list_expired_defensive",
        description=(
            "Read-only handoff list of loss_cut/defensive_trim proposals that "
            "expired or were voided in the last `hours` (default 24, max 720) "
            "without a human decision, optionally filtered by market. Excludes "
            "proposals already superseded or sharing a symbol+side with a "
            "still-active proposal. Every entry needs a fresh current-price "
            "re-judgment (needs_reassessment=true) -- NOT a broker mutation."
        ),
    )(order_proposal_list_expired_defensive)


__all__ = [
    "ORDER_PROPOSAL_TOOL_NAMES",
    "order_proposal_create",
    "order_proposal_expire_sweep",
    "order_proposal_get",
    "order_proposal_list",
    "order_proposal_list_expired_defensive",
    "order_proposal_void",
    "register_order_proposal_tools",
    "run_order_proposal_expire_sweep",
]
