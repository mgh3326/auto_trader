"""Strategy sleeve persistence and fill attribution (ROB-1470).

This module is the only writer of ``review.strategy_sleeves`` and
``review.strategy_sleeve_versions``. Both are append-only (DB triggers); a
budget, loss limit or state change is a new version that copies the fields it
does not change. The operator CLI and the dispatch gate's loss stop both take the
same per-sleeve advisory lock, so a change and a gate decision never interleave.

Fill attribution reads execution_ledger evidence only:

* rows in effect (``execution_ledger_in_effect()``, #1175) from authoritative
  sources (``reconciler`` / ``manual_import``) -- a websocket row is provisional
  (ROB-1379) and only counts once a reconcile books it;
* the broker and ledger account mode of the sleeve's account, the sleeve's
  currency, and the broker order id of one of the sleeve's own rungs;
* the same side and symbol as that rung, filled no earlier than its proposal;
* for KIS, whose order numbers repeat across days, only the first trading day a
  rung's order id matched, and no later than the trading day of the proposal's
  ``valid_until``.

A fill that fails any of these is not the sleeve's fill. The diagnostics count
what was skipped and why.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.strategy_sleeves import StrategySleeve, StrategySleeveVersion
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    normalize_order_id,
    order_trading_day,
)
from app.services.strategy_sleeves.book import SleeveFill, SleeveOrder
from app.services.strategy_sleeves.gate import SleeveVersionView
from app.services.strategy_sleeves.vocabulary import (
    ACCOUNT_MARKETS,
    CANARY_RULES,
    CURRENCY_BY_MARKET,
    LEDGER_IDENTITY,
    SYSTEM_LOSS_GATE_ACTOR,
    SleeveState,
)

ZERO = Decimal("0")
_CRYPTO_PREFIX = "KRW-"
#: NUMERIC(20, 4) holds at most 16 integer digits.
_MAX_AMOUNT = Decimal("1e16")


class SleeveError(ValueError):
    """A sleeve command that cannot be applied; nothing was written."""


@dataclass(frozen=True)
class SleeveInputs:
    orders: tuple[SleeveOrder, ...]
    fills: tuple[SleeveFill, ...]
    diagnostics: dict[str, int] = field(default_factory=dict)


def sleeve_symbol(symbol: str, market: str) -> str:
    """One symbol key per instrument: KR as is, US dot form, crypto ``KRW-X``."""
    upper = str(symbol or "").strip().upper()
    if market == "equity_us":
        return to_db_symbol(upper).upper()
    if market == "crypto" and not upper.startswith(_CRYPTO_PREFIX):
        return f"{_CRYPTO_PREFIX}{upper}"
    return upper


def _amount(name: str, value: Any, *, positive: bool) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SleeveError(f"{name} is required")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise SleeveError(f"{name} must be a number") from exc
    if not parsed.is_finite():
        raise SleeveError(f"{name} must be finite")
    if positive and parsed <= 0:
        raise SleeveError(f"{name} must be greater than zero")
    if not positive and parsed < 0:
        raise SleeveError(f"{name} must not be negative")
    if parsed >= _MAX_AMOUNT:
        raise SleeveError(f"{name} is too large")
    try:
        exact = parsed == parsed.quantize(Decimal("0.0001"))
    except InvalidOperation as exc:
        raise SleeveError(f"{name} must be a plain number") from exc
    if not exact:
        raise SleeveError(f"{name} has more than 4 decimal places")
    return parsed


def _state(value: Any) -> SleeveState:
    try:
        return SleeveState(value)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in SleeveState)
        raise SleeveError(f"state must be one of: {allowed}") from exc


def _nonblank(name: str, value: Any, limit: int) -> str:
    text_value = str(value or "").strip()
    if not text_value or len(text_value) > limit:
        raise SleeveError(f"{name} is required (1-{limit} characters)")
    return text_value


def _actor(value: Any) -> str:
    """An operator actor; the loss gate's system actor is never accepted here."""
    who = _nonblank("actor", value, 128)
    if who == SYSTEM_LOSS_GATE_ACTOR:
        raise SleeveError(f"{SYSTEM_LOSS_GATE_ACTOR} is reserved for the loss gate")
    return who


