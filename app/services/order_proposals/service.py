"""OrderProposalsService — the ONLY writer surface for order_proposals (ROB-816).

Sessions are constructor-injected; this service flush()es (via the repository)
and never commits — callers own the transaction (see global-constraints.md).
"""

from __future__ import annotations

import inspect
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalBatch,
    OrderProposalApprovalBatchMember,
    OrderProposalApprovalDispatchAttempt,
    OrderProposalRung,
)
from app.models.review import TossLiveOrderLedger
from app.services.order_proposals import state_machine as sm
from app.services.order_proposals.broker_gateway import SUPPORTED_TARGET_ACTIONS
from app.services.order_proposals.defensive_ttl import (
    DEFENSIVE_EXIT_INTENTS,
    resolve_defensive_valid_until,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    ApprovalDispatchState,
    ApprovalPublication,
    CallbackEnvelope,
    CallbackGateSnapshot,
    DispatchBinding,
    TelegramDispatchResult,
    assert_callback_gate,
    build_membership_digest,
)
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalNotFound,
    OrderProposalUnsupportedTargetAction,
)
from app.services.order_proposals.payload import (
    ProposalRungSpec,
    compute_proposal_payload_hash,
)
from app.services.order_proposals.repository import OrderProposalRepository
from app.services.order_proposals.target_order import (
    TargetOrderSnapshot,
    canonical_decimal,
)
from app.services.trade_journal.trade_retrospective_service import (
    get_retrospective_by_id,
)

logger = logging.getLogger(__name__)


def _log_dispatch_outcome(result: TelegramDispatchResult, *, surface: str) -> None:
    logger.log(
        logging.INFO if result.ok else logging.ERROR,
        "order_proposals.approval_dispatch.finalized",
        extra={
            "approval_surface": surface,
            "approval_dispatch_state": result.state.value,
            "approval_dispatch_ok": result.ok,
            "http_status": result.status_code,
            "telegram_error_code": result.error_code,
            "telegram_error_classification": (
                result.error_classification.value
                if result.error_classification is not None
                else None
            ),
            "payload_chars": result.payload_chars,
            "failure_code": result.failure_code,
        },
    )


@dataclass
class RungInput:
    rung_index: int
    side: str
    quantity: Decimal
    limit_price: Decimal | None
    notional: Decimal | None


@dataclass(frozen=True)
class BatchMemberSnapshot:
    member_id: int
    proposal_id: uuid.UUID
    approval_nonce: str
    approval_message_id: int
    dispatch_binding: DispatchBinding


@dataclass(frozen=True)
class BatchRegistration:
    batch: OrderProposalApprovalBatch
    member_count: int
    summary_action: Literal["none", "send"]
    binding: DispatchBinding | None = None


@dataclass(frozen=True)
class ExpirySweepResult:
    """One group ``sweep_expired`` transitioned to ``expired`` (ROB-897).

    ``chat_id``/``message_id`` mirror what ``order_proposal_void`` reads from
    ``source_asof`` -- they are ``None`` when the proposal was never dispatched
    to Telegram (e.g. auto-approved or created before ``ORDER_PROPOSALS_TELEGRAM_ENABLED``).
    """

    proposal_id: uuid.UUID
    symbol: str
    chat_id: str | None
    message_id: int | None


@dataclass(frozen=True)
class ExpiredDefensiveProposal:
    """One expired/voided loss_cut/defensive_trim proposal for handoff (ROB-929).

    ``needs_reassessment`` is always ``True`` -- every entry in this list is,
    by construction, a defensive proposal that died without a human decision
    and needs a fresh current-price judgment next session.
    """

    proposal_id: uuid.UUID
    symbol: str
    side: str
    market: str
    exit_intent: str
    lifecycle_state: str
    limit_price: Decimal | None
    valid_until: datetime | None
    expired_or_voided_at: datetime
    needs_reassessment: bool = True


# (account_mode, market) combinations the submit path
# (revalidation.py's `_default_place_order_fn`) actually routes correctly.
# Toss has a dedicated adapter; `_place_order_impl` remains the KIS/Upbit-only
# fallback and has no `account_mode` parameter. Reject any other combination at
# create time rather than let a mock/paper/wrong-broker proposal reach submit.
_SUBMITTABLE_ACCOUNT_MODE_MARKETS: frozenset[tuple[str, str]] = frozenset(
    {
        ("kis_live", "equity_kr"),
        ("kis_live", "equity_us"),
        ("toss_live", "equity_kr"),
        ("toss_live", "equity_us"),
        ("upbit", "crypto"),
    }
)

_ACTION_CAPABILITIES = {
    "place": _SUBMITTABLE_ACCOUNT_MODE_MARKETS,
    "replace": SUPPORTED_TARGET_ACTIONS,
    "cancel": SUPPORTED_TARGET_ACTIONS,
}

# Display order only -- purely cosmetic grouping for the human-facing
# "allowed: ..." message and the supported_matrix response field. Any
# account_mode/market absent from these tuples still sorts, just after the
# named ones.
_ACCOUNT_MODE_DISPLAY_ORDER = ("kis_live", "toss_live", "upbit")
_MARKET_DISPLAY_ORDER = ("equity_kr", "equity_us", "crypto")


def _capability_sort_key(order: tuple[str, ...]) -> Callable[[str], tuple[int, str]]:
    def key(value: str) -> tuple[int, str]:
        try:
            return (order.index(value), value)
        except ValueError:
            return (len(order), value)

    return key


def _format_action_capabilities(pairs: frozenset[tuple[str, str]]) -> str:
    by_mode: dict[str, list[str]] = {}
    for mode, market in pairs:
        by_mode.setdefault(mode, []).append(market)
    mode_key = _capability_sort_key(_ACCOUNT_MODE_DISPLAY_ORDER)
    market_key = _capability_sort_key(_MARKET_DISPLAY_ORDER)
    parts = [
        f"{mode}×{'|'.join(sorted(by_mode[mode], key=market_key))}"
        for mode in sorted(by_mode, key=mode_key)
    ]
    return ", ".join(parts) + "; market aliases kr→equity_kr, us→equity_us"


def _action_contract_message(action: str) -> str:
    """Allowed-combinations text for one action, derived from its own
    capability set -- never a different action's set (ROB-972: cancel/replace
    rejections previously reused place's message and falsely advertised
    support they didn't have).
    """
    return f"allowed: {_format_action_capabilities(_ACTION_CAPABILITIES[action])}"


def _supported_matrix() -> dict[str, list[dict[str, str]]]:
    """Structured per-action allowed account_mode/market pairs.

    Derived from the same ``_ACTION_CAPABILITIES`` sets ``_action_contract_message``
    reads, so the matrix and the human-facing text can never drift apart.
    """
    mode_key = _capability_sort_key(_ACCOUNT_MODE_DISPLAY_ORDER)
    market_key = _capability_sort_key(_MARKET_DISPLAY_ORDER)
    return {
        action: [
            {"account_mode": mode, "market": market}
            for mode, market in sorted(
                pairs, key=lambda pair: (mode_key(pair[0]), market_key(pair[1]))
            )
        ]
        for action, pairs in _ACTION_CAPABILITIES.items()
    }


_LOSS_CUT_EXIT_REASONS = frozenset({"stop_loss", "thesis_change"})
_LOSS_CUT_TRIGGER_TYPES = frozenset({"stop_loss", "thesis_change"})
_LOSS_CUT_MAX_AGE = timedelta(hours=72)
_UNVERIFIED_VOID_SETTLEMENT_GRACE = timedelta(minutes=5)
_VOIDABLE_RUNG_STATES = frozenset(
    {"draft", "pending_approval", "revalidating", "needs_reconfirm", "approved"}
)
# Rung states that can legally absorb broker fill/cancel evidence. Every state
# here has ``filled``/``partially_filled``/``cancelled`` reachable in the
# transition graph (see state_machine._ALLOWED); terminal states are excluded so
# re-delivered evidence short-circuits instead of raising.
_EVIDENCE_ACCEPTING_RUNG_STATES = frozenset(
    {"acked", "resting", "partially_filled", "unverified"}
)
_LOSS_CUT_CONFIRMATION_KEY = "loss_cut_confirmation"
_LOSS_CUT_CONFIRMABLE_RUNG_STATES = frozenset({"pending_approval", "needs_reconfirm"})
_SUPERSEDE_INVALIDATABLE_RUNG_STATES = frozenset(
    {"pending_approval", "needs_reconfirm"}
)
_APPROVAL_TERMINAL_GROUP_STATES = frozenset(
    {"terminal", "rejected", "expired", "voided", "superseded"}
)


def proposal_approval_block_reason(group: OrderProposal) -> str | None:
    """Return the stable operator-facing reason a group cannot be approved."""
    if group.superseded_by_proposal_id is not None:
        return f"proposal_superseded_by:{group.superseded_by_proposal_id}"
    if group.lifecycle_state == "superseded":
        return "proposal_superseded_by:unknown"
    if group.lifecycle_state in _APPROVAL_TERMINAL_GROUP_STATES:
        return f"proposal_terminal:{group.lifecycle_state}"
    return None


def batch_member_block_reason(
    group: OrderProposal,
    rungs: list[OrderProposalRung],
    *,
    now: datetime,
) -> str | None:
    """Return why a proposal cannot be represented by a batch trigger."""
    block_reason = proposal_approval_block_reason(group)
    if block_reason is not None:
        return block_reason
    if group.exit_intent == "loss_cut":
        return "loss_cut_excluded"
    if isinstance((group.source_asof or {}).get("auto_approved"), dict):
        return "auto_approved_excluded"
    if group.approval_dispatch_state != ApprovalDispatchState.SENT_CURRENT.value:
        return "approval_dispatch_not_current"
    if group.approval_dispatch_card_kind not in {
        ApprovalCardKind.MANUAL.value,
        ApprovalCardKind.RECONFIRM.value,
    }:
        return "approval_card_kind_not_batchable"
    if group.valid_until is not None and now >= group.valid_until:
        return "proposal_expired"
    if not group.approval_nonce:
        return "approval_nonce_missing"
    if group.approval_nonce_used_at is not None:
        return "approval_nonce_used"
    if not any(rung.state == "pending_approval" for rung in rungs):
        return "no_pending_approval_rungs"
    return None


