"""hk 5441 — the minimal execution mandate (design 5438 §4) and operator exits.

This module is the only writer of ``review.execution_mandates``,
``review.execution_mandate_revocations`` and ``review.execution_operator_exits``.
All three are append-only (DB triggers). Every command previews first: it
returns what it would write, and the caller commits or rolls back.

Operator decision ``decision/2026-11-20/at-5438-d1-d19``:

* D9 — the values below are the ceilings of any mandate. A mandate may be
  tighter; a looser one needs a new decision and a code change.
* D12 — a shadow mandate is created by the operator command alone, because a
  shadow intent cannot send (I6).
* D2, D17 — a live mandate is bound to the account the operator confirmed: the
  command shows the credential fingerprint and the caller must repeat it and
  the content hash it recorded in an hk decision doc. The 3795 §3.5 approval
  card replaces this temporary path when it exists.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_intent import ExecutionChildRequest, ExecutionIntent
from app.models.execution_mandates import (
    MANDATE_MODES,
    MANDATE_TERMS_VERSION,
    ExecutionMandate,
    ExecutionMandateRevocation,
    ExecutionOperatorExit,
)
from app.models.strategy_sleeves import StrategySleeve
from app.schemas.execution_intent import UPBIT_LIVE_ACCOUNT
from app.services.execution_intent_gateway.upbit_decisions import (
    SLEEVE_ACCOUNT_MODE,
    SLEEVE_MARKET,
    MandateView,
    OperatorExitView,
)
from app.services.strategy_sleeves.store import (
    acquire_sleeve_lock,
    current_version,
    get_sleeve_by_key,
)

#: D9 (operator decision 2026-11-20): the ceilings of a mandate.
MAX_PER_INTENT_NOTIONAL_KRW: Final = Decimal("480000")
MAX_OPEN_INTENTS: Final = 4
MAX_PRICE_BAND_BPS: Final = 30
MAX_INTENT_TTL: Final = timedelta(hours=12)
MAX_MANDATE_LIFETIME: Final = timedelta(days=91)
#: S6: an operator exit is a short, deliberate permission.
MAX_OPERATOR_EXIT_LIFETIME: Final = timedelta(hours=1)

LOSS_SELL_MODE: Final = "human_card"
_SYMBOL: Final = re.compile(r"^KRW-[A-Z0-9]{2,10}$")
_CODE_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_SYSTEM_PREFIX: Final = "system:"
#: The temporary live approval (D2) names the hk decision doc that holds the hash.
_LIVE_APPROVAL_REF: Final = re.compile(r"^decision/[0-9]{4}-[0-9]{2}-[0-9]{2}/[\w.-]+$")
_MIN_QUANTITY: Final = Decimal("0.00000001")


class MandateError(ValueError):
    """A mandate command that cannot be applied; nothing was written."""


@dataclass(frozen=True)
class MandateDraft:
    """What a mandate would say. ``content_sha256`` covers every field here and
    the sleeve version's amounts and state.
    """

    strategy: str
    strategy_code_sha: str
    sleeve_id: int
    sleeve_key: str
    sleeve_version_id: int
    sleeve_version: int
    sleeve_state: str
    sleeve_budget_cap: Decimal
    sleeve_loss_limit: Decimal
    account: str
    mode: str
    allowed_symbols: tuple[str, ...]
    per_intent_max_notional_krw: Decimal
    max_open_intents: int
    price_band_bps: int
    max_intent_ttl: timedelta
    expires_at: datetime
    loss_sell_mode: str
    account_ref: str | None
    content_sha256: str


def _text(value: Any, name: str, limit: int) -> str:
    text_value = str(value or "").strip()
    if not text_value or len(text_value) > limit:
        raise MandateError(f"{name} is required (1-{limit} characters)")
    return text_value


def human_actor(value: Any) -> str:
    """A person. A system actor name is refused (design §4.3)."""
    who = _text(value, "actor", 128)
    if who.lower().startswith(_SYSTEM_PREFIX):
        raise MandateError("actor must be a person, not a system actor")
    return who


def _whole(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise MandateError(f"{name} must be a whole number")
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise MandateError(f"{name} must be a whole number") from exc
    if str(parsed) != str(value).strip():
        raise MandateError(f"{name} must be a whole number")
    return parsed


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool | float) or value is None:
        raise MandateError(f"{name} must be an exact decimal")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MandateError(f"{name} must be an exact decimal") from exc
    if not parsed.is_finite():
        raise MandateError(f"{name} must be finite")
    return parsed


def normalize_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(sorted({str(symbol or "").strip().upper() for symbol in symbols}))
    if not cleaned or len(cleaned) > 32:
        raise MandateError("allowed_symbols needs 1 to 32 symbols")
    for symbol in cleaned:
        if not _SYMBOL.fullmatch(symbol):
            raise MandateError(f"symbol {symbol!r} is not a KRW market symbol")
    return cleaned


def content_sha256(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _draft_fields(draft_values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            format(value, "f")
            if isinstance(value, Decimal)
            else int(value.total_seconds())
            if isinstance(value, timedelta)
            else value.isoformat()
            if isinstance(value, datetime)
            else list(value)
            if isinstance(value, tuple)
            else value
        )
        for key, value in draft_values.items()
    }


async def draft_mandate(
    session: AsyncSession,
    *,
    sleeve_key: str,
    strategy: str,
    strategy_code_sha: str,
    mode: str,
    allowed_symbols: Sequence[str],
    per_intent_max_notional_krw: Any,
    max_open_intents: Any,
    price_band_bps: Any,
    max_intent_ttl_seconds: Any,
    expires_at: datetime,
    account_ref: str | None,
    now: datetime,
) -> MandateDraft:
    """Validate a mandate against its sleeve and D9, and hash it. Writes nothing."""
    if mode not in MANDATE_MODES:
        raise MandateError("mode must be one of: " + ", ".join(MANDATE_MODES))
    label = _text(strategy, "strategy", 128)
    sha = str(strategy_code_sha or "").strip()
    if not _CODE_SHA.fullmatch(sha):
        raise MandateError("strategy_code_sha must be 40 lowercase hex characters")
    symbols = normalize_symbols(allowed_symbols)
    notional = _decimal(per_intent_max_notional_krw, "per_intent_max_notional_krw")
    if notional != notional.to_integral_value() or not (
        Decimal("5000") <= notional <= MAX_PER_INTENT_NOTIONAL_KRW
    ):
        raise MandateError(
            "per_intent_max_notional_krw must be whole KRW between 5000 and "
            f"{MAX_PER_INTENT_NOTIONAL_KRW}"
        )
    open_intents = _whole(max_open_intents, "max_open_intents")
    if not 1 <= open_intents <= MAX_OPEN_INTENTS:
        raise MandateError(f"max_open_intents must be 1 to {MAX_OPEN_INTENTS}")
    band = _whole(price_band_bps, "price_band_bps")
    if not 0 <= band <= MAX_PRICE_BAND_BPS:
        raise MandateError(f"price_band_bps must be 0 to {MAX_PRICE_BAND_BPS}")
    ttl = timedelta(seconds=_whole(max_intent_ttl_seconds, "max_intent_ttl_seconds"))
    if not timedelta(0) < ttl <= MAX_INTENT_TTL:
        raise MandateError("max_intent_ttl must be above zero and at most 12 hours")
    if expires_at.tzinfo is None:
        raise MandateError("expires_at needs a timezone")
    if not now < expires_at <= now + MAX_MANDATE_LIFETIME:
        raise MandateError("expires_at must be in the future, at most 91 days away")

    sleeve = await get_sleeve_by_key(session, _text(sleeve_key, "sleeve_key", 64))
    if sleeve is None:
        raise MandateError(f"sleeve {sleeve_key!r} does not exist")
    # S7: the path is fixed when the sleeve is created; a card sleeve never
    # takes a mandate.
    if getattr(sleeve, "execution_path", "card") != "go":
        raise MandateError("a mandate binds only to a sleeve created on the go path")
    if (sleeve.account_mode, sleeve.market) != (SLEEVE_ACCOUNT_MODE, SLEEVE_MARKET):
        raise MandateError("a mandate binds only to an upbit/crypto sleeve")
    version = await current_version(session, sleeve.id)
    if version is None:
        raise MandateError("the sleeve has no version")
    if version.state != mode:
        raise MandateError(
            f"a {mode} mandate needs the sleeve in state {mode!r}, "
            f"not {version.state!r}"
        )
    ref = (account_ref or "").strip() or None
    if mode == "live" and ref is None:
        # B12: no fingerprint, no live mandate.
        raise MandateError(
            "a live mandate needs the account fingerprint, and it could not be computed"
        )
    values: dict[str, Any] = {
        "terms_version": MANDATE_TERMS_VERSION,
        "strategy": label,
        "strategy_code_sha": sha,
        "sleeve_id": sleeve.id,
        "sleeve_key": sleeve.sleeve_key,
        "sleeve_version_id": version.id,
        "sleeve_version": version.version,
        "sleeve_state": version.state,
        "sleeve_budget_cap": Decimal(str(version.budget_cap)),
        "sleeve_loss_limit": Decimal(str(version.loss_limit)),
        "account": UPBIT_LIVE_ACCOUNT,
        "mode": mode,
        "allowed_symbols": symbols,
        "per_intent_max_notional_krw": notional,
        "max_open_intents": open_intents,
        "price_band_bps": band,
        "max_intent_ttl": ttl,
        "expires_at": expires_at,
        "loss_sell_mode": LOSS_SELL_MODE,
        "account_ref": ref,
    }
    digest = content_sha256(_draft_fields(values))
    values.pop("terms_version")
    return MandateDraft(**values, content_sha256=digest)


async def _sleeve_has_history(session: AsyncSession, sleeve_id: int) -> bool:
    from app.models.order_proposals import OrderProposal

    proposal = await session.scalar(
        select(OrderProposal.id).where(OrderProposal.sleeve_id == sleeve_id).limit(1)
    )
    intent = await session.scalar(
        select(ExecutionIntent.id)
        .where(ExecutionIntent.sleeve_id == sleeve_id)
        .limit(1)
    )
    return proposal is not None or intent is not None


async def _unrevoked_mandate(
    session: AsyncSession, sleeve_id: int
) -> ExecutionMandate | None:
    return await session.scalar(
        select(ExecutionMandate)
        .outerjoin(
            ExecutionMandateRevocation,
            ExecutionMandateRevocation.mandate_id == ExecutionMandate.id,
        )
        .where(
            ExecutionMandate.sleeve_id == sleeve_id,
            ExecutionMandateRevocation.mandate_id.is_(None),
        )
        .limit(1)
    )


async def create_mandate(
    session: AsyncSession,
    draft: MandateDraft,
    *,
    actor: str,
    approval_ref: str | None = None,
    confirm_content_sha256: str | None = None,
    confirm_account_ref: str | None = None,
) -> ExecutionMandate:
    """Insert the mandate row. The caller commits.

    A live mandate is refused unless the caller repeats the content hash and the
    account fingerprint the preview showed, and names the hk decision doc that
    records the hash (D2, D17). Those three are what "the operator approved this
    exact mandate for this exact account" means until 3795 §3.5 exists.
    """
    who = human_actor(actor)
    await acquire_sleeve_lock(session, draft.sleeve_id)
    if draft.mode == "live":
        ref = str(approval_ref or "").strip()
        if not _LIVE_APPROVAL_REF.fullmatch(ref):
            raise MandateError(
                "a live mandate needs approval_ref naming an hk decision doc "
                "(decision/YYYY-MM-DD/<slug>)"
            )
        if confirm_content_sha256 != draft.content_sha256:
            raise MandateError("confirm_content_sha256 does not match this mandate")
        if not draft.account_ref or confirm_account_ref != draft.account_ref:
            raise MandateError(
                "confirm_account_ref does not match the configured account"
            )
    else:
        ref = f"operator-cli/{who}"[:200]
    version = await current_version(session, draft.sleeve_id)
    if (
        version is None
        or version.id != draft.sleeve_version_id
        or version.state != draft.mode
    ):
        raise MandateError("the sleeve changed since the preview; preview again")
    # S7, S1: an empty go sleeve, one mandate at a time.
    if await _sleeve_has_history(session, draft.sleeve_id):
        raise MandateError("the sleeve already has orders; use a new sleeve")
    if await _unrevoked_mandate(session, draft.sleeve_id) is not None:
        raise MandateError("the sleeve already has a mandate that is not revoked")
    row = ExecutionMandate(
        id=uuid.uuid4(),
        terms_version=MANDATE_TERMS_VERSION,
        strategy=draft.strategy,
        strategy_code_sha=draft.strategy_code_sha,
        sleeve_id=draft.sleeve_id,
        sleeve_version_id=draft.sleeve_version_id,
        account=draft.account,
        mode=draft.mode,
        allowed_symbols=list(draft.allowed_symbols),
        per_intent_max_notional_krw=draft.per_intent_max_notional_krw,
        max_open_intents=draft.max_open_intents,
        price_band_bps=draft.price_band_bps,
        max_intent_ttl=draft.max_intent_ttl,
        expires_at=draft.expires_at,
        loss_sell_mode=draft.loss_sell_mode,
        account_ref=draft.account_ref,
        approval_ref=ref,
        content_sha256=draft.content_sha256,
        created_by=who,
    )
    session.add(row)
    await session.flush()
    return row


async def lock_mandate(
    session: AsyncSession, mandate_id: uuid.UUID
) -> ExecutionMandate | None:
    """The mandate row under ``FOR UPDATE``: the revocation fence (B10, S2).

    The revocation writer and every child reservation take this lock, so a
    reservation either commits before the revocation or sees it.
    """
    return await session.scalar(
        select(ExecutionMandate)
        .where(ExecutionMandate.id == mandate_id)
        .with_for_update()
    )


async def is_revoked(session: AsyncSession, mandate_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(ExecutionMandateRevocation.mandate_id).where(
            ExecutionMandateRevocation.mandate_id == mandate_id
        )
    )
    return found is not None


def mandate_view(row: ExecutionMandate, *, revoked: bool) -> MandateView:
    return MandateView(
        mandate_id=row.id,
        strategy=row.strategy,
        strategy_code_sha=row.strategy_code_sha,
        sleeve_id=row.sleeve_id,
        account=row.account,
        mode=row.mode,
        allowed_symbols=frozenset(row.allowed_symbols),
        per_intent_max_notional_krw=Decimal(str(row.per_intent_max_notional_krw)),
        max_open_intents=row.max_open_intents,
        price_band_bps=row.price_band_bps,
        max_intent_ttl=row.max_intent_ttl,
        expires_at=row.expires_at,
        account_ref=row.account_ref,
        revoked=revoked,
    )


@dataclass(frozen=True)
class PendingChild:
    """A child reserved before a revocation that may still reach the broker."""

    intent_id: uuid.UUID
    idempotency_key: str
    action: str
    state: str
    broker_order_id: str | None


async def revoke_mandate(
    session: AsyncSession, *, mandate_id: uuid.UUID, reason: str, actor: str
) -> tuple[ExecutionMandateRevocation, list[PendingChild]]:
    """Revoke a mandate under its row lock. The caller commits.

    After the commit no new intent, submit or amend passes (I1). A child that
    was reserved before it cannot be stopped from here; the pre-send recheck is
    best effort. Those children are returned so the operator can cancel or
    review them (B10). A cancel is never blocked by a revocation.
    """
    who = human_actor(actor)
    why = _text(reason, "reason", 500)
    mandate = await session.get(ExecutionMandate, mandate_id)
    if mandate is None:
        raise MandateError("mandate does not exist")
    await acquire_sleeve_lock(session, mandate.sleeve_id)
    await lock_mandate(session, mandate_id)
    if await is_revoked(session, mandate_id):
        raise MandateError("mandate is already revoked")
    row = ExecutionMandateRevocation(mandate_id=mandate_id, revoked_by=who, reason=why)
    session.add(row)
    await session.flush()
    pending = await session.execute(
        select(ExecutionChildRequest)
        .join(ExecutionIntent, ExecutionIntent.id == ExecutionChildRequest.intent_id)
        .where(
            ExecutionIntent.mandate_id == mandate_id,
            ExecutionChildRequest.state.in_(("sending", "uncertain", "accepted")),
        )
        .order_by(ExecutionChildRequest.id)
    )
    return row, [
        PendingChild(
            intent_id=child.intent_id,
            idempotency_key=child.idempotency_key,
            action=child.action,
            state=child.state,
            broker_order_id=child.broker_order_id,
        )
        for child in pending.scalars().all()
    ]


def operator_exit_view(row: ExecutionOperatorExit) -> OperatorExitView:
    return OperatorExitView(
        exit_id=row.id,
        mandate_id=row.mandate_id,
        symbol=row.symbol,
        max_quantity=Decimal(str(row.max_quantity)),
        expires_at=row.expires_at,
    )


async def create_operator_exit(
    session: AsyncSession,
    *,
    mandate_id: uuid.UUID,
    symbol: str,
    max_quantity: Any,
    reason: str,
    actor: str,
    now: datetime,
    lifetime: timedelta = MAX_OPERATOR_EXIT_LIFETIME,
) -> ExecutionOperatorExit:
    """Insert an operator exit row (S6). The caller commits.

    The mandate may be revoked or expired: the row exists so that a position is
    never stranded by either. It still must name a symbol the mandate covers.
    """
    who = human_actor(actor)
    why = _text(reason, "reason", 500)
    quantity = _decimal(max_quantity, "max_quantity")
    if quantity < _MIN_QUANTITY or quantity != quantity.quantize(_MIN_QUANTITY):
        raise MandateError("max_quantity must be positive with at most 8 decimals")
    if not timedelta(0) < lifetime <= MAX_OPERATOR_EXIT_LIFETIME:
        raise MandateError("an operator exit lasts at most one hour")
    mandate = await session.get(ExecutionMandate, mandate_id)
    if mandate is None:
        raise MandateError("mandate does not exist")
    [normalized] = normalize_symbols([symbol])
    if normalized not in mandate.allowed_symbols:
        raise MandateError("symbol is outside the mandate")
    row = ExecutionOperatorExit(
        id=uuid.uuid4(),
        mandate_id=mandate_id,
        symbol=normalized,
        max_quantity=quantity,
        reason=why,
        created_by=who,
        created_at=now,
        expires_at=now + lifetime,
    )
    session.add(row)
    await session.flush()
    return row


async def get_sleeve_of_mandate(
    session: AsyncSession, mandate: ExecutionMandate
) -> StrategySleeve | None:
    return await session.get(StrategySleeve, mandate.sleeve_id)
