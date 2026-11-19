"""The only writer of the mandate tables (hk 3795 ticket T1).

Nine append-only ``review`` tables (see ``app.models.mandate``). This module
writes them inside the caller's transaction; it never commits. Every decision
the design puts in the store is made here:

* approval requests and the authenticated approval that turns one into a live
  sleeve version + clause, or into an activation profile (§3.5, I21);
* plan registration: I1 (symbol reservation), I2, I3, I4, I15 (registration
  side), I17 and the open ``loss_cut`` refusal of §5.2;
* plan events, validated by the same ``apply_event`` the DB trigger mirrors.

Lock order (``app.services.mandate.locks``): L1 symbol, L2 basket, L3 sleeve.

Nothing here sends an order, reads a broker or opens an approval gate. Account
references, holdings evidence and the one-share price arrive as caller-validated
values: the broker reads belong to the operator tools (T2) and the projection
(T4b).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mandate import (
    MandateActivationProfile,
    MandateApproval,
    MandateApprovalRequest,
    MandatePlan,
    MandatePlanEvent,
    MandatePlanTranche,
    MandateSymbolReservation,
    MandateSymbolReservationRelease,
    MandateTerms,
)
from app.models.order_proposals import OrderProposal
from app.models.strategy_sleeves import StrategySleeve, StrategySleeveVersion
from app.services.mandate import hashing
from app.services.mandate import vocabulary as v
from app.services.mandate.fold import (
    InvalidPlanEvent,
    PlanEvent,
    PlanState,
    apply_event,
    fold_plan_events,
)
from app.services.mandate.invariants import (
    InvariantViolation,
    PlanAmounts,
    account_binding_ok,
    check_one_share,
    is_parking_symbol,
    parse_weights,
    sector_cap_check,
    sleeve_budget_check,
    symbol_allowed,
    tranche_amounts,
)
from app.services.mandate.locks import (
    acquire_basket_lock,
    acquire_sleeve_lock,
    acquire_symbol_lock,
)
from app.services.order_proposals.state_machine import GROUP_STATES
from app.services.strategy_sleeves import store as sleeve_store
from app.services.strategy_sleeves.store import PreRegistration, SleeveError
from app.services.strategy_sleeves.vocabulary import SYSTEM_LOSS_GATE_ACTOR, SleeveState

ZERO = Decimal("0")
_PRINCIPAL_MAX = 2**52 - 1
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
#: ``order_proposals.lifecycle_state`` values after which a proposal is closed.
_CLOSED_GROUP_STATES = ("terminal", "rejected", "expired", "voided", "superseded")
assert set(_CLOSED_GROUP_STATES) <= GROUP_STATES
_LIVE_ACCOUNT_MODES = tuple(
    m for m, r in v.REALM_BY_ACCOUNT_MODE.items() if r == "live"
)
_ADOPTED_EVIDENCE_KEYS = frozenset({"checked_at", "holdings", "price", "price_source"})


class MandateError(ValueError):
    """A command the store refuses; nothing was written. ``code`` is closed."""

    def __init__(self, code: v.MandateRefusal, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise MandateError(v.MandateRefusal.INVALID_FIELDS, "now needs a time zone")
    return value


def _text(name: str, value: Any, limit: int) -> str:
    out = str(value or "").strip()
    if not out or len(out) > limit:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            f"{name} is required (1-{limit} characters)",
        )
    return out


def _operator(value: Any) -> str:
    who = _text("actor", value, 128)
    if who == SYSTEM_LOSS_GATE_ACTOR:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"{SYSTEM_LOSS_GATE_ACTOR} is reserved"
        )
    return who


def _dec(
    name: str,
    value: Any,
    *,
    places: int = 4,
    positive: bool = False,
    upper: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MandateError(v.MandateRefusal.INVALID_FIELDS, f"{name} is required")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"{name} must be a number"
        ) from exc
    if not number.is_finite() or number >= Decimal("1e16"):
        raise MandateError(v.MandateRefusal.INVALID_FIELDS, f"{name} is out of range")
    if number != number.quantize(Decimal(1).scaleb(-places)):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"{name} has more than {places} decimals"
        )
    if (positive and number <= 0) or (not positive and number < 0):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            f"{name} must be {'greater than' if positive else 'at least'} zero",
        )
    if upper is not None and number > upper:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"{name} must be <= {upper}"
        )
    return number


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def map_db_error(exc: DBAPIError) -> MandateError:
    """Name a trigger/constraint refusal; the transaction must be rolled back."""
    text_ = str(getattr(exc, "orig", exc))
    if "unreleased plan" in text_:
        return MandateError(v.MandateRefusal.SYMBOL_RESERVED, "symbol already reserved")
    if "sending authority" in text_ or "effective session" in text_:
        return MandateError(v.MandateRefusal.PLAN_NOT_ACTIVE, text_[:200])
    if "does not bind" in text_ or "does not match" in text_:
        return MandateError(v.MandateRefusal.BINDING_MISMATCH, text_[:200])
    return MandateError(v.MandateRefusal.INVALID_TRANSITION, text_[:200])


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except DBAPIError as exc:
        raise map_db_error(exc) from exc


def realm_and_market(sleeve: StrategySleeve) -> tuple[str, str]:
    try:
        realm = v.realm_of(sleeve.account_mode)
    except ValueError as exc:
        raise MandateError(v.MandateRefusal.UNSUPPORTED, str(exc)) from exc
    if sleeve.market not in v.PLAN_MARKETS:
        raise MandateError(
            v.MandateRefusal.UNSUPPORTED,
            f"market {sleeve.market!r} has no mandate plans",
        )
    return realm, sleeve.market


async def _sleeve(session: AsyncSession, sleeve_id: int) -> StrategySleeve:
    sleeve = await sleeve_store.get_sleeve(session, sleeve_id)
    if sleeve is None:
        raise MandateError(
            v.MandateRefusal.UNSUPPORTED, f"sleeve {sleeve_id} does not exist"
        )
    return sleeve


# --------------------------------------------------------------------------- reads
async def terms_of_version(
    session: AsyncSession, sleeve_version_id: int
) -> MandateTerms | None:
    return (
        await session.execute(
            select(MandateTerms).where(
                MandateTerms.sleeve_version_id == sleeve_version_id
            )
        )
    ).scalar_one_or_none()


async def current_terms(
    session: AsyncSession, sleeve_id: int
) -> tuple[StrategySleeveVersion | None, MandateTerms | None]:
    """The sleeve's current version and its clause (None when the version has none)."""
    version = await sleeve_store.current_version(session, sleeve_id)
    if version is None:
        return None, None
    return version, await terms_of_version(session, version.id)


