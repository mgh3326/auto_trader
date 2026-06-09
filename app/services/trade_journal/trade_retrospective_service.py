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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.models.review import TradeRetrospective
from app.models.trade_journal import TradeJournal

_VALID_ACCOUNT_MODES = {
    "kis_mock",
    "kiwoom_mock",
    "kis_live",
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
_NO_FILL_ACCOUNT_MODES = {"kiwoom_mock"}  # fills not readable (ROB-460)
_KST = ZoneInfo("Asia/Seoul")


class RetrospectiveValidationError(ValueError):
    """Raised when a retrospective payload violates a typed constraint."""


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
        "fill_evidence_available": r.fill_evidence_available,
        "rationale": r.rationale,
        "result_summary": r.result_summary,
        "lesson": r.lesson,
        "next_strategy": r.next_strategy,
        "evidence_snapshot": r.evidence_snapshot,
        "created_by_profile": r.created_by_profile,
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


async def _derive_realized_pnl_from_journal(
    db: AsyncSession, journal_id: int, side: str | None
) -> Decimal | None:
    j = (
        await db.execute(select(TradeJournal).where(TradeJournal.id == journal_id))
    ).scalar_one_or_none()
    if j is None or j.entry_price is None or j.exit_price is None or j.quantity is None:
        return None
    entry = Decimal(str(j.entry_price))
    exit_price = Decimal(str(j.exit_price))
    qty = Decimal(str(j.quantity))
    direction = Decimal("-1") if (side or j.side) == "sell" else Decimal("1")
    return (exit_price - entry) * qty * direction


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

    fill_evidence_available = account_mode not in _NO_FILL_ACCOUNT_MODES
    if not fill_evidence_available and (
        realized_pnl is not None or fill_price is not None
    ):
        raise RetrospectiveValidationError(
            f"{account_mode} cannot read fills (ROB-460); "
            "realized_pnl/fill_price not allowed"
        )

    realized_pnl_value = _to_decimal(realized_pnl)
    realized_pnl_source: str | None = None
    if realized_pnl_value is not None:
        realized_pnl_source = "caller_supplied"
    elif journal_id is not None and fill_evidence_available:
        derived = await _derive_realized_pnl_from_journal(db, journal_id, side)
        if derived is not None:
            realized_pnl_value = derived
            realized_pnl_source = "derived_from_journal"

    payload: dict[str, Any] = {
        "symbol": to_db_symbol(symbol),
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
    }
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
    limit: int = 50,
) -> dict[str, Any]:
    filters = []
    if symbol is not None:
        filters.append(TradeRetrospective.symbol == to_db_symbol(symbol))
    if account_mode is not None:
        filters.append(TradeRetrospective.account_mode == account_mode)
    if strategy_key is not None:
        filters.append(TradeRetrospective.strategy_key == strategy_key)
    if market is not None:
        filters.append(TradeRetrospective.market == market)
    if correlation_id is not None:
        filters.append(TradeRetrospective.correlation_id == correlation_id)
    if days is not None:
        filters.append(
            TradeRetrospective.created_at >= now_kst() - timedelta(days=days)
        )
    stmt = (
        select(TradeRetrospective)
        .where(*filters)
        .order_by(TradeRetrospective.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
    return {
        "entries": [serialize_retrospective(r) for r in rows],
        "summary": {"count": len(rows), "by_outcome": by_outcome},
    }


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
    if group_by not in ("strategy", "day"):
        group_by = "strategy"
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
        if not r.fill_evidence_available:
            excluded_no_evidence += 1
            continue
        key = (
            (r.strategy_key or "no_strategy")
            if group_by == "strategy"
            else _kst_date_str(r.created_at)
        )
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
        by_outcome: dict[str, int] = {}
        for it in items:
            by_outcome[it.outcome] = by_outcome.get(it.outcome, 0) + 1
        out.append(
            {
                "group": key,
                "sample_size": len(items),
                "wins": wins,
                "misses": misses,
                "win_rate_pct": (wins / len(decided) * 100.0) if decided else None,
                "avg_pnl_pct": _avg([it.pnl_pct for it in items]),
                "realized_pnl_sum": realized_sum,
                "by_outcome": by_outcome,
            }
        )
    out.sort(key=lambda g: -g["sample_size"])
    return {
        "group_by": group_by,
        "groups": out,
        "excluded_no_fill_evidence": excluded_no_evidence,
    }