async def acquire_sleeve_lock(session: AsyncSession, sleeve_id: int) -> None:
    """Serialize every decision about one sleeve until the transaction ends."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"review.strategy_sleeves:{int(sleeve_id)}"},
    )


async def get_sleeve(session: AsyncSession, sleeve_id: int) -> StrategySleeve | None:
    return await session.get(StrategySleeve, sleeve_id)


async def get_sleeve_by_key(
    session: AsyncSession, sleeve_key: str
) -> StrategySleeve | None:
    result = await session.execute(
        select(StrategySleeve).where(StrategySleeve.sleeve_key == sleeve_key)
    )
    return result.scalar_one_or_none()


async def list_sleeves(session: AsyncSession) -> list[StrategySleeve]:
    result = await session.execute(select(StrategySleeve).order_by(StrategySleeve.id))
    return list(result.scalars().all())


async def history(session: AsyncSession, sleeve_id: int) -> list[StrategySleeveVersion]:
    result = await session.execute(
        select(StrategySleeveVersion)
        .where(StrategySleeveVersion.sleeve_id == sleeve_id)
        .order_by(StrategySleeveVersion.version)
    )
    return list(result.scalars().all())


async def current_version(
    session: AsyncSession, sleeve_id: int
) -> StrategySleeveVersion | None:
    result = await session.execute(
        select(StrategySleeveVersion)
        .where(StrategySleeveVersion.sleeve_id == sleeve_id)
        .order_by(StrategySleeveVersion.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def version_view(row: StrategySleeveVersion) -> SleeveVersionView:
    return SleeveVersionView(
        sleeve_id=row.sleeve_id,
        version=row.version,
        state=row.state,
        budget_cap=Decimal(str(row.budget_cap)),
        loss_limit=Decimal(str(row.loss_limit)),
    )


@dataclass(frozen=True)
class PreRegistration:
    """ROB-1468 pre-registration of one sleeve; every value is the operator's."""

    metric: str | None = None
    min_effect_pct: Decimal | None = None
    horizon_sessions: int | None = None
    max_duration_days: int | None = None

    @property
    def complete(self) -> bool:
        return None not in (
            self.metric,
            self.min_effect_pct,
            self.horizon_sessions,
            self.max_duration_days,
        )


def prereg_of(row: StrategySleeveVersion) -> PreRegistration:
    return PreRegistration(
        metric=row.prereg_metric,
        min_effect_pct=(
            None
            if row.prereg_min_effect_pct is None
            else Decimal(str(row.prereg_min_effect_pct))
        ),
        horizon_sessions=row.prereg_horizon_sessions,
        max_duration_days=row.prereg_max_duration_days,
    )


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise SleeveError(f"{name} must be a whole number")
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise SleeveError(f"{name} must be a whole number") from exc
    if parsed < 1 or str(parsed) != str(value).strip():
        raise SleeveError(f"{name} must be a whole number of at least 1")
    return parsed


def _merge_prereg(
    base: PreRegistration,
    *,
    metric: Any = None,
    min_effect_pct: Any = None,
    horizon_sessions: Any = None,
    max_duration_days: Any = None,
) -> PreRegistration:
    return PreRegistration(
        metric=base.metric if metric is None else _nonblank("metric", metric, 300),
        min_effect_pct=(
            base.min_effect_pct
            if min_effect_pct is None
            else _amount("min_effect_pct", min_effect_pct, positive=True)
        ),
        horizon_sessions=(
            base.horizon_sessions
            if horizon_sessions is None
            else _positive_int("horizon_sessions", horizon_sessions)
        ),
        max_duration_days=(
            base.max_duration_days
            if max_duration_days is None
            else _positive_int("max_duration_days", max_duration_days)
        ),
    )