async def latest_profile(
    session: AsyncSession, sleeve_id: int
) -> MandateActivationProfile | None:
    return (
        await session.execute(
            select(MandateActivationProfile)
            .where(MandateActivationProfile.sleeve_id == sleeve_id)
            .order_by(MandateActivationProfile.profile_version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def event_to_fold(row: MandatePlanEvent) -> PlanEvent:
    return PlanEvent(
        event=row.event,
        seq=row.seq,
        tranche_index=row.tranche_index,
        terms_id=row.terms_id,
        terms_sha256=row.terms_sha256,
        effective_session=row.effective_session,
        resume_target=row.resume_target,
        proposal_id=row.proposal_id,
        notional=None if row.notional is None else Decimal(str(row.notional)),
        kst_day=row.kst_day,
        approval_mode=row.approval_mode,
        account_ref=row.account_ref,
        attempt_id=row.attempt_id,
        fenced_seq=row.fenced_seq,
        thesis_seq=row.thesis_seq,
        fill_amount=None if row.fill_amount is None else Decimal(str(row.fill_amount)),
        fill_quantity=None
        if row.fill_quantity is None
        else Decimal(str(row.fill_quantity)),
        first_fill_amount=(
            None
            if row.first_fill_amount is None
            else Decimal(str(row.first_fill_amount))
        ),
        first_fill_quantity=(
            None
            if row.first_fill_quantity is None
            else Decimal(str(row.first_fill_quantity))
        ),
        activation_profile_id=row.activation_profile_id,
        evidence=dict(row.evidence or {}),
        actor=row.actor,
    )


async def list_plan_events(
    session: AsyncSession, plan_id: int
) -> list[MandatePlanEvent]:
    return list(
        (
            await session.execute(
                select(MandatePlanEvent)
                .where(MandatePlanEvent.plan_id == plan_id)
                .order_by(MandatePlanEvent.seq)
            )
        )
        .scalars()
        .all()
    )


async def plan_state(session: AsyncSession, plan_id: int) -> PlanState:
    rows = await list_plan_events(session, plan_id)
    return fold_plan_events([event_to_fold(r) for r in rows])


async def get_plan(session: AsyncSession, plan_id: int) -> MandatePlan | None:
    return await session.get(MandatePlan, plan_id)


def _unreleased(plan: type[MandatePlan] = MandatePlan):
    return ~exists().where(
        MandatePlanEvent.plan_id == plan.id, MandatePlanEvent.event == "released"
    )


# --------------------------------------------------------------- approval requests
@dataclass(frozen=True)
class GlobalCaps:
    """The global auto-approval caps a clause may not exceed (read by the caller)."""

    per_order_cap: Decimal
    daily_cap: Decimal


@dataclass(frozen=True)
class TermsInput:
    """What an operator asks to approve as a sleeve's next live version (§3.1)."""

    basket_key: str
    account_ref: str
    universe_rule: str
    allowed_symbols: Sequence[str]
    entry_max_premium_bps: Any
    per_order_cap: Any
    daily_cap: Any
    sector_cap_pct: Any
    basket_drawdown_freeze_pct: Any
    account_drawdown_freeze_pct: Any
    tranche_rule: Mapping[str, Any]
    expires_at: datetime
    budget_cap: Any
    loss_limit: Any
    prereg: PreRegistration
    loss_sell_mode: str = "human_card"


def _tranche_rule(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != {"weights", "triggers_pct_from_first", "min_gap_sessions"}:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "tranche_rule needs weights, triggers_pct_from_first and min_gap_sessions",
        )
    try:
        weights = parse_weights(list(raw["weights"]))
    except InvariantViolation as exc:
        raise MandateError(exc.code, str(exc)) from exc
    triggers = [_signed(t) for t in raw["triggers_pct_from_first"]]
    gap = raw["min_gap_sessions"]
    if (
        len(triggers) != v.MAX_TRANCHES
        or triggers[0] != 0
        or any(t > 0 for t in triggers)
        or any(b > a for a, b in zip(triggers, triggers[1:], strict=False))
    ):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "triggers_pct_from_first start at 0 and never rise",
        )
    if isinstance(gap, bool) or not isinstance(gap, int) or gap < 0:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "min_gap_sessions must be a whole number >= 0",
        )
    return {
        "weights": [hashing.canonical_decimal(w) for w in weights],
        "triggers_pct_from_first": [hashing.canonical_decimal(t) for t in triggers],
        "min_gap_sessions": gap,
    }


def _signed(value: Any) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "trigger must be a number"
        ) from exc
    if not number.is_finite() or number != number.quantize(Decimal("0.0001")):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "trigger must have <= 4 decimals"
        )
    return number


