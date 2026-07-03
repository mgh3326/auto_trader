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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.models.review import (
    KISLiveOrderLedger,
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
        out.append(model.model_dump(exclude_none=True))
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


def serialize_retrospective(r: TradeRetrospective) -> dict[str, Any]:
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
        "next_actions": r.next_actions,
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
        self, correlation_id: str
    ) -> TradeRetrospective | None:
        result = await self.db.execute(
            select(TradeRetrospective).where(
                TradeRetrospective.correlation_id == correlation_id
            )
        )
        return result.scalar_one_or_none()

    async def upsert(self, payload: dict[str, Any]) -> tuple[str, TradeRetrospective]:
        cid = payload.get("correlation_id")
        if cid is not None:
            existing = await self.get_by_correlation_id(cid)
            if existing is not None:
                for key, value in payload.items():
                    setattr(existing, key, value)
                await self.db.flush()
                return "updated", existing
        row = TradeRetrospective(**payload)
        self.db.add(row)
        await self.db.flush()
        return "created", row


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
    side: str | None = None,
    market: str | None = None,
    strategy_key: str | None = None,
    correlation_id: str | None = None,
    journal_id: int | None = None,
    report_uuid: str | None = None,
    report_item_uuid: str | None = None,
    plan_price: float | None = None,
    fill_price: float | None = None,
    realized_pnl: float | None = None,
    realized_pnl_currency: str | None = None,
    pnl_pct: float | None = None,
    rationale: str | None = None,
    result_summary: str | None = None,
    lesson: str | None = None,
    next_strategy: str | None = None,
    evidence_snapshot: dict | None = None,
    created_by_profile: str | None = None,
    buy_fx_rate: float | None = None,
    sell_fx_rate: float | None = None,
    fx_pnl_krw: float | None = None,
    security_pnl_usd: float | None = None,
    security_pnl_krw: float | None = None,
    total_pnl_krw: float | None = None,
    fx_rate_source: str | None = None,
    fx_pnl_accuracy: str | None = None,
    trigger_type: Any = _UNSET,
    root_cause_class: Any = _UNSET,
    intended_vs_happened: Any = _UNSET,
    next_actions: Any = _UNSET,
    guardrail_fired: Any = _UNSET,
    policy_version: Any = _UNSET,
) -> tuple[str, TradeRetrospective]:
    if account_mode not in _VALID_ACCOUNT_MODES:
        raise RetrospectiveValidationError(f"invalid account_mode: {account_mode}")
    if outcome not in _VALID_OUTCOMES:
        raise RetrospectiveValidationError(f"invalid outcome: {outcome}")
    if side is not None and side not in ("buy", "sell"):
        raise RetrospectiveValidationError(f"invalid side: {side}")
    if realized_pnl_currency is not None and realized_pnl_currency not in (
        "KRW",
        "USD",
    ):
        raise RetrospectiveValidationError(
            f"invalid realized_pnl_currency: {realized_pnl_currency}"
        )

    # ROB-647 — postmortem fields. Each uses the _UNSET sentinel so an omitted
    # field is preserved across idempotent correlation_id upserts, while an
    # explicit None clears it.
    trigger_set = trigger_type is not _UNSET and trigger_type is not None
    if trigger_set and trigger_type not in VALID_TRIGGER_TYPES:
        raise RetrospectiveValidationError(f"invalid trigger_type: {trigger_type}")
    if (
        root_cause_class is not _UNSET
        and root_cause_class is not None
        and root_cause_class not in VALID_ROOT_CAUSE_CLASSES
    ):
        raise RetrospectiveValidationError(
            f"invalid root_cause_class: {root_cause_class}"
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
        realized_pnl is not None or fill_price is not None
    ):
        raise RetrospectiveValidationError(
            f"{account_mode} cannot read fills (ROB-460); "
            "realized_pnl/fill_price not allowed"
        )

    journal_row = (
        await _load_trade_journal(db, journal_id)
        if journal_id is not None and fill_evidence_available
        else None
    )

    realized_pnl_value = _to_decimal(realized_pnl)
    realized_pnl_source: str | None = None
    if realized_pnl_value is not None:
        realized_pnl_source = "caller_supplied"
    elif journal_id is not None and fill_evidence_available:
        derived = _realized_pnl_from_journal(journal_row, side)
        if derived is not None:
            realized_pnl_value = derived
            realized_pnl_source = "derived_from_journal"

    if journal_row is not None:
        if buy_fx_rate is None and journal_row.buy_fx_rate is not None:
            buy_fx_rate = float(journal_row.buy_fx_rate)
        if sell_fx_rate is None and journal_row.sell_fx_rate is not None:
            sell_fx_rate = float(journal_row.sell_fx_rate)
        if fx_pnl_krw is None and journal_row.fx_pnl_krw is not None:
            fx_pnl_krw = float(journal_row.fx_pnl_krw)
        if security_pnl_usd is None and journal_row.security_pnl_usd is not None:
            security_pnl_usd = float(journal_row.security_pnl_usd)
        if security_pnl_krw is None and journal_row.security_pnl_krw is not None:
            security_pnl_krw = float(journal_row.security_pnl_krw)
        if total_pnl_krw is None and journal_row.total_pnl_krw is not None:
            total_pnl_krw = float(journal_row.total_pnl_krw)
        fx_rate_source = fx_rate_source or journal_row.fx_rate_source
        fx_pnl_accuracy = fx_pnl_accuracy or journal_row.fx_pnl_accuracy

    if realized_pnl_value is not None and realized_pnl_currency is None:
        realized_pnl_currency = _infer_currency(instrument_type)
        if realized_pnl_currency is None:
            raise RetrospectiveValidationError(
                "realized_pnl requires realized_pnl_currency "
                f"(could not infer from instrument_type={instrument_type})"
            )

    payload: dict[str, Any] = {
        "symbol": _normalize_symbol(symbol, instrument_type),
        "instrument_type": instrument_type,
        "account_mode": account_mode,
        "outcome": outcome,
        "side": side,
        "market": market,
        "strategy_key": strategy_key,
        "correlation_id": correlation_id,
        "journal_id": journal_id,
        "report_uuid": report_uuid,
        "report_item_uuid": report_item_uuid,
        "plan_price": _to_decimal(plan_price),
        "fill_price": _to_decimal(fill_price),
        "realized_pnl": realized_pnl_value,
        "realized_pnl_currency": realized_pnl_currency,
        "realized_pnl_source": realized_pnl_source,
        "pnl_pct": _to_decimal(pnl_pct),
        "fill_evidence_available": fill_evidence_available,
        "rationale": rationale,
        "result_summary": result_summary,
        "lesson": lesson,
        "next_strategy": next_strategy,
        "evidence_snapshot": evidence_snapshot,
        "created_by_profile": created_by_profile,
        "buy_fx_rate": _to_decimal(buy_fx_rate),
        "sell_fx_rate": _to_decimal(sell_fx_rate),
        "fx_pnl_krw": _to_decimal(fx_pnl_krw),
        "security_pnl_usd": _to_decimal(security_pnl_usd),
        "security_pnl_krw": _to_decimal(security_pnl_krw),
        "total_pnl_krw": _to_decimal(total_pnl_krw),
        "fx_rate_source": fx_rate_source,
        "fx_pnl_accuracy": fx_pnl_accuracy,
    }

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
    if next_actions is not _UNSET:
        payload["next_actions"] = (
            None if next_actions_value is _UNSET else next_actions_value
        )
    if guardrail_fired is not _UNSET:
        payload["guardrail_fired"] = guardrail_fired
    if policy_version is not _UNSET:
        payload["policy_version"] = policy_version

    repo = TradeRetrospectiveRepository(db)
    return await repo.upsert(payload)