def _prereg_columns(prereg: PreRegistration) -> dict[str, Any]:
    return {
        "prereg_metric": prereg.metric,
        "prereg_min_effect_pct": prereg.min_effect_pct,
        "prereg_horizon_sessions": prereg.horizon_sessions,
        "prereg_max_duration_days": prereg.max_duration_days,
    }


def _require_prereg_for_live(state: str, prereg: PreRegistration) -> None:
    if state == SleeveState.LIVE and not prereg.complete:
        raise SleeveError(
            "a live sleeve needs its full pre-registration: metric, "
            "min_effect_pct, horizon_sessions and max_duration_days"
        )


async def create_sleeve(
    session: AsyncSession,
    *,
    sleeve_key: str,
    rule_id: str,
    account_mode: str,
    market: str,
    state: str,
    budget_cap: Any,
    loss_limit: Any,
    reason: str,
    actor: str,
    metric: Any = None,
    min_effect_pct: Any = None,
    horizon_sessions: Any = None,
    max_duration_days: Any = None,
) -> tuple[StrategySleeve, StrategySleeveVersion]:
    """Create a sleeve and its version 1. Every value comes from the caller."""
    key = _nonblank("sleeve_key", sleeve_key, 64)
    rule = CANARY_RULES.get(rule_id)
    if rule is None:
        raise SleeveError("rule_id must be one of: " + ", ".join(sorted(CANARY_RULES)))
    if (account_mode, market) not in ACCOUNT_MARKETS:
        raise SleeveError(f"unsupported account/market: {account_mode}/{market}")
    if market not in rule.markets:
        raise SleeveError(f"{rule_id} does not run in {market}")
    initial_state = _state(state)
    if initial_state is SleeveState.STOPPED:
        raise SleeveError("a new sleeve cannot start stopped")
    budget = _amount("budget_cap", budget_cap, positive=False)
    limit = _amount("loss_limit", loss_limit, positive=True)
    prereg = _merge_prereg(
        PreRegistration(),
        metric=metric,
        min_effect_pct=min_effect_pct,
        horizon_sessions=horizon_sessions,
        max_duration_days=max_duration_days,
    )
    _require_prereg_for_live(initial_state.value, prereg)
    who = _actor(actor)
    why = _nonblank("reason", reason, 500)
    if await get_sleeve_by_key(session, key) is not None:
        raise SleeveError(f"sleeve {key!r} already exists")
    sleeve = StrategySleeve(
        sleeve_key=key,
        rule_id=rule_id,
        account_mode=account_mode,
        market=market,
        currency=CURRENCY_BY_MARKET[market],
        created_by=who,
    )
    session.add(sleeve)
    await session.flush()
    version = StrategySleeveVersion(
        sleeve_id=sleeve.id,
        version=1,
        state=initial_state.value,
        budget_cap=budget,
        loss_limit=limit,
        reason=why,
        set_by=who,
        **_prereg_columns(prereg),
    )
    session.add(version)
    await session.flush()
    return sleeve, version


