"""ROB-711 — deterministic past-judgment→outcome context for a symbol.

Read-only aggregation over the existing spine (investment_report_items,
live order ledgers, trade_forecasts, trade_retrospectives). No LLM (ROB-501),
no schema change, no order hot-path touch. Injected into analyze_stock_batch
responses so each fresh analysis session sees the symbol's own history.

Join reality (2026-07-05): report_item_uuid is ~0% populated on live ledgers /
forecasts / retros, so the "exact" provenance join yields nothing today — every
link is symbol + recency. link_quality is therefore "symbol_window" until
ROB-714 mints provenance keys at place-time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItem
from app.models.review import TradeRetrospective
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter

MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}

MAX_DECISIONS = 6
MAX_LESSONS = 3
MAX_OUTCOMES = 5
MAX_FILLS = 6
MAX_CLAIMS = 5
_TRUNC = 220
_SMOKE_TOKENS = ("smoke",)


def _is_smoke(*values: str | None) -> bool:
    """True if any provenance/text field marks this as a test/smoke row.

    Filters our OWN test artifacts (e.g. created_by_profile HERMES_OPERATOR_SMOKE,
    strategy_key rob474_smoke_..., rationale "Smoke-only ..."). Deliberately does
    NOT key on account_mode — mock/paper rows are real practice data (ROB-705).
    """
    for v in values:
        if v and any(tok in v.lower() for tok in _SMOKE_TOKENS):
            return True
    return False


def _truncate(text: str | None, limit: int = _TRUNC) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def build_decision_context(
    db: AsyncSession,
    symbol: str,
    market: str,
    setup_tag: str | None = None,
) -> dict[str, Any]:
    """Build the decision_history payload for one symbol, or None if no signal.

    setup_tag is reserved for realized_r_by_tag (ROB-713 stage 3); unused here.
    """
    instrument_type = MARKET_TO_INSTRUMENT.get(market)
    norm = _normalize_symbol_for_filter(symbol, instrument_type)

    prior_decisions = await _prior_decisions(db, norm)

    lessons, outcomes = await _retrospectives(db, norm)

    ctx: dict[str, Any] = {
        "symbol": norm,
        "market": market,
        "link_quality": "symbol_window",
        "prior_decisions": prior_decisions,
        "prior_lessons": lessons,
        "realized_outcomes": outcomes,
    }
    return ctx


async def _prior_decisions(db: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(InvestmentReportItem)
            .where(InvestmentReportItem.symbol == symbol)
            .order_by(InvestmentReportItem.created_at.desc())
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        if _is_smoke(r.rationale, r.status):
            continue
        out.append(
            {
                "date": r.created_at.date().isoformat() if r.created_at else None,
                "intent": r.intent,
                "side": r.side,
                "decision_bucket": r.decision_bucket,
                "confidence": float(r.confidence) if r.confidence is not None else None,
                "rationale": _truncate(r.rationale),
            }
        )
        if len(out) >= MAX_DECISIONS:
            break
    return out

async def _retrospectives(
    db: AsyncSession, symbol: str
) -> tuple[list[str], list[dict[str, Any]]]:
    rows = (
        await db.execute(
            select(TradeRetrospective)
            .where(TradeRetrospective.symbol == symbol)
            .order_by(TradeRetrospective.created_at.desc())
        )
    ).scalars().all()
    lessons: list[str] = []
    outcomes: list[dict[str, Any]] = []
    for r in rows:
        if _is_smoke(r.created_by_profile, r.strategy_key, r.correlation_id, r.lesson):
            continue
        if r.lesson and r.lesson.strip() and len(lessons) < MAX_LESSONS:
            lessons.append(_truncate(r.lesson))
        if len(outcomes) < MAX_OUTCOMES:
            outcomes.append(
                {
                    "date": r.created_at.date().isoformat() if r.created_at else None,
                    "side": r.side,
                    "outcome": r.outcome,
                    "trigger_type": r.trigger_type,
                    "pnl_pct": float(r.pnl_pct) if r.pnl_pct is not None else None,
                    "realized_pnl": (
                        float(r.realized_pnl) if r.realized_pnl is not None else None
                    ),
                }
            )
    return lessons, outcomes
