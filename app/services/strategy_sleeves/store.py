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
* for KIS, whose order numbers repeat across days, only fills on the trading
  day of the rung's own order. That day comes from the rung's send-ledger row
  (``kis_live_order_ledger`` for KR, ``live_order_ledger`` for US), matched by
  the rung's idempotency key or correlation id *and* its order number -- never
  by the order number alone;
* for Kiwoom mock (hk 5529), whose order numbers are unique only within an
  account and trading day, the same day rule with a stricter match: the rung's
  day comes from its Kiwoom mock ledger place row (read through the ledger
  service), matched by the sleeve's own account, the rung's idempotency key
  *and* its normalized order number. hk 5642: the *fill's* day comes from the
  place row that booked it — ``execution_ledger.raw_payload_json.ledger_id``,
  written by the Kiwoom fill writer — never from the instant the reconcile
  observed the fill, so a fill reconciled after KST midnight still belongs to
  its order's day. A fill whose place row, account, order number or day cannot
  be proven is not guessed onto a rung and is not dropped as another day's:
  the rungs it could belong to are reported as unattributed and the sleeve's
  P&L becomes unmeasured.

A ``go`` sleeve (hk 5441) has a second source of orders: the submit children of
its execution intents, read by ``intent_orders``. Their fills are the same
ledger rows, matched by the child's Upbit order uuid under the same rules. A
``card`` sleeve never reads that source, so its result is unchanged.

A fill that fails any of these is not the sleeve's fill. The diagnostics count
what was skipped and why. A KIS fill that matches a rung whose order day is
unknown is not guessed onto it either: the rung is reported as unattributed and
the sleeve's P&L becomes unmeasured (unknown is never zero). The same holds
for Kiwoom, and a Kiwoom fill that more than one rung could own is never
guessed onto one of them either.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.execution_mandates import ExecutionExitHandoff
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
)
from app.models.strategy_sleeves import (
    SLEEVE_EXECUTION_PATHS,
    StrategySleeve,
    StrategySleeveVersion,
)
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    normalize_order_id,
    order_trading_day,
)
from app.services.kiwoom_mock_order_ledger_service import (
    place_rows_by_ids,
    place_rows_for_idempotency_keys,
)
from app.services.strategy_sleeves.book import SleeveFill, SleeveOrder
from app.services.strategy_sleeves.gate import SleeveVersionView
from app.services.strategy_sleeves.intent_orders import (
    IntentChildOrder,
    child_order_key,
    live_orders,
    load_intent_child_orders,
)
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
    #: Rungs with a ledger fill on their order number that could not be tied
    #: to them (KIS order day unknown). Their sleeve's P&L is unmeasured.
    unattributed_rungs: tuple[int, ...] = ()


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
    execution_path: str = "card",
) -> tuple[StrategySleeve, StrategySleeveVersion]:
    """Create a sleeve and its version 1. Every value comes from the caller.

    ``execution_path`` is fixed here and never changes (design 5438 S7): ``go``
    is a sleeve the Go executor trades through execution intents, on Upbit only.
    """
    key = _nonblank("sleeve_key", sleeve_key, 64)
    if execution_path not in SLEEVE_EXECUTION_PATHS:
        raise SleeveError(
            "execution_path must be one of: " + ", ".join(SLEEVE_EXECUTION_PATHS)
        )
    if execution_path == "go" and (account_mode, market) != ("upbit", "crypto"):
        raise SleeveError("a go-path sleeve runs on upbit/crypto only")
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
        execution_path=execution_path,
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


async def _bump_mandate_authority(
    session: AsyncSession,
    sleeve_id: int,
    extra_baskets: tuple[tuple[str, str], ...] = (),
) -> None:
    """A sleeve version is a fact a mandate send fence reads (hk 3795 T4c, §4.6 F2).

    A sleeve with no mandate clause belongs to no basket and nothing happens. The
    import is lazy: the mandate package imports this module.
    """
    from app.services.mandate.fence_locks import bump_for_sleeve_version

    await bump_for_sleeve_version(session, sleeve_id, extra_baskets=extra_baskets)


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
    await _bump_mandate_authority(session, sleeve_id)
    return row