async def set_sleeve_version(
    session: AsyncSession,
    *,
    sleeve_id: int,
    reason: str,
    actor: str,
    state: str | None = None,
    budget_cap: Any = None,
    loss_limit: Any = None,
    metric: Any = None,
    min_effect_pct: Any = None,
    horizon_sessions: Any = None,
    max_duration_days: Any = None,
) -> StrategySleeveVersion:
    """Append the next version; unchanged fields are copied from the current one.

    The pre-registration is frozen once the sleeve has had a live version: a
    changed metric, effect, horizon or duration is a new sleeve (a new epoch),
    never an edit of a running one.
    """
    who = _actor(actor)
    why = _nonblank("reason", reason, 500)
    await acquire_sleeve_lock(session, sleeve_id)
    rows = await history(session, sleeve_id)
    if not rows:
        raise SleeveError(f"sleeve {sleeve_id} does not exist")
    current = rows[-1]
    new_state = current.state if state is None else _state(state).value
    new_budget = (
        Decimal(str(current.budget_cap))
        if budget_cap is None
        else _amount("budget_cap", budget_cap, positive=False)
    )
    new_limit = (
        Decimal(str(current.loss_limit))
        if loss_limit is None
        else _amount("loss_limit", loss_limit, positive=True)
    )
    old_prereg = prereg_of(current)
    new_prereg = _merge_prereg(
        old_prereg,
        metric=metric,
        min_effect_pct=min_effect_pct,
        horizon_sessions=horizon_sessions,
        max_duration_days=max_duration_days,
    )
    if new_prereg != old_prereg and any(r.state == SleeveState.LIVE for r in rows):
        raise SleeveError(
            "the pre-registration is frozen once the sleeve has been live; "
            "create a new sleeve for a new epoch"
        )
    _require_prereg_for_live(new_state, new_prereg)
    if (
        new_state == current.state
        and new_budget == Decimal(str(current.budget_cap))
        and new_limit == Decimal(str(current.loss_limit))
        and new_prereg == old_prereg
    ):
        raise SleeveError("nothing changes; no version written")
    row = StrategySleeveVersion(
        sleeve_id=sleeve_id,
        version=current.version + 1,
        state=new_state,
        budget_cap=new_budget,
        loss_limit=new_limit,
        reason=why,
        set_by=who,
        **_prereg_columns(new_prereg),
    )
    session.add(row)
    await session.flush()
    return row


async def stop_at_loss_limit(
    session: AsyncSession,
    *,
    expected: SleeveVersionView,
    reason: str,
) -> StrategySleeveVersion | None:
    """Append ``stopped`` iff the sleeve is still at ``expected`` and live.

    The caller already holds the sleeve lock; the re-read makes a stale view a
    no-op instead of a second write. Everything else is copied unchanged.
    """
    current = await current_version(session, expected.sleeve_id)
    if (
        current is None
        or current.version != expected.version
        or current.state != SleeveState.LIVE
    ):
        return None
    row = StrategySleeveVersion(
        sleeve_id=expected.sleeve_id,
        version=current.version + 1,
        state=SleeveState.STOPPED.value,
        budget_cap=Decimal(str(current.budget_cap)),
        loss_limit=Decimal(str(current.loss_limit)),
        reason=reason[:500],
        set_by=SYSTEM_LOSS_GATE_ACTOR,
        **_prereg_columns(prereg_of(current)),
    )
    session.add(row)
    await session.flush()
    return row


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class _RungRef:
    order: SleeveOrder
    created_at: datetime
    valid_until: datetime | None


def _ledger_symbol_keys(row: ExecutionLedger, market: str) -> set[str]:
    keys = {sleeve_symbol(row.symbol, market)}
    if row.raw_symbol:
        keys.add(sleeve_symbol(row.raw_symbol, market))
    return keys


def _instrument(row: ExecutionLedger) -> str:
    return str(getattr(row.instrument_type, "value", row.instrument_type) or "")


