# app/services/trade_journal/trade_retrospective_service.py
"""ROB-474 — structured trade retrospective storage + aggregation.

Repository is the only write surface for review.trade_retrospectives.
Reads are plain module-level async functions (no class), JSON-safe, null-not-zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.models.paper_trading import PaperTrade
from app.models.review import (
    AlpacaPaperOrderLedger,
    KISLiveOrderLedger,
    KISMockOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeRetrospective,
)
from app.models.trade_journal import TradeJournal
from app.schemas.trade_retrospective import (
    VALID_ROOT_CAUSE_CLASSES,
    VALID_TRIGGER_TYPES,
    IntendedVsHappened,
    NextAction,
)
from app.services.alpaca_paper_ledger_service import (
    RECORD_KIND_EXECUTION as _ALPACA_PAPER_RECORD_KIND_EXECUTION,
)
from app.services.alpaca_paper_ledger_service import (
    RECORD_KIND_RECONCILE as _ALPACA_PAPER_RECORD_KIND_RECONCILE,
)
from app.services.brokers.kis.mock_scalping_exec.ledger_state import real_order_filter
from app.services.trade_journal.retrospective_action_repository import (
    ActionControlError,
    RetrospectiveActionRepository,
    get_control_mode,
)
from app.services.trade_journal.retrospective_query_filters import (
    VALID_OUTCOME_FILTERS,
)
from app.services.trade_journal.retrospective_query_filters import (
    kst_day_end as _kst_day_end,
)
from app.services.trade_journal.retrospective_query_filters import (
    kst_day_start as _kst_day_start,
)
from app.services.trade_journal.retrospective_query_filters import (
    sql_is_decided as _sql_is_decided,
)
from app.services.trade_journal.retrospective_query_filters import (
    sql_is_loss as _sql_is_loss,
)
from app.services.trade_journal.retrospective_query_filters import (
    sql_is_win as _sql_is_win,
)

# Sentinel: distinguishes "caller did not provide this field" (preserve on
# upsert) from "caller explicitly set None" (clear the field).
_UNSET: Any = object()

_VALID_ACCOUNT_MODES = {
    "kis_mock",
    "kiwoom_mock",
    "kis_live",
    "toss_live",
    "alpaca_paper",
    "upbit_live",
    "paper",
}
_VALID_OUTCOMES = {
    "filled",
    "partially_filled",
    "unfilled",
    "rejected",
    "cancelled",
}
_KST = ZoneInfo("Asia/Seoul")


class RetrospectiveValidationError(ValueError):
    """Raised when a retrospective payload violates a typed constraint."""


def _coerce_intended_vs_happened(raw: Any) -> dict[str, Any]:
    """Validate ``intended_vs_happened`` through the pydantic contract."""
    if not isinstance(raw, dict):
        raise RetrospectiveValidationError(
            "intended_vs_happened must be an object (dict)"
        )
    try:
        model = IntendedVsHappened.model_validate(raw)
    except ValidationError as exc:
        raise RetrospectiveValidationError(
            f"invalid intended_vs_happened: {exc.errors(include_url=False)}"
        ) from exc
    return model.model_dump(exclude_none=True)


def _coerce_next_actions(raw: Any) -> list[dict[str, Any]]:
    """Validate ``next_actions`` (a list of NextAction objects)."""
    if not isinstance(raw, list):
        raise RetrospectiveValidationError("next_actions must be a list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RetrospectiveValidationError(
                f"next_actions[{i}] must be an object (dict)"
            )
        try:
            model = NextAction.model_validate(item)
        except ValidationError as exc:
            raise RetrospectiveValidationError(
                f"invalid next_actions[{i}]: {exc.errors(include_url=False)}"
            ) from exc
        out.append(
            model.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        )
    return out


def _normalize_symbol(symbol: str, instrument_type: str) -> str:
    """Instrument-aware symbol normalization."""
    normalized = symbol.strip().upper()
    if instrument_type == "crypto":
        if normalized and "-" not in normalized:
            return f"KRW-{normalized}"
        return normalized
    if instrument_type == "equity_us":
        return to_db_symbol(normalized).upper()
    return normalized


def _infer_currency(instrument_type: str) -> str | None:
    """Best-effort settlement currency for an absolute realized_pnl amount."""
    if instrument_type in ("equity_kr", "crypto"):
        return "KRW"
    if instrument_type == "equity_us":
        return "USD"
    return None


def _to_decimal(x: float | None) -> Decimal | None:
    return Decimal(str(x)) if x is not None else None


def _avg(values: list) -> float | None:
    nums: list[Decimal] = []
    for v in values:
        if v is None:
            continue
        try:
            nums.append(Decimal(str(v)))
        except (InvalidOperation, TypeError, ValueError):
            continue
    if not nums:
        return None
    return float(sum(nums) / len(nums))


def serialize_retrospective(
    r: TradeRetrospective, *, next_actions_override: Any = _UNSET
) -> dict[str, Any]:
    return {
        "id": r.id,
        "correlation_id": r.correlation_id,
        "journal_id": r.journal_id,
        "report_uuid": r.report_uuid,
        "report_item_uuid": r.report_item_uuid,
        "symbol": r.symbol,
        "instrument_type": (
            r.instrument_type.value
            if hasattr(r.instrument_type, "value")
            else str(r.instrument_type)
        ),
        "side": r.side,
        "account_mode": r.account_mode,
        "market": r.market,
        "strategy_key": r.strategy_key,
        "outcome": r.outcome,
        "plan_price": float(r.plan_price) if r.plan_price is not None else None,
        "fill_price": float(r.fill_price) if r.fill_price is not None else None,
        "realized_pnl": float(r.realized_pnl) if r.realized_pnl is not None else None,
        "realized_pnl_currency": r.realized_pnl_currency,
        "realized_pnl_source": r.realized_pnl_source,
        "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
        "buy_fx_rate": float(r.buy_fx_rate) if r.buy_fx_rate is not None else None,
        "sell_fx_rate": float(r.sell_fx_rate) if r.sell_fx_rate is not None else None,
        "fx_pnl_krw": float(r.fx_pnl_krw) if r.fx_pnl_krw is not None else None,
        "security_pnl_usd": float(r.security_pnl_usd)
        if r.security_pnl_usd is not None
        else None,
        "security_pnl_krw": float(r.security_pnl_krw)
        if r.security_pnl_krw is not None
        else None,
        "total_pnl_krw": float(r.total_pnl_krw)
        if r.total_pnl_krw is not None
        else None,
        "fx_rate_source": r.fx_rate_source,
        "fx_pnl_accuracy": r.fx_pnl_accuracy,
        "fill_evidence_available": r.fill_evidence_available,
        "rationale": r.rationale,
        "result_summary": r.result_summary,
        "lesson": r.lesson,
        "next_strategy": r.next_strategy,
        "evidence_snapshot": r.evidence_snapshot,
        "created_by_profile": r.created_by_profile,
        "trigger_type": r.trigger_type,
        "root_cause_class": r.root_cause_class,
        "intended_vs_happened": r.intended_vs_happened,
        "next_actions": (
            r.next_actions if next_actions_override is _UNSET else next_actions_override
        ),
        "guardrail_fired": r.guardrail_fired,
        "policy_version": r.policy_version,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


class TradeRetrospectiveRepository:
    """The only write surface for review.trade_retrospectives."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_correlation_id(
        self,
        correlation_id: str,
        account_mode: str | None = None,
        *,
        for_update: bool = False,
    ) -> TradeRetrospective | None:
        stmt = select(TradeRetrospective).where(
            TradeRetrospective.correlation_id == correlation_id
        )
        if account_mode is not None:
            stmt = stmt.where(TradeRetrospective.account_mode == account_mode)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.db.execute(stmt.limit(1))
        return result.scalar_one_or_none()

    async def upsert(
        self,
        payload: dict[str, Any],
        *,
        create_defaults: dict[str, Any] | None = None,
    ) -> tuple[str, TradeRetrospective]:
        cid = payload.get("correlation_id")
        account_mode = payload.get("account_mode")
        if cid is not None:
            existing = await self.get_by_correlation_id(
                cid, account_mode, for_update=True
            )
            if existing is not None:
                for key, value in payload.items():
                    setattr(existing, key, value)
                await self.db.flush()
                return "updated", existing
        create_payload = dict(create_defaults or {})
        create_payload.update(payload)
        row = TradeRetrospective(**create_payload)
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
        except IntegrityError as exc:
            constraint = _integrity_constraint_name(exc)
            if "uq_trade_retrospectives_correlation_account" not in constraint:
                raise
            if cid is None:
                raise
            existing = await self.get_by_correlation_id(
                cid, account_mode, for_update=True
            )
            if existing is None:
                raise
            for key, value in payload.items():
                setattr(existing, key, value)
            await self.db.flush()
            return "updated", existing
        return "created", row


