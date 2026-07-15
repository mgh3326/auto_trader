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

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.order_proposals import (
    OrderProposal,
    OrderProposalApprovalBatch,
    OrderProposalApprovalBatchMember,
    OrderProposalRung,
)
from app.models.review import TossLiveOrderLedger
from app.services.order_proposals import state_machine as sm
from app.services.order_proposals.broker_gateway import SUPPORTED_TARGET_ACTIONS
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalNotFound,
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


@dataclass(frozen=True)
class BatchRegistration:
    batch: OrderProposalApprovalBatch
    member_count: int
    summary_action: Literal["none", "send", "edit"]


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

_ALLOWED_ACTION_CONTRACT_MESSAGE = (
    "allowed: kis_live×equity_kr|equity_us, "
    "toss_live×equity_kr|equity_us, upbit×crypto; "
    "market aliases kr→equity_kr, us→equity_us"
)

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
    if group.valid_until is not None and now >= group.valid_until:
        return "proposal_expired"
    if not group.approval_nonce:
        return "approval_nonce_missing"
    if group.approval_nonce_used_at is not None:
        return "approval_nonce_used"
    if not any(rung.state == "pending_approval" for rung in rungs):
        return "no_pending_approval_rungs"
    return None


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
    normalized = action or "place"
    if normalized not in _ACTION_CAPABILITIES:
        raise OrderProposalError("action must be one of: place, replace, cancel")
    if (account_mode, market) not in _ACTION_CAPABILITIES[normalized]:
        raise OrderProposalError(
            "unsupported account_mode/market/action: "
            f"{account_mode}/{market}/{normalized} "
            f"({_ALLOWED_ACTION_CONTRACT_MESSAGE})"
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
        if valid_until is None:
            valid_until = (now.astimezone(KST) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            self._require_timezone_aware(valid_until)
            if valid_until <= now:
                raise OrderProposalError("valid_until must be in the future")
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
            raise OrderProposalError("unknown exit_intent (only 'loss_cut')")

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
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        block_reason = proposal_approval_block_reason(group)
        if block_reason is not None:
            raise OrderProposalError(block_reason)
        await self._repo.update_group(
            group, approval_nonce=nonce, approval_nonce_used_at=None
        )

    async def consume_approval_nonce(
        self, proposal_id: uuid.UUID, nonce: str, *, now: datetime
    ) -> OrderProposal:
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        block_reason = proposal_approval_block_reason(group)
        if block_reason is not None:
            raise OrderProposalError(block_reason)
        if isinstance((group.source_asof or {}).get("auto_approved"), dict):
            raise OrderProposalError("auto_veto_nonce_requires_vc")
        if group.approval_nonce != nonce:
            raise OrderProposalError("nonce_mismatch")
        if group.approval_nonce_used_at is not None:
            raise OrderProposalError("nonce_replay")
        return await self._repo.update_group(group, approval_nonce_used_at=now)

    async def consume_auto_veto_nonce(
        self, proposal_id: uuid.UUID, nonce: str, *, now: datetime
    ) -> OrderProposal:
        """Consume an auto-submit veto nonce, including after a broker fill."""
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        if group.superseded_by_proposal_id is not None:
            raise OrderProposalError(
                f"proposal_superseded_by:{group.superseded_by_proposal_id}"
            )
        if group.lifecycle_state == "superseded":
            raise OrderProposalError("proposal_superseded_by:unknown")
        if not isinstance((group.source_asof or {}).get("auto_approved"), dict):
            raise OrderProposalError("auto_veto_not_available")
        if group.approval_nonce != nonce:
            raise OrderProposalError("nonce_mismatch")
        if group.approval_nonce_used_at is not None:
            raise OrderProposalError("nonce_replay")
        return await self._repo.update_group(group, approval_nonce_used_at=now)

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
        """Atomically validate, audit, and consume a loss-cut second-step nonce."""
        self._require_timezone_aware(now)
        group = await self._repo.get_group_by_proposal_id(proposal_id, for_update=True)
        if group is None:
            raise OrderProposalNotFound(str(proposal_id))
        block_reason = proposal_approval_block_reason(group)
        if block_reason is not None:
            raise OrderProposalError(block_reason)
        if group.approval_nonce != nonce:
            raise OrderProposalError("nonce_mismatch")
        if group.approval_nonce_used_at is not None:
            raise OrderProposalError("nonce_replay")
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
            envelope.get("proposal_id") != str(proposal_id)
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
        return await self._repo.update_group(
            group,
            source_asof=source_asof,
            approval_nonce_used_at=now,
        )

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
        """Record where the initial Telegram approval message was sent.

        No new column exists for this (see ``dispatch.py``'s module docstring)
        -- ``message_id``/``chat_id``/``sent_at`` are merged into the existing
        ``source_asof`` JSONB column so a later Telegram ``edit_message`` call
        can find them. This merges on top of whatever keys are already there
        (e.g. ``resting_deadline``, read by
        ``approval_message.py::_build_time_lines``) rather than overwriting
        the column outright.
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
        return await self._repo.update_group(group, source_asof=merged)

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
        """Register one still-manual proposal in the chat's open batch."""
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
                approval_nonce=secrets.token_urlsafe(12),
                summary_dispatch_state="idle",
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
        )
        members = await self._repo.list_approval_batch_members(batch.id)
        validity_deadlines: list[datetime | None] = []
        # The two-member summary threshold must count only members that are
        # still batch-eligible right now. A replacement created via supersede
        # registers into the same window as the proposal it just invalidated;
        # counting that dead member would announce an "전체 승인" batch whose
        # live membership is a single proposal.
        live_member_count = 0
        for member in members:
            member_group = await self._repo.get_group_by_pk(member.proposal_pk)
            if member_group is None:
                continue
            validity_deadlines.append(member_group.valid_until)
            member_rungs = await self._repo.list_rungs(member_group.id)
            if batch_member_block_reason(member_group, member_rungs, now=now) is None:
                live_member_count += 1
        await self._repo.update_approval_batch(
            batch,
            expires_at=self._bounded_batch_expiry(
                now=now,
                ttl_seconds=ttl_seconds,
                validity_deadlines=validity_deadlines,
            ),
        )

        member_count = live_member_count
        summary_action: Literal["none", "send", "edit"] = "none"
        if member_count >= 2:
            if batch.summary_message_id is not None:
                summary_action = "edit"
            elif batch.summary_dispatch_state == "idle" or (
                batch.summary_dispatch_state == "sending"
                and batch.summary_dispatch_lease_until is not None
                and batch.summary_dispatch_lease_until <= now
            ):
                await self._repo.update_approval_batch(
                    batch,
                    summary_dispatch_state="sending",
                    summary_dispatch_lease_until=now + timedelta(seconds=30),
                )
                summary_action = "send"
        return BatchRegistration(
            batch=batch,
            member_count=member_count,
            summary_action=summary_action,
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

    async def consume_approval_batch_nonce(
        self,
        batch_id: uuid.UUID,
        nonce: str,
        *,
        chat_id: str,
        telegram_user_id: str,
        now: datetime,
    ) -> tuple[OrderProposalApprovalBatch, list[BatchMemberSnapshot]]:
        """Atomically consume a batch trigger and freeze its ordered members."""
        self._require_timezone_aware(now)
        batch = await self._repo.get_approval_batch_by_id(batch_id, for_update=True)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        if batch.chat_id != chat_id:
            raise OrderProposalError("approval_batch_chat_mismatch")
        if batch.approval_nonce != nonce:
            raise OrderProposalError("approval_batch_nonce_mismatch")
        if batch.approval_nonce_used_at is not None:
            raise OrderProposalError("approval_batch_nonce_replay")
        if now >= batch.expires_at:
            raise OrderProposalError("approval_batch_expired")
        members = await self._repo.list_approval_batch_members(batch.id)
        if len(members) < 2:
            raise OrderProposalError("approval_batch_too_small")

        await self._repo.update_approval_batch(
            batch,
            approval_nonce_used_at=now,
            approved_by_telegram_user_id=telegram_user_id,
            approved_at=now,
        )
        snapshots: list[BatchMemberSnapshot] = []
        for member in members:
            group = await self._repo.get_group_by_pk(member.proposal_pk)
            if group is None:
                continue
            snapshots.append(
                BatchMemberSnapshot(
                    member_id=member.id,
                    proposal_id=group.proposal_id,
                    approval_nonce=member.approval_nonce_snapshot,
                    approval_message_id=member.approval_message_id,
                )
            )
        return batch, snapshots

    async def record_approval_batch_summary(
        self,
        batch_id: uuid.UUID,
        *,
        message_id: int,
        now: datetime,
    ) -> OrderProposalApprovalBatch:
        self._require_timezone_aware(now)
        batch = await self._repo.get_approval_batch_by_id(batch_id, for_update=True)
        if batch is None:
            raise OrderProposalError("approval_batch_not_found")
        return await self._repo.update_approval_batch(
            batch,
            summary_message_id=message_id,
            summary_dispatch_state="sent",
            summary_dispatch_lease_until=None,
            updated_at=now,
        )

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
        terminal_state: Literal["filled", "partially_filled", "cancelled"] = "filled",
        now: datetime,
        account_mode: str | None = None,
    ) -> OrderProposalRung | None:
        """Converge a rung from broker fill/cancel evidence (ROB-816 PR-3c).

        Called by the live reconcile kernel. Fail-safe by construction:

        - Only rungs in an evidence-accepting (non-terminal) state are matched,
          so re-delivered evidence for an already-terminal rung short-circuits
          to ``None`` instead of raising ``OrderProposalInvalidStateTransition``
          (which reconcile would otherwise mislabel as an anomaly).
        - The matched rung is re-read under a row lock and re-checked for
          terminality, closing the find→transition race with a concurrent pass.
        - ``cancelled`` carries no fill quantity (``filled_qty=None``) so a
          partial fill booked before a cancel is preserved, not zeroed.
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

    @staticmethod
    def _require_timezone_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