async def append_version_with_terms(
    session: AsyncSession,
    *,
    sleeve_id: int,
    expected_version: int,
    state: str,
    budget_cap: Any,
    loss_limit: Any,
    prereg: PreRegistration,
    reason: str,
    set_by: str,
    extra_baskets: tuple[tuple[str, str], ...] = (),
) -> StrategySleeveVersion:
    """Append the next version as the mandate approval writes it (hk 3795 T1).

    ``set_sleeve_version`` refuses a version that changes nothing; a mandate
    clause can change while the sleeve values stay the same, and an approved
    request fixes every value. So this append takes the exact values, requires
    the sleeve to still be at ``expected_version`` and keeps the same
    pre-registration freeze. The caller holds the sleeve lock and the approval
    transaction; ``set_sleeve_version`` and the ROB-1470 triggers are unchanged.
    """
    who = _nonblank("set_by", set_by, 128)
    why = _nonblank("reason", reason, 500)
    rows = await history(session, sleeve_id)
    if not rows:
        raise SleeveError(f"sleeve {sleeve_id} does not exist")
    current = rows[-1]
    if current.version != expected_version:
        raise SleeveError(
            f"sleeve {sleeve_id} is at version {current.version}, "
            f"not {expected_version}"
        )
    new_state = _state(state).value
    if prereg != prereg_of(current) and any(r.state == SleeveState.LIVE for r in rows):
        raise SleeveError(
            "the pre-registration is frozen once the sleeve has been live; "
            "create a new sleeve for a new epoch"
        )
    _require_prereg_for_live(new_state, prereg)
    row = StrategySleeveVersion(
        sleeve_id=sleeve_id,
        version=current.version + 1,
        state=new_state,
        budget_cap=_amount("budget_cap", budget_cap, positive=False),
        loss_limit=_amount("loss_limit", loss_limit, positive=True),
        reason=why,
        set_by=who,
        **_prereg_columns(prereg),
    )
    session.add(row)
    await session.flush()
    await _bump_mandate_authority(session, sleeve_id, extra_baskets)
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
    await _bump_mandate_authority(session, expected.sleeve_id)
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
    #: KIS only: the trading day of the rung's own order, from its send-ledger
    #: row. ``None`` for other brokers and when it cannot be established.
    order_day: Any = None


@dataclass(frozen=True)
class _SendKeys:
    rung_id: int
    order_id: str
    idempotency_key: str | None
    correlation_id: str | None


def _ledger_symbol_keys(row: ExecutionLedger, market: str) -> set[str]:
    keys = {sleeve_symbol(row.symbol, market)}
    if row.raw_symbol:
        keys.add(sleeve_symbol(row.raw_symbol, market))
    return keys


def _instrument(row: ExecutionLedger) -> str:
    return str(getattr(row.instrument_type, "value", row.instrument_type) or "")


