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
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeForecast,
    TradeRetrospective,
)
from app.services.trade_journal.forecast_service import (
    _normalize_symbol_for_filter,
    build_forecast_calibration_aggregate,
)

MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}

MAX_DECISIONS = 6
MAX_LESSONS = 3
MAX_OUTCOMES = 5
MAX_FILLS = 6
MAX_CLAIMS = 5
_TRUNC = 220
_SMOKE_TOKENS = ("smoke",)
_MAX_TAGS = 3
_R_KEYS = (
    "n",
    "expectancy_r",
    "win_rate",
    "profit_factor",
    "avg_mae",
    "insufficient_sample",
)


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
) -> dict[str, Any] | None:
    """Build the decision_history payload for one symbol, or None if no signal.

    ``setup_tag``, when supplied, selects which setup comes first in
    ``realized_r_by_tag`` (ROB-713). It is otherwise unused.
    """
    instrument_type = MARKET_TO_INSTRUMENT.get(market)
    norm = _normalize_symbol_for_filter(symbol, instrument_type)

    prior_decisions = await _prior_decisions(db, norm)

    lessons, outcomes = await _retrospectives(db, norm)
    fills = await _recent_fills(db, norm)
    open_claims = await _open_claims(db, norm)

    if not (prior_decisions or lessons or outcomes or fills or open_claims):
        return None  # no signal — omit the field entirely

    brier_symbol = _fold_brier(
        await build_forecast_calibration_aggregate(
            db, symbol=norm, instrument_type=instrument_type
        )
    )
    brier_global = _fold_brier(await build_forecast_calibration_aggregate(db))
    realized_r = await _realized_r_by_tag(db, market, setup_tag)

    ctx: dict[str, Any] = {
        "symbol": norm,
        "market": market,
        "link_quality": "symbol_window",
        "prior_decisions": prior_decisions,
        "prior_lessons": lessons,
        "realized_outcomes": outcomes,
        "recent_fills": fills,
        "open_claims": open_claims,
        "running_brier_symbol": brier_symbol,
        "running_brier_global": brier_global,
        "realized_r_by_tag": realized_r,
    }
    return ctx


def _fold_brier(agg: dict[str, Any]) -> dict[str, Any]:
    groups = agg.get("groups", [])
    n = sum(int(g["sample_size"]) for g in groups)
    scored = [g for g in groups if g.get("avg_brier_score") is not None]
    denom = sum(int(g["sample_size"]) for g in scored)
    mean = (
        sum(g["avg_brier_score"] * g["sample_size"] for g in scored) / denom
        if denom
        else None
    )
    return {
        "n": n,
        "mean_brier": round(mean, 4) if mean is not None else None,
        "flag": "insufficient_sample" if n < 10 else "ok",
    }


async def _realized_r_by_tag(
    db: AsyncSession, market: str, setup_tag: str | None
) -> dict[str, dict[str, Any]]:
    """Bounded per-tag map for the ROB-713 scoreboard — portfolio-wide, not per-symbol.

    Imported lazily so `import app.services.decision_history` stays free of the
    market-data/broker import chain — `app.services.invest_view_model`
    (ROB-716 stock-detail surface) imports this module and must not transitively
    pull execution paths (`tests/test_invest_view_model_safety.py`).
    """
    from app.services.trade_journal.aggregates import build_trading_scoreboard

    board = await build_trading_scoreboard(db, market=market)
    groups = board.get("groups", [])
    ordered = sorted(groups, key=lambda g: (g["tag"] != setup_tag, -int(g["n"])))
    out: dict[str, dict[str, Any]] = {}
    for g in ordered[:_MAX_TAGS]:
        if g["tag"] == "untagged":
            continue
        out[g["tag"]] = {k: g.get(k) for k in _R_KEYS}
    return out


async def _prior_decisions(db: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(InvestmentReportItem)
                .where(InvestmentReportItem.symbol == symbol)
                .order_by(InvestmentReportItem.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
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
        (
            await db.execute(
                select(TradeRetrospective)
                .where(TradeRetrospective.symbol == symbol)
                .order_by(TradeRetrospective.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
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


def _fill_row(source: str, r: Any) -> dict[str, Any]:
    return {
        "date": r.trade_date.date().isoformat() if r.trade_date else None,
        "side": r.side,
        "status": r.status,
        "qty": float(r.quantity) if r.quantity is not None else None,
        "filled_qty": float(r.filled_qty) if r.filled_qty is not None else None,
        "avg_fill_price": (
            float(r.avg_fill_price) if r.avg_fill_price is not None else None
        ),
        "target_price": (
            float(r.target_price)
            if getattr(r, "target_price", None) is not None
            else None
        ),
        "stop_loss": (
            float(r.stop_loss) if getattr(r, "stop_loss", None) is not None else None
        ),
        "source": source,
    }


async def _recent_fills(db: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    collected: list[tuple[Any, str, Any]] = []
    for source, model in (
        ("kis", KISLiveOrderLedger),
        ("live", LiveOrderLedger),
        ("toss", TossLiveOrderLedger),
    ):
        rows = (
            (await db.execute(select(model).where(model.symbol == symbol)))
            .scalars()
            .all()
        )
        for r in rows:
            collected.append((r.trade_date, source, r))
    # newest first across all three ledgers; None trade_date sorts last
    collected.sort(key=lambda t: (t[0] is not None, t[0]), reverse=True)
    return [_fill_row(source, r) for (_dt, source, r) in collected[:MAX_FILLS]]


async def _open_claims(db: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(TradeForecast)
                .where(TradeForecast.symbol == symbol, TradeForecast.status == "open")
                .order_by(TradeForecast.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows[:MAX_CLAIMS]:
        target = r.forecast_target if isinstance(r.forecast_target, dict) else {}
        out.append(
            {
                "probability": float(r.probability)
                if r.probability is not None
                else None,
                "horizon": r.horizon,
                "review_date": r.review_date.isoformat() if r.review_date else None,
                "direction": target.get("direction"),
                "target_price": target.get("target_price"),
            }
        )
    return out