def _kst_day_start(date_str: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=_KST)


def _kst_day_end(date_str: str) -> datetime:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=_KST)


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
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    return {
        "entries": [serialize_retrospective(r) for r in rows],
        "summary": {"count": len(rows), "by_outcome": by_outcome, "total": int(total)},
    }


async def get_open_next_actions(
    db: AsyncSession,
    *,
    market: str | None = None,
    symbol: str | None = None,
    statuses: frozenset[str] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Flatten incomplete next_actions across recent retrospectives.

    Bounded scan (``limit`` most-recent rows) — NOT full history; the
    ``scan_limit`` echo makes that explicit to callers. ``statuses=None``
    means "not done" (open/in_progress/unset all surface); a set narrows to
    exact status values.
    """
    filters = []
    if market is not None:
        filters.append(TradeRetrospective.market == market)
    if symbol is not None:
        filters.append(TradeRetrospective.symbol == symbol.strip().upper())
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


def _is_win(r: TradeRetrospective) -> bool:
    if r.realized_pnl is not None:
        return r.realized_pnl > 0
    return r.pnl_pct is not None and r.pnl_pct > 0


def _is_decided(r: TradeRetrospective) -> bool:
    return r.realized_pnl is not None or r.pnl_pct is not None


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

_KIS_LIVE_TERMINAL = _KIS_LIVE_DEFAULT_TERMINAL | _KIS_LIVE_CANCEL_TERMINAL
_GENERIC_LIVE_TERMINAL = _GENERIC_LIVE_DEFAULT_TERMINAL | _GENERIC_LIVE_CANCEL_TERMINAL
_TOSS_TERMINAL = _TOSS_DEFAULT_TERMINAL | _TOSS_CANCEL_TERMINAL

# Statuses hidden from `pending` unless include_cancelled=True. Disjoint from the
# DEFAULT statuses (note: `rejected` is DEFAULT; `cancel_rejected` /
# `replace_rejected` are cancel-family).
_CANCEL_FAMILY_STATUSES = (
    _KIS_LIVE_CANCEL_TERMINAL | _GENERIC_LIVE_CANCEL_TERMINAL | _TOSS_CANCEL_TERMINAL
)
# Bound per-ledger scan so a wide window cannot load unbounded rows.
_PENDING_LEDGER_FETCH_CAP = 1000


async def _covered_keys(db: AsyncSession) -> tuple[set[str], set[str]]:
    """(correlation_ids, report_item_uuids) already carrying a retrospective."""
    rows = (
        await db.execute(
            select(
                TradeRetrospective.correlation_id,
                TradeRetrospective.report_item_uuid,
            )
        )
    ).all()
    covered_cids = {str(cid) for cid, _ in rows if cid}
    covered_uuids = {str(uid) for _, uid in rows if uid}
    return covered_cids, covered_uuids


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
        # The correlation_id a session should pass to save_trade_retrospective so
        # the row is marked covered on the next pending scan.
        "suggested_correlation_id": f"{ledger}:{ref}",
    }


def _is_covered(
    entry: dict[str, Any], covered_cids: set[str], covered_uuids: set[str]
) -> bool:
    if entry["report_item_uuid"] and entry["report_item_uuid"] in covered_uuids:
        return True
    return entry["suggested_correlation_id"] in covered_cids


async def build_retrospective_pending(
    db: AsyncSession,
    *,
    kst_date_from: str,
    kst_date_to: str,
    account_mode: str | None = None,
    limit: int = 100,
    include_cancelled: bool = False,
) -> dict[str, Any]:
    """List terminal live-ledger orders (3 ledgers) lacking a retrospective.

    Read-only. Scans review.{kis_live_order_ledger, live_order_ledger,
    toss_live_order_ledger} for lifecycle-terminal rows in the KST trade_date
    window, then subtracts rows already covered by a retrospective (matched on
    report_item_uuid or the suggested correlation_id). Mirrors the ROB-120
    coverage pattern (terminal-without-artifact).

    Cancel-family rows (cancelled / cancel_rejected / replace_rejected) are
    hidden unless include_cancelled=True; the hidden count is reported in
    excluded_by_filter.
    """
    window_start = _kst_day_start(kst_date_from)
    window_end = _kst_day_end(kst_date_to)
    covered_cids, covered_uuids = await _covered_keys(db)

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
            if not _is_covered(entry, covered_cids, covered_uuids):
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
            if not _is_covered(entry, covered_cids, covered_uuids):
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
            if not _is_covered(entry, covered_cids, covered_uuids):
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
