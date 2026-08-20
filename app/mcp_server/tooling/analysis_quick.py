"""DB-only fast projection for ``analyze_stock_batch(quick=True)``.

The quick surface intentionally has no provider or broker imports.  It reads
the complete daily history needed for ROB-1236 from the daily-candle store,
then computes the small projection in process.  Full analysis remains in
``analysis_analyze`` and is not reused here.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pandas as pd
from sqlalchemy import and_, case, select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.earnings_context import _compact_earnings
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_fibonacci,
    _cluster_price_levels,
    _compute_indicators,
    _format_fibonacci_source,
    _split_support_resistance_levels,
)
from app.models.investment_reports import InvestmentReportItem
from app.models.market_events import MarketEvent
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeForecast,
    TradeRetrospective,
    TradeRetrospectiveAction,
)
from app.services.daily_candles.read_service import rows_to_frame
from app.services.daily_candles.repository import DailyCandlesRepository, MarketKey
from app.services.decision_history import (
    _ACTIVE_ACTION_STATUSES,
    ACTION_TEXT_LIMIT,
    ISSUE_ID_LIMIT,
    MAX_OPEN_ACTIONS,
    OPEN_ACTIONS_BYTE_BUDGET,
    OWNER_LIMIT,
    _truncate_field,
    _visibility_predicate,
)
from app.services.halt_detection import (
    HALTED_SUSPECT_DATA_STATE,
    classify_ohlcv_frame,
)
from app.services.trade_journal.retrospective_type import sql_is_learning_eligible

QUICK_HTTP_REQUEST_LIMIT = 0
# Three equity/crypto candle groups plus the bounded decision-history and
# earnings read models. Crypto's candle group uses two statements (identity +
# candles), so this remains a hard upper bound for the whole quick batch.
QUICK_DB_QUERY_LIMIT = 12
QUICK_CANDLE_COUNT = 250
QUICK_SUPPORT_RESISTANCE_COUNT = 60
QUICK_PROJECTION_FIELDS = frozenset(
    {
        "symbol",
        "market_type",
        "source",
        "current_price",
        "ohlcv",
        "rsi_14",
        "supports",
        "resistances",
        "data_state",
        "data_state_reason",
        "derived_as_of",
        "fetched_at",
        "data_age_seconds",
        "cache_hit",
        "fallback_source",
        "provider_provenance",
        "halt_suspect",
        "decision_history",
        "earnings",
    }
)

_SOURCE_BY_MARKET = {
    "equity_kr": "daily_candles_db",
    "equity_us": "daily_candles_db",
    "crypto": "daily_candles_db",
}
_MARKET_KEY_BY_TYPE = {
    "equity_kr": MarketKey.KR,
    "equity_us": MarketKey.US,
    "crypto": MarketKey.CRYPTO,
}


def _empty_projection(symbol: str, market_type: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market_type": market_type,
        "source": _SOURCE_BY_MARKET[market_type],
        "current_price": None,
        "ohlcv": None,
        "rsi_14": None,
        "supports": [],
        "resistances": [],
        "data_state": "missing",
        "data_state_reason": "daily_candles_unavailable",
        "derived_as_of": None,
        "fetched_at": None,
        "data_age_seconds": None,
        "cache_hit": False,
        "fallback_source": "daily_candles_db",
        "provider_provenance": [],
    }


def _build_support_resistance(
    frame: pd.DataFrame, current_price: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if current_price <= 0:
        return [], []
    fib = _calculate_fibonacci(frame, current_price)
    levels: list[tuple[float, str]] = []
    for level_key, price in (fib.get("levels") or {}).items():
        try:
            level_price = float(price)
        except (TypeError, ValueError):
            continue
        if level_price > 0:
            levels.append((level_price, _format_fibonacci_source(str(level_key))))

    # The quick contract keeps the same deterministic level sources as the
    # standalone S/R tool, but calculates them from the already-loaded frame.
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float) if "volume" in frame else None
    if volume is not None and not volume.empty:
        weighted = (close * volume).sum() / volume.sum() if volume.sum() else None
        if weighted is not None and weighted > 0:
            levels.append((float(weighted), "volume_poc"))
    bollinger = _compute_indicators(frame, ["bollinger"]).get("bollinger") or {}
    for key, source in (
        ("upper", "bb_upper"),
        ("middle", "bb_middle"),
        ("lower", "bb_lower"),
    ):
        try:
            level_price = float(bollinger.get(key))
        except (TypeError, ValueError):
            continue
        if level_price > 0:
            levels.append((level_price, source))

    clustered = _cluster_price_levels(levels, tolerance_pct=0.02)
    return _split_support_resistance_levels(clustered, current_price)


def _project_symbol(symbol: str, market_type: str, rows: list[Any]) -> dict[str, Any]:
    frame = rows_to_frame(rows)
    if frame.empty:
        return _empty_projection(symbol, market_type)

    last = frame.iloc[-1]
    current_price = float(last["close"])
    latest_row = rows[-1]
    latest_time = latest_row.time_utc
    if latest_time.tzinfo is None:
        latest_time = latest_time.replace(tzinfo=datetime.UTC)
    latest_iso = latest_time.isoformat()
    now = datetime.datetime.now(datetime.UTC)
    age_seconds = max(0.0, (now - latest_time).total_seconds())
    suspicion = classify_ohlcv_frame(frame)

    result = {
        "symbol": symbol,
        "market_type": market_type,
        "source": _SOURCE_BY_MARKET[market_type],
        "current_price": current_price,
        "ohlcv": {
            "time": latest_iso,
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": current_price,
            "volume": float(last["volume"]),
            "value": float(last["value"]),
        },
        "rsi_14": None,
        "supports": [],
        "resistances": [],
        "data_state": "stale",
        "data_state_reason": "db_only_projection",
        "derived_as_of": latest_iso,
        "fetched_at": latest_iso,
        "data_age_seconds": age_seconds,
        "cache_hit": False,
        "fallback_source": "daily_candles_db",
        "provider_provenance": [],
    }

    if suspicion.suspected:
        result["data_state"] = HALTED_SUSPECT_DATA_STATE
        result["data_state_reason"] = "halted_suspect_frozen_bars"
        result["rsi_14"] = None
        result["supports"] = None
        result["resistances"] = None
        result["halt_suspect"] = suspicion.to_dict()
        return result

    try:
        indicators = _compute_indicators(frame, ["rsi"])
        result["rsi_14"] = (indicators.get("rsi") or {}).get("14")
        sr_frame = frame.tail(QUICK_SUPPORT_RESISTANCE_COUNT)
        supports, resistances = _build_support_resistance(sr_frame, current_price)
        result["supports"] = supports[:3]
        result["resistances"] = resistances[:3]
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        result["data_state"] = "degraded"
        result["data_state_reason"] = "projection_calculation_failed"
    return result


def _date_iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.date().isoformat() if hasattr(value, "date") else str(value)


def _looks_like_smoke(*values: Any) -> bool:
    return any("smoke" in str(value).lower() for value in values if value is not None)


def _quick_fill(source: str, row: Any) -> dict[str, Any]:
    return {
        "date": _date_iso(getattr(row, "trade_date", None)),
        "side": getattr(row, "side", None),
        "status": getattr(row, "status", None),
        "qty": float(row.quantity)
        if getattr(row, "quantity", None) is not None
        else None,
        "filled_qty": (
            float(row.filled_qty)
            if getattr(row, "filled_qty", None) is not None
            else None
        ),
        "avg_fill_price": (
            float(row.avg_fill_price)
            if getattr(row, "avg_fill_price", None) is not None
            else None
        ),
        "target_price": (
            float(row.target_price)
            if getattr(row, "target_price", None) is not None
            else None
        ),
        "stop_loss": (
            float(row.stop_loss)
            if getattr(row, "stop_loss", None) is not None
            else None
        ),
        "source": source,
    }


async def _load_decision_history_batch(
    session: Any,
    symbols: list[tuple[str, str]],
    *,
    account_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Read the compact decision-history contract with set-based queries.

    The previous quick path called ``build_decision_context`` once per symbol;
    that function is intentionally retained for the full/deep surface. This
    read model selects each source table once for the complete symbol batch.
    The calibration fields remain explicit empty aggregates when their source
    is not part of the quick projection, rather than silently invoking the
    deep scoreboard query tree.
    """
    symbols_by_key = {
        symbol.upper(): (symbol, market_type) for symbol, market_type in symbols
    }
    db_symbols = list(symbols_by_key)
    contexts: dict[str, dict[str, Any]] = {}
    if not db_symbols:
        return contexts

    prior_rows = (
        (
            await session.execute(
                select(InvestmentReportItem)
                .where(InvestmentReportItem.symbol.in_(db_symbols))
                .order_by(InvestmentReportItem.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    retrospectives = (
        (
            await session.execute(
                select(TradeRetrospective)
                .where(TradeRetrospective.symbol.in_(db_symbols))
                .where(_visibility_predicate(account_mode))
                .where(sql_is_learning_eligible())
                .order_by(TradeRetrospective.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    today_kst = now_kst().date()
    overdue_expr = and_(
        TradeRetrospectiveAction.status.in_(_ACTIVE_ACTION_STATUSES),
        TradeRetrospectiveAction.due_kst_date.isnot(None),
        TradeRetrospectiveAction.due_kst_date < today_kst,
    )
    actions = (
        await session.execute(
            select(TradeRetrospectiveAction, TradeRetrospective)
            .join(
                TradeRetrospective,
                TradeRetrospectiveAction.retrospective_id == TradeRetrospective.id,
            )
            .where(TradeRetrospective.symbol.in_(db_symbols))
            .where(TradeRetrospectiveAction.status.in_(_ACTIVE_ACTION_STATUSES))
            .where(_visibility_predicate(account_mode))
            .order_by(
                case((overdue_expr, 0), else_=1),
                case((TradeRetrospectiveAction.status == "in_progress", 0), else_=1),
                TradeRetrospectiveAction.due_kst_date.asc().nullslast(),
                TradeRetrospectiveAction.updated_at.desc(),
                TradeRetrospectiveAction.id.asc(),
            )
        )
    ).all()
    forecasts = (
        (
            await session.execute(
                select(TradeForecast)
                .where(TradeForecast.symbol.in_(db_symbols))
                .where(TradeForecast.status == "open")
                .order_by(TradeForecast.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    fill_rows: list[tuple[str, Any]] = []
    for source, model in (
        ("kis", KISLiveOrderLedger),
        ("live", LiveOrderLedger),
        ("toss", TossLiveOrderLedger),
    ):
        rows = (
            (
                await session.execute(
                    select(model)
                    .where(model.symbol.in_(db_symbols))
                    .order_by(model.trade_date.desc())
                )
            )
            .scalars()
            .all()
        )
        fill_rows.extend((source, row) for row in rows)

    def key_for(symbol: Any) -> str:
        return str(symbol or "").strip().upper()

    per_symbol: dict[str, dict[str, Any]] = {
        symbol: {
            "symbol": symbol,
            "market": market_type.replace("equity_", ""),
            "link_quality": "symbol_window",
            "prior_decisions": [],
            "prior_lessons": [],
            "realized_outcomes": [],
            "recent_fills": [],
            "open_claims": [],
            "running_brier_symbol": {
                "n": 0,
                "mean_brier": None,
                "flag": "insufficient_sample",
            },
            "running_brier_global": {
                "n": 0,
                "mean_brier": None,
                "flag": "insufficient_sample",
            },
            "realized_r_by_tag": {},
            "open_actions": [],
            "open_actions_meta": {
                "count": 0,
                "truncated": False,
                "authority": "historical_advisory",
                "executable": False,
            },
        }
        for symbol, market_type in symbols
    }

    for row in prior_rows:
        symbol = key_for(getattr(row, "symbol", None))
        target = per_symbol.get(symbol)
        if target is None or _looks_like_smoke(getattr(row, "rationale", None)):
            continue
        target["prior_decisions"].append(
            {
                "date": _date_iso(getattr(row, "created_at", None)),
                "intent": getattr(row, "intent", None),
                "side": getattr(row, "side", None),
                "decision_bucket": getattr(row, "decision_bucket", None),
                "confidence": (
                    float(row.confidence)
                    if getattr(row, "confidence", None) is not None
                    else None
                ),
                "rationale": getattr(row, "rationale", None),
            }
        )
        target["prior_decisions"] = target["prior_decisions"][:6]

    for row in retrospectives:
        symbol = key_for(getattr(row, "symbol", None))
        target = per_symbol.get(symbol)
        # Visibility (kis_mock exact / default excludes mock-counterfactual)
        # and learning-eligibility (excludes intake rows) are enforced in SQL
        # above via `_visibility_predicate` / `sql_is_learning_eligible`.
        if target is None or _looks_like_smoke(
            getattr(row, "created_by_profile", None),
            getattr(row, "strategy_key", None),
            getattr(row, "correlation_id", None),
            getattr(row, "lesson", None),
        ):
            continue
        lesson = getattr(row, "lesson", None)
        if lesson and len(target["prior_lessons"]) < 3:
            target["prior_lessons"].append(str(lesson)[:219])
        if len(target["realized_outcomes"]) < 5:
            target["realized_outcomes"].append(
                {
                    "date": _date_iso(getattr(row, "created_at", None)),
                    "side": getattr(row, "side", None),
                    "outcome": getattr(row, "outcome", None),
                    "trigger_type": getattr(row, "trigger_type", None),
                    "pnl_pct": (
                        float(row.pnl_pct)
                        if getattr(row, "pnl_pct", None) is not None
                        else None
                    ),
                    "realized_pnl": (
                        float(row.realized_pnl)
                        if getattr(row, "realized_pnl", None) is not None
                        else None
                    ),
                }
            )

    # Rows arrive pre-sorted by the SQL ORDER BY (overdue -> in_progress ->
    # due ASC NULLS LAST -> updated_at DESC -> id ASC), same as the canonical
    # per-symbol `_open_actions` order. Bucket by symbol first, preserving
    # that order, then apply the MAX_OPEN_ACTIONS cap + byte budget per
    # symbol exactly like the canonical helper (ROB-884).
    per_symbol_actions: dict[str, list[dict[str, Any]]] = {
        symbol: [] for symbol in per_symbol
    }
    per_symbol_action_truncated: dict[str, bool] = dict.fromkeys(per_symbol, False)
    for action, retro in actions:
        symbol = key_for(getattr(retro, "symbol", None))
        if symbol not in per_symbol_actions or _looks_like_smoke(
            getattr(retro, "created_by_profile", None),
            getattr(retro, "strategy_key", None),
            getattr(retro, "correlation_id", None),
        ):
            continue
        due = getattr(action, "due_kst_date", None)
        status = getattr(action, "status", None)
        is_overdue = (
            status in _ACTIVE_ACTION_STATUSES and due is not None and due < today_kst
        )
        orig_action = getattr(action, "action", None) or ""
        orig_owner = getattr(action, "owner", None) or ""
        orig_issue = getattr(action, "issue_id", None) or ""
        if (
            len(orig_action) > ACTION_TEXT_LIMIT
            or len(orig_owner) > OWNER_LIMIT
            or len(orig_issue) > ISSUE_ID_LIMIT
        ):
            per_symbol_action_truncated[symbol] = True
        per_symbol_actions[symbol].append(
            {
                "action_id": str(getattr(action, "id", "")),
                "action": _truncate_field(
                    getattr(action, "action", None), ACTION_TEXT_LIMIT
                ),
                "status": status,
                "owner": _truncate_field(getattr(action, "owner", None), OWNER_LIMIT),
                "issue_id": _truncate_field(
                    getattr(action, "issue_id", None), ISSUE_ID_LIMIT
                ),
                "due_kst_date": due.isoformat() if due else None,
                "overdue": is_overdue,
            }
        )

    for symbol, items in per_symbol_actions.items():
        truncated = per_symbol_action_truncated[symbol]
        if len(items) > MAX_OPEN_ACTIONS:
            items = items[:MAX_OPEN_ACTIONS]
            truncated = True
        while items:
            payload = json.dumps(items, ensure_ascii=False).encode("utf-8")
            if len(payload) <= OPEN_ACTIONS_BYTE_BUDGET:
                break
            items.pop()
            truncated = True
        target = per_symbol[symbol]
        target["open_actions"] = items
        target["open_actions_meta"]["truncated"] = truncated
        target["open_actions_meta"]["count"] = len(items)

    for row in forecasts:
        target = per_symbol.get(key_for(getattr(row, "symbol", None)))
        if target is None:
            continue
        forecast_target = getattr(row, "forecast_target", None)
        forecast_target = forecast_target if isinstance(forecast_target, dict) else {}
        target["open_claims"].append(
            {
                "probability": float(row.probability),
                "horizon": getattr(row, "horizon", None),
                "review_date": _date_iso(getattr(row, "review_date", None)),
                "direction": forecast_target.get("direction"),
                "target_price": forecast_target.get("target_price"),
            }
        )
        target["open_claims"] = target["open_claims"][:5]

    for source, row in fill_rows:
        target = per_symbol.get(key_for(getattr(row, "symbol", None)))
        if target is not None and len(target["recent_fills"]) < 6:
            target["recent_fills"].append(_quick_fill(source, row))

    for symbol, context in per_symbol.items():
        has_signal = any(
            context[key]
            for key in (
                "prior_decisions",
                "prior_lessons",
                "realized_outcomes",
                "recent_fills",
                "open_claims",
                "open_actions",
            )
        )
        if has_signal:
            contexts[symbol] = context
    return contexts


async def _load_earnings_batch(
    session: Any,
    symbols: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """Read the existing earnings meaning from the DB without provider calls."""
    equity = {
        symbol: market_type.replace("equity_", "")
        for symbol, market_type in symbols
        if market_type in {"equity_kr", "equity_us"}
    }
    if not equity:
        return {}
    today = datetime.date.today()
    rows = (
        (
            await session.execute(
                select(MarketEvent)
                .where(MarketEvent.category == "earnings")
                .where(MarketEvent.market.in_(tuple(set(equity.values()))))
                .where(MarketEvent.symbol.in_(tuple(equity)))
                .where(MarketEvent.event_date >= today)
                .where(MarketEvent.event_date <= today + datetime.timedelta(days=30))
                .order_by(MarketEvent.event_date.asc())
            )
        )
        .scalars()
        .all()
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in equity}
    for row in rows:
        symbol = str(getattr(row, "symbol", "")).strip().upper()
        if symbol not in by_symbol:
            continue
        by_symbol[symbol].append(
            {
                "date": _date_iso(getattr(row, "event_date", None)),
                "hour": getattr(row, "time_hint", None),
                "time_hint": getattr(row, "time_hint", None),
                "quarter": getattr(row, "fiscal_quarter", None),
                "year": getattr(row, "fiscal_year", None),
                "status": getattr(row, "status", None),
                "eps_estimate": (
                    (getattr(row, "raw_payload_json", None) or {}).get("eps_estimate")
                ),
                "revenue_estimate": (
                    (getattr(row, "raw_payload_json", None) or {}).get(
                        "revenue_estimate"
                    )
                ),
            }
        )
    return {
        symbol: _compact_earnings(
            {
                "symbol": symbol,
                "market": market,
                "source": "market_events",
                "earnings": earnings,
            },
            today=today,
            freshness="stale",
            data_as_of=None,
        )
        for symbol, market in equity.items()
        for earnings in [by_symbol[symbol]]
    }


async def load_quick_projection_batch(
    symbols: list[tuple[str, str]],
    *,
    decision_history_account_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Load candles, decision history, and earnings in bounded batch SQL."""
    result = {
        symbol: _empty_projection(symbol, market_type)
        for symbol, market_type in symbols
    }
    grouped: dict[str, list[str]] = {}
    for symbol, market_type in symbols:
        grouped.setdefault(market_type, []).append(symbol)

    async with AsyncSessionLocal() as session:
        repo = DailyCandlesRepository(session=session)
        for market_type, group in grouped.items():
            market = _MARKET_KEY_BY_TYPE[market_type]
            partition = "KRX" if market == MarketKey.KR else None
            if market == MarketKey.CRYPTO:
                partition = "upbit_krw"
            try:
                rows_by_symbol = await repo.fetch_recent_batch(
                    market=market,
                    symbols=group,
                    partition=partition,
                    count=QUICK_CANDLE_COUNT,
                )
            except Exception:
                rows_by_symbol = {symbol: [] for symbol in group}
            for symbol in group:
                result[symbol] = _project_symbol(
                    symbol, market_type, rows_by_symbol.get(symbol, [])
                )
        # History/earnings are advisory read models. A partially migrated test
        # database or an unavailable optional table must not turn the price
        # projection into an error; the full/deep surface keeps its own
        # established fail-open behavior.
        try:
            decision_history = await _load_decision_history_batch(
                session, symbols, account_mode=decision_history_account_mode
            )
        except Exception:
            decision_history = {}
        try:
            earnings = await _load_earnings_batch(session, symbols)
        except Exception:
            earnings = {}
        for symbol, row in result.items():
            if symbol in decision_history:
                row["decision_history"] = decision_history[symbol]
            if symbol in earnings:
                row["earnings"] = earnings[symbol]
        for symbol, row in result.items():
            result[symbol] = {
                key: value
                for key, value in row.items()
                if key in QUICK_PROJECTION_FIELDS
            }
    return result


__all__ = [
    "QUICK_DB_QUERY_LIMIT",
    "QUICK_HTTP_REQUEST_LIMIT",
    "QUICK_PROJECTION_FIELDS",
    "load_quick_projection_batch",
]