def _terms_columns(sleeve: StrategySleeve, data: TermsInput) -> dict[str, Any]:
    realm, market = realm_and_market(sleeve)
    del realm
    symbols = sorted(
        {sleeve_store.sleeve_symbol(s, market) for s in data.allowed_symbols}
    )
    parked = [s for s in symbols if is_parking_symbol(s)]
    if parked:
        raise MandateError(
            v.MandateRefusal.PARKING_SYMBOL,
            f"parking symbols are not plan targets: {parked}",
        )
    if data.loss_sell_mode not in v.LOSS_SELL_MODES:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "loss_sell_mode must be human_card"
        )
    if data.expires_at.tzinfo is None:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "expires_at needs a time zone"
        )
    return {
        "terms_version": v.TERMS_VERSION,
        "basket_key": _text("basket_key", data.basket_key, 64),
        "account_ref": _text("account_ref", data.account_ref, 200),
        "universe_rule": _text("universe_rule", data.universe_rule, 500),
        "allowed_symbols": symbols,
        "entry_max_premium_bps": _dec(
            "entry_max_premium_bps", data.entry_max_premium_bps, positive=True
        ),
        "per_order_cap": _dec("per_order_cap", data.per_order_cap, positive=True),
        "daily_cap": _dec("daily_cap", data.daily_cap, positive=True),
        "sector_cap_pct": _dec(
            "sector_cap_pct", data.sector_cap_pct, positive=True, upper=Decimal(100)
        ),
        "basket_drawdown_freeze_pct": _dec(
            "basket_drawdown_freeze_pct",
            data.basket_drawdown_freeze_pct,
            positive=True,
            upper=Decimal(100),
        ),
        "account_drawdown_freeze_pct": _dec(
            "account_drawdown_freeze_pct",
            data.account_drawdown_freeze_pct,
            positive=True,
            upper=Decimal(100),
        ),
        "tranche_rule": _tranche_rule(data.tranche_rule),
        "loss_sell_mode": data.loss_sell_mode,
        "expires_at": data.expires_at,
    }


def _content_from_terms(
    sleeve_id: int,
    cols: Mapping[str, Any],
    *,
    state: str,
    budget_cap: Any,
    loss_limit: Any,
    prereg: PreRegistration,
) -> dict[str, Any]:
    return hashing.terms_content(
        sleeve_id=sleeve_id,
        terms_version=cols["terms_version"],
        basket_key=cols["basket_key"],
        account_ref=cols["account_ref"],
        universe_rule=cols["universe_rule"],
        allowed_symbols=cols["allowed_symbols"],
        entry_max_premium_bps=cols["entry_max_premium_bps"],
        per_order_cap=cols["per_order_cap"],
        daily_cap=cols["daily_cap"],
        sector_cap_pct=cols["sector_cap_pct"],
        basket_drawdown_freeze_pct=cols["basket_drawdown_freeze_pct"],
        account_drawdown_freeze_pct=cols["account_drawdown_freeze_pct"],
        tranche_rule=cols["tranche_rule"],
        loss_sell_mode=cols["loss_sell_mode"],
        expires_at=cols["expires_at"],
        sleeve_state=state,
        budget_cap=budget_cap,
        loss_limit=loss_limit,
        prereg_metric=prereg.metric,
        prereg_min_effect_pct=prereg.min_effect_pct,
        prereg_horizon_sessions=prereg.horizon_sessions,
        prereg_max_duration_days=prereg.max_duration_days,
    )


def terms_content_from_rows(
    terms: MandateTerms, version: StrategySleeveVersion
) -> dict[str, Any]:
    """Recompute the approved content from the database columns (I21 re-check)."""
    return hashing.terms_content(
        sleeve_id=version.sleeve_id,
        terms_version=terms.terms_version,
        basket_key=terms.basket_key,
        account_ref=terms.account_ref,
        universe_rule=terms.universe_rule,
        allowed_symbols=list(terms.allowed_symbols),
        entry_max_premium_bps=terms.entry_max_premium_bps,
        per_order_cap=terms.per_order_cap,
        daily_cap=terms.daily_cap,
        sector_cap_pct=terms.sector_cap_pct,
        basket_drawdown_freeze_pct=terms.basket_drawdown_freeze_pct,
        account_drawdown_freeze_pct=terms.account_drawdown_freeze_pct,
        tranche_rule=terms.tranche_rule,
        loss_sell_mode=terms.loss_sell_mode,
        expires_at=terms.expires_at,
        sleeve_state=version.state,
        budget_cap=version.budget_cap,
        loss_limit=version.loss_limit,
        prereg_metric=version.prereg_metric,
        prereg_min_effect_pct=version.prereg_min_effect_pct,
        prereg_horizon_sessions=version.prereg_horizon_sessions,
        prereg_max_duration_days=version.prereg_max_duration_days,
    )


def profile_content_from_row(profile: MandateActivationProfile) -> dict[str, Any]:
    return hashing.profile_content(
        sleeve_id=profile.sleeve_id,
        profile_version=profile.profile_version,
        terms_id=profile.terms_id,
        max_tranche_index=profile.max_tranche_index,
        effective_session=profile.effective_session,
        expires_at=profile.expires_at,
    )


async def create_terms_request(
    session: AsyncSession,
    *,
    sleeve_id: int,
    data: TermsInput,
    global_caps: GlobalCaps,
    approval_ref: str,
    requested_by: str,
    now: datetime | None = None,
) -> MandateApprovalRequest:
    """Snapshot a clause (and the sleeve values of its version) for an operator card."""
    who = _operator(requested_by)
    ref = _text("approval_ref", approval_ref, 300)
    moment = _now(now)
    sleeve = await _sleeve(session, sleeve_id)
    realm, market = realm_and_market(sleeve)
    cols = _terms_columns(sleeve, data)
    if cols["per_order_cap"] > global_caps.per_order_cap:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "per_order_cap exceeds the global per-order cap",
        )
    if cols["daily_cap"] > global_caps.daily_cap:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "daily_cap exceeds the global daily cap"
        )
    if cols["expires_at"] <= moment:
        raise MandateError(
            v.MandateRefusal.TERMS_EXPIRED, "expires_at is not in the future"
        )
    budget = _dec("budget_cap", data.budget_cap)
    limit = _dec("loss_limit", data.loss_limit, positive=True)
    rows = await sleeve_store.history(session, sleeve_id)
    if not rows:
        raise MandateError(v.MandateRefusal.UNSUPPORTED, "the sleeve has no version")
    current = rows[-1]
    if data.prereg != sleeve_store.prereg_of(current) and any(
        r.state == SleeveState.LIVE for r in rows
    ):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "the pre-registration is frozen once the sleeve has been live",
        )
    if not data.prereg.complete:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "a live version needs its full pre-registration",
        )
    await _check_basket_market(session, realm, cols["basket_key"], sleeve)
    content = _content_from_terms(
        sleeve_id,
        cols,
        state=SleeveState.LIVE.value,
        budget_cap=budget,
        loss_limit=limit,
        prereg=data.prereg,
    )
    request = MandateApprovalRequest(
        subject_kind="terms",
        sleeve_id=sleeve_id,
        base_sleeve_version=current.version,
        content=content,
        content_sha256=hashing.content_sha256(content),
        approval_ref=ref,
        requested_by=who,
    )
    session.add(request)
    await _flush(session)
    return request