async def load_sleeve_inputs(
    session: AsyncSession, sleeve: StrategySleeve
) -> SleeveInputs:
    """The sleeve's own orders and the authoritative fills attributed to them."""
    rung_rows = await session.execute(
        select(OrderProposal, OrderProposalRung)
        .join(OrderProposalRung, OrderProposalRung.proposal_pk == OrderProposal.id)
        .where(OrderProposal.sleeve_id == sleeve.id)
        .order_by(OrderProposalRung.id)
    )
    refs: list[_RungRef] = []
    by_order_id: dict[str, list[_RungRef]] = defaultdict(list)
    for proposal, rung in rung_rows.all():
        order = SleeveOrder(
            proposal_pk=proposal.id,
            rung_id=rung.id,
            symbol=sleeve_symbol(proposal.symbol, sleeve.market),
            side=rung.side,
            state=rung.state,
            quantity=_dec(rung.quantity) or ZERO,
            limit_price=_dec(rung.limit_price),
            notional=_dec(rung.notional),
            rung_filled_qty=_dec(rung.filled_qty),
            signal_key=str(proposal.root_proposal_id),
            signal_at=proposal.created_at,
        )
        ref = _RungRef(order, proposal.created_at, proposal.valid_until)
        refs.append(ref)
        order_id = normalize_order_id(rung.broker_order_id)
        if order_id:
            by_order_id[order_id].append(ref)

    diagnostics: Counter[str] = Counter()
    fills: list[SleeveFill] = []
    broker, ledger_mode = LEDGER_IDENTITY[sleeve.account_mode]
    if by_order_id:
        ledger_rows = await session.execute(
            select(ExecutionLedger).where(
                execution_ledger_in_effect(),
                ExecutionLedger.broker == broker,
                ExecutionLedger.account_mode == ledger_mode,
                ExecutionLedger.source.in_(AUTHORITATIVE_SOURCES),
                func.ltrim(ExecutionLedger.broker_order_id, "0").in_(
                    sorted({key.lstrip("0") for key in by_order_id})
                ),
            )
        )
        fills, skipped = attribute_fills(
            rows=list(ledger_rows.scalars().all()),
            by_order_id=by_order_id,
            sleeve=sleeve,
        )
        diagnostics.update(skipped)
    return SleeveInputs(
        orders=tuple(ref.order for ref in refs),
        fills=tuple(fills),
        diagnostics=dict(diagnostics),
    )


def attribute_fills(
    *,
    rows: Sequence[ExecutionLedger],
    by_order_id: dict[str, list[_RungRef]],
    sleeve: StrategySleeve,
) -> tuple[list[SleeveFill], Counter[str]]:
    """Match ledger rows to the sleeve's rungs; return fills and skip counts."""
    skipped: Counter[str] = Counter()
    per_rung: dict[int, list[tuple[Any, SleeveFill]]] = defaultdict(list)
    for row in rows:
        if str(row.currency or "").upper() != sleeve.currency:
            skipped["currency_mismatch"] += 1
            continue
        candidates = by_order_id.get(normalize_order_id(row.broker_order_id), [])
        symbols = _ledger_symbol_keys(row, sleeve.market)
        instrument = _instrument(row)
        day = order_trading_day(row.broker, instrument, row.filled_at)
        matches = [
            ref
            for ref in candidates
            if ref.order.side == row.side
            and ref.order.symbol in symbols
            and row.filled_at >= ref.created_at
            and (
                day is None
                or ref.valid_until is None
                or day <= order_trading_day(row.broker, instrument, ref.valid_until)
            )
        ]
        if not matches:
            skipped["no_matching_order"] += 1
            continue
        ref = max(matches, key=lambda r: (r.created_at, r.order.rung_id))
        quantity = _dec(row.filled_qty)
        price = _dec(row.filled_price)
        if quantity is None or price is None or quantity <= 0 or price <= 0:
            skipped["unreadable_fill"] += 1
            continue
        fee = _dec(row.fee_amount) or ZERO
        fee_currency = str(row.fee_currency or "").upper()
        if fee and fee_currency and fee_currency != sleeve.currency:
            skipped["fee_other_currency"] += 1
            fee = ZERO
        per_rung[ref.order.rung_id].append(
            (
                day,
                SleeveFill(
                    ledger_id=row.id,
                    rung_id=ref.order.rung_id,
                    symbol=ref.order.symbol,
                    side=row.side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    filled_at=row.filled_at,
                ),
            )
        )
    fills: list[SleeveFill] = []
    for entries in per_rung.values():
        days = {day for day, _fill in entries if day is not None}
        first_day = min(days) if days else None
        for day, fill in entries:
            if first_day is not None and day != first_day:
                # A KIS order number reused on a later day is another order.
                skipped["later_day_order_id_reuse"] += 1
                continue
            fills.append(fill)
    return fills, skipped