def _integrity_constraint_name(exc: IntegrityError) -> str:
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None)
    if name:
        return str(name)
    return str(orig or exc)


async def _load_trade_journal(
    db: AsyncSession,
    journal_id: int,
) -> TradeJournal | None:
    return (
        await db.execute(select(TradeJournal).where(TradeJournal.id == journal_id))
    ).scalar_one_or_none()


def _realized_pnl_from_journal(
    j: TradeJournal | None,
    side: str | None,
) -> Decimal | None:
    if j is None or j.entry_price is None or j.exit_price is None or j.quantity is None:
        return None
    entry = Decimal(str(j.entry_price))
    exit_price = Decimal(str(j.exit_price))
    qty = Decimal(str(j.quantity))
    direction = Decimal("-1") if (side or j.side) == "sell" else Decimal("1")
    return (exit_price - entry) * qty * direction


async def _derive_realized_pnl_from_journal(
    db: AsyncSession, journal_id: int, side: str | None
) -> Decimal | None:
    return _realized_pnl_from_journal(await _load_trade_journal(db, journal_id), side)


async def save_retrospective(
    db: AsyncSession,
    *,
    symbol: str,
    instrument_type: str,
    account_mode: str,
    outcome: str,
    side: Any = _UNSET,
    market: Any = _UNSET,
    strategy_key: Any = _UNSET,
    correlation_id: str | None = None,
    journal_id: Any = _UNSET,
    report_uuid: Any = _UNSET,
    report_item_uuid: Any = _UNSET,
    plan_price: Any = _UNSET,
    fill_price: Any = _UNSET,
    realized_pnl: Any = _UNSET,
    realized_pnl_currency: Any = _UNSET,
    pnl_pct: Any = _UNSET,
    rationale: Any = _UNSET,
    result_summary: Any = _UNSET,
    lesson: Any = _UNSET,
    next_strategy: Any = _UNSET,
    evidence_snapshot: Any = _UNSET,
    created_by_profile: Any = _UNSET,
    buy_fx_rate: Any = _UNSET,
    sell_fx_rate: Any = _UNSET,
    fx_pnl_krw: Any = _UNSET,
    security_pnl_usd: Any = _UNSET,
    security_pnl_krw: Any = _UNSET,
    total_pnl_krw: Any = _UNSET,
    fx_rate_source: Any = _UNSET,
    fx_pnl_accuracy: Any = _UNSET,
    trigger_type: Any = _UNSET,
    root_cause_class: Any = _UNSET,
    intended_vs_happened: Any = _UNSET,
    next_actions: Any = _UNSET,
    guardrail_fired: Any = _UNSET,
    policy_version: Any = _UNSET,
    actor: str = "internal:save_retrospective",
) -> tuple[str, TradeRetrospective]:
    if account_mode not in _VALID_ACCOUNT_MODES:
        raise RetrospectiveValidationError(f"invalid account_mode: {account_mode}")
    if outcome not in _VALID_OUTCOMES:
        raise RetrospectiveValidationError(
            f"invalid outcome: {outcome} "
            f"(allowed: {', '.join(sorted(_VALID_OUTCOMES))})"
        )
    if side is not _UNSET and side is not None and side not in ("buy", "sell"):
        raise RetrospectiveValidationError(f"invalid side: {side}")
    if (
        realized_pnl_currency is not _UNSET
        and realized_pnl_currency is not None
        and realized_pnl_currency not in ("KRW", "USD")
    ):
        raise RetrospectiveValidationError(
            f"invalid realized_pnl_currency: {realized_pnl_currency}"
        )

    # ROB-647 — postmortem fields. Each uses the _UNSET sentinel so an omitted
    # field is preserved across idempotent correlation_id upserts, while an
    # explicit None clears it.
    trigger_set = trigger_type is not _UNSET and trigger_type is not None
    if trigger_set and trigger_type not in VALID_TRIGGER_TYPES:
        raise RetrospectiveValidationError(
            f"invalid trigger_type: {trigger_type} "
            f"(allowed: {sorted(VALID_TRIGGER_TYPES)})"
        )
    if (
        root_cause_class is not _UNSET
        and root_cause_class is not None
        and root_cause_class not in VALID_ROOT_CAUSE_CLASSES
    ):
        raise RetrospectiveValidationError(
            f"invalid root_cause_class: {root_cause_class} "
            f"(allowed: {sorted(VALID_ROOT_CAUSE_CLASSES)})"
        )

    next_actions_value: Any = _UNSET
    if next_actions is not _UNSET and next_actions is not None:
        next_actions_value = _coerce_next_actions(next_actions)

    # Conditional obligation: setting a trigger_type demands a non-empty
    # next_actions list in the same call (backcompat: no obligation otherwise).
    if trigger_set and (next_actions_value is _UNSET or not next_actions_value):
        raise RetrospectiveValidationError(
            "next_actions is required (non-empty list) when trigger_type is set"
        )

    intended_vs_happened_value: Any = _UNSET
    if intended_vs_happened is not _UNSET and intended_vs_happened is not None:
        intended_vs_happened_value = _coerce_intended_vs_happened(intended_vs_happened)

    # Note: kiwoom_mock is legacy special case; for US/crypto live, evidence
    # should be available.
    fill_evidence_available = account_mode != "kiwoom_mock"
    if not fill_evidence_available and (
        (realized_pnl is not _UNSET and realized_pnl is not None)
        or (fill_price is not _UNSET and fill_price is not None)
    ):
        raise RetrospectiveValidationError(
            f"{account_mode} cannot read fills (ROB-460); "
            "realized_pnl/fill_price not allowed"
        )

    create_defaults: dict[str, Any] = {}
    journal_row = (
        await _load_trade_journal(db, journal_id)
        if journal_id is not _UNSET
        and journal_id is not None
        and fill_evidence_available
        else None
    )

    realized_pnl_value: Any = _UNSET
    realized_pnl_source: Any = _UNSET
    derived_realized_pnl: Decimal | None = None
    if realized_pnl is not _UNSET:
        realized_pnl_value = _to_decimal(realized_pnl)
        realized_pnl_source = (
            "caller_supplied" if realized_pnl_value is not None else None
        )
    elif (
        journal_id is not _UNSET and journal_id is not None and fill_evidence_available
    ):
        derived = _realized_pnl_from_journal(
            journal_row, None if side is _UNSET else side
        )
        if derived is not None:
            derived_realized_pnl = derived
            create_defaults["realized_pnl"] = derived
            create_defaults["realized_pnl_source"] = "derived_from_journal"

    if journal_row is not None:
        if buy_fx_rate is _UNSET and journal_row.buy_fx_rate is not None:
            create_defaults["buy_fx_rate"] = journal_row.buy_fx_rate
        if sell_fx_rate is _UNSET and journal_row.sell_fx_rate is not None:
            create_defaults["sell_fx_rate"] = journal_row.sell_fx_rate
        if fx_pnl_krw is _UNSET and journal_row.fx_pnl_krw is not None:
            create_defaults["fx_pnl_krw"] = journal_row.fx_pnl_krw
        if security_pnl_usd is _UNSET and journal_row.security_pnl_usd is not None:
            create_defaults["security_pnl_usd"] = journal_row.security_pnl_usd
        if security_pnl_krw is _UNSET and journal_row.security_pnl_krw is not None:
            create_defaults["security_pnl_krw"] = journal_row.security_pnl_krw
        if total_pnl_krw is _UNSET and journal_row.total_pnl_krw is not None:
            create_defaults["total_pnl_krw"] = journal_row.total_pnl_krw
        if fx_rate_source is _UNSET and journal_row.fx_rate_source is not None:
            create_defaults["fx_rate_source"] = journal_row.fx_rate_source
        if fx_pnl_accuracy is _UNSET and journal_row.fx_pnl_accuracy is not None:
            create_defaults["fx_pnl_accuracy"] = journal_row.fx_pnl_accuracy

    if (
        realized_pnl_value is not _UNSET
        and realized_pnl_value is not None
        and (realized_pnl_currency is _UNSET or realized_pnl_currency is None)
    ):
        realized_pnl_currency = _infer_currency(instrument_type)
        if realized_pnl_currency is None:
            raise RetrospectiveValidationError(
                "realized_pnl requires realized_pnl_currency "
                f"(could not infer from instrument_type={instrument_type})"
            )
    elif derived_realized_pnl is not None and (
        realized_pnl_currency is _UNSET or realized_pnl_currency is None
    ):
        inferred_currency = _infer_currency(instrument_type)
        if inferred_currency is None:
            raise RetrospectiveValidationError(
                "derived realized_pnl requires realized_pnl_currency "
                f"(could not infer from instrument_type={instrument_type})"
            )
        create_defaults["realized_pnl_currency"] = inferred_currency

    payload: dict[str, Any] = {
        "symbol": _normalize_symbol(symbol, instrument_type),
        "instrument_type": instrument_type,
        "account_mode": account_mode,
        "outcome": outcome,
        "correlation_id": correlation_id,
        "fill_evidence_available": fill_evidence_available,
    }
    raw_optional_payload = {
        "side": side,
        "market": market,
        "strategy_key": strategy_key,
        "journal_id": journal_id,
        "report_uuid": report_uuid,
        "report_item_uuid": report_item_uuid,
        "realized_pnl": realized_pnl_value,
        "realized_pnl_currency": realized_pnl_currency,
        "realized_pnl_source": realized_pnl_source,
        "rationale": rationale,
        "result_summary": result_summary,
        "lesson": lesson,
        "next_strategy": next_strategy,
        "evidence_snapshot": evidence_snapshot,
        "created_by_profile": created_by_profile,
        "fx_rate_source": fx_rate_source,
        "fx_pnl_accuracy": fx_pnl_accuracy,
    }
    payload.update(
        {
            key: value
            for key, value in raw_optional_payload.items()
            if value is not _UNSET
        }
    )
    decimal_optional_payload = {
        "plan_price": plan_price,
        "fill_price": fill_price,
        "pnl_pct": pnl_pct,
        "buy_fx_rate": buy_fx_rate,
        "sell_fx_rate": sell_fx_rate,
        "fx_pnl_krw": fx_pnl_krw,
        "security_pnl_usd": security_pnl_usd,
        "security_pnl_krw": security_pnl_krw,
        "total_pnl_krw": total_pnl_krw,
    }
    payload.update(
        {
            key: _to_decimal(value)
            for key, value in decimal_optional_payload.items()
            if value is not _UNSET
        }
    )

    # ROB-880: In canonical mode the write-fence trigger rejects direct
    # next_actions writes, so the payload excludes it and the repository
    # reconciles children + writes the projection with the GUC marker.
    ctrl_mode = await get_control_mode(db)
    _is_canonical = ctrl_mode == "canonical"

    if next_actions_value is not _UNSET and not _is_canonical:
        shadow_actions: list[dict[str, Any]] = []
        for index, action_item in enumerate(next_actions_value):
            if action_item.get("status") in {"obsolete", "expired"}:
                raise RetrospectiveValidationError(
                    f"next_actions[{index}] status is canonical-only in shadow mode"
                )
            if "action_id" in action_item or "version" in action_item:
                raise RetrospectiveValidationError(
                    f"next_actions[{index}] canonical identity is invalid in shadow mode"
                )
            stored_item = dict(action_item)
            # force_new is request intent. Keep only the stable creation_key in
            # the shadow JSON so cutover can preserve retry idempotency.
            stored_item.pop("force_new", None)
            shadow_actions.append(stored_item)
        next_actions_value = shadow_actions

    # ROB-647 — only include provided postmortem fields so an idempotent
    # re-save that omits them does not clobber prior values (partial-update).
    if trigger_type is not _UNSET:
        payload["trigger_type"] = trigger_type
    if root_cause_class is not _UNSET:
        payload["root_cause_class"] = root_cause_class
    if intended_vs_happened is not _UNSET:
        payload["intended_vs_happened"] = (
            None if intended_vs_happened_value is _UNSET else intended_vs_happened_value
        )
    if next_actions is not _UNSET and not _is_canonical:
        payload["next_actions"] = (
            None if next_actions_value is _UNSET else next_actions_value
        )
    if guardrail_fired is not _UNSET:
        payload["guardrail_fired"] = guardrail_fired
    if policy_version is not _UNSET:
        payload["policy_version"] = policy_version

    repo = TradeRetrospectiveRepository(db)
    status, retro = await repo.upsert(payload, create_defaults=create_defaults)

    if _is_canonical and next_actions is not _UNSET and next_actions is not None:
        action_repo = RetrospectiveActionRepository(db)
        await action_repo.reconcile_actions(
            retro.id,
            next_actions_value if next_actions_value is not _UNSET else [],
            actor=actor,
            control_mode=ctrl_mode,
        )

    return status, retro