async def _check_basket_market(
    session: AsyncSession, realm: str, basket_key: str, sleeve: StrategySleeve
) -> None:
    """One basket holds one market and one currency (§3.1)."""
    modes = [m for m, r in v.REALM_BY_ACCOUNT_MODE.items() if r == realm]
    rows = (
        await session.execute(
            select(StrategySleeve.market, StrategySleeve.currency)
            .join(
                StrategySleeveVersion,
                StrategySleeveVersion.sleeve_id == StrategySleeve.id,
            )
            .join(
                MandateTerms, MandateTerms.sleeve_version_id == StrategySleeveVersion.id
            )
            .where(
                MandateTerms.basket_key == basket_key,
                StrategySleeve.account_mode.in_(modes),
            )
        )
    ).all()
    if any(m != sleeve.market or c != sleeve.currency for m, c in rows):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "a basket holds one market and one currency",
        )


async def create_profile_request(
    session: AsyncSession,
    *,
    sleeve_id: int,
    terms_id: int,
    max_tranche_index: int,
    effective_session: date,
    expires_at: datetime,
    approval_ref: str,
    requested_by: str,
    now: datetime | None = None,
) -> MandateApprovalRequest:
    """Snapshot an activation profile; the next profile version is fixed here (§3.6)."""
    who = _operator(requested_by)
    ref = _text("approval_ref", approval_ref, 300)
    moment = _now(now)
    sleeve = await _sleeve(session, sleeve_id)
    realm_and_market(sleeve)
    if (
        isinstance(max_tranche_index, bool)
        or not 0 <= max_tranche_index <= v.MAX_TRANCHES
    ):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "max_tranche_index must be 0..3"
        )
    if expires_at.tzinfo is None or expires_at <= moment:
        raise MandateError(
            v.MandateRefusal.TERMS_EXPIRED, "expires_at is not in the future"
        )
    version, terms = await current_terms(session, sleeve_id)
    if (
        version is None
        or terms is None
        or terms.id != terms_id
        or version.state != "live"
    ):
        raise MandateError(
            v.MandateRefusal.TERMS_NOT_CURRENT,
            "a profile opens the sleeve's current live clause",
        )
    latest = await latest_profile(session, sleeve_id)
    content = hashing.profile_content(
        sleeve_id=sleeve_id,
        profile_version=(latest.profile_version if latest else 0) + 1,
        terms_id=terms_id,
        max_tranche_index=max_tranche_index,
        effective_session=effective_session,
        expires_at=expires_at,
    )
    request = MandateApprovalRequest(
        subject_kind="activation_profile",
        sleeve_id=sleeve_id,
        base_sleeve_version=version.version,
        content=content,
        content_sha256=hashing.content_sha256(content),
        approval_ref=ref,
        requested_by=who,
    )
    session.add(request)
    await _flush(session)
    return request


# ------------------------------------------------------------------- approval records
def _check_principal(principal: Any) -> str:
    if (
        not isinstance(principal, str)
        or re.fullmatch(r"[1-9][0-9]{0,15}", principal) is None
        or int(principal) > _PRINCIPAL_MAX
    ):
        raise MandateError(
            v.MandateRefusal.APPROVER_NOT_ALLOWED,
            "the principal is the callback's from.id as a canonical decimal string",
        )
    return principal


@dataclass(frozen=True)
class ApprovalResult:
    approval: MandateApproval
    terms: MandateTerms | None = None
    version: StrategySleeveVersion | None = None
    profile: MandateActivationProfile | None = None


async def record_approval(
    session: AsyncSession,
    *,
    request_id: int,
    decision: str,
    principal: str,
    nonce_digest: str,
    now: datetime | None = None,
) -> ApprovalResult:
    """Record one authenticated decision; an approval writes the clause or profile.

    The caller (the callback, T3b) has already checked the operator principal
    list and consumed the nonce; this function checks shape and staleness. A stale
    request (the sleeve moved on) writes nothing. All rows are written in the
    caller's transaction.
    """
    if decision not in v.APPROVAL_DECISIONS:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "decision must be approved or denied"
        )
    who = _check_principal(principal)
    if not _is_sha(nonce_digest):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "nonce_digest must be a sha256 hex"
        )
    moment = _now(now)
    request = await session.get(MandateApprovalRequest, request_id)
    if request is None:
        raise MandateError(
            v.MandateRefusal.UNSUPPORTED, f"request {request_id} does not exist"
        )
    if hashing.content_sha256(request.content) != request.content_sha256:
        raise MandateError(
            v.MandateRefusal.BINDING_BROKEN, "the request content changed"
        )
    already = (
        await session.execute(
            select(MandateApproval.id).where(MandateApproval.request_id == request_id)
        )
    ).first()
    if already is not None:
        raise MandateError(
            v.MandateRefusal.REQUEST_STALE, "the request already has a decision"
        )
    sleeve = await _sleeve(session, request.sleeve_id)
    realm, _market = realm_and_market(sleeve)

    basket_key: str | None = None
    if request.subject_kind == "terms":
        basket_key = str(request.content["basket_key"])
    else:
        terms = await session.get(MandateTerms, int(request.content["terms_id"]))
        basket_key = None if terms is None else terms.basket_key
    if basket_key is not None:
        await acquire_basket_lock(session, realm, basket_key)
    await acquire_sleeve_lock(session, request.sleeve_id)

    if decision == "approved":
        version = await sleeve_store.current_version(session, request.sleeve_id)
        if version is None or version.version != request.base_sleeve_version:
            raise MandateError(
                v.MandateRefusal.REQUEST_STALE, "the sleeve changed after the request"
            )
    approval = MandateApproval(
        request_id=request_id,
        decision=decision,
        subject_sha256=request.content_sha256,
        principal=who,
        nonce_digest=nonce_digest,
        decided_at=moment,
    )
    session.add(approval)
    await _flush(session)
    if decision == "denied":
        return ApprovalResult(approval)
    if request.subject_kind == "terms":
        return await _write_terms(session, request, approval, sleeve, who, moment)
    return await _write_profile(session, request, approval, moment)