def _key_text(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value or None


def kiwoom_rung_order_days(
    *, rungs: Sequence[_SendKeys], send_rows: Sequence[Any], account_mode: str
) -> dict[int, str]:
    """rung id -> the Kiwoom trading day of the rung's own order (pure).

    A place row belongs to a rung only if it is on the sleeve's own account,
    carries the rung's idempotency key *and* the rung's normalized order number.
    A rung with no such row, or with rows on more than one trading day, gets no
    day. Kiwoom order numbers repeat across accounts and days, so the order
    number alone never matches.
    """
    days: dict[int, str] = {}
    for rung in rungs:
        if rung.idempotency_key is None:
            continue
        found = {
            _key_text(row.trading_day)
            for row in send_rows
            if row.account_mode == account_mode
            and getattr(row, "operation_kind", "place") == "place"
            and normalize_order_id(row.broker_order_id) == rung.order_id
            and _key_text(row.idempotency_key) == rung.idempotency_key
        }
        if len(found) == 1 and None not in found:
            days[rung.rung_id] = next(iter(found))  # type: ignore[arg-type]
    return days


def _fill_trading_day(row: ExecutionLedger, instrument: str) -> Any:
    """The trading day a non-Kiwoom fill row belongs to.

    Kiwoom rows never take their day from the row (hk 5642): the reconcile
    observes a fill later than the trade, so ``filled_at`` is not its day. Their
    day comes from the place ledger row that booked them
    (:func:`_kiwoom_fill_days`) or is unknown.
    """
    return order_trading_day(row.broker, instrument, row.filled_at)


def rung_order_days(
    *, rungs: Sequence[_SendKeys], send_rows: Sequence[Any], instrument: str
) -> dict[int, Any]:
    """rung id -> the KIS trading day of the rung's own order (pure).

    A send row belongs to a rung only if it carries the rung's idempotency key
    or correlation id *and* the rung's order number. A rung with no such row,
    or with rows on more than one trading day, gets no day.
    """
    days: dict[int, Any] = {}
    for rung in rungs:
        found = {
            order_trading_day("kis", instrument, row.trade_date)
            for row in send_rows
            if normalize_order_id(row.order_no) == rung.order_id
            and row.trade_date is not None
            and (
                (
                    rung.idempotency_key is not None
                    and _key_text(row.idempotency_key) == rung.idempotency_key
                )
                or (
                    rung.correlation_id is not None
                    and _key_text(row.correlation_id) == rung.correlation_id
                )
            )
        }
        if len(found) == 1:
            days[rung.rung_id] = next(iter(found))
    return days


async def _kis_send_rows(
    session: AsyncSession, sleeve: StrategySleeve, rungs: Sequence[_SendKeys]
) -> list[Any]:
    """Send-ledger rows that may be the sleeve's KIS rungs' own orders."""
    idem = sorted({r.idempotency_key for r in rungs if r.idempotency_key})
    corr = sorted({r.correlation_id for r in rungs if r.correlation_id})
    if not idem and not corr:
        return []
    model: Any = LiveOrderLedger if sleeve.market == "equity_us" else KISLiveOrderLedger
    clauses = []
    if idem:
        clauses.append(model.idempotency_key.in_(idem))
    if corr:
        clauses.append(model.correlation_id.in_(corr))
    stmt = select(
        model.order_no, model.idempotency_key, model.correlation_id, model.trade_date
    ).where(model.broker == "kis", or_(*clauses))
    if model is LiveOrderLedger:
        stmt = stmt.where(
            LiveOrderLedger.account_scope == "kis_live", LiveOrderLedger.market == "us"
        )
    else:
        stmt = stmt.where(KISLiveOrderLedger.account_mode == "kis_live")
    return list((await session.execute(stmt)).all())


async def _kiwoom_send_rows(
    session: AsyncSession, sleeve: StrategySleeve, rungs: Sequence[_SendKeys]
) -> list[Any]:
    """Place rows of the sleeve's own Kiwoom mock account for the rungs' keys."""
    idem = sorted({r.idempotency_key for r in rungs if r.idempotency_key})
    return await place_rows_for_idempotency_keys(
        session, account_mode=sleeve.account_mode, idempotency_keys=idem
    )


def _payload_ledger_id(row: ExecutionLedger) -> int | None:
    """The place ledger row the fill writer recorded for this row, if usable.

    The Kiwoom fill writer stores the id of the order row it booked from
    (hk 4734). It is the only per-row binding from a fill to its order: the
    order number alone repeats across days.
    """
    payload = getattr(row, "raw_payload_json", None)
    if not isinstance(payload, dict):
        return None
    ledger_id = payload.get("ledger_id")
    if isinstance(ledger_id, bool) or not isinstance(ledger_id, int):
        return None
    return ledger_id if ledger_id > 0 else None


async def _kiwoom_fill_days(
    session: AsyncSession, sleeve: StrategySleeve, rows: Sequence[ExecutionLedger]
) -> dict[int, str]:
    """execution_ledger row id -> the day of the place row that booked it.

    Only a ``place`` row on the sleeve's own account whose normalized order
    number is the fill's gives a day. Everything else (no binding, another
    account, a correction row, a different order number, no recorded day)
    leaves the fill without a day, and :func:`attribute_fills` treats such a
    fill as unmeasured rather than as another day's.
    """
    place_ids = sorted(
        {place_id for place_id in map(_payload_ledger_id, rows) if place_id is not None}
    )
    if not place_ids:
        return {}
    place_rows = await place_rows_by_ids(
        session, account_mode=sleeve.account_mode, ledger_ids=place_ids
    )
    by_id = {int(place.id): place for place in place_rows}
    days: dict[int, str] = {}
    for row in rows:
        row_id = getattr(row, "id", None)
        if row_id is None:
            continue
        place = by_id.get(_payload_ledger_id(row) or 0)
        if place is None:
            continue
        if getattr(place, "operation_kind", "place") != "place":
            continue
        if place.account_mode != sleeve.account_mode:
            continue
        if normalize_order_id(place.broker_order_id) != normalize_order_id(
            row.broker_order_id
        ):
            continue
        day = _key_text(place.trading_day)
        if day is not None:
            days[int(row_id)] = day
    return days


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
    orders: list[tuple[SleeveOrder, datetime, str | None]] = []
    send_keys: list[_SendKeys] = []
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
        order_id = normalize_order_id(rung.broker_order_id) or None
        orders.append((order, proposal.created_at, order_id))
        if order_id:
            send_keys.append(
                _SendKeys(
                    rung.id,
                    order_id,
                    _key_text(rung.idempotency_key),
                    _key_text(rung.correlation_id),
                )
            )

    child_orders: list[IntentChildOrder] = []
    if getattr(sleeve, "execution_path", "card") == "go":
        orders = await _mark_unmeasured_handoff_cards(session, sleeve, orders)
        child_orders = live_orders(await load_intent_child_orders(session, sleeve.id))
        for child in child_orders:
            orders.append(
                (
                    child.as_sleeve_order(),
                    child.intent_created_at,
                    normalize_order_id(child.order_uuid) or None,
                )
            )

    broker, ledger_mode = LEDGER_IDENTITY[sleeve.account_mode]
    order_days: dict[int, Any] = {}
    if broker == "kis" and send_keys:
        order_days = rung_order_days(
            rungs=send_keys,
            send_rows=await _kis_send_rows(session, sleeve, send_keys),
            instrument="equity_us" if sleeve.market == "equity_us" else "equity_kr",
        )
    elif broker == "kiwoom" and send_keys:
        order_days = kiwoom_rung_order_days(
            rungs=send_keys,
            send_rows=await _kiwoom_send_rows(session, sleeve, send_keys),
            account_mode=sleeve.account_mode,
        )
    by_order_id: dict[str, list[_RungRef]] = defaultdict(list)
    for order, created_at, order_id in orders:
        if order_id:
            by_order_id[order_id].append(
                _RungRef(order, created_at, order_days.get(order.rung_id))
            )

    diagnostics: Counter[str] = Counter()
    fills: list[SleeveFill] = []
    unattributed: set[int] = set()
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
        rows = list(ledger_rows.scalars().all())
        fill_days = (
            await _kiwoom_fill_days(session, sleeve, rows)
            if broker == "kiwoom"
            else None
        )
        fills, skipped, unattributed = attribute_fills(
            rows=rows,
            by_order_id=by_order_id,
            sleeve=sleeve,
            fill_days=fill_days,
        )
        diagnostics.update(skipped)
    marked = mark_children_with_unbooked_fills(
        [order for order, _created, _id in orders], child_orders, fills
    )
    return SleeveInputs(
        orders=tuple(marked),
        fills=tuple(fills),
        diagnostics=dict(diagnostics),
        unattributed_rungs=tuple(sorted(unattributed)),
    )


def mark_children_with_unbooked_fills(
    orders: Sequence[SleeveOrder],
    child_orders: Sequence[IntentChildOrder],
    fills: Sequence[SleeveFill],
) -> list[SleeveOrder]:
    """Mark a broker-final child whose fills the ledger has not all booked. Pure.

    The broker's final reading says how much the order traded; the lots are
    built from ledger rows. While the two differ -- a reconcile that has not
    caught up, a trade outside its window -- the position built from the ledger
    is wrong by the difference: after a sell it still shows coins the sleeve no
    longer owns, and on a shared account the next sell would take another
    sleeve's. The order is marked, which makes the whole sleeve's holdings and
    P&L unknown until the two quantities are equal.
    """
    final = {
        child_order_key(child.child_id): child.filled_quantity
        for child in child_orders
        if child.filled_quantity is not None
    }
    if not final:
        return list(orders)
    booked: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for fill in fills:
        booked[fill.rung_id] += fill.quantity
    return [
        replace(order, fill_unproven=True)
        if order.rung_id in final and booked[order.rung_id] != final[order.rung_id]
        else order
        for order in orders
    ]


#: Book key of the placeholder order for an unmeasured handoff whose card has
#: no rung row to mark. Far below any child order key.
_HANDOFF_PLACEHOLDER_BASE = -(2**48)


async def _mark_unmeasured_handoff_cards(
    session: AsyncSession,
    sleeve: StrategySleeve,
    orders: list[tuple[SleeveOrder, datetime, str | None]],
) -> list[tuple[SleeveOrder, datetime, str | None]]:
    """Mark the card orders of handoffs whose fills could not be proven (O2, O4).

    The handoff settle step sets ``fills_unmeasured`` when the broker's
    cumulative fill and the ledger's attributed total could not both be read, or
    differ. Until a later settle proves them equal, the lots built from the
    ledger may be missing a sell, so the card's order is marked and the book
    reports the whole sleeve as unmeasured.
    """
    result = await session.execute(
        select(ExecutionExitHandoff.id, OrderProposal.id)
        .outerjoin(
            OrderProposal, OrderProposal.proposal_id == ExecutionExitHandoff.proposal_id
        )
        .where(
            ExecutionExitHandoff.sleeve_id == sleeve.id,
            ExecutionExitHandoff.state == "card_claimed",
            ExecutionExitHandoff.fills_unmeasured.is_(True),
        )
    )
    flagged = list(result.all())
    if not flagged:
        return orders
    card_pks = {proposal_pk for _handoff, proposal_pk in flagged if proposal_pk}
    marked = [
        (
            replace(order, fill_unproven=True)
            if order.proposal_pk in card_pks
            else order,
            created_at,
            order_id,
        )
        for order, created_at, order_id in orders
    ]
    seen = {order.proposal_pk for order, _c, _i in marked if order.fill_unproven}
    for handoff_id, proposal_pk in flagged:
        if proposal_pk in seen:
            continue
        marked.append(
            (
                SleeveOrder(
                    proposal_pk=proposal_pk or 0,
                    rung_id=_HANDOFF_PLACEHOLDER_BASE - int(handoff_id),
                    symbol="",
                    side="sell",
                    state="unverified",
                    quantity=ZERO,
                    limit_price=None,
                    notional=None,
                    rung_filled_qty=None,
                    fill_unproven=True,
                ),
                sleeve.created_at,
                None,
            )
        )
    return marked


def attribute_fills(
    *,
    rows: Sequence[ExecutionLedger],
    by_order_id: dict[str, list[_RungRef]],
    sleeve: StrategySleeve,
    fill_days: Mapping[int, str] | None = None,
) -> tuple[list[SleeveFill], Counter[str], set[int]]:
    """Match ledger rows to the sleeve's rungs.

    Returns the fills, the skip counts, and the rungs that had a fill on their
    order number that could not be tied to them.

    ``fill_days`` (execution_ledger row id -> trading day) is the Kiwoom
    account's proof of a fill's day, resolved from the place ledger row that
    booked it. It is never derived from ``filled_at``: the reconcile observes
    a fill later than the trade, so a row reconciled after KST midnight would
    otherwise fall on the next day and be dropped as a reused order number.
    """
    skipped: Counter[str] = Counter()
    fills: list[SleeveFill] = []
    unattributed: set[int] = set()
    for row in rows:
        if str(row.currency or "").upper() != sleeve.currency:
            skipped["currency_mismatch"] += 1
            continue
        candidates = by_order_id.get(normalize_order_id(row.broker_order_id), [])
        symbols = _ledger_symbol_keys(row, sleeve.market)
        instrument = _instrument(row)
        matches = [
            ref
            for ref in candidates
            if ref.order.side == row.side
            and ref.order.symbol in symbols
            and row.filled_at >= ref.created_at
        ]
        if row.broker == "kiwoom":
            day = (fill_days or {}).get(row.id)
            if matches and day is None:
                # No proven day is not "another day": the fill could be the
                # rung's own, so measure nothing instead of dropping it.
                skipped["kiwoom_fill_day_unknown"] += 1
                unattributed.update(ref.order.rung_id for ref in matches)
                continue
        else:
            day = _fill_trading_day(row, instrument)
        if day is not None and matches:
            # KIS and Kiwoom reuse order numbers on other days: the fill must
            # be on the trading day of the rung's own order.
            # A candidate with an unknown day could own the fill, even when
            # another candidate's day matches: ambiguous, so never guessed.
            unknown = [ref for ref in matches if ref.order_day is None]
            if unknown:
                skipped[
                    "kiwoom_order_day_unknown"
                    if row.broker == "kiwoom"
                    else "kis_order_day_unknown"
                ] += 1
                unattributed.update(ref.order.rung_id for ref in unknown)
                continue
            same_day = [ref for ref in matches if ref.order_day == day]
            if not same_day:
                skipped["order_id_reused_on_another_day"] += 1
                continue
            matches = same_day
            if row.broker == "kiwoom" and len(matches) > 1:
                # Account + day + number is unique at the broker: two rungs
                # claiming it cannot both be right, and neither is guessed.
                skipped["kiwoom_order_match_ambiguous"] += 1
                unattributed.update(ref.order.rung_id for ref in matches)
                continue
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
        fills.append(
            SleeveFill(
                ledger_id=row.id,
                rung_id=ref.order.rung_id,
                symbol=ref.order.symbol,
                side=row.side,
                quantity=quantity,
                price=price,
                fee=fee,
                filled_at=row.filled_at,
            )
        )
    return fills, skipped, unattributed
