"""Read and check a mandate plan under its basket and sleeve locks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.mandate import (
    MandateApproval,
    MandateApprovalRequest,
    MandatePlan,
    MandatePlanTranche,
    MandateTerms,
)
from app.models.strategy_sleeves import StrategySleeve
from app.services.mandate import store
from app.services.mandate import vocabulary as v
from app.services.mandate.fold import PlanState
from app.services.mandate.hashing import content_sha256
from app.services.mandate.invariants import (
    account_binding_ok,
    min_gap_met,
    sector_exposure,
    tranche_order_ok,
    tranche_trigger_met,
)
from app.services.mandate.locks import acquire_in_order


@dataclass(frozen=True)
class PlanProposalContext:
    plan: MandatePlan
    sleeve: StrategySleeve
    state: PlanState
    terms: MandateTerms
    tranche: MandatePlanTranche

    def check_authority(self, now: datetime) -> None:
        effective = self.state.effective_session
        if (
            self.state.authority not in {"approved", "active"}
            or effective is None
            or now.astimezone(KST).date() < effective
        ):
            raise store.MandateError(
                v.MandateRefusal.PLAN_NOT_ACTIVE, "plan is not effective"
            )

    def check_binding(self, *, symbol: str, market: str, account_mode: str) -> None:
        if (symbol, market, account_mode) != (
            self.plan.symbol,
            self.sleeve.market,
            self.sleeve.account_mode,
        ):
            raise store.MandateError(
                v.MandateRefusal.BINDING_MISMATCH, "proposal identity differs from plan"
            )
        if not account_binding_ok(
            terms_account_ref=self.terms.account_ref,
            plan_account_ref=self.plan.account_ref,
            scope_account_refs=self.plan.scope_account_refs,
            realm=v.realm_of(account_mode),
        ):
            raise store.MandateError(
                v.MandateRefusal.ACCOUNT_MISMATCH, "plan account binding differs"
            )


def proposal_account_allowed(account_mode: str, configured: str) -> bool:
    return account_mode == "kiwoom_mock" and account_mode in {
        mode.strip() for mode in configured.split(",")
    }


async def lock_plan_proposal(
    session: AsyncSession, plan_id: int, tranche_index: int
) -> PlanProposalContext:
    if type(plan_id) is not int or not 0 < plan_id < 2**63:
        raise store.MandateError(v.MandateRefusal.INVALID_FIELDS, "invalid plan id")
    if type(tranche_index) is not int or not 1 <= tranche_index <= v.MAX_TRANCHES:
        raise store.MandateError(
            v.MandateRefusal.INVALID_FIELDS, "invalid tranche index"
        )
    plan = await store.get_plan(session, plan_id)
    if plan is None:
        raise store.MandateError(v.MandateRefusal.BINDING_MISMATCH, "plan is missing")
    sleeve = await session.get(StrategySleeve, plan.sleeve_id)
    original_terms = await session.get(MandateTerms, plan.approved_terms_id)
    if sleeve is None or original_terms is None:
        raise store.MandateError(
            v.MandateRefusal.BINDING_BROKEN, "plan identity is missing"
        )
    realm, _market = store.realm_and_market(sleeve)
    await acquire_in_order(
        session, realm=realm, basket_key=original_terms.basket_key, sleeve_id=sleeve.id
    )
    state = await store.plan_state(session, plan_id)
    terms = (
        await session.get(MandateTerms, state.current_terms_id)
        if state.current_terms_id is not None
        else original_terms
    )
    tranche = await session.get(MandatePlanTranche, (plan_id, tranche_index))
    if (
        terms is None
        or terms.basket_key != original_terms.basket_key
        or tranche is None
    ):
        raise store.MandateError(
            v.MandateRefusal.BINDING_MISMATCH, "plan tranche or basket differs"
        )
    return PlanProposalContext(plan, sleeve, state, terms, tranche)


@dataclass(frozen=True)
class FreezeVerdict:
    frozen: bool
    reason: str
    evidence: dict[str, Any]
    observed_version: int | None = None
    valid_until: datetime | None = None


async def buy_freeze_state(
    session: AsyncSession,
    *,
    account_mode: str,
    market: str,
    sleeve_id: int,
    now: datetime,
) -> FreezeVerdict:
    """Fail closed until the shared T4c/hk 4900 freeze reader is available."""
    return FreezeVerdict(frozen=True, reason="unavailable", evidence={})


async def _is_frozen(
    session: AsyncSession, context: PlanProposalContext, now: datetime
) -> bool:
    verdict = await buy_freeze_state(
        session,
        account_mode=context.sleeve.account_mode,
        market=context.sleeve.market,
        sleeve_id=context.sleeve.id,
        now=now,
    )
    return verdict.frozen or verdict.valid_until is None or verdict.valid_until <= now


async def check_proposal_creation(
    session: AsyncSession,
    context: PlanProposalContext,
    *,
    notional: Decimal,
    now: datetime,
) -> None:
    context.check_authority(now)
    index = context.tranche.tranche_index
    state = context.state
    tranche = state.tranche(index)
    if tranche.unclosed_fences:
        raise store.MandateError(
            v.MandateRefusal.TRANCHE_UNCERTAIN, "tranche has an unclosed fence"
        )
    if tranche.open_proposals or tranche.completed:
        raise store.MandateError(
            v.MandateRefusal.TRANCHE_DUPLICATE,
            "tranche already has a proposal or is complete",
        )
    if not tranche_order_ok(state, index) or (index > 1 and not tranche.armed):
        raise store.MandateError(
            v.MandateRefusal.INVALID_TRANSITION, "tranche has not armed in order"
        )
    if notional > context.tranche.amount - tranche.filled_amount:
        raise ValueError("mandate_tranche_amount_exceeded")
    filled = sum((item.filled_amount for item in state.tranches), Decimal(0))
    if notional > context.plan.planned_amount - context.plan.adopted_amount - filled:
        raise ValueError("mandate_plan_amount_exceeded")
    if await _is_frozen(session, context, now):
        raise ValueError("mandate_drawdown_frozen")


async def check_dispatch(
    session: AsyncSession,
    context: PlanProposalContext,
    *,
    proposal_id: UUID,
    broker_account_id: str | None,
    now: datetime,
    market_evidence: TrancheMarketEvidence | None = None,
) -> tuple[str, dict[str, str]]:
    """Evaluate durable evidence only. This verdict carries no send authority."""
    context.check_authority(now)
    plan = context.plan
    terms = context.terms
    index = context.tranche.tranche_index
    tranche = context.state.tranche(index)
    if broker_account_id != plan.account_ref:
        return "mandate_account_mismatch", {}
    if tranche.unclosed_fences:
        return "mandate_tranche_uncertain", {}
    if tranche.open_proposals != (proposal_id,):
        return "mandate_tranche_duplicate", {}
    if not tranche_order_ok(context.state, index):
        return "mandate_tranche_not_due", {"tranche_index": str(index)}
    version, current_terms = await store.current_terms(session, plan.sleeve_id)
    if version is None or version.state != "live" or current_terms is None:
        return "mandate_terms_missing", {}
    if current_terms.id != terms.id or terms.expires_at <= now:
        return "mandate_expired", {}
    if (
        context.state.current_terms_sha256 != terms.content_sha256
        or not await _approval_matches(
            session,
            terms.approval_id,
            terms.content_sha256,
            store.terms_content_from_rows(terms, version),
            plan.sleeve_id,
            "terms",
        )
    ):
        return "mandate_binding_broken", {}
    profile = await store.latest_profile(session, plan.sleeve_id)
    if (
        profile is None
        or profile.terms_id != terms.id
        or profile.expires_at <= now
        or profile.effective_session > now.astimezone(KST).date()
        or profile.max_tranche_index == 0
    ):
        return "mandate_activation_missing", {}
    if not await _approval_matches(
        session,
        profile.approval_id,
        profile.content_sha256,
        store.profile_content_from_row(profile),
        plan.sleeve_id,
        "activation_profile",
    ):
        return "mandate_binding_broken", {}
    if index > profile.max_tranche_index:
        return "mandate_tranche_not_activated", {
            "tranche_index": str(index),
            "max_tranche_index": str(profile.max_tranche_index),
        }
    realm = v.realm_of(context.sleeve.account_mode)
    basket = await store.basket_view(session, realm, terms.basket_key)
    amounts = await store.unreleased_plan_amounts(
        session, realm=realm, basket_key=terms.basket_key
    )
    sector, denominator = sector_exposure(
        amounts, basket.constituent_budget_caps, plan.sector_key
    )
    if sector * 100 > denominator * basket.sector_cap_pct:
        return "mandate_sector_cap", {
            "mandate_sector_amount": format(sector, "f"),
            "mandate_sector_denominator": format(denominator, "f"),
            "mandate_sector_cap_pct": format(basket.sector_cap_pct, "f"),
        }
    if await _is_frozen(session, context, now):
        return "mandate_drawdown_frozen", {}
    if market_evidence is None:
        # T4d supplies fresh market evidence. T4a keeps dispatch free of
        # preview/transport calls for bound proposals.
        return "mandate_ask_unavailable", {}
    refusal = check_tranche_market_evidence(context, market_evidence, now=now)
    if refusal is not None:
        return refusal
    return "mandate_send_boundary_pending", {}


@dataclass(frozen=True)
class TrancheMarketEvidence:
    current_price: Decimal
    limit_price: Decimal
    quantity: Decimal
    trading_sessions: tuple[date, ...]
    best_ask: Decimal | None = None
    ask_observed_at: datetime | None = None
    tick_size: Decimal | None = None


@dataclass(frozen=True)
class ProposalQuote:
    current_price: Decimal
    best_ask: Decimal
    ask_observed_at: datetime
    tick_size: Decimal


async def load_proposal_quote(context: PlanProposalContext) -> ProposalQuote | None:
    """T4d supplies the authoritative venue quote at this boundary."""
    return None


def check_near_ask(
    *,
    limit_price: Decimal,
    best_ask: Decimal | None,
    observed_at: datetime | None,
    tick_size: Decimal | None,
    premium_bps: Decimal,
    now: datetime,
) -> tuple[str, dict[str, str]] | None:
    if (
        best_ask is None
        or not best_ask.is_finite()
        or best_ask <= 0
        or tick_size is None
        or not tick_size.is_finite()
        or tick_size <= 0
        or observed_at is None
        or observed_at.utcoffset() is None
        or not timedelta(0) <= now - observed_at <= timedelta(seconds=5)
    ):
        return "mandate_ask_unavailable", {}
    ceiling = best_ask * (1 + premium_bps / 10_000)
    threshold = (ceiling // tick_size) * tick_size
    if limit_price > threshold:
        return "mandate_limit_above_ask_band", {
            "limit_price": format(limit_price, "f"),
            "threshold": format(threshold, "f"),
        }
    return None


def check_tranche_market_evidence(
    context: PlanProposalContext, evidence: TrancheMarketEvidence, *, now: datetime
) -> tuple[str, dict[str, str]] | None:
    if not all(
        value.is_finite() and value > 0
        for value in (evidence.current_price, evidence.limit_price, evidence.quantity)
    ):
        return "price_or_quantity_missing", {}
    state = context.state
    index = context.tranche.tranche_index
    if index > 1:
        if not tranche_trigger_met(
            evidence.current_price,
            state.first_fill_amount or Decimal(0),
            state.first_fill_quantity or Decimal(0),
            context.tranche.trigger_pct_from_first,
        ):
            return "mandate_tranche_not_due", {
                "current_price": format(evidence.current_price, "f"),
                "trigger_pct_from_first": format(
                    context.tranche.trigger_pct_from_first, "f"
                ),
            }
        if not min_gap_met(
            state.tranche(index - 1).settled_on,
            now.astimezone(KST).date(),
            evidence.trading_sessions,
            context.tranche.min_gap_sessions,
        ):
            return "mandate_tranche_not_due", {
                "min_gap_sessions": str(context.tranche.min_gap_sessions),
            }
    notional = max(evidence.current_price, evidence.limit_price) * evidence.quantity
    remaining = context.tranche.amount - state.tranche(index).filled_amount
    plan_remaining = (
        context.plan.planned_amount
        - context.plan.adopted_amount
        - sum((item.filled_amount for item in state.tranches), Decimal(0))
    )
    if notional > plan_remaining:
        return "mandate_plan_amount_exceeded", {
            "notional": format(notional, "f"),
            "mandate_remaining": format(plan_remaining, "f"),
        }
    if notional > remaining:
        return "mandate_tranche_amount", {
            "notional": format(notional, "f"),
            "mandate_remaining": format(remaining, "f"),
        }
    band_refusal = check_near_ask(
        limit_price=evidence.limit_price,
        best_ask=evidence.best_ask,
        observed_at=evidence.ask_observed_at,
        tick_size=evidence.tick_size,
        premium_bps=context.terms.entry_max_premium_bps,
        now=now,
    )
    if band_refusal is not None:
        return band_refusal
    if notional > context.terms.per_order_cap:
        return "mandate_per_order_cap", {
            "notional": format(notional, "f"),
            "per_order_cap": format(context.terms.per_order_cap, "f"),
        }
    return None


async def _approval_matches(
    session: AsyncSession,
    approval_id: int,
    digest: str,
    content: dict[str, Any],
    sleeve_id: int,
    subject_kind: str,
) -> bool:
    approval = await session.get(MandateApproval, approval_id)
    if approval is None or approval.decision != "approved":
        return False
    request = await session.get(MandateApprovalRequest, approval.request_id)
    return (
        request is not None
        and request.sleeve_id == sleeve_id
        and request.subject_kind == subject_kind
        and request.content_sha256 == approval.subject_sha256 == digest
        and content_sha256(request.content) == content_sha256(content) == digest
    )