async def _write_terms(
    session: AsyncSession,
    request: MandateApprovalRequest,
    approval: MandateApproval,
    sleeve: StrategySleeve,
    principal: str,
    moment: datetime,
) -> ApprovalResult:
    c = request.content
    expires_at = datetime.strptime(c["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    )
    if expires_at <= moment:
        raise MandateError(
            v.MandateRefusal.TERMS_EXPIRED, "the clause is already expired"
        )
    s = c["sleeve"]
    prereg = PreRegistration(
        metric=s["prereg_metric"],
        min_effect_pct=(
            None
            if s["prereg_min_effect_pct"] is None
            else Decimal(s["prereg_min_effect_pct"])
        ),
        horizon_sessions=s["prereg_horizon_sessions"],
        max_duration_days=s["prereg_max_duration_days"],
    )
    try:
        version = await sleeve_store.append_version_with_terms(
            session,
            sleeve_id=request.sleeve_id,
            expected_version=request.base_sleeve_version,
            state=s["state"],
            budget_cap=s["budget_cap"],
            loss_limit=s["loss_limit"],
            prereg=prereg,
            reason=f"mandate terms approved: {request.approval_ref}"[:500],
            set_by=f"operator:telegram:{principal}",
        )
    except SleeveError as exc:
        raise MandateError(v.MandateRefusal.REQUEST_STALE, str(exc)) from exc
    realm, _market = realm_and_market(sleeve)
    await _check_basket_market(session, realm, c["basket_key"], sleeve)
    terms = MandateTerms(
        sleeve_version_id=version.id,
        terms_version=c["terms_version"],
        basket_key=c["basket_key"],
        account_ref=c["account_ref"],
        universe_rule=c["universe_rule"],
        allowed_symbols=list(c["allowed_symbols"]),
        entry_max_premium_bps=Decimal(c["entry_max_premium_bps"]),
        per_order_cap=Decimal(c["per_order_cap"]),
        daily_cap=Decimal(c["daily_cap"]),
        sector_cap_pct=Decimal(c["sector_cap_pct"]),
        basket_drawdown_freeze_pct=Decimal(c["basket_drawdown_freeze_pct"]),
        account_drawdown_freeze_pct=Decimal(c["account_drawdown_freeze_pct"]),
        tranche_rule=dict(c["tranche_rule"]),
        loss_sell_mode=c["loss_sell_mode"],
        expires_at=expires_at,
        approval_id=approval.id,
        content_sha256=request.content_sha256,
    )
    session.add(terms)
    await _flush(session)
    await session.refresh(terms)
    await session.refresh(version)
    if (
        hashing.content_sha256(terms_content_from_rows(terms, version))
        != terms.content_sha256
    ):
        raise MandateError(
            v.MandateRefusal.BINDING_BROKEN,
            "the stored clause does not hash to the request",
        )
    return ApprovalResult(approval, terms=terms, version=version)


async def _write_profile(
    session: AsyncSession,
    request: MandateApprovalRequest,
    approval: MandateApproval,
    moment: datetime,
) -> ApprovalResult:
    c = request.content
    version, terms = await current_terms(session, request.sleeve_id)
    if (
        version is None
        or terms is None
        or terms.id != int(c["terms_id"])
        or version.state != "live"
    ):
        raise MandateError(
            v.MandateRefusal.TERMS_NOT_CURRENT,
            "the profile's clause is no longer current",
        )
    latest = await latest_profile(session, request.sleeve_id)
    if int(c["profile_version"]) != (latest.profile_version if latest else 0) + 1:
        raise MandateError(
            v.MandateRefusal.REQUEST_STALE, "another profile was approved first"
        )
    expires_at = datetime.strptime(c["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    )
    if expires_at <= moment:
        raise MandateError(
            v.MandateRefusal.TERMS_EXPIRED, "the profile is already expired"
        )
    profile = MandateActivationProfile(
        sleeve_id=request.sleeve_id,
        profile_version=int(c["profile_version"]),
        terms_id=int(c["terms_id"]),
        max_tranche_index=int(c["max_tranche_index"]),
        effective_session=date.fromisoformat(c["effective_session"]),
        expires_at=expires_at,
        approval_id=approval.id,
        content_sha256=request.content_sha256,
    )
    session.add(profile)
    await _flush(session)
    await session.refresh(profile)
    if (
        hashing.content_sha256(profile_content_from_row(profile))
        != profile.content_sha256
    ):
        raise MandateError(
            v.MandateRefusal.BINDING_BROKEN,
            "the stored profile does not hash to the request",
        )
    return ApprovalResult(approval, profile=profile)


# --------------------------------------------------------------------- plan registration
@dataclass(frozen=True)
class PlanInput:
    sleeve_id: int
    symbol: str
    planned_amount: Any
    adopted_amount: Any
    #: closed keys: checked_at, holdings [{account_ref, quantity, amount}], price,
    #: price_source. Every scope account is listed, zero included.
    adopted_evidence: Mapping[str, Any]
    account_ref: str
    scope_account_refs: Sequence[str]
    thesis: str
    invalidation_conditions: Sequence[str]
    sector_key: str
    created_by: str
    #: the close of the last complete daily bar, read by the caller (DB candles)
    one_share_price: Any


def _check_adopted_evidence(
    evidence: Mapping[str, Any], scope: Sequence[str], adopted: Decimal
) -> dict[str, Any]:
    if not set(evidence) <= _ADOPTED_EVIDENCE_KEYS or "holdings" not in evidence:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "adopted_evidence needs holdings and only checked_at, holdings, price, price_source",
        )
    holdings = evidence["holdings"]
    if not isinstance(holdings, Sequence) or isinstance(holdings, str | bytes):
        raise MandateError(v.MandateRefusal.INVALID_FIELDS, "holdings must be a list")
    refs: list[str] = []
    total = ZERO
    for item in holdings:
        if not isinstance(item, Mapping) or set(item) != {
            "account_ref",
            "quantity",
            "amount",
        }:
            raise MandateError(
                v.MandateRefusal.INVALID_FIELDS,
                "each holding names account_ref, quantity and amount",
            )
        refs.append(str(item["account_ref"]))
        _dec("holding quantity", item["quantity"], places=12)
        total += _dec("holding amount", item["amount"])
    if sorted(refs) != sorted(scope):
        raise MandateError(
            v.MandateRefusal.SCOPE_UNKNOWN,
            "holdings must cover every scope account exactly once (zero included)",
        )
    if total != adopted:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS,
            "adopted_amount is not the sum of the holdings",
        )
    return json_safe(dict(evidence))


def json_safe(value: Any) -> Any:
    """Plain JSON values: decimals and datetimes become strings."""
    if isinstance(value, Mapping):
        return {str(k): json_safe(x) for k, x in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(x) for x in value]
    if isinstance(value, Decimal):
        return hashing.canonical_decimal(value)
    if isinstance(value, datetime):
        return hashing.canonical_timestamp(value)
    if isinstance(value, date | UUID):
        return str(value)
    return value


async def _open_loss_cut(session: AsyncSession, market: str, symbol: str) -> bool:
    """§5.2: an unclosed ``loss_cut`` proposal on this instrument in any live account."""
    rows = (
        await session.execute(
            select(OrderProposal.symbol).where(
                OrderProposal.market == market,
                OrderProposal.exit_intent == "loss_cut",
                OrderProposal.account_mode.in_(_LIVE_ACCOUNT_MODES),
                OrderProposal.lifecycle_state.not_in(_CLOSED_GROUP_STATES),
            )
        )
    ).all()
    return any(sleeve_store.sleeve_symbol(r[0], market) == symbol for r in rows)


async def _unreleased_plan_amounts(
    session: AsyncSession,
    *,
    sleeve_id: int | None = None,
    realm: str | None = None,
    basket_key: str | None = None,
) -> list[PlanAmounts]:
    stmt = select(
        MandatePlan.planned_amount, MandatePlan.adopted_amount, MandatePlan.sector_key
    ).where(_unreleased())
    if sleeve_id is not None:
        stmt = stmt.where(MandatePlan.sleeve_id == sleeve_id)
    if basket_key is not None and realm is not None:
        modes = [m for m, r in v.REALM_BY_ACCOUNT_MODE.items() if r == realm]
        stmt = (
            stmt.join(MandateTerms, MandateTerms.id == MandatePlan.approved_terms_id)
            .join(StrategySleeve, StrategySleeve.id == MandatePlan.sleeve_id)
            .where(
                MandateTerms.basket_key == basket_key,
                StrategySleeve.account_mode.in_(modes),
            )
        )
    rows = (await session.execute(stmt)).all()
    return [PlanAmounts(Decimal(str(p)), Decimal(str(a)), str(k)) for p, a, k in rows]


@dataclass(frozen=True)
class BasketView:
    constituent_budget_caps: tuple[Decimal, ...]
    sector_cap_pct: Decimal  # the strictest live clause of the basket
    mismatch: bool = False


async def basket_view(session: AsyncSession, realm: str, basket_key: str) -> BasketView:
    """The basket's current live clauses: budgets and the strictest sector cap (§3.1)."""
    modes = [m for m, r in v.REALM_BY_ACCOUNT_MODE.items() if r == realm]
    sleeves = (
        (
            await session.execute(
                select(StrategySleeve).where(StrategySleeve.account_mode.in_(modes))
            )
        )
        .scalars()
        .all()
    )
    caps: list[Decimal] = []
    sector_caps: list[Decimal] = []
    for sleeve in sleeves:
        version, terms = await current_terms(session, sleeve.id)
        if version is None or terms is None or terms.basket_key != basket_key:
            continue
        caps.append(Decimal(str(version.budget_cap)))
        sector_caps.append(Decimal(str(terms.sector_cap_pct)))
    if not sector_caps:
        raise MandateError(
            v.MandateRefusal.TERMS_NOT_CURRENT, "the basket has no current clause"
        )
    return BasketView(tuple(caps), min(sector_caps), len(set(sector_caps)) > 1)


@dataclass(frozen=True)
class RegisteredPlan:
    plan: MandatePlan
    tranches: tuple[MandatePlanTranche, ...]
    reservation: MandateSymbolReservation
    proposed: MandatePlanEvent


async def register_plan(
    session: AsyncSession, data: PlanInput, *, now: datetime | None = None
) -> RegisteredPlan:
    """Register a plan in state ``proposed`` (I1, I2, I3, I4, I15, I17, §5.2).

    Locks: L1 symbol, then L2 basket, then L3 sleeve, taken before any decision
    read. Nothing is written unless every check passes.
    """
    who = _operator(data.created_by)
    moment = _now(now)
    sleeve = await _sleeve(session, data.sleeve_id)
    realm, market = realm_and_market(sleeve)
    symbol = sleeve_store.sleeve_symbol(data.symbol, market)
    if is_parking_symbol(symbol):
        raise MandateError(
            v.MandateRefusal.PARKING_SYMBOL, f"{symbol} is a parking symbol"
        )

    _version0, terms0 = await current_terms(session, data.sleeve_id)
    if terms0 is None:
        raise MandateError(
            v.MandateRefusal.TERMS_NOT_CURRENT, "the sleeve has no live clause"
        )
    await acquire_symbol_lock(session, realm, market, symbol)
    await acquire_basket_lock(session, realm, terms0.basket_key)
    await acquire_sleeve_lock(session, data.sleeve_id)

    version, terms = await current_terms(session, data.sleeve_id)
    if (
        version is None
        or terms is None
        or terms.id != terms0.id
        or version.state != SleeveState.LIVE.value
    ):
        raise MandateError(
            v.MandateRefusal.TERMS_NOT_CURRENT,
            "the sleeve's live clause changed under the locks",
        )
    if terms.expires_at <= moment:
        raise MandateError(v.MandateRefusal.TERMS_EXPIRED, "the clause is expired")
    if not symbol_allowed(symbol, terms.allowed_symbols):
        raise MandateError(
            v.MandateRefusal.SYMBOL_NOT_ALLOWED, f"{symbol} is not in allowed_symbols"
        )
    scope = [_text("scope_account_refs", r, 200) for r in data.scope_account_refs]
    account_ref = _text("account_ref", data.account_ref, 200)
    if not account_binding_ok(
        terms_account_ref=terms.account_ref,
        plan_account_ref=account_ref,
        scope_account_refs=scope,
    ):
        raise MandateError(
            v.MandateRefusal.ACCOUNT_MISMATCH,
            "the plan account is not the clause account or not in its scope accounts",
        )
    planned = _dec("planned_amount", data.planned_amount, positive=True)
    adopted = _dec("adopted_amount", data.adopted_amount)
    evidence = _check_adopted_evidence(data.adopted_evidence, scope, adopted)
    thesis = _text("thesis", data.thesis, 1000)
    conditions = [
        _text("invalidation condition", c, 500) for c in data.invalidation_conditions
    ]
    if len(conditions) > 3:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "at most 3 invalidation conditions"
        )
    sector_key = _text("sector_key", data.sector_key, 100)

    reserved = (
        await session.execute(
            select(MandateSymbolReservation.id).where(
                MandateSymbolReservation.realm == realm,
                MandateSymbolReservation.market == market,
                MandateSymbolReservation.symbol == symbol,
                ~exists().where(
                    MandateSymbolReservationRelease.reservation_id
                    == MandateSymbolReservation.id
                ),
            )
        )
    ).first()
    if reserved is not None:
        raise MandateError(
            v.MandateRefusal.SYMBOL_RESERVED,
            f"{symbol} already has an unreleased plan ({realm})",
        )
    if realm == "live" and await _open_loss_cut(session, market, symbol):
        raise MandateError(
            v.MandateRefusal.OPEN_LOSS_CUT,
            f"{symbol} has an unclosed loss_cut proposal",
        )

    try:
        amounts = tranche_amounts(planned, adopted, terms.tranche_rule["weights"])
        check_one_share(
            amounts, _dec("one_share_price", data.one_share_price, positive=True)
        )
        new = PlanAmounts(planned, adopted, sector_key)
        sleeve_budget_check(
            new,
            await _unreleased_plan_amounts(session, sleeve_id=data.sleeve_id),
            Decimal(str(version.budget_cap)),
        )
        view = await basket_view(session, realm, terms.basket_key)
        sector_cap_check(
            new,
            await _unreleased_plan_amounts(
                session, realm=realm, basket_key=terms.basket_key
            ),
            view.constituent_budget_caps,
            view.sector_cap_pct,
        )
    except InvariantViolation as exc:
        raise MandateError(exc.code, str(exc)) from exc

    plan = MandatePlan(
        sleeve_id=data.sleeve_id,
        symbol=symbol,
        approved_terms_id=terms.id,
        account_ref=account_ref,
        scope_account_refs=scope,
        planned_amount=planned,
        adopted_amount=adopted,
        adopted_evidence=evidence,
        thesis=thesis,
        invalidation_conditions=conditions,
        sector_key=sector_key,
        created_by=who,
    )
    session.add(plan)
    await _flush(session)
    rule = terms.tranche_rule
    tranches = tuple(
        MandatePlanTranche(
            plan_id=plan.id,
            tranche_index=i,
            weight=Decimal(str(rule["weights"][i - 1])),
            amount=amounts[i - 1],
            trigger_pct_from_first=Decimal(str(rule["triggers_pct_from_first"][i - 1])),
            min_gap_sessions=0 if i == 1 else int(rule["min_gap_sessions"]),
        )
        for i in range(1, v.MAX_TRANCHES + 1)
    )
    session.add_all(tranches)
    proposed = MandatePlanEvent(plan_id=plan.id, seq=1, event="proposed", actor=who)
    session.add(proposed)
    await _flush(session)
    reservation = MandateSymbolReservation(
        realm=realm, market=market, symbol=symbol, plan_id=plan.id
    )
    session.add(reservation)
    await _flush(session)
    return RegisteredPlan(plan, tranches, reservation, proposed)


# --------------------------------------------------------------------------- plan events
def validate_holdings_evidence(
    evidence: Mapping[str, Any], *, scope_account_refs: Sequence[str], actor: str
) -> None:
    """I19: what a ``holdings_disposed`` event must prove (mirrors the DB trigger)."""
    kind = evidence.get("holdings_disposition")
    if kind == "kept_outside_basket" and actor.startswith("system:"):
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "kept_outside_basket is an operator record"
        )
    if kind != "zero_holdings":
        return
    refs = evidence.get("holdings_account_refs")
    if not isinstance(refs, list) or sorted(str(r) for r in refs) != sorted(
        scope_account_refs
    ):
        raise MandateError(
            v.MandateRefusal.SCOPE_UNKNOWN,
            "zero_holdings must read every scope account fixed at registration",
        )
    try:
        quantity = Decimal(str(evidence.get("holdings_quantity")))
    except InvalidOperation as exc:
        raise MandateError(
            v.MandateRefusal.SCOPE_UNKNOWN, "holdings_quantity is unreadable"
        ) from exc
    if quantity != 0:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, "holdings remain in the scope"
        )