def check_action_capability(
    *, action: str | None, account_mode: str, market: str
) -> str:
    """Validate (account_mode, market) is supported for ``action``.

    Raises ``OrderProposalUnsupportedTargetAction`` (a structured, per-action
    ``supported_matrix``) when unsupported. Returns the normalized action
    ("place" default) on success.

    Callers creating a replace/cancel proposal must run this check *before*
    ``fetch_target_order`` -- that function has its own, narrower
    ``SUPPORTED_TARGET_ACTIONS`` gate with an unstructured message, and would
    otherwise be the first thing to reject an unsupported combination,
    silently bypassing this structured error (ROB-972).
    """
    normalized = action or "place"
    if normalized not in _ACTION_CAPABILITIES:
        raise OrderProposalError("action must be one of: place, replace, cancel")
    if (account_mode, market) not in _ACTION_CAPABILITIES[normalized]:
        raise OrderProposalUnsupportedTargetAction(
            "unsupported account_mode/market/action: "
            f"{account_mode}/{market}/{normalized} "
            f"({_action_contract_message(normalized)})",
            supported_matrix=_supported_matrix(),
            requested={
                "account_mode": account_mode,
                "market": market,
                "action": normalized,
            },
        )
    return normalized


def _validate_action_contract(
    *,
    action: str | None,
    account_mode: str,
    market: str,
    symbol: str,
    side: str,
    order_type: str,
    rungs: list[RungInput],
    target_broker_order_id: str | None,
    target_order_snapshot: dict[str, str | None] | None,
) -> tuple[str, TargetOrderSnapshot | None]:
    normalized = check_action_capability(
        action=action, account_mode=account_mode, market=market
    )
    if normalized == "place":
        if target_broker_order_id is not None or target_order_snapshot is not None:
            raise OrderProposalError("place proposal cannot target a broker order")
        return normalized, None
    if len(rungs) != 1:
        raise OrderProposalError(f"{normalized} proposal requires exactly one rung")
    if not target_broker_order_id or target_order_snapshot is None:
        raise OrderProposalError(f"{normalized} requires target broker evidence")

    snapshot = TargetOrderSnapshot.from_payload(target_order_snapshot)
    if snapshot.broker_order_id != target_broker_order_id:
        raise OrderProposalError("target broker order id does not match snapshot")
    if snapshot.status != "open" or Decimal(snapshot.remaining_quantity) <= 0:
        raise OrderProposalError(
            "target broker order must be open with remaining quantity"
        )
    if (
        snapshot.symbol != symbol
        or snapshot.side != side
        or snapshot.order_type != order_type
    ):
        raise OrderProposalError("target broker evidence conflicts with proposal")
    if normalized == "cancel":
        rung = rungs[0]
        if (
            canonical_decimal(rung.quantity) != snapshot.remaining_quantity
            or canonical_decimal(rung.limit_price) != snapshot.limit_price
            or rung.side != snapshot.side
        ):
            raise OrderProposalError("cancel rung must equal target broker snapshot")
    return normalized, snapshot


class OrderProposalsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = OrderProposalRepository(session)

    async def acquire_target_mutation_lock(self, group: OrderProposal) -> bool:
        """Serialize replace/cancel transactions for one broker order target.

        The transaction-scoped advisory lock must be acquired before any
        proposal row lock.  This avoids two independently-created proposals
        both cancelling/replacing the same broker order while still allowing
        distinct ladder orders to proceed independently.
        """
        action = group.action or "place"
        if action not in {"replace", "cancel"}:
            return False
        if not group.target_broker_order_id:
            raise OrderProposalError("target action requires target_broker_order_id")

        await self.acquire_broker_order_mutation_lock(
            group, group.target_broker_order_id
        )
        return True

    async def acquire_broker_order_mutation_lock(
        self, group: OrderProposal, broker_order_id: str
    ) -> None:
        """Serialize any mutation targeting one concrete broker order."""
        if not broker_order_id.strip():
            raise OrderProposalError("broker_order_id is required for mutation lock")

        lock_key = "|".join(
            (
                "order_proposal_target",
                group.account_mode,
                group.market,
                group.broker_account_id or "",
                broker_order_id,
            )
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    async def create_proposal(
        self,
        *,
        symbol: str,
        market: str,
        account_mode: str,
        side: str,
        order_type: str,
        proposer: str,
        rungs: list[RungInput],
        thesis: str | None = None,
        strategy: str | None = None,
        rationale: dict | None = None,
        broker_account_id: str | None = None,
        lot_context: dict | None = None,
        valid_until: datetime | None = None,
        exit_intent: str | None = None,
        exit_reason: str | None = None,
        retrospective_id: int | None = None,
        approval_issue_id: str | None = None,
        correlation_id: str | None = None,
        source_asof: dict | None = None,
        supersedes_proposal_id: uuid.UUID | None = None,
        action: str | None = None,
        target_broker_order_id: str | None = None,
        target_order_snapshot: dict[str, str | None] | None = None,
        now: datetime | None = None,
    ) -> OrderProposal:
        if not rungs:
            raise ValueError("at least one rung required")
        normalized_action, target_snapshot = _validate_action_contract(
            action=action,
            account_mode=account_mode,
            market=market,
            symbol=symbol,
            side=side,
            order_type=order_type,
            rungs=rungs,
            target_broker_order_id=target_broker_order_id,
            target_order_snapshot=target_order_snapshot,
        )
        normalized_target_snapshot = (
            target_snapshot.to_payload() if target_snapshot is not None else None
        )
        merged_source_asof = dict(source_asof or {})
        if normalized_target_snapshot is not None:
            merged_source_asof["target_order_snapshot"] = normalized_target_snapshot
        now = now or datetime.now(UTC)
        self._require_timezone_aware(now)
        defensive_floor = (
            resolve_defensive_valid_until(market, now)
            if exit_intent in DEFENSIVE_EXIT_INTENTS
            else None
        )
        if valid_until is None:
            valid_until = (now.astimezone(KST) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            self._require_timezone_aware(valid_until)
            if valid_until <= now:
                raise OrderProposalError("valid_until must be in the future")
        # ROB-929: loss_cut proposals must stay approvable through the next
        # observed Telegram approval window -- this only raises a too-short
        # valid_until (default or caller-supplied) up to that window's end; a
        # caller-supplied longer window is left untouched. DEFENSIVE_EXIT_INTENTS
        # also names "defensive_trim" for forward-compat with the read-only
        # handoff surface below, but _validate_exit_binding still rejects
        # exit_intent="defensive_trim" at create time (no execution-path
        # support yet) -- so only loss_cut can reach here in practice today.
        if defensive_floor is not None and valid_until < defensive_floor:
            valid_until = defensive_floor
        await self._validate_exit_binding(
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            side=side,
            order_type=order_type,
            exit_intent=exit_intent,
            exit_reason=exit_reason,
            retrospective_id=retrospective_id,
            approval_issue_id=approval_issue_id,
            now=now,
        )
        proposal_id = uuid.uuid4()
        root_id = proposal_id
        superseded_group: OrderProposal | None = None
        if supersedes_proposal_id is not None:
            superseded_group = await self._repo.get_group_by_proposal_id(
                supersedes_proposal_id, for_update=True
            )
            if superseded_group is None:
                raise OrderProposalNotFound(str(supersedes_proposal_id))
            root_id = superseded_group.root_proposal_id

        payload_hash = compute_proposal_payload_hash(
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            order_type=order_type,
            exit_intent=exit_intent,
            exit_reason=exit_reason,
            retrospective_id=retrospective_id,
            approval_issue_id=approval_issue_id,
            action=normalized_action,
            target_broker_order_id=target_broker_order_id,
            target_order_snapshot=normalized_target_snapshot,
            rungs=[
                ProposalRungSpec(
                    r.rung_index,
                    r.side,
                    str(r.quantity),
                    None if r.limit_price is None else str(r.limit_price),
                    None if r.notional is None else str(r.notional),
                )
                for r in rungs
            ],
        )

        group = await self._repo.insert_group(
            proposal_id=proposal_id,
            root_proposal_id=root_id,
            revision=1,
            supersedes_proposal_id=supersedes_proposal_id,
            no_resubmit=False,
            payload_hash=payload_hash,
            symbol=symbol,
            market=market,
            account_mode=account_mode,
            side=side,
            order_type=order_type,
            proposer=proposer,
            thesis=thesis,
            strategy=strategy,
            rationale=rationale,
            broker_account_id=broker_account_id,
            lot_context=lot_context,
            action=normalized_action,
            target_broker_order_id=target_broker_order_id,
            exit_intent=exit_intent,
            exit_reason=exit_reason,
            retrospective_id=retrospective_id,
            approval_issue_id=approval_issue_id,
            lifecycle_state="proposed",
            correlation_id=correlation_id,
            valid_until=valid_until,
            source_asof=merged_source_asof or None,
        )
        for r in rungs:
            await self._repo.insert_rung(
                proposal_pk=group.id,
                rung_index=r.rung_index,
                side=r.side,
                quantity=r.quantity,
                limit_price=r.limit_price,
                notional=r.notional,
                state="pending_approval",
            )
        if superseded_group is not None:
            superseded_rungs = await self._repo.list_rungs(superseded_group.id)
            for superseded_rung in superseded_rungs:
                if superseded_rung.state not in _SUPERSEDE_INVALIDATABLE_RUNG_STATES:
                    continue
                sm.assert_rung_transition(superseded_rung.state, "superseded")
                await self._repo.update_rung(
                    superseded_rung,
                    state="superseded",
                    updated_at=now,
                )
            await self._repo.update_group(
                superseded_group,
                lifecycle_state="superseded",
                superseded_by_proposal_id=proposal_id,
                approval_nonce_used_at=now,
            )
        return group

    async def _validate_exit_binding(
        self,
        *,
        symbol: str,
        market: str,
        account_mode: str,
        side: str,
        order_type: str,
        exit_intent: str | None,
        exit_reason: str | None,
        retrospective_id: int | None,
        approval_issue_id: str | None,
        now: datetime,
    ) -> None:
        supporting = (exit_reason, retrospective_id, approval_issue_id)
        if exit_intent is None:
            if any(value is not None for value in supporting):
                raise OrderProposalError("exit binding fields require exit_intent")
            return
        if exit_intent != "loss_cut":
            # ROB-929 code review: every submit path (order_execution.py,
            # orders_kis_variants.py, orders_toss_variants.py) still rejects
            # anything but exit_intent="loss_cut" -- accepting "defensive_trim"
            # here would create a proposal that TTL-floors and lists correctly
            # but dies in revalidation the moment it's approved (a zombie
            # lane). Execution-side support for defensive_trim is a separate,
            # not-yet-scoped issue; fail closed here until that lands.
            raise OrderProposalError(
                "unknown exit_intent (only 'loss_cut' is currently supported "
                "end-to-end; defensive_trim execution support is a separate "
                "issue)"
            )

        errors: list[str] = []
        if exit_reason not in _LOSS_CUT_EXIT_REASONS:
            errors.append(
                "loss_cut requires exit_reason in ['stop_loss', 'thesis_change']"
            )
        if retrospective_id is None:
            errors.append("loss_cut requires retrospective_id")
        if (account_mode, market) not in {
            ("kis_live", "equity_kr"),
            ("kis_live", "equity_us"),
            ("toss_live", "equity_kr"),
            ("toss_live", "equity_us"),
            ("upbit", "crypto"),
        }:
            errors.append("loss_cut requires a supported live account and market")
        if side != "sell":
            errors.append("loss_cut requires side='sell'")
        if order_type != "limit":
            errors.append("loss_cut requires order_type='limit'")

        retro = None
        if retrospective_id is not None:
            retro = await get_retrospective_by_id(self._session, retrospective_id)
            if retro is None:
                errors.append(f"retrospective_id {retrospective_id} not found")
            else:
                if (retro.symbol or "").strip().upper() != symbol.strip().upper():
                    errors.append(
                        f"retrospective_id {retrospective_id} symbol mismatch"
                    )
                if retro.trigger_type not in _LOSS_CUT_TRIGGER_TYPES:
                    errors.append("retrospective trigger_type is not loss-cut eligible")
                created = retro.created_at
                if created is not None:
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=UTC)
                    if (
                        now.astimezone(UTC) - created.astimezone(UTC)
                        > _LOSS_CUT_MAX_AGE
                    ):
                        errors.append(
                            f"retrospective_id {retrospective_id} is stale (> 72h old)"
                        )
        if errors:
            raise OrderProposalError("loss_cut proposal invalid: " + "; ".join(errors))

    async def get_proposal(
        self, proposal_id: uuid.UUID
    ) -> tuple[OrderProposal, list[OrderProposalRung]]:
        group = await self._repo.get_group_by_proposal_id(proposal_id)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        rungs = await self._repo.list_rungs(group.id)
        return group, rungs

    async def resolve_proposal_id_prefix(
        self, proposal_prefix: str
    ) -> uuid.UUID | None:
        if len(proposal_prefix) != 8 or any(
            char not in "0123456789abcdefABCDEF" for char in proposal_prefix
        ):
            return None
        matches = await self._repo.list_groups_by_proposal_prefix(proposal_prefix)
        if len(matches) != 1:
            return None
        return matches[0].proposal_id

    async def list_recent(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
        lifecycle_state: str | None = None,
    ) -> list[tuple[OrderProposal, list[OrderProposalRung]]]:
        groups = await self._repo.list_recent_groups(
            limit=limit, symbol=symbol, lifecycle_state=lifecycle_state
        )
        return [(g, await self._repo.list_rungs(g.id)) for g in groups]

    async def transition_rung(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        new_state: str,
        **audit_fields: Any,
    ) -> OrderProposalRung:
        group, rung = await self._get_locked_rung(proposal_id, rung_index)
        return await self._transition_locked_rung(
            group, rung, new_state=new_state, **audit_fields
        )

    async def _get_locked_rung(
        self, proposal_id: uuid.UUID, rung_index: int
    ) -> tuple[OrderProposal, OrderProposalRung]:
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        rungs = await self._repo.list_rungs(group.id)
        rung = next((r for r in rungs if r.rung_index == rung_index), None)
        if rung is None:
            raise OrderProposalNotFound(f"{proposal_id}#{rung_index}")
        return group, rung

    async def _transition_locked_rung(
        self,
        group: OrderProposal,
        rung: OrderProposalRung,
        *,
        new_state: str,
        **audit_fields: Any,
    ) -> OrderProposalRung:
        sm.assert_rung_transition(rung.state, new_state)
        rung = await self._repo.update_rung(rung, state=new_state, **audit_fields)
        rungs = await self._repo.list_rungs(group.id)
        await self._repo.update_group(
            group, lifecycle_state=self._recompute_group_state(rungs)
        )
        return rung

    @staticmethod
    def _recompute_group_state(rungs: list[OrderProposalRung]) -> str:
        states = {r.state for r in rungs}
        if states <= {
            "filled",
            "cancelled",
            "expired",
            "rejected",
            "voided",
            "voided_local_stale",
            "superseded",
        }:
            if states == {"expired"}:
                return "expired"
            if states == {"rejected"}:
                return "rejected"
            if states <= {"voided", "voided_local_stale"}:
                return "voided"
            return "terminal"
        if states & {"acked", "resting", "partially_filled", "filled", "submitting"}:
            if states & {
                "pending_approval",
                "revalidating",
                "approved",
                "needs_reconfirm",
            }:
                return "partially_submitted"
            return "submitted"
        if states & {"approved"}:
            return "approved"
        return "proposed"

    async def expire_if_needed(self, proposal_id: uuid.UUID, *, now: datetime) -> bool:
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        if group.valid_until is not None and group.valid_until > now:
            return False

        rungs = await self._repo.list_rungs(group.id)
        invalid_rung = next(
            (rung for rung in rungs if rung.state not in _VOIDABLE_RUNG_STATES), None
        )
        if invalid_rung is not None:
            raise OrderProposalError(
                f"cannot expire proposal with rung {invalid_rung.rung_index} "
                f"in state {invalid_rung.state!r}"
            )

        expired_rungs = []
        for rung in rungs:
            sm.assert_rung_transition(rung.state, "expired")
            expired_rungs.append(
                await self._repo.update_rung(rung, state="expired", updated_at=now)
            )
        await self._repo.update_group(
            group,
            lifecycle_state=self._recompute_group_state(expired_rungs),
            approval_nonce=None,
        )
        return True

    async def void_proposal(
        self,
        proposal_id: uuid.UUID,
        *,
        reason: str,
        now: datetime,
        broker_evidence: Callable[..., Any] | None = None,
    ) -> list[OrderProposalRung]:
        self._require_timezone_aware(now)
        reason = reason.strip()
        if not reason:
            raise OrderProposalError("void reason is required")

        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        rungs = await self._repo.list_rungs(group.id)
        allowed_states = _VOIDABLE_RUNG_STATES | {"unverified"}
        invalid_rung = next(
            (rung for rung in rungs if rung.state not in allowed_states), None
        )
        if invalid_rung is not None:
            raise OrderProposalError(
                f"cannot void proposal with rung {invalid_rung.rung_index} "
                f"in state {invalid_rung.state!r}"
            )

        unverified_rungs = [rung for rung in rungs if rung.state == "unverified"]
        evidence_summary = ""
        if unverified_rungs:
            if broker_evidence is None:
                invalid_rung = unverified_rungs[0]
                raise OrderProposalError(
                    f"cannot void proposal with rung {invalid_rung.rung_index} "
                    "in state 'unverified' without broker absence evidence"
                )
            # The submit path records ``unverified`` only after its broker call
            # has returned or raised. A successful Toss response commits its
            # accepted-only ledger row before that path can transition the
            # proposal. The grace interval covers broker-side visibility after
            # an ambiguous timeout; the proposal row lock then prevents submit
            # state changes while the remote scan and final ledger read run.
            recent_rung = next(
                (
                    rung
                    for rung in unverified_rungs
                    if now - rung.updated_at < _UNVERIFIED_VOID_SETTLEMENT_GRACE
                ),
                None,
            )
            if recent_rung is not None:
                raise OrderProposalError(
                    "cannot void proposal: broker settlement grace has not elapsed "
                    f"for unverified rung {recent_rung.rung_index} "
                    f"(required={int(_UNVERIFIED_VOID_SETTLEMENT_GRACE.total_seconds())}s)"
                )
            try:
                evidence_by_rung = broker_evidence(
                    group=group,
                    rungs=unverified_rungs,
                    now=now,
                )
                if inspect.isawaitable(evidence_by_rung):
                    evidence_by_rung = await evidence_by_rung
            except Exception as exc:
                raise OrderProposalError(
                    f"broker evidence lookup failed; refusing void: {exc}"
                ) from exc

            toss_ledger_rows: list[TossLiveOrderLedger] = []
            if group.account_mode == "toss_live":
                client_order_ids = [
                    str(rung.idempotency_key).strip()
                    for rung in unverified_rungs
                    if rung.idempotency_key
                ]
                broker_order_ids = [
                    str(rung.broker_order_id).strip()
                    for rung in unverified_rungs
                    if rung.broker_order_id
                ]
                predicates = []
                if client_order_ids:
                    predicates.append(
                        TossLiveOrderLedger.client_order_id.in_(client_order_ids)
                    )
                if broker_order_ids:
                    predicates.append(
                        TossLiveOrderLedger.broker_order_id.in_(broker_order_ids)
                    )
                if predicates:
                    toss_ledger_rows = list(
                        (
                            await self._session.execute(
                                select(TossLiveOrderLedger).where(or_(*predicates))
                            )
                        )
                        .scalars()
                        .all()
                    )

            summaries = []
            for rung in unverified_rungs:
                evidence = evidence_by_rung.get(rung.rung_index)
                if evidence is None:
                    raise OrderProposalError(
                        "broker evidence lookup incomplete; refusing void for "
                        f"unverified rung {rung.rung_index}"
                    )
                outcome = str(getattr(evidence, "outcome", "unknown"))
                scope = " ".join(str(getattr(evidence, "lookup_scope", "")).split())
                if outcome == "found":
                    broker_state = getattr(evidence, "broker_state", None)
                    broker_order_id = getattr(evidence, "broker_order_id", None)
                    raise OrderProposalError(
                        f"cannot void proposal: broker order exists for rung "
                        f"{rung.rung_index} (state={broker_state!r}, "
                        f"broker_order_id={broker_order_id!r}, scope={scope!r}); "
                        "run reconcile"
                    )
                if outcome != "absent":
                    evidence_reason = getattr(evidence, "reason", None)
                    raise OrderProposalError(
                        f"cannot void proposal: broker absence is unverified for rung "
                        f"{rung.rung_index} (outcome={outcome!r}, "
                        f"reason={evidence_reason!r}, scope={scope!r})"
                    )
                matching_ledger_rows = [
                    row
                    for row in toss_ledger_rows
                    if (row.broker_order_id is not None or row.status != "rejected")
                    and (
                        (
                            rung.idempotency_key
                            and row.client_order_id == rung.idempotency_key
                        )
                        or (
                            rung.broker_order_id
                            and row.broker_order_id == rung.broker_order_id
                        )
                    )
                ]
                if matching_ledger_rows:
                    ledger_row = matching_ledger_rows[0]
                    raise OrderProposalError(
                        "cannot void proposal: toss_live_order_ledger contains "
                        f"broker evidence for rung {rung.rung_index} "
                        f"(status={ledger_row.status!r}, "
                        f"broker_order_id={ledger_row.broker_order_id!r})"
                    )
                ledger_summary = (
                    " toss_live_order_ledger rows=0"
                    if group.account_mode == "toss_live"
                    else ""
                )
                summaries.append(
                    f"rung={rung.rung_index} outcome=absent "
                    f"scope={scope!r}{ledger_summary}"
                )
            evidence_summary = " | broker_evidence: " + "; ".join(summaries)

        audit_reason = reason + evidence_summary

        voided_rungs = []
        for rung in rungs:
            target_state = (
                "voided_local_stale" if rung.state == "unverified" else "voided"
            )
            sm.assert_rung_transition(rung.state, target_state)
            audit_fields: dict[str, Any] = {
                "state": target_state,
                "updated_at": now,
            }
            if target_state == "voided_local_stale":
                audit_fields["void_reason"] = audit_reason
            voided_rungs.append(await self._repo.update_rung(rung, **audit_fields))
        await self._repo.update_group(
            group,
            lifecycle_state=self._recompute_group_state(voided_rungs),
            void_reason=audit_reason,
            no_resubmit=True,
            approval_nonce=None,
            approval_nonce_used_at=now,
        )
        return voided_rungs

    # -- PR-2 helpers -------------------------------------------------------
    async def set_approval_nonce(self, proposal_id: uuid.UUID, nonce: str) -> None:
        """Stage a nonce while fail-closing any previously published card.

        Production dispatch calls this immediately before
        ``start_approval_dispatch`` in the same transaction. Keeping the
        intermediate state non-approvable also makes direct/legacy callers
        unable to pair a fresh nonce with an older published binding.
        """
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        block_reason = proposal_approval_block_reason(group)
        if block_reason is not None:
            raise OrderProposalError(block_reason)
        await self._repo.update_group(
            group,
            approval_nonce=nonce,
            approval_nonce_used_at=None,
            approval_dispatch_state=ApprovalDispatchState.FAILED.value,
            approval_dispatch_published_at=None,
            approval_dispatch_failure_code="approval_dispatch_snapshot_missing",
        )

    async def consume_approval_nonce(
        self, proposal_id: uuid.UUID, nonce: str, *, now: datetime
    ) -> OrderProposal:
        """Compatibility wrapper; every consume still crosses the common gate."""
        group = await self._repo.get_group_by_proposal_id(proposal_id)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        callback = self._current_callback_envelope(group, action="op", nonce=nonce)
        return await self.consume_published_proposal_callback(
            proposal_id, callback=callback, now=now
        )

    async def consume_auto_veto_nonce(
        self, proposal_id: uuid.UUID, nonce: str, *, now: datetime
    ) -> OrderProposal:
        """Compatibility wrapper for the same authoritative callback gate."""
        group = await self._repo.get_group_by_proposal_id(proposal_id)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        callback = self._current_callback_envelope(group, action="vc", nonce=nonce)
        return await self.consume_published_proposal_callback(
            proposal_id, callback=callback, now=now
        )

    @staticmethod
    def _current_callback_envelope(
        group: OrderProposal, *, action: str, nonce: str
    ) -> CallbackEnvelope:
        attempt_id = group.approval_dispatch_attempt_id
        revision = group.approval_dispatch_membership_revision
        digest = group.approval_dispatch_membership_digest
        if attempt_id is None or revision is None or digest is None:
            raise OrderProposalError("approval_dispatch_snapshot_missing")
        return CallbackEnvelope(
            action=action,
            subject_short=str(group.proposal_id)[:8],
            attempt_id=attempt_id,
            membership_revision=revision,
            membership_digest=digest,
            nonce=nonce,
        )

    @staticmethod
    def _assert_published_proposal_binding(
        group: OrderProposal, *, callback: CallbackEnvelope
    ) -> ApprovalCardKind:
        """Validate one proposal callback without consuming its nonce.

        Both the read-only preflight and the row-locked consuming gate call this
        exact function.  The second call is intentional: any owner/nonce/
        membership change while external preview work is in flight fails
        closed before a nonce or proposal mutation is written.
        """
        block_reason = proposal_approval_block_reason(group)
        if block_reason is not None:
            raise OrderProposalError(block_reason)
        try:
            state = ApprovalDispatchState(str(group.approval_dispatch_state))
        except ValueError as exc:
            raise OrderProposalError("approval_dispatch_state_invalid") from exc
        try:
            card_kind = (
                ApprovalCardKind(str(group.approval_dispatch_card_kind))
                if group.approval_dispatch_card_kind is not None
                else None
            )
        except ValueError as exc:
            raise OrderProposalError("approval_dispatch_card_kind_invalid") from exc
        try:
            assert_callback_gate(
                snapshot=CallbackGateSnapshot(
                    subject_short=str(group.proposal_id)[:8],
                    state=state,
                    attempt_id=group.approval_dispatch_attempt_id,
                    card_kind=card_kind,
                    membership_revision=(group.approval_dispatch_membership_revision),
                    membership_digest=group.approval_dispatch_membership_digest,
                    nonce=group.approval_nonce,
                    nonce_used=group.approval_nonce_used_at is not None,
                ),
                callback=callback,
            )
        except ValueError as exc:
            raise OrderProposalError(str(exc)) from exc
        if card_kind is None:  # Kept explicit for the narrowed return type.
            raise OrderProposalError("approval_dispatch_card_kind_invalid")

        auto_approved = isinstance(
            ((group.source_asof or {}).get("auto_approved")), dict
        )
        if card_kind is ApprovalCardKind.AUTO_VETO:
            if not auto_approved:
                raise OrderProposalError("auto_veto_not_available")
        elif auto_approved:
            raise OrderProposalError("auto_veto_nonce_requires_vc")
        return card_kind

    async def preflight_published_proposal_callback(
        self,
        proposal_id: uuid.UUID,
        *,
        callback: CallbackEnvelope,
    ) -> OrderProposal:
        """Read-only binding gate that must precede callback-side external I/O."""
        group = await self._repo.get_group_by_proposal_id(proposal_id)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        self._assert_published_proposal_binding(group, callback=callback)
        return group

    async def consume_published_proposal_callback(
        self,
        proposal_id: uuid.UUID,
        *,
        callback: CallbackEnvelope,
        now: datetime,
        telegram_user_id: str | None = None,
    ) -> OrderProposal:
        """The single nonce-consumption gate for every proposal card kind."""
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        self._assert_published_proposal_binding(group, callback=callback)

        fields: dict[str, Any] = {"approval_nonce_used_at": now}
        if callback.action == "lc":
            fields["source_asof"] = await self._validated_loss_cut_confirmation_source(
                group,
                nonce=callback.nonce,
                telegram_user_id=telegram_user_id or "",
                now=now,
            )
        return await self._repo.update_group(group, **fields)

    async def issue_loss_cut_confirmation(
        self,
        proposal_id: uuid.UUID,
        *,
        first_nonce: str,
        confirmation_nonce: str,
        telegram_user_id: str,
        now: datetime,
        ttl_seconds: int = 90,
    ) -> OrderProposal:
        """Replace a consumed first-step nonce with a bound second-step nonce."""
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        if group.exit_intent != "loss_cut":
            raise OrderProposalError("loss_cut_confirmation_requires_loss_cut")
        if group.approval_nonce != first_nonce or group.approval_nonce_used_at is None:
            raise OrderProposalError("loss_cut_first_nonce_not_consumed")
        rungs = await self._repo.list_rungs(group.id)
        binding = [
            {
                "rung_index": rung.rung_index,
                "approval_revision": rung.approval_revision or 0,
            }
            for rung in sorted(rungs, key=lambda item: item.rung_index)
            if rung.state in _LOSS_CUT_CONFIRMABLE_RUNG_STATES
        ]
        if not binding:
            raise OrderProposalError("loss_cut_confirmation_has_no_eligible_rungs")
        envelope = {
            "proposal_id": str(proposal_id),
            "rungs": binding,
            "nonce": confirmation_nonce,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "first_click": {
                "telegram_user_id": telegram_user_id,
                "clicked_at": now.isoformat(),
                "nonce": first_nonce,
            },
            "second_click": None,
        }
        source_asof = {
            **(group.source_asof or {}),
            _LOSS_CUT_CONFIRMATION_KEY: envelope,
        }
        return await self._repo.update_group(
            group,
            source_asof=source_asof,
            approval_nonce=confirmation_nonce,
            approval_nonce_used_at=None,
        )

    async def consume_loss_cut_confirmation(
        self,
        proposal_id: uuid.UUID,
        nonce: str,
        *,
        telegram_user_id: str,
        now: datetime,
    ) -> OrderProposal:
        """Compatibility wrapper for the common published-callback gate."""
        group = await self._repo.get_group_by_proposal_id(proposal_id)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        callback = self._current_callback_envelope(group, action="lc", nonce=nonce)
        return await self.consume_published_proposal_callback(
            proposal_id,
            callback=callback,
            telegram_user_id=telegram_user_id,
            now=now,
        )

    async def _validated_loss_cut_confirmation_source(
        self,
        group: OrderProposal,
        *,
        nonce: str,
        telegram_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        envelope = (group.source_asof or {}).get(_LOSS_CUT_CONFIRMATION_KEY)
        if not isinstance(envelope, dict):
            raise OrderProposalError("loss_cut_confirmation_missing")
        try:
            expires_at = datetime.fromisoformat(str(envelope["expires_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderProposalError("loss_cut_confirmation_invalid") from exc
        self._require_timezone_aware(expires_at)
        if now > expires_at:
            raise OrderProposalError("loss_cut_confirmation_expired")
        rungs = await self._repo.list_rungs(group.id)
        current_binding = [
            {
                "rung_index": rung.rung_index,
                "approval_revision": rung.approval_revision or 0,
            }
            for rung in sorted(rungs, key=lambda item: item.rung_index)
            if rung.state in _LOSS_CUT_CONFIRMABLE_RUNG_STATES
        ]
        if (
            envelope.get("proposal_id") != str(group.proposal_id)
            or envelope.get("nonce") != nonce
            or envelope.get("rungs") != current_binding
        ):
            raise OrderProposalError("loss_cut_confirmation_binding_mismatch")
        updated_envelope = {
            **envelope,
            "second_click": {
                "telegram_user_id": telegram_user_id,
                "clicked_at": now.isoformat(),
                "nonce": nonce,
            },
        }
        source_asof = {
            **(group.source_asof or {}),
            _LOSS_CUT_CONFIRMATION_KEY: updated_envelope,
        }
        return source_asof

    async def record_approval(
        self,
        proposal_id: uuid.UUID,
        *,
        telegram_user_id: str,
        now: datetime,
    ) -> OrderProposal:
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        return await self._repo.update_group(
            group, approved_by_telegram_user_id=telegram_user_id, approved_at=now
        )

    async def record_approval_dispatch(
        self,
        proposal_id: uuid.UUID,
        *,
        message_id: int,
        chat_id: str,
        now: datetime,
    ) -> OrderProposal:
        """Record legacy message location without creating an approvable card.

        ``message_id``/``chat_id``/``sent_at`` stay in ``source_asof`` so later
        Telegram edits can find them. A caller without an attempt-bound,
        immutable snapshot cannot manufacture ``SENT_CURRENT`` through this
        compatibility helper.
        """
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        existing = group.source_asof or {}
        merged = {
            **existing,
            "approval_message_id": message_id,
            "approval_chat_id": chat_id,
            "approval_sent_at": now.isoformat(),
        }
        return await self._repo.update_group(
            group,
            source_asof=merged,
            approval_dispatch_state=ApprovalDispatchState.FAILED.value,
            approval_dispatch_attempted_at=(
                group.approval_dispatch_attempted_at or now
            ),
            approval_dispatch_published_at=None,
            approval_dispatch_failure_code="approval_dispatch_snapshot_missing",
            approval_nonce=None,
            approval_nonce_used_at=None,
        )

    async def start_approval_dispatch(
        self,
        proposal_id: uuid.UUID,
        *,
        attempt_id: uuid.UUID,
        binding: DispatchBinding,
        now: datetime,
        payload_chars: int,
        context_message_count: int,
    ) -> OrderProposalApprovalDispatchAttempt:
        """Commit a pending attempt before any Telegram I/O begins."""
        self._require_timezone_aware(now)
        if payload_chars < 0:
            raise ValueError("payload_chars must be non-negative")
        if context_message_count < 0:
            raise ValueError("context_message_count must be non-negative")
        if binding.attempt_id != attempt_id:
            raise ValueError("dispatch binding attempt_id mismatch")
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        expected_revision = (group.approval_dispatch_membership_revision or 0) + 1
        if binding.membership_revision != expected_revision:
            raise OrderProposalError("approval_membership_revision_not_next")
        if binding.card_kind is ApprovalCardKind.BATCH:
            raise OrderProposalError("proposal_dispatch_card_kind_invalid")
        expected_digest = build_membership_digest(
            card_kind=binding.card_kind,
            membership_revision=binding.membership_revision,
            members=[
                {
                    "proposal_id": str(group.proposal_id),
                    "approval_nonce": group.approval_nonce,
                }
            ],
        )
        if binding.membership_digest != expected_digest:
            raise OrderProposalError("approval_membership_digest_invalid")

        previous_attempt_id = group.approval_dispatch_attempt_id
        if previous_attempt_id is not None and previous_attempt_id != attempt_id:
            previous = await self._repo.get_approval_dispatch_attempt(
                previous_attempt_id, for_update=True
            )
            if (
                previous is not None
                and previous.state == ApprovalDispatchState.SENT_CURRENT.value
            ):
                await self._repo.update_approval_dispatch_attempt(
                    previous,
                    state=ApprovalDispatchState.SENT_SUPERSEDED.value,
                    failure_code="approval_dispatch_superseded",
                )
        attempt = await self._repo.insert_approval_dispatch_attempt(
            attempt_id=attempt_id,
            proposal_pk=group.id,
            state=ApprovalDispatchState.PENDING.value,
            attempted_at=now,
            payload_chars=payload_chars,
            context_message_count=context_message_count,
            card_kind=binding.card_kind.value,
            membership_revision=binding.membership_revision,
            membership_digest=binding.membership_digest,
        )
        await self._repo.update_group(
            group,
            approval_dispatch_state=ApprovalDispatchState.PENDING.value,
            approval_dispatch_attempt_id=attempt_id,
            approval_dispatch_attempted_at=now,
            approval_dispatch_published_at=None,
            approval_dispatch_failure_code=None,
            approval_dispatch_payload_chars=payload_chars,
            approval_dispatch_card_kind=binding.card_kind.value,
            approval_dispatch_membership_revision=binding.membership_revision,
            approval_dispatch_membership_digest=binding.membership_digest,
        )
        return attempt

    async def finish_approval_dispatch(
        self,
        proposal_id: uuid.UUID,
        *,
        attempt_id: uuid.UUID,
        publication: ApprovalPublication,
        chat_id: str | None,
        now: datetime,
    ) -> TelegramDispatchResult:
        """Resolve physical publication through the current-owner fence."""
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        attempt = await self._repo.get_approval_dispatch_attempt(
            attempt_id, for_update=True
        )
        if attempt is None or attempt.proposal_pk != group.id:
            raise OrderProposalError("approval_dispatch_attempt_not_found")
        if attempt.state != ApprovalDispatchState.PENDING.value:
            raise OrderProposalError("approval_dispatch_attempt_already_finished")
        if publication.card_published and (
            publication.message_id is None or chat_id is None
        ):
            raise OrderProposalError(
                "successful dispatch requires message_id and chat_id"
            )

        try:
            attempt_card_kind = ApprovalCardKind(str(attempt.card_kind))
        except ValueError as exc:
            raise OrderProposalError("approval_dispatch_card_kind_invalid") from exc
        expected_digest = build_membership_digest(
            card_kind=attempt_card_kind,
            membership_revision=attempt.membership_revision,
            members=[
                {
                    "proposal_id": str(group.proposal_id),
                    "approval_nonce": group.approval_nonce,
                }
            ],
        )
        is_current_owner = (
            group.approval_dispatch_state == ApprovalDispatchState.PENDING.value
            and group.approval_dispatch_attempt_id == attempt_id
            and group.approval_dispatch_card_kind == attempt.card_kind
            and group.approval_dispatch_membership_revision
            == attempt.membership_revision
            and group.approval_dispatch_membership_digest == attempt.membership_digest
            and expected_digest == attempt.membership_digest
        )
        snapshot_missing = publication.card_published and not group.approval_nonce
        if not is_current_owner:
            state = (
                ApprovalDispatchState.SENT_SUPERSEDED
                if publication.card_published
                else ApprovalDispatchState.FAILED_SUPERSEDED
            )
            failure_code = "approval_dispatch_superseded"
        elif snapshot_missing:
            state = ApprovalDispatchState.FAILED
            failure_code = "approval_dispatch_snapshot_missing"
        elif publication.card_published:
            state = ApprovalDispatchState.SENT_CURRENT
            failure_code = None
        elif publication.partial:
            state = ApprovalDispatchState.PARTIAL_FAILED
            failure_code = publication.failure_code or "telegram_dispatch_failed"
        else:
            state = ApprovalDispatchState.FAILED
            failure_code = publication.failure_code or "telegram_dispatch_failed"

        await self._repo.update_approval_dispatch_attempt(
            attempt,
            state=state.value,
            completed_at=now,
            message_id=publication.message_id,
            status_code=publication.status_code,
            telegram_error_code=publication.error_code,
            error_classification=(
                publication.error_classification.value
                if publication.error_classification is not None
                else None
            ),
            failure_code=failure_code,
        )

        result = TelegramDispatchResult.from_publication(
            publication, state=state, failure_code=failure_code
        )
        if not is_current_owner:
            _log_dispatch_outcome(result, surface="proposal")
            return result

        fields: dict[str, Any] = {
            "approval_dispatch_state": state.value,
            "approval_dispatch_failure_code": failure_code,
            "approval_dispatch_payload_chars": publication.payload_chars,
        }
        if state is ApprovalDispatchState.SENT_CURRENT:
            existing = group.source_asof or {}
            fields["source_asof"] = {
                **existing,
                "approval_message_id": publication.message_id,
                "approval_chat_id": chat_id,
                "approval_sent_at": now.isoformat(),
            }
            fields["approval_dispatch_published_at"] = now
        else:
            # Fail closed if Telegram accepted a message but its response was
            # lost: an unconfirmed button cannot consume this attempt's nonce.
            fields["approval_nonce"] = None
            fields["approval_nonce_used_at"] = None
            fields["approval_dispatch_published_at"] = None
        await self._repo.update_group(group, **fields)
        _log_dispatch_outcome(result, surface="proposal")
        return result

    async def register_approval_batch_member(
        self,
        proposal_id: uuid.UUID,
        *,
        chat_id: str,
        approval_message_id: int,
        now: datetime,
        window_seconds: int = 600,
        ttl_seconds: int = 600,
    ) -> BatchRegistration | None:
        """Stage a member, freezing the batch before its first publication.

        A frozen batch is never reopened or edited.  The next proposal starts
        a new staged batch, so membership displayed with a button is immutable.
        """
        self._require_timezone_aware(now)
        await self._repo.acquire_approval_batch_chat_lock(chat_id)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        rungs = await self._repo.list_rungs(group.id)
        if batch_member_block_reason(group, rungs, now=now) is not None:
            return None
        if await self._repo.get_approval_batch_member_by_nonce(
            proposal_pk=group.id,
            approval_nonce=str(group.approval_nonce),
        ):
            return None

        batch = await self._repo.get_open_approval_batch(
            chat_id=chat_id, now=now, for_update=True
        )
        if batch is None:
            batch = await self._repo.insert_approval_batch(
                batch_id=uuid.uuid4(),
                chat_id=chat_id,
                window_started_at=now,
                window_closes_at=now + timedelta(seconds=window_seconds),
                expires_at=self._bounded_batch_expiry(
                    now=now,
                    ttl_seconds=ttl_seconds,
                    validity_deadlines=[group.valid_until],
                ),
                approval_nonce=secrets.token_urlsafe(8),
                summary_dispatch_state="idle",
                approval_dispatch_state=ApprovalDispatchState.PENDING.value,
                membership_revision=1,
            )

        existing_members = await self._repo.list_approval_batch_members(batch.id)
        if any(member.proposal_pk == group.id for member in existing_members):
            return None

        await self._repo.insert_approval_batch_member(
            batch_pk=batch.id,
            proposal_pk=group.id,
            approval_nonce_snapshot=str(group.approval_nonce),
            approval_message_id=approval_message_id,
            added_at=now,
            approval_dispatch_attempt_id_snapshot=(group.approval_dispatch_attempt_id),
            approval_membership_revision_snapshot=(
                group.approval_dispatch_membership_revision
            ),
            approval_membership_digest_snapshot=(
                group.approval_dispatch_membership_digest
            ),
            approval_card_kind_snapshot=group.approval_dispatch_card_kind,
        )
        members = await self._repo.list_approval_batch_members(batch.id)
        validity_deadlines: list[datetime | None] = []
        # The two-member summary threshold must count only members that are
        # still batch-eligible right now. A replacement created via supersede
        # registers into the same window as the proposal it just invalidated;
        # counting that dead member would announce an "전체 승인" batch whose
        # live membership is a single proposal.
        live_members: list[tuple[OrderProposalApprovalBatchMember, OrderProposal]] = []
        for member in members:
            member_group = await self._repo.get_group_by_pk(member.proposal_pk)
            if member_group is None:
                continue
            validity_deadlines.append(member_group.valid_until)
            member_rungs = await self._repo.list_rungs(member_group.id)
            snapshot_is_current = (
                member_group.approval_nonce == member.approval_nonce_snapshot
                and member_group.approval_dispatch_attempt_id
                == member.approval_dispatch_attempt_id_snapshot
                and member_group.approval_dispatch_membership_revision
                == member.approval_membership_revision_snapshot
                and member_group.approval_dispatch_membership_digest
                == member.approval_membership_digest_snapshot
                and member_group.approval_dispatch_card_kind
                == member.approval_card_kind_snapshot
            )
            if (
                snapshot_is_current
                and batch_member_block_reason(member_group, member_rungs, now=now)
                is None
            ):
                live_members.append((member, member_group))
        await self._repo.update_approval_batch(
            batch,
            expires_at=self._bounded_batch_expiry(
                now=now,
                ttl_seconds=ttl_seconds,
                validity_deadlines=validity_deadlines,
            ),
        )

        member_count = len(live_members)
        summary_action: Literal["none", "send"] = "none"
        binding: DispatchBinding | None = None
        if member_count >= 2:
            revision = batch.membership_revision or 1
            attempt_id = uuid.uuid4()
            digest_members = [
                {
                    "proposal_id": str(member_group.proposal_id),
                    "approval_nonce": member.approval_nonce_snapshot,
                    "approval_message_id": member.approval_message_id,
                    "approval_dispatch_attempt_id": str(
                        member.approval_dispatch_attempt_id_snapshot
                    ),
                    "approval_membership_revision": (
                        member.approval_membership_revision_snapshot
                    ),
                    "approval_membership_digest": (
                        member.approval_membership_digest_snapshot
                    ),
                }
                for member, member_group in live_members
            ]
            digest = build_membership_digest(
                card_kind=ApprovalCardKind.BATCH,
                membership_revision=revision,
                members=digest_members,
            )
            for member, _member_group in live_members:
                await self._repo.update_approval_batch_member(
                    member, membership_revision=revision
                )
            await self._repo.update_approval_batch(
                batch,
                approval_dispatch_state=ApprovalDispatchState.PENDING.value,
                approval_dispatch_attempt_id=attempt_id,
                approval_dispatch_attempted_at=now,
                approval_dispatch_failure_code=None,
                membership_revision=revision,
                membership_digest=digest,
                membership_frozen_at=now,
                summary_dispatch_state="sending",
                summary_dispatch_lease_until=None,
            )
            binding = DispatchBinding(
                attempt_id=attempt_id,
                card_kind=ApprovalCardKind.BATCH,
                membership_revision=revision,
                membership_digest=digest,
            )
            summary_action = "send"
        return BatchRegistration(
            batch=batch,
            member_count=member_count,
            summary_action=summary_action,
            binding=binding,
        )

    @staticmethod
    def _bounded_batch_expiry(
        *,
        now: datetime,
        ttl_seconds: int,
        validity_deadlines: list[datetime | None],
    ) -> datetime:
        expiry = now + timedelta(seconds=ttl_seconds)
        bounded = [deadline for deadline in validity_deadlines if deadline is not None]
        return min([expiry, *bounded]) if bounded else expiry

    async def resolve_approval_batch_id_prefix(
        self, batch_short: str
    ) -> uuid.UUID | None:
        return await self._repo.resolve_approval_batch_id_prefix(batch_short)

    @staticmethod
    def _assert_published_batch_binding(
        batch: OrderProposalApprovalBatch,
        *,
        callback: CallbackEnvelope,
        chat_id: str,
        now: datetime,
    ) -> None:
        if batch.chat_id != chat_id:
            raise OrderProposalError("approval_batch_chat_mismatch")
        if now >= batch.expires_at:
            raise OrderProposalError("approval_batch_expired")
        try:
            state = ApprovalDispatchState(str(batch.approval_dispatch_state))
        except ValueError as exc:
            raise OrderProposalError("approval_dispatch_state_invalid") from exc
        try:
            assert_callback_gate(
                snapshot=CallbackGateSnapshot(
                    subject_short=str(batch.batch_id)[:8],
                    state=state,
                    attempt_id=batch.approval_dispatch_attempt_id,
                    card_kind=ApprovalCardKind.BATCH,
                    membership_revision=batch.membership_revision,
                    membership_digest=batch.membership_digest,
                    nonce=batch.approval_nonce,
                    nonce_used=batch.approval_nonce_used_at is not None,
                ),
                callback=callback,
            )
        except ValueError as exc:
            raise OrderProposalError(str(exc)) from exc

    async def preflight_published_batch_callback(
        self,
        batch_id: uuid.UUID,
        *,
        callback: CallbackEnvelope,
        chat_id: str,
        now: datetime,
    ) -> OrderProposalApprovalBatch:
        """Read-only batch binding gate; consuming gate rechecks under lock."""
        self._require_timezone_aware(now)
        batch = await self._repo.get_approval_batch_by_id(batch_id)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        self._assert_published_batch_binding(
            batch, callback=callback, chat_id=chat_id, now=now
        )
        return batch

    async def consume_approval_batch_nonce(
        self,
        batch_id: uuid.UUID,
        *,
        callback: CallbackEnvelope,
        chat_id: str,
        telegram_user_id: str,
        now: datetime,
    ) -> tuple[OrderProposalApprovalBatch, list[BatchMemberSnapshot]]:
        """Consume exactly the immutable membership snapshot shown on the card."""
        self._require_timezone_aware(now)
        batch = await self._repo.get_approval_batch_by_id(batch_id, for_update=True)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        self._assert_published_batch_binding(
            batch, callback=callback, chat_id=chat_id, now=now
        )

        members = [
            member
            for member in await self._repo.list_approval_batch_members(batch.id)
            if member.membership_revision == callback.membership_revision
        ]
        if len(members) < 2:
            raise OrderProposalError("approval_batch_too_small")

        snapshots: list[BatchMemberSnapshot] = []
        digest_members: list[dict[str, Any]] = []
        for member in members:
            group = await self._repo.get_group_by_pk(member.proposal_pk)
            if group is None:
                raise OrderProposalError("approval_batch_member_snapshot_invalid")
            try:
                card_kind = ApprovalCardKind(str(member.approval_card_kind_snapshot))
            except ValueError as exc:
                raise OrderProposalError(
                    "approval_batch_member_snapshot_invalid"
                ) from exc
            if (
                member.approval_dispatch_attempt_id_snapshot is None
                or member.approval_membership_revision_snapshot is None
                or member.approval_membership_digest_snapshot is None
            ):
                raise OrderProposalError("approval_batch_member_snapshot_invalid")
            digest_members.append(
                {
                    "proposal_id": str(group.proposal_id),
                    "approval_nonce": member.approval_nonce_snapshot,
                    "approval_message_id": member.approval_message_id,
                    "approval_dispatch_attempt_id": str(
                        member.approval_dispatch_attempt_id_snapshot
                    ),
                    "approval_membership_revision": (
                        member.approval_membership_revision_snapshot
                    ),
                    "approval_membership_digest": (
                        member.approval_membership_digest_snapshot
                    ),
                }
            )
            snapshots.append(
                BatchMemberSnapshot(
                    member_id=member.id,
                    proposal_id=group.proposal_id,
                    approval_nonce=member.approval_nonce_snapshot,
                    approval_message_id=member.approval_message_id,
                    dispatch_binding=DispatchBinding(
                        attempt_id=member.approval_dispatch_attempt_id_snapshot,
                        card_kind=card_kind,
                        membership_revision=(
                            member.approval_membership_revision_snapshot
                        ),
                        membership_digest=(member.approval_membership_digest_snapshot),
                    ),
                )
            )
        actual_digest = build_membership_digest(
            card_kind=ApprovalCardKind.BATCH,
            membership_revision=callback.membership_revision,
            members=digest_members,
        )
        if actual_digest != batch.membership_digest:
            raise OrderProposalError("approval_batch_membership_digest_mismatch")

        await self._repo.update_approval_batch(
            batch,
            approval_nonce_used_at=now,
            approved_by_telegram_user_id=telegram_user_id,
            approved_at=now,
        )
        return batch, snapshots

    async def record_approval_batch_payload(
        self,
        batch_id: uuid.UUID,
        *,
        attempt_id: uuid.UUID,
        payload_chars: int,
    ) -> OrderProposalApprovalBatch:
        batch = await self._repo.get_approval_batch_by_id(batch_id, for_update=True)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        if (
            batch.approval_dispatch_attempt_id != attempt_id
            or batch.approval_dispatch_state != ApprovalDispatchState.PENDING.value
        ):
            raise OrderProposalError("approval_batch_dispatch_owner_mismatch")
        return await self._repo.update_approval_batch(
            batch, approval_dispatch_payload_chars=payload_chars
        )

    async def finish_approval_batch_dispatch(
        self,
        batch_id: uuid.UUID,
        *,
        attempt_id: uuid.UUID,
        publication: ApprovalPublication,
        now: datetime,
    ) -> TelegramDispatchResult:
        """Finalize one immutable batch publication through its owner fence."""
        self._require_timezone_aware(now)
        batch = await self._repo.get_approval_batch_by_id(batch_id, for_update=True)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        is_current_owner = (
            batch.approval_dispatch_state == ApprovalDispatchState.PENDING.value
            and batch.approval_dispatch_attempt_id == attempt_id
            and batch.membership_revision is not None
            and batch.membership_digest is not None
            and batch.membership_frozen_at is not None
        )
        if not is_current_owner:
            state = (
                ApprovalDispatchState.SENT_SUPERSEDED
                if publication.card_published
                else ApprovalDispatchState.FAILED_SUPERSEDED
            )
            result = TelegramDispatchResult.from_publication(
                publication,
                state=state,
                failure_code="approval_dispatch_superseded",
            )
            _log_dispatch_outcome(result, surface="batch")
            return result
        if publication.card_published:
            state = ApprovalDispatchState.SENT_CURRENT
            failure_code = None
        elif publication.partial:
            state = ApprovalDispatchState.PARTIAL_FAILED
            failure_code = publication.failure_code or "telegram_dispatch_failed"
        else:
            state = ApprovalDispatchState.FAILED
            failure_code = publication.failure_code or "telegram_dispatch_failed"
        await self._repo.update_approval_batch(
            batch,
            approval_dispatch_state=state.value,
            approval_dispatch_published_at=(
                now if state is ApprovalDispatchState.SENT_CURRENT else None
            ),
            approval_dispatch_failure_code=failure_code,
            approval_dispatch_payload_chars=publication.payload_chars,
            telegram_status_code=publication.status_code,
            telegram_error_code=publication.error_code,
            error_classification=(
                publication.error_classification.value
                if publication.error_classification is not None
                else None
            ),
            summary_message_id=(
                publication.message_id
                if state is ApprovalDispatchState.SENT_CURRENT
                else None
            ),
            summary_dispatch_state=(
                "sent" if state is ApprovalDispatchState.SENT_CURRENT else "idle"
            ),
            summary_dispatch_lease_until=None,
            updated_at=now,
        )
        result = TelegramDispatchResult.from_publication(
            publication, state=state, failure_code=failure_code
        )
        _log_dispatch_outcome(result, surface="batch")
        return result

    async def record_approval_batch_summary(
        self,
        batch_id: uuid.UUID,
        *,
        message_id: int,
        now: datetime,
    ) -> OrderProposalApprovalBatch:
        """Reject the legacy ownerless success transition.

        Batch publication must be finalized with
        ``finish_approval_batch_dispatch`` and its exact attempt ID.
        """
        self._require_timezone_aware(now)
        del batch_id, message_id
        raise OrderProposalError("approval_batch_ownerless_finalize_forbidden")

    async def release_approval_batch_summary_claim(
        self, batch_id: uuid.UUID, *, now: datetime
    ) -> OrderProposalApprovalBatch:
        self._require_timezone_aware(now)
        batch = await self._repo.get_approval_batch_by_id(batch_id, for_update=True)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        return await self._repo.update_approval_batch(
            batch,
            summary_dispatch_state="idle",
            approval_dispatch_state=ApprovalDispatchState.FAILED.value,
            approval_dispatch_failure_code="approval_batch_dispatch_failed",
            summary_dispatch_lease_until=None,
            updated_at=now,
        )

    async def get_approval_batch_display(
        self, batch_id: uuid.UUID
    ) -> tuple[
        OrderProposalApprovalBatch,
        list[tuple[OrderProposal, list[OrderProposalRung]]],
    ]:
        batch = await self._repo.get_approval_batch_by_id(batch_id)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        proposals: list[tuple[OrderProposal, list[OrderProposalRung]]] = []
        for member in await self._repo.list_approval_batch_members(batch.id):
            if (
                batch.membership_frozen_at is not None
                and member.membership_revision != batch.membership_revision
            ):
                continue
            group = await self._repo.get_group_by_pk(member.proposal_pk)
            if group is None:
                continue
            proposals.append((group, await self._repo.list_rungs(group.id)))
        return batch, proposals

    async def record_approval_batch_member_result(
        self,
        member_id: int,
        *,
        result: str,
        detail: dict[str, Any],
        now: datetime,
    ) -> OrderProposalApprovalBatchMember:
        self._require_timezone_aware(now)
        member = await self._repo.get_approval_batch_member_by_id(
            member_id, for_update=True
        )
        if member is None:
            raise OrderProposalError("approval_batch_member_not_found")
        bounded_detail = {
            str(key)[:80]: str(value)[:500] for key, value in detail.items()
        }
        return await self._repo.update_approval_batch_member(
            member,
            result=result[:40],
            result_detail=bounded_detail,
            processed_at=now,
        )

    async def record_auto_approval(
        self,
        proposal_id: uuid.UUID,
        *,
        policy_version: str,
        eligibility: list[dict[str, Any]],
        outcomes: list[str],
        now: datetime,
    ) -> OrderProposal:
        """Persist machine approval provenance without impersonating a human."""
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        source_asof = {
            **(group.source_asof or {}),
            "auto_approved": {
                "policy_version": policy_version,
                "approved_at": now.isoformat(),
                "eligibility": eligibility,
                "outcomes": outcomes,
            },
        }
        return await self._repo.update_group(group, source_asof=source_asof)

    async def record_auto_veto(
        self,
        proposal_id: uuid.UUID,
        *,
        telegram_user_id: str,
        outcomes: list[dict[str, Any]],
        now: datetime,
    ) -> OrderProposal:
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        source_asof = dict(group.source_asof or {})
        auto = dict(source_asof.get("auto_approved") or {})
        auto["veto"] = {
            "telegram_user_id": telegram_user_id,
            "clicked_at": now.isoformat(),
            "outcomes": outcomes,
        }
        source_asof["auto_approved"] = auto
        return await self._repo.update_group(group, source_asof=source_asof)

    async def record_auto_notification_failure(
        self,
        proposal_id: uuid.UUID,
        *,
        error: str,
        outcomes: list[dict[str, Any]],
        now: datetime,
    ) -> OrderProposal:
        """Audit compensating cancellation after veto delivery failed."""
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        source_asof = dict(group.source_asof or {})
        auto = dict(source_asof.get("auto_approved") or {})
        auto["notification_failure"] = {
            "error": error,
            "handled_at": now.isoformat(),
            "outcomes": outcomes,
        }
        source_asof["auto_approved"] = auto
        return await self._repo.update_group(group, source_asof=source_asof)

    async def auto_approved_daily_notional(
        self, group: OrderProposal, *, now: datetime
    ) -> Decimal:
        """Return this account's KST-day cumulative auto-approved notional."""
        self._require_timezone_aware(now)
        local = now.astimezone(KST)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        account_key = group.broker_account_id or "default"
        await self._repo.acquire_auto_approve_lock(
            f"order_proposals:auto_approve:{group.account_mode}:{group.market}:"
            f"{account_key}:{start.date()}"
        )
        return await self._repo.auto_approved_notional_between(
            account_mode=group.account_mode,
            market=group.market,
            broker_account_id=group.broker_account_id,
            start=start,
            end=end,
        )

    async def acquire_auto_dispatch_lock(self, proposal_id: uuid.UUID) -> None:
        """Serialize dispatch attempts for one proposal across processes."""
        await self._repo.acquire_auto_approve_lock(
            f"order_proposals:auto_dispatch:{proposal_id}"
        )

    async def acquire_commit_lease(
        self,
        proposal_id: uuid.UUID,
        *,
        now: datetime,
        lease_seconds: int = 10,
    ) -> bool:
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        lease_until = group.commit_lease_until
        if lease_until is not None:
            self._require_timezone_aware(lease_until)
            if lease_until > now:
                return False
        await self._repo.update_group(
            group, commit_lease_until=now + timedelta(seconds=lease_seconds)
        )
        return True

    async def record_ack(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        broker_order_id: str,
        correlation_id: str,
        idempotency_key: str,
        approval_hash_digest: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="acked",
            broker_order_id=broker_order_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            approval_hash_digest=approval_hash_digest,
            validated_at=now,
            updated_at=now,
        )

    async def record_resting(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        broker_order_id: str,
        correlation_id: str,
        idempotency_key: str,
        approval_hash_digest: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="resting",
            broker_order_id=broker_order_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            approval_hash_digest=approval_hash_digest,
            validated_at=now,
            updated_at=now,
        )

    async def record_unverified(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        reason: str,
        now: datetime,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        evidence = {}
        if correlation_id is not None:
            evidence["correlation_id"] = correlation_id
        if idempotency_key is not None:
            evidence["idempotency_key"] = idempotency_key
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="unverified",
            void_reason=reason,
            validated_at=now,
            updated_at=now,
            **evidence,
        )

    async def record_cancelled(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        broker_order_id: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="cancelled",
            broker_order_id=broker_order_id,
            validated_at=now,
            updated_at=now,
        )

    async def record_fill_evidence(
        self,
        *,
        correlation_id: str | None = None,
        broker_order_id: str | None = None,
        idempotency_key: str | None = None,
        filled_qty: Decimal | None = None,
        terminal_state: Literal[
            "filled", "partially_filled", "cancelled", "expired"
        ] = "filled",
        now: datetime,
        account_mode: str | None = None,
    ) -> OrderProposalRung | None:
        """Converge a rung from broker terminal evidence (ROB-816 PR-3c).

        Called by the live reconcile kernel. Fail-safe by construction:

        - Only rungs in an evidence-accepting (non-terminal) state are matched,
          so re-delivered evidence for an already-terminal rung short-circuits
          to ``None`` instead of raising ``OrderProposalInvalidStateTransition``
          (which reconcile would otherwise mislabel as an anomaly).
        - The matched rung is re-read under a row lock and re-checked for
          terminality, closing the find→transition race with a concurrent pass.
        - ``cancelled`` and ``expired`` carry no fill quantity
          (``filled_qty=None``) so a partial fill booked before terminal broker
          evidence is preserved, not zeroed.
        - No terminal state is ever inferred without matching broker evidence.
        - Upbit client identifiers match the rung ``idempotency_key`` while
          broker UUIDs continue to match ``broker_order_id``.
        """
        self._require_timezone_aware(now)
        match = await self._repo.find_rung_by_evidence(
            correlation_id=correlation_id,
            broker_order_id=broker_order_id,
            idempotency_key=idempotency_key,
            states=_EVIDENCE_ACCEPTING_RUNG_STATES,
            account_mode=account_mode,
        )
        if match is None:
            return None
        proposal_id, rung = match
        group, locked = await self._get_locked_rung(proposal_id, rung.rung_index)
        # ``locked`` may be the same instance ``find_rung_by_evidence`` already
        # loaded into this session's identity map; a plain re-SELECT would return
        # it with its find-time ``state`` intact. Refresh it under the group lock
        # so the terminality re-check below reads the state a concurrent reconcile
        # committed, not a stale snapshot (which could regress a filled rung).
        await self._session.refresh(locked)
        if sm.is_terminal(locked.state):
            # Converged by a concurrent reconcile between find and lock.
            return None
        audit: dict[str, Any] = {"updated_at": now}
        if filled_qty is not None:
            audit["filled_qty"] = filled_qty
        if locked.state == terminal_state:
            # Repeated non-terminal evidence (e.g. a larger partial fill on an
            # already-partially_filled rung): refresh audit fields in place
            # rather than attempt an illegal self-transition.
            if (
                filled_qty is not None
                and locked.filled_qty is not None
                and filled_qty <= Decimal(str(locked.filled_qty))
            ):
                return None
            return await self._repo.update_rung(locked, **audit)
        return await self._transition_locked_rung(
            group, locked, new_state=terminal_state, **audit
        )

    async def find_unambiguous_evidence_rung_id(
        self,
        *,
        correlation_id: str | None,
        broker_order_id: str | None,
        idempotency_key: str | None = None,
        account_mode: str,
        symbol: str,
        market: str,
    ) -> int | None:
        """Resolve one rung only when all nonempty evidence-key sets intersect.

        Terminal rungs intentionally participate in this comparison: their
        presence must block an otherwise matching resting rung rather than be
        hidden by an evidence-accepting state filter.
        """
        if (
            correlation_id is None
            and broker_order_id is None
            and idempotency_key is None
        ):
            return None
        broker_match = (
            OrderProposalRung.broker_order_id == broker_order_id
            if broker_order_id is not None
            else literal(False)
        )
        correlation_match = (
            OrderProposalRung.correlation_id == correlation_id
            if correlation_id is not None
            else literal(False)
        )
        idempotency_match = (
            OrderProposalRung.idempotency_key == idempotency_key
            if idempotency_key is not None
            else literal(False)
        )
        rows = list(
            (
                await self._session.execute(
                    select(
                        OrderProposalRung.id,
                        OrderProposalRung.state,
                        broker_match.label("broker_match"),
                        correlation_match.label("correlation_match"),
                        idempotency_match.label("idempotency_match"),
                    )
                    .join(
                        OrderProposal,
                        OrderProposalRung.proposal_pk == OrderProposal.id,
                    )
                    .where(
                        or_(broker_match, correlation_match, idempotency_match),
                        OrderProposal.account_mode == account_mode,
                        OrderProposal.symbol == symbol,
                        OrderProposal.market == market,
                    )
                )
            ).all()
        )
        broker_ids = {row.id for row in rows if row.broker_match}
        correlation_ids = {row.id for row in rows if row.correlation_match}
        idempotency_ids = {row.id for row in rows if row.idempotency_match}
        evidence_sets = [
            ids for ids in (broker_ids, correlation_ids, idempotency_ids) if ids
        ]
        if not evidence_sets:
            return None
        # R1 resolving-keys intersection: absence is neutral, while every
        # present key constrains ownership.  This preserves broker attribution
        # when a content-hash correlation has legitimate sibling rungs.
        if len(broker_ids) > 1:
            raise OrderProposalError("broker_id_duplicate")
        if not broker_ids and not idempotency_ids and len(correlation_ids) > 1:
            raise OrderProposalError("content_hash_only_ambiguous")
        intersection = set.intersection(*evidence_sets)
        if not intersection:
            raise OrderProposalError("proposal_evidence_conflict")
        if len(intersection) > 1:
            raise OrderProposalError("proposal_evidence_ambiguous")
        return next(iter(intersection))

    async def record_fill_evidence_for_rung(
        self,
        *,
        rung_id: int,
        correlation_id: str | None,
        broker_order_id: str | None,
        idempotency_key: str | None = None,
        filled_qty: Decimal | None,
        terminal_state: Literal["filled", "partially_filled", "cancelled", "expired"],
        now: datetime,
        account_mode: str,
        symbol: str,
        market: str,
    ) -> OrderProposalRung | None:
        """Lock and transition the already validated exact rung, never re-pick."""
        target = (
            await self._session.execute(
                select(OrderProposal.proposal_id, OrderProposalRung.rung_index)
                .join(
                    OrderProposalRung, OrderProposalRung.proposal_pk == OrderProposal.id
                )
                .where(OrderProposalRung.id == rung_id)
            )
        ).one_or_none()
        if target is None:
            return None
        group, locked = await self._get_locked_rung(
            target.proposal_id, target.rung_index
        )
        verified_id = await self.find_unambiguous_evidence_rung_id(
            correlation_id=correlation_id,
            broker_order_id=broker_order_id,
            idempotency_key=idempotency_key,
            account_mode=account_mode,
            symbol=symbol,
            market=market,
        )
        if verified_id != rung_id:
            return None
        if sm.is_terminal(locked.state):
            if (
                locked.state == terminal_state
                and locked.broker_order_id == broker_order_id
            ):
                return locked
            raise OrderProposalError("proposal_terminal_evidence_conflict")
        audit: dict[str, Any] = {"updated_at": now}
        if filled_qty is not None:
            audit["filled_qty"] = filled_qty
        return await self._transition_locked_rung(
            group, locked, new_state=terminal_state, **audit
        )

    async def mark_needs_reconfirm(
        self, proposal_id: uuid.UUID, rung_index: int, *, now: datetime
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        group, rung = await self._get_locked_rung(proposal_id, rung_index)
        return await self._transition_locked_rung(
            group,
            rung,
            new_state="needs_reconfirm",
            approval_revision=(rung.approval_revision or 0) + 1,
            validated_at=now,
            updated_at=now,
        )

    async def record_rejected(
        self,
        proposal_id: uuid.UUID,
        rung_index: int,
        *,
        reason: str,
        now: datetime,
    ) -> OrderProposalRung:
        self._require_timezone_aware(now)
        return await self.transition_rung(
            proposal_id,
            rung_index,
            new_state="rejected",
            void_reason=reason,
            updated_at=now,
        )

    async def sweep_local_stale(
        self,
        *,
        now: datetime,
        broker_evidence: Callable[[OrderProposalRung], str | Awaitable[str]],
    ) -> list[uuid.UUID]:
        self._require_timezone_aware(now)
        candidates = await self._repo.list_local_stale_candidates()
        swept: list[uuid.UUID] = []
        swept_set: set[uuid.UUID] = set()
        for proposal_id, candidate in candidates:
            evidence = broker_evidence(candidate)
            if inspect.isawaitable(evidence):
                evidence = await evidence
            if evidence != "no_broker_order":
                continue

            group, rung = await self._get_locked_rung(proposal_id, candidate.rung_index)
            if rung.state != "pending_approval" or rung.broker_order_id is not None:
                continue
            await self._transition_locked_rung(
                group,
                rung,
                new_state="voided_local_stale",
                void_reason="no_broker_order",
                updated_at=now,
            )
            if proposal_id not in swept_set:
                swept.append(proposal_id)
                swept_set.add(proposal_id)
        return swept

    async def list_expiry_candidates(
        self, *, now: datetime
    ) -> list[tuple[OrderProposal, list[OrderProposalRung]]]:
        """Read-only preview of groups ``sweep_expired`` would act on (ROB-897).

        Used by the ``dry_run=True`` MCP tool path and by the sweep's own
        skipped-count accounting -- never mutates.
        """
        self._require_timezone_aware(now)
        candidate_ids = await self._repo.list_expiry_candidates(now=now)
        results: list[tuple[OrderProposal, list[OrderProposalRung]]] = []
        for proposal_id in candidate_ids:
            group = await self._repo.get_group_by_proposal_id(proposal_id)
            if group is None:
                continue
            results.append((group, await self._repo.list_rungs(group.id)))
        return results

    async def sweep_expired(self, *, now: datetime) -> list[ExpirySweepResult]:
        """Batch-expire every non-terminal group whose ``valid_until`` has passed.

        ROB-897 cause (1) structural fix: ``expire_if_needed`` only ran from the
        Telegram approval callback, so a proposal nobody tapped stayed
        ``proposed``/``needs_reconfirm`` indefinitely once its deadline passed.
        This runs the same per-rung transition ``expire_if_needed`` does, but
        for every due group and with different failure semantics: a group with
        any rung outside ``_VOIDABLE_RUNG_STATES`` (e.g. ``submitting``,
        ``resting``, ``filled``) is *skipped*, not raised on -- a
        partially-submitted or filled proposal past its window must not be
        force-expired, and one bad group must not abort the whole sweep.
        """
        self._require_timezone_aware(now)
        candidate_ids = await self._repo.list_expiry_candidates(now=now)
        results: list[ExpirySweepResult] = []
        for proposal_id in candidate_ids:
            group = await self._repo.get_group_by_proposal_id(
                proposal_id, for_update=True
            )
            if group is None:
                continue
            # Re-check under the row lock: a concurrent mutation (approval,
            # void, resubmit) may have moved valid_until or lifecycle_state
            # since the candidate scan above.
            if group.valid_until is None or group.valid_until > now:
                continue
            if group.lifecycle_state in _APPROVAL_TERMINAL_GROUP_STATES:
                continue

            rungs = await self._repo.list_rungs(group.id)
            non_voidable_rung = next(
                (rung for rung in rungs if rung.state not in _VOIDABLE_RUNG_STATES),
                None,
            )
            if non_voidable_rung is not None:
                logger.info(
                    "sweep_expired: skipping proposal_id=%s (rung %s in "
                    "non-voidable state %r past valid_until; not force-expired)",
                    proposal_id,
                    non_voidable_rung.rung_index,
                    non_voidable_rung.state,
                )
                continue

            expired_rungs = []
            for rung in rungs:
                sm.assert_rung_transition(rung.state, "expired")
                expired_rungs.append(
                    await self._repo.update_rung(rung, state="expired", updated_at=now)
                )
            await self._repo.update_group(
                group,
                lifecycle_state=self._recompute_group_state(expired_rungs),
                approval_nonce=None,
            )
            source_asof = group.source_asof or {}
            results.append(
                ExpirySweepResult(
                    proposal_id=proposal_id,
                    symbol=group.symbol,
                    chat_id=source_asof.get("approval_chat_id"),
                    message_id=source_asof.get("approval_message_id"),
                )
            )
        return results

    async def list_expired_defensive_handoff(
        self, *, now: datetime, hours: int = 24, market: str | None = None
    ) -> list[ExpiredDefensiveProposal]:
        """Read-only handoff list of recently expired/voided defensive proposals.

        ROB-929: 07-15 US 방어 제안 6건이 미응답 만료되고 다음 세션이 같은 판단을
        처음부터 재구축했다 -- this surfaces exactly the loss_cut/defensive_trim
        proposals that died without a decision so a session prompt can force a
        current-price re-judgment instead of silently forgetting them. Noise
        suppression: a group already superseded, or sharing a symbol+side with
        a still-active (non-terminal) proposal, is dropped -- both mean the
        decision has already moved on and re-surfacing it would just be noise.
        """
        self._require_timezone_aware(now)
        since = now - timedelta(hours=hours)
        candidates = await self._repo.list_expired_defensive_candidates(
            since=since, market=market
        )
        eligible = [
            group for group in candidates if group.superseded_by_proposal_id is None
        ]
        active_pairs = await self._repo.list_active_symbol_sides(
            [(group.symbol, group.side) for group in eligible]
        )

        results: list[ExpiredDefensiveProposal] = []
        for group in eligible:
            if (group.symbol, group.side) in active_pairs:
                continue
            rungs = await self._repo.list_rungs(group.id)
            limit_price = next(
                (rung.limit_price for rung in rungs if rung.limit_price is not None),
                None,
            )
            results.append(
                ExpiredDefensiveProposal(
                    proposal_id=group.proposal_id,
                    symbol=group.symbol,
                    side=group.side,
                    market=group.market,
                    exit_intent=str(group.exit_intent),
                    lifecycle_state=group.lifecycle_state,
                    limit_price=(
                        Decimal(str(limit_price)) if limit_price is not None else None
                    ),
                    valid_until=group.valid_until,
                    expired_or_voided_at=group.updated_at,
                )
            )
        return results

    @staticmethod
    def _require_timezone_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