def _kst_date_str(dt: datetime) -> str:
    return dt.astimezone(_KST).date().isoformat()


async def get_retrospectives(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    account_mode: str | None = None,
    strategy_key: str | None = None,
    market: str | None = None,
    correlation_id: str | None = None,
    days: int | None = None,
    trigger_type: str | None = None,
    root_cause_class: str | None = None,
    outcome_filter: str | None = None,
    symbol_search: str | None = None,
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    filters = []
    if symbol is not None:
        filters.append(TradeRetrospective.symbol == symbol.strip().upper())
    if account_mode is not None:
        filters.append(TradeRetrospective.account_mode == account_mode)
    if strategy_key is not None:
        filters.append(TradeRetrospective.strategy_key == strategy_key)
    if market is not None:
        filters.append(TradeRetrospective.market == market)
    if correlation_id is not None:
        filters.append(TradeRetrospective.correlation_id == correlation_id)
    if trigger_type is not None:
        filters.append(TradeRetrospective.trigger_type == trigger_type)
    if root_cause_class is not None:
        filters.append(TradeRetrospective.root_cause_class == root_cause_class)
    if symbol_search is not None and symbol_search.strip():
        filters.append(
            TradeRetrospective.symbol.ilike(_prefix_like(symbol_search), escape="\\")
        )
    # kst_date_from/to reuse the same `_kst_day_start`/`_kst_day_end` KST
    # calendar-day helpers as build_retrospective_aggregate (§ plan 3.3/4) —
    # do not reimplement the day-boundary math here.
    if kst_date_from is not None:
        filters.append(TradeRetrospective.created_at >= _kst_day_start(kst_date_from))
    if kst_date_to is not None:
        filters.append(TradeRetrospective.created_at <= _kst_day_end(kst_date_to))
    if outcome_filter is not None:
        if outcome_filter == "win":
            filters.append(_sql_is_win())
        elif outcome_filter == "loss":
            filters.append(_sql_is_loss())
        elif outcome_filter == "decided":
            filters.append(_sql_is_decided())
        else:
            raise RetrospectiveValidationError(
                f"invalid outcome_filter: {outcome_filter} "
                f"(allowed: {sorted(VALID_OUTCOME_FILTERS)})"
            )
    if days is not None:
        filters.append(
            TradeRetrospective.created_at >= now_kst() - timedelta(days=days)
        )
    total = (
        await db.execute(
            select(func.count()).select_from(TradeRetrospective).where(*filters)
        )
    ).scalar_one()
    stmt = (
        select(TradeRetrospective)
        .where(*filters)
        .order_by(TradeRetrospective.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()
    actions_by_parent = await RetrospectiveActionRepository(db).read_actions_many(rows)
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    return {
        "entries": [
            serialize_retrospective(
                r,
                next_actions_override=actions_by_parent[r.id],
            )
            for r in rows
        ],
        "summary": {"count": len(rows), "by_outcome": by_outcome, "total": int(total)},
    }


async def get_retrospective_by_id(
    db: AsyncSession, retro_id: int
) -> TradeRetrospective | None:
    """ROB-800 — fetch a single retrospective by primary key (read-only)."""
    result = await db.execute(
        select(TradeRetrospective).where(TradeRetrospective.id == retro_id).limit(1)
    )
    return result.scalar_one_or_none()


async def get_open_next_actions(
    db: AsyncSession,
    *,
    market: str | None = None,
    symbol: str | None = None,
    statuses: frozenset[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Flatten incomplete next_actions across recent retrospectives.

    In shadow mode: bounded scan of parent JSONB.
    In canonical mode: queries the child ledger directly (no scan limit needed).
    """
    ctrl_mode = await get_control_mode(db)

    if ctrl_mode == "canonical":
        return await _get_open_next_actions_canonical(
            db, market=market, symbol=symbol, statuses=statuses, limit=limit
        )

    filters: list = []
    if market is not None:
        filters.append(TradeRetrospective.market == market)
    if symbol is not None:
        filters.append(TradeRetrospective.symbol == symbol.strip().upper())

    # ROB-667: pre-select (in SQL) only retrospectives that have at least one
    # actionable, non-done next_action, so the recency bound below caps the
    # RELEVANT set instead of silently dropping open actions behind newer
    # done-only rows. jsonb_typeof guard avoids errors on legacy non-array rows.
    if statuses is None:
        status_clause = "COALESCE(elem->>'status','') <> 'done'"
        bind: dict = {}
    else:
        status_clause = "elem->>'status' = ANY(:na_statuses)"
        bind = {"na_statuses": list(statuses)}

    has_open_action = text(
        f"""
        jsonb_typeof(trade_retrospectives.next_actions) = 'array'
        AND EXISTS (
            SELECT 1
            FROM jsonb_array_elements(trade_retrospectives.next_actions) AS elem
            WHERE elem ? 'action'
              AND {status_clause}
        )
        """
    )
    if bind:
        has_open_action = has_open_action.bindparams(**bind)
    filters.append(has_open_action)

    stmt = (
        select(TradeRetrospective)
        .where(*filters)
        .order_by(TradeRetrospective.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()

    def _incomplete(status: str | None) -> bool:
        if statuses is None:
            return status != "done"
        return status in statuses

    items: list[dict[str, Any]] = []
    for r in rows:
        for action in r.next_actions or []:
            if not isinstance(action, dict) or not action.get("action"):
                continue
            if not _incomplete(action.get("status")):
                continue
            items.append(
                {
                    "action": action.get("action"),
                    "owner": action.get("owner"),
                    "issue_id": action.get("issue_id"),
                    "status": action.get("status"),
                    "due_kst_date": action.get("due_kst_date"),
                    "symbol": r.symbol,
                    "market": r.market,
                    "retro_id": r.id,
                    "correlation_id": r.correlation_id,
                    "trigger_type": r.trigger_type,
                    "realized_pnl": float(r.realized_pnl)
                    if r.realized_pnl is not None
                    else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )
    return {"items": items, "count": len(items), "scan_limit": limit}


async def _get_open_next_actions_canonical(
    db: AsyncSession,
    *,
    market: str | None = None,
    symbol: str | None = None,
    statuses: frozenset[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Canonical-mode implementation of get_open_next_actions.

    Reads from the child ledger and formats results in the legacy response
    shape, with action_id and version added additively.
    """
    repo = RetrospectiveActionRepository(db)
    canonical_statuses = statuses
    if statuses is not None and "done" in statuses:
        canonical_statuses = (statuses - {"done"}) | {
            "done",
            "obsolete",
            "expired",
        }
    result = await repo.query_actions(
        statuses=canonical_statuses,
        market=market,
        symbol=symbol,
        limit=None,
        offset=0,
    )
    items: list[dict[str, Any]] = []
    for item in result["items"]:
        canonical_status = item.get("status")
        legacy_status = (
            "done" if canonical_status in {"obsolete", "expired"} else canonical_status
        )
        items.append(
            {
                "action": item["action"],
                "owner": item.get("owner"),
                "issue_id": item.get("issue_id"),
                "status": legacy_status,
                "terminal_status": (
                    canonical_status
                    if canonical_status in {"obsolete", "expired"}
                    else None
                ),
                "due_kst_date": item.get("due_kst_date"),
                "symbol": item.get("symbol"),
                "market": item.get("market"),
                "retro_id": item.get("retrospective_id"),
                "correlation_id": item.get("correlation_id"),
                "trigger_type": item.get("trigger_type"),
                "realized_pnl": item.get("realized_pnl"),
                "created_at": item.get("created_at"),
                "action_id": item.get("action_id"),
                "version": item.get("version"),
                "overdue": item.get("overdue", False),
            }
        )
    return {"items": items, "count": len(items), "scan_limit": 0}


async def get_canonical_actions(
    db: AsyncSession,
    *,
    statuses: frozenset[str] | None = None,
    market: str | None = None,
    symbol: str | None = None,
    symbol_search: str | None = None,
    owner: str | None = None,
    issue_id: str | None = None,
    overdue_only: bool = False,
    trigger_type: str | None = None,
    outcome_filter: str | None = None,
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    due_before: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Canonical action query with pagination, filters, and overdue-first ordering."""
    mode = await get_control_mode(db)
    if mode != "canonical":
        raise ActionControlError(
            f"canonical action reader is unavailable while mode is {mode}"
        )
    repo = RetrospectiveActionRepository(db)
    return await repo.query_actions(
        statuses=statuses,
        market=market,
        symbol=symbol,
        symbol_search=symbol_search,
        owner=owner,
        issue_id=issue_id,
        overdue_only=overdue_only,
        trigger_type=trigger_type,
        outcome_filter=outcome_filter,
        kst_date_from=kst_date_from,
        kst_date_to=kst_date_to,
        due_before=due_before,
        limit=limit,
        offset=offset,
    )


def _is_win(r: TradeRetrospective) -> bool:
    if r.realized_pnl is not None:
        return r.realized_pnl > 0
    return r.pnl_pct is not None and r.pnl_pct > 0


def _is_decided(r: TradeRetrospective) -> bool:
    return r.realized_pnl is not None or r.pnl_pct is not None


def _prefix_like(raw: str) -> str:
    """Prefix-match token for `Column.ilike(..., escape="\\\\")`.

    Symbols are stored upper-cased (see `_normalize_symbol`), so the search
    token is upper-cased too. `%`/`_` are LIKE wildcards — escape them so a
    literal search token (e.g. a symbol containing `_`) does not accidentally
    widen the match.
    """
    escaped = (
        raw.strip()
        .upper()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"{escaped}%"


async def build_retrospective_aggregate(
    db: AsyncSession,
    *,
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    account_mode: str | None = None,
    market: str | None = None,
    strategy_key: str | None = None,
    group_by: str = "strategy",
) -> dict[str, Any]:
    if group_by not in ("strategy", "day", "trigger_type", "root_cause"):
        group_by = "strategy"
    # Process dimensions (trigger_type/root_cause) are about *why* an order
    # resolved, not PnL — so no-fill-evidence rows (e.g. rejected/cancelled)
    # must be included, not excluded as they are for the PnL-oriented dims.
    include_no_evidence = group_by in ("trigger_type", "root_cause")
    filters = []
    if account_mode is not None:
        filters.append(TradeRetrospective.account_mode == account_mode)
    if market is not None:
        filters.append(TradeRetrospective.market == market)
    if strategy_key is not None:
        filters.append(TradeRetrospective.strategy_key == strategy_key)
    if kst_date_from is not None:
        filters.append(TradeRetrospective.created_at >= _kst_day_start(kst_date_from))
    if kst_date_to is not None:
        filters.append(TradeRetrospective.created_at <= _kst_day_end(kst_date_to))

    rows = (
        (await db.execute(select(TradeRetrospective).where(*filters))).scalars().all()
    )

    groups: dict[str, list[TradeRetrospective]] = {}
    excluded_no_evidence = 0
    for r in rows:
        if not r.fill_evidence_available and not include_no_evidence:
            excluded_no_evidence += 1
            continue
        if group_by == "strategy":
            key = r.strategy_key or "no_strategy"
        elif group_by == "day":
            key = _kst_date_str(r.created_at)
        elif group_by == "trigger_type":
            key = r.trigger_type or "no_trigger_type"
        else:  # root_cause
            key = r.root_cause_class or "no_root_cause"
        groups.setdefault(key, []).append(r)

    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        decided = [it for it in items if _is_decided(it)]
        wins = sum(1 for it in decided if _is_win(it))
        misses = len(decided) - wins
        realized_sum: dict[str, float] = {}
        for it in items:
            if it.realized_pnl is not None and it.realized_pnl_currency:
                realized_sum[it.realized_pnl_currency] = realized_sum.get(
                    it.realized_pnl_currency, 0.0
                ) + float(it.realized_pnl)
        fx_pnl_krw_sum = sum(
            float(it.fx_pnl_krw) for it in items if it.fx_pnl_krw is not None
        )
        total_pnl_krw_sum = sum(
            float(it.total_pnl_krw) for it in items if it.total_pnl_krw is not None
        )
        by_outcome: dict[str, int] = {}
        by_trigger_type: dict[str, int] = {}
        by_root_cause_class: dict[str, int] = {}
        for it in items:
            by_outcome[it.outcome] = by_outcome.get(it.outcome, 0) + 1
            if it.trigger_type:
                by_trigger_type[it.trigger_type] = (
                    by_trigger_type.get(it.trigger_type, 0) + 1
                )
            if it.root_cause_class:
                by_root_cause_class[it.root_cause_class] = (
                    by_root_cause_class.get(it.root_cause_class, 0) + 1
                )
        out.append(
            {
                "group": key,
                "sample_size": len(items),
                "wins": wins,
                "misses": misses,
                "win_rate_pct": (wins / len(decided) * 100.0) if decided else None,
                "avg_pnl_pct": _avg([it.pnl_pct for it in items]),
                "realized_pnl_sum": realized_sum,
                "fx_pnl_krw_sum": fx_pnl_krw_sum,
                "total_pnl_krw_sum": total_pnl_krw_sum,
                "by_outcome": by_outcome,
                "by_trigger_type": by_trigger_type,
                "by_root_cause_class": by_root_cause_class,
            }
        )
    out.sort(key=lambda g: -g["sample_size"])
    return {
        "group_by": group_by,
        "groups": out,
        "excluded_no_fill_evidence": excluded_no_evidence,
    }


# ROB-647/ROB-661/ROB-665 — terminal (lifecycle-complete) statuses per live
# ledger, split into a DEFAULT group (always due for a retrospective: filled /
# rejected / anomaly) and a CANCEL-family group (DAY expiry collapses to
# `cancelled`, plus Toss cancel/replace rejections). Cancel-family is noise by
# default (grid re-placement churn) and only surfaces when include_cancelled=True.
# Non-terminal states (accepted / pending / partial / replaced) stay omitted —
# they may still change.
# ROB-665 item 2: KIS reconcile writes raw status="expired" to the ledger (the
# expired→cancelled collapse in the ROB-661 spec applies only to lifecycle_state).
# Without "expired" here the status.in_() scan silently drops those rows from
# both modes and the excluded count. Treat it as cancel-family.
_KIS_LIVE_DEFAULT_TERMINAL = frozenset({"filled", "rejected", "anomaly"})
_KIS_LIVE_CANCEL_TERMINAL = frozenset({"cancelled", "expired"})
_GENERIC_LIVE_DEFAULT_TERMINAL = frozenset({"filled", "rejected", "anomaly"})
_GENERIC_LIVE_CANCEL_TERMINAL = frozenset({"cancelled"})
_TOSS_DEFAULT_TERMINAL = frozenset({"filled", "rejected", "anomaly"})
_TOSS_CANCEL_TERMINAL = frozenset({"cancelled", "cancel_rejected", "replace_rejected"})
# ROB-730: kis_mock terminality lives in `lifecycle_state` (not `status`, which is
# constrained to accepted/rejected/unknown). A holdings-delta fill lands as
# `fill`/`reconciled`; a send reject as `failed`; ambiguity as `anomaly`.
# `cancelled`/`stale` (never-confirmed timeout / operator cancel) are cancel-family
# churn, hidden by default like live expiry.
_KIS_MOCK_DEFAULT_TERMINAL = frozenset({"fill", "reconciled", "failed", "anomaly"})
_KIS_MOCK_CANCEL_TERMINAL = frozenset({"cancelled", "stale"})
# ROB-954: Alpaca Paper terminality is keyed on `lifecycle_state` (the ROB-90
# canonical state machine), never the raw broker `order_status` — same
# convention as kis_mock above. ROB-953 reconcile books proven fills into
# filled/position_reconciled/closed/final_reconciled; ROB-994 additionally
# terminalizes known broker-terminal zero-fill orders (expired/canceled/
# rejected evidence) into `anomaly`, or into `canceled` when cancel evidence
# is present — so both belong in the terminal set or those rows never surface.
# NOTE spelling: the alpaca ledger's cancel state is `canceled` (one L) — do
# not conflate with the `cancelled` (two L) spelling used by the KIS/generic/
# Toss ledgers above; they are deliberately distinct strings.
_ALPACA_PAPER_DEFAULT_TERMINAL = frozenset(
    {"filled", "position_reconciled", "closed", "final_reconciled", "anomaly"}
)
_ALPACA_PAPER_CANCEL_TERMINAL = frozenset({"canceled"})
# ROB-954 round-2: record_kind alone is not a stable identity for "the
# execution row". AlpacaPaperLedgerService.record_final_reconcile() flips
# record_kind from 'execution' to 'reconcile' on the *same* row (an in-place
# UPDATE, not a second INSERT) at the exact moment it books
# lifecycle_state='final_reconciled' — every other terminal lifecycle_state
# (filled/position_reconciled/closed/anomaly/canceled) is reached through
# record_status/record_cancel/record_position_snapshot/record_close/
# record_submit_failure, none of which ever touch record_kind, so they stay
# 'execution'. Scanning only 'execution' therefore made every
# final_reconciled roundtrip invisible to the due-list. 'reconcile' is safe
# to add here: it is never produced by an INSERT (only this one UPDATE path),
# so it cannot introduce a second row for an order already counted under
# 'execution'. plan/preview/validation_attempt remain excluded — audited
# against every AlpacaPaperLedgerService.record_* method, none of them ever
# set a terminal lifecycle_state on those record_kinds.
_ALPACA_PAPER_RECORD_KINDS = frozenset(
    {_ALPACA_PAPER_RECORD_KIND_EXECUTION, _ALPACA_PAPER_RECORD_KIND_RECONCILE}
)

_KIS_LIVE_TERMINAL = _KIS_LIVE_DEFAULT_TERMINAL | _KIS_LIVE_CANCEL_TERMINAL
_GENERIC_LIVE_TERMINAL = _GENERIC_LIVE_DEFAULT_TERMINAL | _GENERIC_LIVE_CANCEL_TERMINAL
_TOSS_TERMINAL = _TOSS_DEFAULT_TERMINAL | _TOSS_CANCEL_TERMINAL
_KIS_MOCK_TERMINAL = _KIS_MOCK_DEFAULT_TERMINAL | _KIS_MOCK_CANCEL_TERMINAL
_ALPACA_PAPER_TERMINAL = _ALPACA_PAPER_DEFAULT_TERMINAL | _ALPACA_PAPER_CANCEL_TERMINAL

# Statuses hidden from `pending` unless include_cancelled=True. Disjoint from the
# DEFAULT statuses (note: `rejected` is DEFAULT; `cancel_rejected` /
# `replace_rejected` are cancel-family).
_CANCEL_FAMILY_STATUSES = (
    _KIS_LIVE_CANCEL_TERMINAL
    | _GENERIC_LIVE_CANCEL_TERMINAL
    | _TOSS_CANCEL_TERMINAL
    | _KIS_MOCK_CANCEL_TERMINAL
    | _ALPACA_PAPER_CANCEL_TERMINAL
)
# Bound per-ledger scan so a wide window cannot load unbounded rows.
_PENDING_LEDGER_FETCH_CAP = 1000


async def _covered_keys(
    db: AsyncSession,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Account-scoped provenance keys already carrying a retrospective."""
    rows = (
        await db.execute(
            select(
                TradeRetrospective.correlation_id,
                TradeRetrospective.account_mode,
                TradeRetrospective.report_item_uuid,
            )
        )
    ).all()
    covered_cids = {
        (str(cid), str(account_mode))
        for cid, account_mode, _ in rows
        if cid and account_mode
    }
    covered_item_uuids = {
        (str(uid), str(account_mode))
        for _, account_mode, uid in rows
        if uid and account_mode
    }
    return covered_cids, covered_item_uuids


def _pending_entry(
    *,
    ledger: str,
    account_mode: str,
    market: str,
    instrument_type: str,
    symbol: str,
    side: str | None,
    status: str,
    order_ref: str | None,
    report_item_uuid: Any,
    trade_date: datetime | None,
    row_id: int,
    suggested_correlation_id: str | None = None,
    suggested_trigger_type: str | None = None,
) -> dict[str, Any]:
    ref = order_ref or f"id:{row_id}"
    return {
        "ledger": ledger,
        "ledger_row_id": row_id,
        "account_mode": account_mode,
        "market": market,
        "instrument_type": instrument_type,
        "symbol": symbol,
        "side": side,
        "status": status,
        "order_ref": order_ref,
        "report_item_uuid": str(report_item_uuid) if report_item_uuid else None,
        "trade_date_kst": trade_date.astimezone(_KST).isoformat()
        if trade_date
        else None,
        "suggested_correlation_id": suggested_correlation_id or f"{ledger}:{ref}",
        "suggested_trigger_type": suggested_trigger_type,
    }


def _is_covered(
    entry: dict[str, Any],
    covered_cids: set[tuple[str, str]],
    covered_item_uuids: set[tuple[str, str]],
) -> bool:
    if (
        entry["report_item_uuid"]
        and (
            entry["report_item_uuid"],
            entry["account_mode"],
        )
        in covered_item_uuids
    ):
        return True
    key = (entry["suggested_correlation_id"], entry["account_mode"])
    return key in covered_cids


async def build_retrospective_pending(
    db: AsyncSession,
    *,
    kst_date_from: str,
    kst_date_to: str,
    account_mode: str | None = None,
    limit: int = 100,
    include_cancelled: bool = False,
) -> dict[str, Any]:
    """List terminal ledger orders lacking a retrospective.

    Read-only. Scans review.{kis_live_order_ledger, live_order_ledger,
    toss_live_order_ledger, kis_mock_order_ledger, alpaca_paper_order_ledger}
    plus paper_trades for lifecycle-terminal rows in the KST trade_date window,
    then subtracts rows already covered by a retrospective (matched on
    report_item_uuid or the suggested correlation_id). Mirrors the ROB-120
    coverage pattern (terminal-without-artifact). Filter to one source with
    account_mode (e.g. "kis_mock" for the counterfactual mock loop, "kis_live"/
    "toss_live"/"upbit_live"/"paper"/"alpaca_paper" otherwise); None scans all.

    Cancel-family rows (cancelled / cancel_rejected / replace_rejected) are
    hidden unless include_cancelled=True; the hidden count is reported in
    excluded_by_filter.
    """
    window_start = _kst_day_start(kst_date_from)
    window_end = _kst_day_end(kst_date_to)
    covered_cids, covered_item_uuids = await _covered_keys(db)

    pending: list[dict[str, Any]] = []
    scanned = 0

    # 1. KIS live (KR domestic)
    if account_mode in (None, "kis_live"):
        stmt = (
            select(KISLiveOrderLedger)
            .where(
                KISLiveOrderLedger.status.in_(_KIS_LIVE_TERMINAL),
                KISLiveOrderLedger.trade_date >= window_start,
                KISLiveOrderLedger.trade_date <= window_end,
            )
            .order_by(KISLiveOrderLedger.trade_date.desc())
            .limit(_PENDING_LEDGER_FETCH_CAP)
        )
        for row in (await db.execute(stmt)).scalars().all():
            scanned += 1
            entry = _pending_entry(
                ledger="kis_live",
                account_mode=row.account_mode,
                market="kr",
                instrument_type=row.instrument_type,
                symbol=row.symbol,
                side=row.side,
                status=row.status,
                order_ref=row.order_no,
                report_item_uuid=row.report_item_uuid,
                trade_date=row.trade_date,
                row_id=row.id,
            )
            if not _is_covered(entry, covered_cids, covered_item_uuids):
                pending.append(entry)

    # 2. Generic live ledger (US KIS + crypto Upbit)
    if account_mode in (None, "kis_live", "upbit_live"):
        gfilters = [
            LiveOrderLedger.status.in_(_GENERIC_LIVE_TERMINAL),
            LiveOrderLedger.trade_date >= window_start,
            LiveOrderLedger.trade_date <= window_end,
        ]
        if account_mode is not None:
            gfilters.append(LiveOrderLedger.account_scope == account_mode)
        stmt = (
            select(LiveOrderLedger)
            .where(*gfilters)
            .order_by(LiveOrderLedger.trade_date.desc())
            .limit(_PENDING_LEDGER_FETCH_CAP)
        )
        for row in (await db.execute(stmt)).scalars().all():
            scanned += 1
            instrument_type = "equity_us" if row.market == "us" else "crypto"
            entry = _pending_entry(
                ledger="live",
                account_mode=row.account_scope,
                market=row.market,
                instrument_type=instrument_type,
                symbol=row.symbol,
                side=row.side,
                status=row.status,
                order_ref=row.order_no,
                report_item_uuid=row.report_item_uuid,
                trade_date=row.trade_date,
                row_id=row.id,
            )
            if not _is_covered(entry, covered_cids, covered_item_uuids):
                pending.append(entry)

    # 3. Toss live ledger (place operations only; modify/cancel are follow-ups)
    if account_mode in (None, "toss_live"):
        stmt = (
            select(TossLiveOrderLedger)
            .where(
                TossLiveOrderLedger.status.in_(_TOSS_TERMINAL),
                TossLiveOrderLedger.operation_kind == "place",
                TossLiveOrderLedger.trade_date >= window_start,
                TossLiveOrderLedger.trade_date <= window_end,
            )
            .order_by(TossLiveOrderLedger.trade_date.desc())
            .limit(_PENDING_LEDGER_FETCH_CAP)
        )
        for row in (await db.execute(stmt)).scalars().all():
            scanned += 1
            instrument_type = "equity_kr" if row.market == "kr" else "equity_us"
            entry = _pending_entry(
                ledger="toss_live",
                account_mode=row.account_mode,
                market=row.market,
                instrument_type=instrument_type,
                symbol=row.symbol,
                side=row.side,
                status=row.status,
                order_ref=row.broker_order_id or row.client_order_id,
                report_item_uuid=row.report_item_uuid,
                trade_date=row.trade_date,
                row_id=row.id,
            )
            if not _is_covered(entry, covered_cids, covered_item_uuids):
                pending.append(entry)

    # 4. Paper trades (ROB-705) — every paper_trades row is a fill (no status
    # column); window-filter only. Loss-making sells carry a stop_loss hint.
    if account_mode in (None, "paper"):
        paper_rows = (
            (
                await db.execute(
                    select(PaperTrade)
                    .where(
                        PaperTrade.executed_at >= window_start,
                        PaperTrade.executed_at <= window_end,
                    )
                    .order_by(PaperTrade.executed_at.desc())
                    .limit(_PENDING_LEDGER_FETCH_CAP)
                )
            )
            .scalars()
            .all()
        )
        for r in paper_rows:
            scanned += 1
            itype = r.instrument_type.value
            market = "crypto" if itype == "crypto" else itype.removeprefix("equity_")
            trig = (
                "stop_loss"
                if r.side == "sell"
                and r.realized_pnl is not None
                and r.realized_pnl < 0
                else None
            )
            entry = _pending_entry(
                ledger="paper_trades",
                account_mode="paper",
                market=market,
                instrument_type=itype,
                symbol=r.symbol,
                side=r.side,
                status="filled",
                order_ref=r.correlation_id or f"paper_trade:{r.id}",
                report_item_uuid=None,
                trade_date=r.executed_at,
                row_id=r.id,
                suggested_correlation_id=(r.correlation_id or f"paper_trade:{r.id}"),
                suggested_trigger_type=trig,
            )
            if not _is_covered(entry, covered_cids, covered_item_uuids):
                pending.append(entry)

    # 5. KIS mock ledger (ROB-730/ROB-734) — counterfactual learning loop.
    # Terminality is keyed on lifecycle_state (the mock `status` column only holds
    # the send-time accepted/rejected/unknown).
    if account_mode in (None, "kis_mock"):
        stmt = (
            select(KISMockOrderLedger)
            .where(
                KISMockOrderLedger.lifecycle_state.in_(_KIS_MOCK_TERMINAL),
                KISMockOrderLedger.trade_date >= window_start,
                KISMockOrderLedger.trade_date <= window_end,
                # ROB-843 P2: never retrospect a control/reservation row.
                real_order_filter(),
            )
            .order_by(KISMockOrderLedger.trade_date.desc())
            .limit(_PENDING_LEDGER_FETCH_CAP)
        )
        for row in (await db.execute(stmt)).scalars().all():
            scanned += 1
            itype = row.instrument_type.value
            market = "crypto" if itype == "crypto" else itype.removeprefix("equity_")
            entry = _pending_entry(
                ledger="kis_mock",
                account_mode="kis_mock",
                market=market,
                instrument_type=itype,
                symbol=row.symbol,
                side=row.side,
                status=row.lifecycle_state,
                order_ref=row.order_no,
                report_item_uuid=row.report_item_uuid,
                trade_date=row.trade_date,
                row_id=row.id,
                suggested_correlation_id=row.correlation_id,
            )
            if not _is_covered(entry, covered_cids, covered_item_uuids):
                pending.append(entry)

    # 6. Alpaca paper ledger (ROB-954) — US equity/crypto paper execution loop
    # (ROB-953 reconcile + ROB-994 zero-fill terminalization). Scoped to
    # record_kind IN {execution, reconcile} — see _ALPACA_PAPER_RECORD_KINDS
    # for why 'reconcile' belongs alongside 'execution'. plan/preview/
    # validation_attempt record_kinds are bookkeeping rows sharing the same
    # (client_order_id, record_kind) unique-slot family, not a second order —
    # scanning them would double-surface a single execution as multiple
    # due-list entries.
    if account_mode in (None, "alpaca_paper"):
        # ROB-954 round-2: window anchors on `updated_at`, not `created_at`.
        # `created_at` is claim time and never changes again; a row claimed
        # days ago that only becomes terminal today was invisible in today's
        # window under the old anchor (the REGN long-stall repro — see
        # test_stale_created_at_with_recent_terminal_transition_surfaces_in_narrow_window).
        # `updated_at` (NOT NULL, onupdate=func.now()) is bumped by every
        # ledger write, including the terminal-transition write itself, so it
        # tracks "when this row last became actionable" the same way
        # KISMockOrderLedger.reconciled_at is stamped in
        # apply_lifecycle_transition when next_state is terminal — a
        # terminal-transition timestamp, not a send-time one.
        stmt = (
            select(AlpacaPaperOrderLedger)
            .where(
                AlpacaPaperOrderLedger.record_kind.in_(_ALPACA_PAPER_RECORD_KINDS),
                AlpacaPaperOrderLedger.lifecycle_state.in_(_ALPACA_PAPER_TERMINAL),
                AlpacaPaperOrderLedger.updated_at >= window_start,
                AlpacaPaperOrderLedger.updated_at <= window_end,
            )
            .order_by(AlpacaPaperOrderLedger.updated_at.desc())
            .limit(_PENDING_LEDGER_FETCH_CAP)
        )
        # ROB-954 round-2: a buy/sell roundtrip's two execution rows can share
        # one lifecycle_correlation_id. review.trade_retrospectives enforces
        # UNIQUE(correlation_id, account_mode) (app/models/review.py), so two
        # independent due entries for the same correlation_id can never both
        # be resolved — saving one retrospective always covers both rows at
        # once. Collapse to a single due entry per (lifecycle_correlation_id)
        # before appending, keyed on the row with the latest updated_at (the
        # most recently terminal-transitioned leg — typically the closing/
        # sell leg, since it settles after the entry leg).
        by_correlation: dict[str, AlpacaPaperOrderLedger] = {}
        for row in (await db.execute(stmt)).scalars().all():
            scanned += 1
            key = row.lifecycle_correlation_id
            current = by_correlation.get(key)
            if current is None or row.updated_at > current.updated_at:
                by_correlation[key] = row
        for row in by_correlation.values():
            itype = row.instrument_type.value
            market = "crypto" if itype == "crypto" else itype.removeprefix("equity_")
            entry = _pending_entry(
                ledger="alpaca_paper",
                account_mode="alpaca_paper",
                market=market,
                instrument_type=itype,
                symbol=row.execution_symbol,
                side=row.side,
                status=row.lifecycle_state,
                order_ref=row.client_order_id,
                report_item_uuid=None,
                trade_date=row.updated_at,
                row_id=row.id,
                suggested_correlation_id=row.lifecycle_correlation_id,
            )
            if not _is_covered(entry, covered_cids, covered_item_uuids):
                pending.append(entry)

    excluded_cancelled = 0
    if not include_cancelled:
        kept: list[dict[str, Any]] = []
        for entry in pending:
            if entry["status"] in _CANCEL_FAMILY_STATUSES:
                excluded_cancelled += 1
            else:
                kept.append(entry)
        pending = kept

    pending.sort(key=lambda e: e["trade_date_kst"] or "", reverse=True)
    total_pending = len(pending)
    limited = pending[: max(0, limit)]
    return {
        "kst_date_from": kst_date_from,
        "kst_date_to": kst_date_to,
        "account_mode": account_mode,
        "include_cancelled": include_cancelled,
        "terminal_scanned": scanned,
        "total_pending": total_pending,
        "returned": len(limited),
        "excluded_by_filter": {"cancelled": excluded_cancelled},
        "pending": limited,
    }