_EVENT_COLUMNS = frozenset(
    {
        "tranche_index",
        "terms_id",
        "terms_sha256",
        "effective_session",
        "resume_target",
        "batch_member_id",
        "proposal_id",
        "notional",
        "kst_day",
        "approval_mode",
        "account_ref",
        "attempt_id",
        "activation_profile_id",
        "fenced_seq",
        "thesis_seq",
        "fill_amount",
        "fill_quantity",
        "first_fill_amount",
        "first_fill_quantity",
    }
)


def _event_field_problem(event: str, fields: Mapping[str, Any]) -> str | None:
    """The per-event NULL/required matrix of ``vocabulary.FIELD_RULES`` (CHECK twin)."""
    present = {k for k, val in fields.items() if val is not None}
    for group, (required, allowed) in v.FIELD_RULES.items():
        cols = set(v.FIELD_COLUMNS[group])
        if event in required and not cols <= present:
            return f"{event} needs {sorted(cols - present)}"
        if event not in allowed and cols & present:
            return f"{event} takes no {sorted(cols & present)}"
    first_fill = {"first_fill_amount", "first_fill_quantity"}
    wants = event in v.TRANCHE_COMPLETION_EVENTS and fields.get("tranche_index") == 1
    if wants and not first_fill <= present:
        return "the first tranche's completion records first_fill_amount and first_fill_quantity"
    if not wants and first_fill & present:
        return "first_fill_* belongs to the first tranche's completion only"
    fenced_auto = (
        event == "tranche_send_fenced" and fields.get("approval_mode") == "auto"
    )
    if fenced_auto != (fields.get("activation_profile_id") is not None):
        return "activation_profile_id is for auto fences only, and required there"
    return None


