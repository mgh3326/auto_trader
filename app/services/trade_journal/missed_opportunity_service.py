"""ROB-1017 — zero-entry opportunity-cost forecast storage.

This is a DB-only learning path. It never imports a broker, places an order, or
registers a scheduler. A qualifying session writes one deterministic D+5
``return_at_horizon`` forecast and one linked ``missed_opportunity``
retrospective per ranked candidate in the caller's transaction.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
from app.schemas.trade_retrospective import MissedOpportunityCandidate
from app.services.market_events.session_calendar import (
    is_trading_session,
    next_trading_session,
)
from app.services.trade_journal.forecast_service import (
    _normalize_symbol,
    get_forecast,
    save_forecast,
)
from app.services.trade_journal.trade_retrospective_service import (
    VALID_ACCOUNT_MODES,
    TradeRetrospectiveRepository,
    save_retrospective,
)

INDEX_MOVE_THRESHOLD_PCT = 2.0
DEFAULT_TOP_N = 3
MAX_TOP_N = 10
HORIZON_SESSIONS = 5
MISSED_TRIGGER = "missed_opportunity"
RETURN_AT_HORIZON_KIND = "return_at_horizon"

_MARKET_INSTRUMENT = {
    "kr": "equity_kr",
    "us": "equity_us",
    "crypto": "crypto",
}


class MissedOpportunityValidationError(ValueError):
    """Raised when a zero-entry cohort cannot be saved deterministically."""


def _parse_session_date(raw: str | date) -> date:
    if isinstance(raw, date):
        return raw
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise MissedOpportunityValidationError(
            f"session_date must be YYYY-MM-DD: {raw!r}"
        ) from exc


def _d5_review_date(market: str, session_date: date) -> tuple[date, str]:
    if market == "crypto":
        return session_date + timedelta(days=HORIZON_SESSIONS), "D+5 calendar days"
    if market not in {"kr", "us"}:
        raise MissedOpportunityValidationError(f"unsupported market: {market}")
    if not is_trading_session(market, session_date):
        raise MissedOpportunityValidationError(
            f"session_date is not a confirmed {market} trading session: "
            f"{session_date.isoformat()}"
        )
    cursor = session_date
    for _ in range(HORIZON_SESSIONS):
        following = next_trading_session(market, cursor)
        if following is None:
            raise MissedOpportunityValidationError(
                f"could not resolve D+5 trading session for {market} from "
                f"{session_date.isoformat()}"
            )
        cursor = following
    return cursor, "D+5 trading sessions"


def _required_text(value: str | None, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise MissedOpportunityValidationError(f"{field} is required")
    return cleaned


def _validate_gate(
    *, market: str, index_change_pct: float, new_buy_count: int
) -> tuple[float, int]:
    if market not in _MARKET_INSTRUMENT:
        raise MissedOpportunityValidationError(
            f"market must be one of {sorted(_MARKET_INSTRUMENT)}"
        )
    try:
        change = float(index_change_pct)
    except (TypeError, ValueError) as exc:
        raise MissedOpportunityValidationError(
            "index_change_pct must be a finite number"
        ) from exc
    if not math.isfinite(change):
        raise MissedOpportunityValidationError(
            "index_change_pct must be a finite number"
        )
    if isinstance(new_buy_count, bool) or not isinstance(new_buy_count, int):
        raise MissedOpportunityValidationError("new_buy_count must be an integer")
    if new_buy_count < 0:
        raise MissedOpportunityValidationError("new_buy_count must be >= 0")
    return change, new_buy_count


def _validate_candidates(
    raw_candidates: list[dict[str, Any]], *, market: str, top_n: int
) -> list[MissedOpportunityCandidate]:
    if isinstance(top_n, bool) or not isinstance(top_n, int):
        raise MissedOpportunityValidationError("top_n must be an integer")
    if not 1 <= top_n <= MAX_TOP_N:
        raise MissedOpportunityValidationError(f"top_n must be within [1, {MAX_TOP_N}]")
    if not isinstance(raw_candidates, list) or len(raw_candidates) != top_n:
        raise MissedOpportunityValidationError(
            f"candidates must contain exactly top_n={top_n} ranked entries"
        )

    candidates: list[MissedOpportunityCandidate] = []
    normalized_symbols: set[str] = set()
    expected_instrument = _MARKET_INSTRUMENT[market]
    for index, raw in enumerate(raw_candidates):
        try:
            candidate = MissedOpportunityCandidate.model_validate(raw)
        except ValidationError as exc:
            raise MissedOpportunityValidationError(
                f"invalid candidates[{index}]: {exc.errors(include_url=False)}"
            ) from exc
        if candidate.instrument_type != expected_instrument:
            raise MissedOpportunityValidationError(
                f"candidates[{index}].instrument_type must be "
                f"{expected_instrument!r} for market={market!r}"
            )
        normalized = _normalize_symbol(candidate.symbol, candidate.instrument_type)
        if normalized in normalized_symbols:
            raise MissedOpportunityValidationError(
                f"duplicate candidate symbol after normalization: {normalized}"
            )
        normalized_symbols.add(normalized)
        candidates.append(candidate)
    return candidates


def _batch_prefix(
    *, market: str, session_date: date, account_mode: str, session_label: str
) -> str:
    session_digest = hashlib.sha256(session_label.encode("utf-8")).hexdigest()[:16]
    return (
        f"missed-opportunity:{market}:{session_date.isoformat()}:"
        f"{account_mode}:{session_digest}"
    )


def _correlation_id(prefix: str, rank: int, normalized_symbol: str) -> str:
    return f"{prefix}:{rank}:{normalized_symbol}"


def _forecast_target(
    *,
    candidate: MissedOpportunityCandidate,
    batch_id: str,
    rank: int,
    account_mode: str,
    index_symbol: str,
    index_change_pct: float,
) -> dict[str, Any]:
    return {
        "kind": RETURN_AT_HORIZON_KIND,
        "direction": "at_or_above",
        "reference_price": candidate.reference_price,
        "target_return_pct": candidate.target_return_pct,
        "measurement": "D0_reference_to_D5_close",
        "cohort": MISSED_TRIGGER,
        "batch_id": batch_id,
        "rank": rank,
        "account_mode": account_mode,
        "index_symbol": index_symbol,
        "index_change_pct": index_change_pct,
    }


def _closed_forecast_matches(
    row: TradeForecast,
    *,
    correlation_id: str,
    target: dict[str, Any],
    review_date: date,
    probability: float,
) -> bool:
    return (
        row.correlation_id == correlation_id
        and row.forecast_target == target
        and row.review_date == review_date
        and math.isclose(float(row.probability), probability, abs_tol=1e-9)
    )


async def _existing_batch_correlations(db: AsyncSession, *, prefix: str) -> set[str]:
    result = await db.execute(
        select(TradeForecast.correlation_id).where(
            TradeForecast.correlation_id.startswith(f"{prefix}:", autoescape=True)
        )
    )
    return {value for value in result.scalars().all() if value is not None}


async def save_missed_opportunities(
    db: AsyncSession,
    *,
    created_by: str,
    market: str,
    session_date: str | date,
    account_mode: str,
    index_symbol: str,
    index_change_pct: float,
    new_buy_count: int,
    candidates: list[dict[str, Any]],
    session_label: str,
    top_n: int = DEFAULT_TOP_N,
    model_label: str | None = None,
    policy_version: str | None = None,
    artifact_uuid: str | None = None,
    report_uuid: str | None = None,
) -> dict[str, Any]:
    """Save the mandatory top-N cohort when the strict session gate fires.

    A move of exactly +2% or -2% does not fire; the issue requires an absolute
    move *greater than* 2%. Candidate validation and all deterministic IDs are
    completed before the first write, so malformed required batches are atomic.
    """

    change, buy_count = _validate_gate(
        market=market,
        index_change_pct=index_change_pct,
        new_buy_count=new_buy_count,
    )
    parsed_date = _parse_session_date(session_date)
    required = abs(change) > INDEX_MOVE_THRESHOLD_PCT and buy_count == 0
    if not required:
        reasons: list[str] = []
        if abs(change) <= INDEX_MOVE_THRESHOLD_PCT:
            reasons.append("index_move_not_above_2pct")
        if buy_count != 0:
            reasons.append("new_buy_count_not_zero")
        return {
            "required": False,
            "action": "not_required",
            "reasons": reasons,
            "forecast_count": 0,
            "entries": [],
        }

    creator = _required_text(created_by, "created_by")
    label = _required_text(session_label, "session_label")
    index = _required_text(index_symbol, "index_symbol")
    if account_mode not in VALID_ACCOUNT_MODES:
        raise MissedOpportunityValidationError(f"invalid account_mode: {account_mode}")
    review_date, horizon = _d5_review_date(market, parsed_date)
    validated = _validate_candidates(candidates, market=market, top_n=top_n)
    prefix = _batch_prefix(
        market=market,
        session_date=parsed_date,
        account_mode=account_mode,
        session_label=label,
    )

    prepared: list[dict[str, Any]] = []
    for rank, candidate in enumerate(validated, start=1):
        normalized = _normalize_symbol(candidate.symbol, candidate.instrument_type)
        correlation_id = _correlation_id(prefix, rank, normalized)
        prepared.append(
            {
                "rank": rank,
                "candidate": candidate,
                "normalized_symbol": normalized,
                "correlation_id": correlation_id,
                "forecast_id": uuid.uuid5(uuid.NAMESPACE_URL, correlation_id),
                "target": _forecast_target(
                    candidate=candidate,
                    batch_id=prefix,
                    rank=rank,
                    account_mode=account_mode,
                    index_symbol=index,
                    index_change_pct=change,
                ),
            }
        )

    expected_correlations = {item["correlation_id"] for item in prepared}
    existing_correlations = await _existing_batch_correlations(db, prefix=prefix)
    if existing_correlations and existing_correlations != expected_correlations:
        raise MissedOpportunityValidationError(
            "an existing missed-opportunity batch has a different ranked candidate "
            "set; use a new session_label instead of mutating cohort membership"
        )

    entries: list[dict[str, Any]] = []
    for item in prepared:
        candidate = item["candidate"]
        forecast_id = item["forecast_id"]
        correlation_id = item["correlation_id"]
        target = item["target"]
        existing_forecast = await get_forecast(db, forecast_id)
        if existing_forecast is not None and existing_forecast.status != "open":
            if not _closed_forecast_matches(
                existing_forecast,
                correlation_id=correlation_id,
                target=target,
                review_date=review_date,
                probability=candidate.probability,
            ):
                raise MissedOpportunityValidationError(
                    f"closed forecast conflicts with retry: {forecast_id}"
                )
            forecast_action = "unchanged"
            forecast = existing_forecast
        else:
            forecast_action, forecast = await save_forecast(
                db,
                forecast_id=forecast_id,
                created_by=creator,
                symbol=candidate.symbol,
                instrument_type=candidate.instrument_type,
                forecast_target=target,
                probability=candidate.probability,
                forecast_start_date=parsed_date,
                review_date=review_date,
                horizon=horizon,
                evidence_ids=candidate.evidence_ids,
                contrary_evidence=candidate.rejection_reason,
                session_label=label,
                model_label=model_label,
                policy_version=policy_version,
                artifact_uuid=artifact_uuid,
                report_uuid=report_uuid,
                report_item_uuid=candidate.report_item_uuid,
                correlation_id=correlation_id,
            )

        retrospective_repo = TradeRetrospectiveRepository(db)
        existing_retro = await retrospective_repo.get_by_correlation_id(
            correlation_id, account_mode
        )
        evidence_snapshot = dict(
            (existing_retro.evidence_snapshot or {}) if existing_retro else {}
        )
        evidence_snapshot.update(
            {
                "cohort": MISSED_TRIGGER,
                "batch_id": prefix,
                "rank": item["rank"],
                "index_symbol": index,
                "index_change_pct": change,
                "session_date": parsed_date.isoformat(),
                "review_date": review_date.isoformat(),
                "reference_price": candidate.reference_price,
                "target_return_pct": candidate.target_return_pct,
                "forecast_id": str(forecast.forecast_id),
            }
        )

        if existing_retro is not None and forecast.status != "open":
            retro_action = "unchanged"
            retro = existing_retro
        else:
            retro_kwargs: dict[str, Any] = {}
            if existing_retro is None:
                retro_kwargs = {
                    "trigger_type": MISSED_TRIGGER,
                    "next_actions": [
                        {
                            "action": "Score the linked D+5 missed-opportunity forecast",
                            "status": "open",
                            "due_kst_date": review_date.isoformat(),
                        }
                    ],
                }
            retro_action, retro = await save_retrospective(
                db,
                symbol=candidate.symbol,
                instrument_type=candidate.instrument_type,
                account_mode=account_mode,
                outcome="unfilled",
                side="buy",
                market=market,
                strategy_key=MISSED_TRIGGER,
                correlation_id=correlation_id,
                report_uuid=report_uuid,
                report_item_uuid=candidate.report_item_uuid,
                rationale=candidate.rejection_reason,
                result_summary="D+5 missed-opportunity forecast pending",
                next_strategy="Compare zero-entry decision with the scored missed cohort",
                evidence_snapshot=evidence_snapshot,
                created_by_profile=creator,
                policy_version=forecast.policy_version,
                **retro_kwargs,
            )

        entries.append(
            {
                "rank": item["rank"],
                "symbol": item["normalized_symbol"],
                "correlation_id": correlation_id,
                "forecast_id": str(forecast.forecast_id),
                "retrospective_id": retro.id,
                "forecast_action": forecast_action,
                "retrospective_action": retro_action,
            }
        )

    return {
        "required": True,
        "action": "published",
        "market": market,
        "session_date": parsed_date.isoformat(),
        "review_date": review_date.isoformat(),
        "horizon": horizon,
        "index_symbol": index,
        "index_change_pct": change,
        "new_buy_count": buy_count,
        "top_n": top_n,
        "forecast_count": len(entries),
        "entries": entries,
    }


async def sync_resolved_missed_retrospective(
    db: AsyncSession,
    *,
    forecast: TradeForecast,
    observed_return_pct: float,
) -> bool:
    """Project a resolved D+5 return into its linked missed cohort row."""

    target = forecast.forecast_target or {}
    if (
        target.get("kind") != RETURN_AT_HORIZON_KIND
        or target.get("cohort") != MISSED_TRIGGER
        or not forecast.correlation_id
    ):
        return False
    account_mode = target.get("account_mode")
    if account_mode not in VALID_ACCOUNT_MODES:
        return False
    repo = TradeRetrospectiveRepository(db)
    retro = await repo.get_by_correlation_id(forecast.correlation_id, account_mode)
    if retro is None or retro.trigger_type != MISSED_TRIGGER:
        return False

    snapshot = dict(retro.evidence_snapshot or {})
    detail = forecast.resolution_detail or {}
    snapshot["resolution"] = {
        "forecast_id": str(forecast.forecast_id),
        "review_date": forecast.review_date.isoformat(),
        "outcome": forecast.outcome,
        "observed_return_pct": observed_return_pct,
        "horizon_close": detail.get("horizon_close"),
        "brier_score": (
            float(forecast.brier_score) if forecast.brier_score is not None else None
        ),
        "resolution_source": forecast.resolution_source,
    }
    await repo.upsert(
        {
            "correlation_id": forecast.correlation_id,
            "account_mode": account_mode,
            "pnl_pct": Decimal(str(round(observed_return_pct, 4))),
            "result_summary": (
                f"D+5 missed-opportunity return {observed_return_pct:+.4f}%"
            ),
            "evidence_snapshot": snapshot,
        }
    )
    return True


__all__ = [
    "DEFAULT_TOP_N",
    "INDEX_MOVE_THRESHOLD_PCT",
    "MISSED_TRIGGER",
    "MissedOpportunityValidationError",
    "RETURN_AT_HORIZON_KIND",
    "save_missed_opportunities",
    "sync_resolved_missed_retrospective",
]