async def append_plan_event(
    session: AsyncSession,
    *,
    plan_id: int,
    event: str,
    actor: str,
    evidence: Mapping[str, Any] | None = None,
    **fields: Any,
) -> MandatePlanEvent:
    """Append one event after the fold accepts it (the DB trigger checks it again).

    Locks: L1 first for ``released``, then L3 (the plan's sleeve), as the design
    asks event writers to hold (§4.6). A fence event additionally needs the
    plan's own account reference.
    """
    unknown = set(fields) - _EVENT_COLUMNS
    if unknown:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"unknown event fields {sorted(unknown)}"
        )
    who = _text("actor", actor, 128)
    if who == SYSTEM_LOSS_GATE_ACTOR:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"{SYSTEM_LOSS_GATE_ACTOR} is reserved"
        )
    plan = await get_plan(session, plan_id)
    if plan is None:
        raise MandateError(
            v.MandateRefusal.UNSUPPORTED, f"plan {plan_id} does not exist"
        )
    sleeve = await _sleeve(session, plan.sleeve_id)
    realm, market = realm_and_market(sleeve)
    if event == "released":
        await acquire_symbol_lock(session, realm, market, plan.symbol)
    await acquire_sleeve_lock(session, plan.sleeve_id)

    clean_evidence = json_safe(dict(evidence or {}))
    bad_keys = set(clean_evidence) - set(v.EVIDENCE_KEYS)
    if bad_keys:
        raise MandateError(
            v.MandateRefusal.INVALID_FIELDS, f"unknown evidence keys {sorted(bad_keys)}"
        )
    if event not in v.ALL_EVENTS:
        raise MandateError(
            v.MandateRefusal.INVALID_TRANSITION, f"unknown event {event!r}"
        )
    problem = _event_field_problem(event, fields)
    if problem is not None:
        raise MandateError(v.MandateRefusal.INVALID_FIELDS, problem)
    if event == "holdings_disposed":
        validate_holdings_evidence(
            clean_evidence, scope_account_refs=list(plan.scope_account_refs), actor=who
        )
    if event == "tranche_send_fenced" and fields.get("account_ref") != plan.account_ref:
        raise MandateError(
            v.MandateRefusal.ACCOUNT_MISMATCH, "the fence account is not the plan's"
        )
    if fields.get("terms_id") is not None:
        terms = await session.get(MandateTerms, int(fields["terms_id"]))
        version = (
            None
            if terms is None
            else await session.get(StrategySleeveVersion, terms.sleeve_version_id)
        )
        if terms is None or version is None or version.sleeve_id != plan.sleeve_id:
            raise MandateError(
                v.MandateRefusal.BINDING_MISMATCH, "the clause is not this sleeve's"
            )
        if fields.get("terms_sha256") is None:
            fields = {**fields, "terms_sha256": terms.content_sha256}
        if fields["terms_sha256"] != terms.content_sha256:
            raise MandateError(
                v.MandateRefusal.BINDING_BROKEN, "terms_sha256 is not the clause hash"
            )
        if (
            event in ("approved", "terms_rebound")
            and terms.account_ref != plan.account_ref
        ):
            raise MandateError(
                v.MandateRefusal.ACCOUNT_MISMATCH,
                "a clause with another account cannot rebind",
            )

    state = await plan_state(session, plan_id)
    candidate = PlanEvent(
        event=event,
        seq=state.last_seq + 1,
        evidence=clean_evidence,
        actor=who,
        **fields,
    )
    try:
        apply_event(state, candidate)
    except InvalidPlanEvent as exc:
        raise MandateError(exc.code, str(exc)) from exc

    row = MandatePlanEvent(
        plan_id=plan_id,
        seq=state.last_seq + 1,
        event=event,
        evidence=clean_evidence,
        actor=who,
        **fields,
    )
    session.add(row)
    await _flush(session)
    return row


__all__ = [
    "ApprovalResult",
    "BasketView",
    "GlobalCaps",
    "MandateError",
    "PlanInput",
    "RegisteredPlan",
    "TermsInput",
    "append_plan_event",
    "basket_view",
    "create_profile_request",
    "create_terms_request",
    "current_terms",
    "get_plan",
    "latest_profile",
    "list_plan_events",
    "plan_state",
    "record_approval",
    "register_plan",
    "terms_content_from_rows",
    "validate_holdings_evidence",
]
