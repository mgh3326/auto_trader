# app/services/trade_journal/forecast_service.py
"""ROB-650 — resolvable forecast ledger: record, deterministic resolve, score.

The repository is the only write surface for ``review.trade_forecasts``.
Composition of a forecast (choosing the probability/thesis) is a Claude session
(LLM boundary); everything here — recording, OHLCV-backed resolution, Brier
scoring, calibration aggregation — is fully deterministic and side-effect-free
apart from the DB write.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.models.review import TradeForecast
from app.models.trading import InstrumentType
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.trading_policy_service import policy_version_stamp

logger = logging.getLogger(__name__)


# Fail-open fallback policy stamp. ROB-659: the default now comes from the ROB-646
# trading-policy YAML single source via ``_default_policy_version`` (below); this
# literal is only used if that YAML is unreadable, so a forecast write never
# crashes on a missing policy file. A caller-supplied ``policy_version`` still wins.
POLICY_VERSION = "forecast.v1"

# Crypto pairs are stored with a market-prefix separated by '-' (e.g. "KRW-BTC");
# that dash is a real separator, unlike an equity ticker's ("BRK-B" -> "BRK.B").
_CRYPTO_QUOTE_CURRENCIES = {"KRW", "BTC", "USDT", "USD"}


def _default_policy_version() -> str:
    """ROB-659: stamp the ROB-646 policy version, fail-open to the legacy literal."""
    try:
        return policy_version_stamp()["version"]
    except Exception:
        return POLICY_VERSION


def _normalize_symbol_for_filter(
    symbol: str, instrument_type: str | None = None
) -> str:
    """Normalize a *query* symbol to the stored DB form for filtering.

    ROB-659: mirrors the write-side ``_normalize_symbol`` so a query like "BRK-B"
    matches the stored "BRK.B". When ``instrument_type`` is known we reuse the exact
    write-side normalization; without it we apply the dash/slash -> dot rewrite but
    leave crypto pairs ("KRW-BTC") intact (their dash is a real market separator).
    """
    if instrument_type is not None:
        return _normalize_symbol(symbol, instrument_type)
    normalized = symbol.strip().upper()
    quote, sep, _base = normalized.partition("-")
    if sep and quote in _CRYPTO_QUOTE_CURRENCIES:
        return normalized
    return to_db_symbol(normalized)


_KST = ZoneInfo("Asia/Seoul")

_VALID_INSTRUMENTS = {t.value for t in InstrumentType}
# Instrument types with a loaded daily-candle store → deterministic auto-resolve.
_AUTO_RESOLVABLE_INSTRUMENTS = {"equity_kr", "equity_us", "crypto"}
_PRICE_DIRECTIONS = {"at_or_above", "at_or_below"}
_TERMINAL_CLOSE_KIND = "terminal_close"
_TERMINAL_CLOSE_DIRECTIONS = {"up", "down"}
_TERMINAL_CLOSE_INSTRUMENTS = {"equity_kr", "equity_us"}
_TERMINAL_CLOSE_RULE_VERSION = "terminal-close-v1-up-gte-down-lt"
_TERMINAL_CLOSE_ADJUSTMENT_POLICIES = {
    "explicit-factor-v1",
    "unverified_fail_closed",
}
# These source labels are written only by closed daily-candle paths. KIS/Toss
# request provider-adjusted prices; Yahoo requests auto_adjust=False. The
# terminal resolver records this distinction and always reads ``close`` (never
# high/low or the incompletely-populated US adj_close column).
_REGULAR_SESSION_CLOSE_SOURCE_BASIS = {
    "kis": "provider_adjusted",
    "toss": "provider_adjusted",
    "toss_fallback": "provider_adjusted",
    "yahoo": "raw",
    "yahoo_fallback": "raw",
}
_GROUP_BY_FIELDS = {"created_by", "session_label", "model_label", "day"}
_NO_RESOLVABLE_FORECAST_KIND = "no_resolvable_forecast"
_CLOSED_NO_CLAIM_STATUS = "closed_no_claim"


class ForecastValidationError(ValueError):
    """Raised when a forecast payload violates a typed constraint."""


class TerminalCloseDataError(ForecastValidationError):
    """Typed fail-closed condition for terminal-close source data."""

    def __init__(
        self,
        status: str,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.evidence = evidence or {}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in isolation)
# ---------------------------------------------------------------------------
def brier_score(probability: float, outcome: bool) -> float:
    """Brier score for a single binary forecast: ``(p - o)**2``, o in {0,1}.

    Boundaries: p=0/outcome=False -> 0; p=1/outcome=True -> 0; p=0.5 -> 0.25
    regardless of outcome; a fully-wrong confident call (p=1, outcome=False)
    -> 1.
    """
    o = 1.0 if outcome else 0.0
    return (float(probability) - o) ** 2


def classify_price_target_outcome(
    candles: list[DailyCandleRow],
    *,
    direction: str,
    target_price: float,
) -> tuple[bool, float]:
    """Deterministically resolve a price-target claim over a candle window.

    ``at_or_above``: outcome is True iff any bar's ``high`` reaches the target;
    the observed extreme is the window ``max(high)``. ``at_or_below``: True iff
    any bar's ``low`` reaches the target; observed extreme is ``min(low)``.
    Raises on empty candles (caller must guard) or an unknown direction.
    """
    if not candles:
        raise ForecastValidationError("cannot classify an empty candle window")
    if direction == "at_or_above":
        extreme = max(c.high for c in candles)
        return extreme >= target_price, extreme
    if direction == "at_or_below":
        extreme = min(c.low for c in candles)
        return extreme <= target_price, extreme
    raise ForecastValidationError(f"invalid price-target direction: {direction!r}")


def classify_terminal_close_outcome(
    candles: list[DailyCandleRow],
    *,
    review_date: date,
    direction: str,
    target_price: float,
) -> tuple[bool, float, DailyCandleRow]:
    """Resolve one review-session terminal close against a typed threshold.

    Exactly one trusted regular-session daily candle dated ``review_date`` is
    required. Only its ``close`` is observed. V1 defines complementary events:
    ``up`` is ``close >= target`` and ``down`` is ``close < target``.
    """
    if direction not in _TERMINAL_CLOSE_DIRECTIONS:
        raise ForecastValidationError(
            f"invalid terminal-close direction: {direction!r}"
        )

    matching = [candle for candle in candles if _row_date(candle) == review_date]
    if not matching:
        candidate_dates = sorted({_row_date(candle).isoformat() for candle in candles})
        status = (
            "unresolved_stale_data"
            if candidate_dates
            else "unresolved_no_review_candle"
        )
        reason = (
            f"no candle dated review_date={review_date.isoformat()}; "
            f"candidate_dates={candidate_dates}"
        )
        raise TerminalCloseDataError(
            status,
            reason,
            evidence={"candidate_source_dates": candidate_dates},
        )
    if len(matching) != 1:
        raise TerminalCloseDataError(
            "unresolved_ambiguous_review_candle",
            (
                f"expected exactly one review-date candle, found {len(matching)} "
                f"for {review_date.isoformat()}"
            ),
            evidence={"review_date_candle_count": len(matching)},
        )

    selected = matching[0]
    source = str(selected.source or "")
    source_basis = _REGULAR_SESSION_CLOSE_SOURCE_BASIS.get(source)
    if source_basis is None:
        raise TerminalCloseDataError(
            "unresolved_untrusted_source",
            f"daily candle source={source!r} is not a trusted regular-session source",
            evidence={"source": source},
        )

    close = float(selected.close)
    if not math.isfinite(close) or close <= 0:
        raise TerminalCloseDataError(
            "unresolved_invalid_close",
            f"review-date close must be positive and finite: {selected.close!r}",
            evidence={"source": source, "source_price": selected.close},
        )

    outcome = close >= target_price if direction == "up" else close < target_price
    return outcome, close, selected


def _to_decimal(x: float | None) -> Decimal | None:
    return Decimal(str(x)) if x is not None else None


def _normalize_symbol(symbol: str, instrument_type: str) -> str:
    normalized = symbol.strip().upper()
    if instrument_type == "crypto":
        if normalized and "-" not in normalized:
            return f"KRW-{normalized}"
        return normalized
    if instrument_type == "equity_us":
        return to_db_symbol(normalized).upper()
    return normalized


def _kst_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(_KST).date()


def _row_date(row: DailyCandleRow) -> date:
    ts = row.time_utc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return ts.date()


def _parse_date(value: str | date, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        raise ForecastValidationError(f"{field} must be YYYY-MM-DD: {value!r}") from exc


def _validate_forecast_target(
    target: Any,
    *,
    instrument_type: str,
    review_date: date,
) -> None:
    if not isinstance(target, dict):
        raise ForecastValidationError("forecast_target must be an object")
    kind = target.get("kind")
    if not kind or not isinstance(kind, str):
        raise ForecastValidationError("forecast_target.kind is required")
    if kind == "price_target":
        direction = target.get("direction")
        if direction not in _PRICE_DIRECTIONS:
            raise ForecastValidationError(
                f"price_target.direction must be one of {sorted(_PRICE_DIRECTIONS)}"
            )
        price = target.get("target_price")
        try:
            price_f = float(price)
        except (TypeError, ValueError) as exc:
            raise ForecastValidationError(
                "price_target.target_price must be a number"
            ) from exc
        if price_f <= 0:
            raise ForecastValidationError("price_target.target_price must be > 0")
        return
    if kind != _TERMINAL_CLOSE_KIND:
        return

    if instrument_type not in _TERMINAL_CLOSE_INSTRUMENTS:
        raise ForecastValidationError(
            "terminal_close requires instrument_type equity_kr or equity_us"
        )
    direction = target.get("direction")
    if direction not in _TERMINAL_CLOSE_DIRECTIONS:
        raise ForecastValidationError(
            "terminal_close.direction must be one of "
            f"{sorted(_TERMINAL_CLOSE_DIRECTIONS)}"
        )
    try:
        target_price = float(target.get("target_price"))
    except (TypeError, ValueError) as exc:
        raise ForecastValidationError(
            "terminal_close.target_price must be a number"
        ) from exc
    if not math.isfinite(target_price) or target_price <= 0:
        raise ForecastValidationError(
            "terminal_close.target_price must be positive and finite"
        )

    rule_version = target.get("outcome_rule_version")
    if rule_version != _TERMINAL_CLOSE_RULE_VERSION:
        raise ForecastValidationError(
            "terminal_close.outcome_rule_version must be "
            f"{_TERMINAL_CLOSE_RULE_VERSION!r}"
        )

    adjustment_policy = target.get("price_adjustment_policy")
    if adjustment_policy not in _TERMINAL_CLOSE_ADJUSTMENT_POLICIES:
        raise ForecastValidationError(
            "terminal_close.price_adjustment_policy must be one of "
            f"{sorted(_TERMINAL_CLOSE_ADJUSTMENT_POLICIES)}"
        )
    if adjustment_policy == "unverified_fail_closed":
        return

    try:
        factor = float(target.get("target_to_close_factor"))
    except (TypeError, ValueError) as exc:
        raise ForecastValidationError(
            "terminal_close.target_to_close_factor must be a number"
        ) from exc
    if not math.isfinite(factor) or factor <= 0:
        raise ForecastValidationError(
            "terminal_close.target_to_close_factor must be positive and finite"
        )
    if not math.isfinite(target_price * factor):
        raise ForecastValidationError(
            "terminal_close effective target must be positive and finite"
        )

    provenance = target.get("adjustment_provenance")
    if not isinstance(provenance, dict):
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance must be an object"
        )
    for field in ("source", "evidence_ref"):
        value = provenance.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ForecastValidationError(
                f"terminal_close.adjustment_provenance.{field} is required"
            )
    verified_through = _parse_date(
        provenance.get("verified_through_date"),
        "terminal_close.adjustment_provenance.verified_through_date",
    )
    if verified_through != review_date:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.verified_through_date "
            "must equal review_date"
        )


def serialize_forecast(r: TradeForecast) -> dict[str, Any]:
    return {
        "id": r.id,
        "forecast_id": str(r.forecast_id),
        "artifact_uuid": r.artifact_uuid,
        "journal_id": r.journal_id,
        "report_uuid": r.report_uuid,
        "report_item_uuid": r.report_item_uuid,
        "correlation_id": r.correlation_id,
        "created_by": r.created_by,
        "session_label": r.session_label,
        "model_label": r.model_label,
        "policy_version": r.policy_version,
        "symbol": r.symbol,
        "instrument_type": (
            r.instrument_type.value
            if hasattr(r.instrument_type, "value")
            else str(r.instrument_type)
        ),
        "forecast_target": r.forecast_target,
        "horizon": r.horizon,
        "probability": float(r.probability) if r.probability is not None else None,
        "probability_range_low": (
            float(r.probability_range_low)
            if r.probability_range_low is not None
            else None
        ),
        "probability_range_high": (
            float(r.probability_range_high)
            if r.probability_range_high is not None
            else None
        ),
        "evidence_ids": r.evidence_ids,
        "contrary_evidence": r.contrary_evidence,
        "resolution_source": r.resolution_source,
        "forecast_start_date": (
            r.forecast_start_date.isoformat() if r.forecast_start_date else None
        ),
        "review_date": r.review_date.isoformat() if r.review_date else None,
        "status": r.status,
        "outcome": r.outcome,
        "observed_value": (
            float(r.observed_value) if r.observed_value is not None else None
        ),
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "brier_score": float(r.brier_score) if r.brier_score is not None else None,
        "resolution_detail": r.resolution_detail,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Repository — the only write surface
# ---------------------------------------------------------------------------
class ForecastRepository:
    """The only write surface for review.trade_forecasts."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_forecast_id(self, forecast_id: uuid.UUID) -> TradeForecast | None:
        result = await self.db.execute(
            select(TradeForecast).where(TradeForecast.forecast_id == forecast_id)
        )
        return result.scalar_one_or_none()

    async def upsert(self, payload: dict[str, Any]) -> tuple[str, TradeForecast]:
        fid = payload.get("forecast_id")
        if fid is not None:
            existing = await self.get_by_forecast_id(fid)
            if existing is not None:
                if existing.status != "open":
                    raise ForecastValidationError(
                        f"cannot modify a closed (resolved) forecast; forecast_id={fid}"
                    )
                for key, value in payload.items():
                    setattr(existing, key, value)
                await self.db.flush()
                return "updated", existing
        row = TradeForecast(**payload)
        self.db.add(row)
        await self.db.flush()
        return "created", row


def _coerce_forecast_id(value: str | uuid.UUID | None) -> uuid.UUID:
    if value is None:
        return uuid.uuid4()
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise ForecastValidationError(f"invalid forecast_id: {value!r}") from exc


async def save_forecast(
    db: AsyncSession,
    *,
    created_by: str,
    symbol: str,
    instrument_type: str,
    forecast_target: dict,
    probability: float,
    review_date: str | date,
    forecast_id: str | uuid.UUID | None = None,
    horizon: str | None = None,
    probability_range_low: float | None = None,
    probability_range_high: float | None = None,
    evidence_ids: list | None = None,
    contrary_evidence: str | None = None,
    forecast_start_date: str | date | None = None,
    resolution_source: str | None = None,
    session_label: str | None = None,
    model_label: str | None = None,
    policy_version: str | None = None,
    artifact_uuid: str | None = None,
    journal_id: int | None = None,
    report_uuid: str | None = None,
    report_item_uuid: str | None = None,
    correlation_id: str | None = None,
) -> tuple[str, TradeForecast]:
    if not (created_by or "").strip():
        raise ForecastValidationError("created_by is required")
    if instrument_type not in _VALID_INSTRUMENTS:
        raise ForecastValidationError(f"invalid instrument_type: {instrument_type}")
    if not (symbol or "").strip():
        raise ForecastValidationError("symbol is required")
    try:
        prob = float(probability)
    except (TypeError, ValueError) as exc:
        raise ForecastValidationError("probability must be a number") from exc
    if not 0.0 <= prob <= 1.0:
        raise ForecastValidationError("probability must be within [0, 1]")

    lo = probability_range_low
    hi = probability_range_high
    if (lo is None) != (hi is None):
        raise ForecastValidationError(
            "probability_range requires both low and high (or neither)"
        )
    if lo is not None and hi is not None:
        lo_f, hi_f = float(lo), float(hi)
        if not (0.0 <= lo_f <= 1.0 and 0.0 <= hi_f <= 1.0):
            raise ForecastValidationError("probability_range must be within [0, 1]")
        if lo_f > hi_f:
            raise ForecastValidationError("probability_range_low must be <= high")
        if not lo_f <= prob <= hi_f:
            raise ForecastValidationError(
                "probability must fall within probability_range"
            )

    review = _parse_date(review_date, "review_date")
    _validate_forecast_target(
        forecast_target,
        instrument_type=instrument_type,
        review_date=review,
    )
    start = (
        _parse_date(forecast_start_date, "forecast_start_date")
        if forecast_start_date is not None
        else None
    )
    if start is not None and start > review:
        raise ForecastValidationError("forecast_start_date must be <= review_date")

    payload: dict[str, Any] = {
        "forecast_id": _coerce_forecast_id(forecast_id),
        "created_by": created_by.strip(),
        "symbol": _normalize_symbol(symbol, instrument_type),
        "instrument_type": instrument_type,
        "forecast_target": forecast_target,
        "probability": _to_decimal(prob),
        "probability_range_low": _to_decimal(lo),
        "probability_range_high": _to_decimal(hi),
        "review_date": review,
        "forecast_start_date": start,
        "horizon": horizon,
        "evidence_ids": evidence_ids,
        "contrary_evidence": contrary_evidence,
        "resolution_source": resolution_source,
        "session_label": session_label,
        "model_label": model_label,
        "policy_version": policy_version or _default_policy_version(),
        "artifact_uuid": artifact_uuid,
        "journal_id": journal_id,
        "report_uuid": report_uuid,
        "report_item_uuid": report_item_uuid,
        "correlation_id": correlation_id,
        "status": "open",
    }
    return await ForecastRepository(db).upsert(payload)


async def get_forecast(
    db: AsyncSession, forecast_id: str | uuid.UUID
) -> TradeForecast | None:
    return await ForecastRepository(db).get_by_forecast_id(
        _coerce_forecast_id(forecast_id)
    )


async def list_due_forecasts(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[TradeForecast]:
    today = (now or now_kst()).astimezone(_KST).date()
    stmt = (
        select(TradeForecast)
        .where(
            TradeForecast.status == "open",
            TradeForecast.review_date <= today,
        )
        .order_by(TradeForecast.review_date.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_forecasts(
    db: AsyncSession,
    *,
    status: str | None = None,
    symbol: str | None = None,
    created_by: str | None = None,
    correlation_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    filters = []
    if status is not None:
        filters.append(TradeForecast.status == status)
    if symbol is not None:
        filters.append(TradeForecast.symbol == _normalize_symbol_for_filter(symbol))
    if created_by is not None:
        filters.append(TradeForecast.created_by == created_by)
    if correlation_id is not None:
        filters.append(TradeForecast.correlation_id == correlation_id)
    stmt = (
        select(TradeForecast)
        .where(*filters)
        .order_by(TradeForecast.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "entries": [serialize_forecast(r) for r in rows],
        "summary": {"count": len(rows), "by_status": by_status},
    }


def _forecast_scope_filters(
    *,
    status: str | None,
    symbol: str | None,
    created_by: str | None,
    instrument_type: str | None,
) -> list[Any]:
    filters: list[Any] = []
    if status is not None:
        filters.append(TradeForecast.status == status)
    if symbol is not None:
        filters.append(
            TradeForecast.symbol
            == _normalize_symbol_for_filter(symbol, instrument_type)
        )
    if created_by is not None:
        filters.append(TradeForecast.created_by == created_by)
    if instrument_type is not None:
        filters.append(TradeForecast.instrument_type == instrument_type)
    return filters


async def _run_forecast_listing(
    db: AsyncSession, *, filters: list[Any], order_by: Any, limit: int
) -> dict[str, Any]:
    stmt = select(TradeForecast).where(*filters).order_by(order_by).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "entries": [serialize_forecast(r) for r in rows],
        "summary": {"count": len(rows), "by_status": by_status},
    }


async def list_open_forecasts(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    created_by: str | None = None,
    instrument_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Open forecasts ordered by ``review_date`` ASC — the scoring-due queue (ROB-663).

    Soonest (and overdue) review dates sort first so the web surface can show the
    "채점 due 대기열". Ordering is done in SQL so ``limit`` selects the most imminent
    rows rather than merely the most-recently-created ones.
    """
    filters = _forecast_scope_filters(
        status="open",
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
    )
    return await _run_forecast_listing(
        db, filters=filters, order_by=TradeForecast.review_date.asc(), limit=limit
    )


async def list_closed_forecasts(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    created_by: str | None = None,
    instrument_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Closed/scored forecasts ordered by ``resolved_at`` DESC — recent scoring
    history with ``outcome``/``brier_score`` populated (ROB-663)."""
    filters = _forecast_scope_filters(
        status="closed",
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
    )
    return await _run_forecast_listing(
        db,
        filters=filters,
        order_by=TradeForecast.resolved_at.desc().nulls_last(),
        limit=limit,
    )


_BACKFILL_HORIZON_BARS = 200


async def _resolve_candle_partition(
    db: AsyncSession, *, symbol: str, instrument_type: str
) -> tuple[MarketKey, str] | None:
    """Resolve (market, partition) for the daily-candle store.

    Single source of truth so the resolution read (_read_window_candles) and the
    lazy backfill (_backfill_daily_candles) always use the SAME partition string
    — crypto in particular must be "upbit_krw" so both sides resolve the same
    crypto_instruments row (repository.py:141). Returns None when the US exchange
    lookup fails or the instrument has no daily store. ROB-712.
    """
    if instrument_type == "equity_kr":
        return MarketKey.KR, "KRX"
    if instrument_type == "crypto":
        return MarketKey.CRYPTO, "upbit_krw"
    if instrument_type == "equity_us":
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        try:
            partition = await get_us_exchange_by_symbol(symbol, db=db)
        except Exception:
            return None
        if not partition:
            return None
        return MarketKey.US, partition
    return None


async def _backfill_daily_candles(
    *,
    symbol: str,
    market: MarketKey,
    partition: str,
    horizon_bars: int = _BACKFILL_HORIZON_BARS,
) -> int:
    """Best-effort one-symbol daily-candle fetch+persist for a not-yet-loaded
    (typically rejected/non-held) symbol so its price_target forecast can
    resolve. Uses the shared sync service on its OWN session (commits+closes via
    close_callbacks). Never raises — returns 0 on any failure so resolve stays
    graceful (unresolved_no_data). ROB-712.
    """
    from app.services.daily_candles.sync_service import (
        SyncTarget,
        _build_default_service,
    )

    try:
        service = await _build_default_service()
    except Exception:
        logger.exception("ROB-712 backfill: service build failed symbol=%s", symbol)
        return 0
    try:
        result = await service.sync_one(
            target=SyncTarget(market=market, symbol=symbol, partition=partition),
            horizon_bars=horizon_bars,
        )
        return result.rows_upserted
    except Exception:
        logger.exception("ROB-712 backfill: sync_one failed symbol=%s", symbol)
        return 0
    finally:
        await service.close()


async def _read_window_candles(
    db: AsyncSession,
    *,
    symbol: str,
    instrument_type: str,
    start_date: date,
    review_date: date,
) -> list[DailyCandleRow] | None:
    """Read loaded daily candles within [start_date, review_date] (inclusive).

    Returns ``None`` when the instrument's partition cannot be resolved (US
    exchange lookup failure) so the caller can mark the forecast unresolved
    rather than scoring against an empty window.
    """
    resolved = await _resolve_candle_partition(
        db, symbol=symbol, instrument_type=instrument_type
    )
    if resolved is None:
        return None
    market, partition = resolved

    # Pad the UTC window by 2 days each side to absorb tz/session boundary skew,
    # then filter by the candle's calendar date for a clean inclusive window.
    start_dt = datetime.combine(
        start_date - timedelta(days=2), dt.time(0, 0), tzinfo=dt.UTC
    )
    end_dt = datetime.combine(
        review_date + timedelta(days=2), dt.time(23, 59, 59), tzinfo=dt.UTC
    )
    repo = DailyCandlesRepository(session=db)
    rows = await repo.fetch_range(
        market=market,
        symbol=symbol,
        partition=partition,
        start=start_dt,
        end=end_dt,
    )
    return [r for r in rows if start_date <= _row_date(r) <= review_date]


def _terminal_close_session_failure(
    *,
    instrument_type: str,
    review_date: date,
    now: datetime,
) -> str | None:
    """Return a fail-closed reason unless the review session is final."""
    from app.services.daily_candles.read_service import (
        get_calendar,
        last_final_session_kr,
        last_final_session_us,
    )

    calendar_name = "XKRX" if instrument_type == "equity_kr" else "XNYS"
    try:
        if not bool(get_calendar(calendar_name).is_session(review_date.isoformat())):
            return (
                f"review_date={review_date.isoformat()} is not a "
                f"{calendar_name} regular session"
            )
    except Exception as exc:
        return f"could not verify {calendar_name} review session: {exc}"

    last_final = (
        last_final_session_kr(now)
        if instrument_type == "equity_kr"
        else last_final_session_us(now)
    )
    if last_final is None:
        return f"could not determine the latest final {calendar_name} session"
    if review_date > last_final:
        return (
            f"review session {review_date.isoformat()} is not final; "
            f"latest_final_session={last_final.isoformat()}"
        )
    return None


def _terminal_resolution_evidence(
    row: TradeForecast,
    target: dict[str, Any],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "target_kind": _TERMINAL_CLOSE_KIND,
        "outcome_rule_version": target.get("outcome_rule_version"),
        "direction": target.get("direction"),
        "target_price": float(target.get("target_price")),
        "review_date": row.review_date.isoformat(),
        "price_adjustment_policy": target.get("price_adjustment_policy"),
    }
    if target.get("target_to_close_factor") is not None:
        evidence["target_to_close_factor"] = float(target.get("target_to_close_factor"))
    if target.get("adjustment_provenance") is not None:
        evidence["adjustment_provenance"] = target.get("adjustment_provenance")
    return evidence


async def resolve_forecast(
    db: AsyncSession,
    *,
    forecast_id: str | uuid.UUID,
    persist: bool,
    manual_outcome: bool | None = None,
    manual_observed_value: float | None = None,
    manual_evidence: Any | None = None,
    now: datetime | None = None,
    backfill_missing: bool = True,
) -> dict[str, Any]:
    """Resolve one forecast. Idempotent: a closed forecast is never re-scored.

    ``persist=False`` computes the outcome/Brier and returns a preview without
    mutating the row (the dry-run default at the tool boundary). Price-target
    forecasts retain their window-touch OHLCV semantics. Terminal-close
    forecasts use exactly one final review-date regular-session ``close`` under
    their typed outcome/adjustment contract. Other kinds require
    ``manual_outcome`` + ``manual_evidence``.
    """
    repo = ForecastRepository(db)
    row = await repo.get_by_forecast_id(_coerce_forecast_id(forecast_id))
    if row is None:
        raise ForecastValidationError(f"forecast not found: {forecast_id}")
    if row.status != "open":
        return {
            "status": "already_closed",
            "changed": False,
            "forecast": serialize_forecast(row),
        }

    resolved_now = now or now_kst()
    target = row.forecast_target or {}
    kind = target.get("kind")
    instrument = (
        row.instrument_type.value
        if hasattr(row.instrument_type, "value")
        else str(row.instrument_type)
    )

    if kind == _NO_RESOLVABLE_FORECAST_KIND:
        reason = "placeholder has no resolvable claim"
        if not persist:
            return {
                "status": "would_close_no_claim",
                "changed": False,
                "auto_close": True,
                "computed": None,
                "reason": reason,
                "forecast": serialize_forecast(row),
            }

        row.resolution_source = "not_applicable"
        row.resolution_detail = {
            "resolved_kind": kind,
            "reason": reason,
        }
        row.resolved_at = resolved_now
        row.status = _CLOSED_NO_CLAIM_STATUS
        await db.flush()
        await db.refresh(row)
        return {
            "status": _CLOSED_NO_CLAIM_STATUS,
            "changed": True,
            "auto_close": True,
            "computed": None,
            "reason": reason,
            "forecast": serialize_forecast(row),
        }

    if kind == _TERMINAL_CLOSE_KIND and manual_outcome is not None:
        raise ForecastValidationError(
            "terminal_close does not accept a free-form manual outcome; "
            "update its typed price-adjustment evidence and resolve deterministically"
        )

    if manual_outcome is not None:
        if not manual_evidence:
            raise ForecastValidationError(
                "manual resolution requires evidence (manual_evidence)"
            )
        outcome = bool(manual_outcome)
        observed = manual_observed_value
        resolution_source = "manual"
        detail: dict[str, Any] = {
            "resolved_kind": kind,
            "manual_evidence": manual_evidence,
        }
    elif kind == "price_target":
        if instrument not in _AUTO_RESOLVABLE_INSTRUMENTS:
            return {
                "status": "requires_manual",
                "changed": False,
                "reason": (
                    f"instrument_type={instrument} has no daily candle store; "
                    "supply manual_outcome + manual_evidence"
                ),
                "forecast": serialize_forecast(row),
            }
        start_date = row.forecast_start_date or _kst_date(row.created_at)
        candles = await _read_window_candles(
            db,
            symbol=row.symbol,
            instrument_type=instrument,
            start_date=start_date,
            review_date=row.review_date,
        )
        # ROB-712: rejected (non-held) symbols usually have no daily OHLCV in
        # the DB yet. Lazily fetch+persist once via the shared sync service, then
        # re-read. Never raises — backfill returns 0 on failure and the existing
        # unresolved_no_data branch still runs below.
        if not candles and backfill_missing:
            resolved = await _resolve_candle_partition(
                db, symbol=row.symbol, instrument_type=instrument
            )
            if resolved is not None:
                market, partition = resolved
                rows = await _backfill_daily_candles(
                    symbol=row.symbol, market=market, partition=partition
                )
                if rows:
                    candles = await _read_window_candles(
                        db,
                        symbol=row.symbol,
                        instrument_type=instrument,
                        start_date=start_date,
                        review_date=row.review_date,
                    )
        if not candles:
            return {
                "status": "unresolved_no_data",
                "changed": False,
                "reason": "no loaded daily candles in the resolution window",
                "forecast": serialize_forecast(row),
            }

        direction = target.get("direction")
        target_price = float(target.get("target_price"))
        outcome, observed = classify_price_target_outcome(
            candles, direction=direction, target_price=target_price
        )
        resolution_source = "ohlcv_day"
        detail = {
            "window_start": start_date.isoformat(),
            "window_end": row.review_date.isoformat(),
            "candles": len(candles),
            "direction": direction,
            "target_price": target_price,
            "observed_extreme": observed,
        }
    elif kind == _TERMINAL_CLOSE_KIND:
        terminal_evidence = _terminal_resolution_evidence(row, target)
        if instrument not in _TERMINAL_CLOSE_INSTRUMENTS:
            return {
                "status": "requires_manual",
                "changed": False,
                "reason": (
                    f"instrument_type={instrument} has no regular-session "
                    "terminal-close contract"
                ),
                "resolution_evidence": terminal_evidence,
                "forecast": serialize_forecast(row),
            }

        adjustment_policy = target.get("price_adjustment_policy")
        if adjustment_policy != "explicit-factor-v1":
            return {
                "status": "requires_adjustment_evidence",
                "changed": False,
                "reason": (
                    "terminal_close is fail-closed until an explicit target-to-close "
                    "factor and review-date corporate-action provenance are stored"
                ),
                "resolution_evidence": terminal_evidence,
                "forecast": serialize_forecast(row),
            }

        session_failure = _terminal_close_session_failure(
            instrument_type=instrument,
            review_date=row.review_date,
            now=resolved_now,
        )
        if session_failure is not None:
            return {
                "status": "unresolved_session_not_final",
                "changed": False,
                "reason": session_failure,
                "resolution_evidence": terminal_evidence,
                "forecast": serialize_forecast(row),
            }

        candles = await _read_window_candles(
            db,
            symbol=row.symbol,
            instrument_type=instrument,
            start_date=row.review_date,
            review_date=row.review_date,
        )
        if not candles and backfill_missing:
            resolved = await _resolve_candle_partition(
                db, symbol=row.symbol, instrument_type=instrument
            )
            if resolved is not None:
                market, partition = resolved
                await _backfill_daily_candles(
                    symbol=row.symbol, market=market, partition=partition
                )
                # Daily-candle batch upserts may report rowcount=0 even after a
                # successful write, so always re-read once after the attempt.
                candles = await _read_window_candles(
                    db,
                    symbol=row.symbol,
                    instrument_type=instrument,
                    start_date=row.review_date,
                    review_date=row.review_date,
                )

        original_target = float(target.get("target_price"))
        adjustment_factor = float(target.get("target_to_close_factor"))
        effective_target = original_target * adjustment_factor
        try:
            outcome, observed, selected = classify_terminal_close_outcome(
                candles or [],
                review_date=row.review_date,
                direction=str(target.get("direction")),
                target_price=effective_target,
            )
        except TerminalCloseDataError as exc:
            return {
                "status": exc.status,
                "changed": False,
                "reason": str(exc),
                "resolution_evidence": {
                    **terminal_evidence,
                    **exc.evidence,
                },
                "forecast": serialize_forecast(row),
            }

        direction = str(target.get("direction"))
        source = str(selected.source)
        resolution_source = "ohlcv_day_terminal_close"
        detail = {
            **terminal_evidence,
            "comparison_operator": ">=" if direction == "up" else "<",
            "original_target_price": original_target,
            "target_to_close_factor": adjustment_factor,
            "effective_target_price": effective_target,
            "source_date": _row_date(selected).isoformat(),
            "source_timestamp": selected.time_utc.isoformat(),
            "source": source,
            "source_partition": selected.partition,
            "source_price": observed,
            "source_price_field": "close",
            "source_price_basis": _REGULAR_SESSION_CLOSE_SOURCE_BASIS[source],
            "regular_session_only": True,
            "adj_close_used": False,
        }
    else:
        return {
            "status": "requires_manual",
            "changed": False,
            "reason": (
                f"forecast_target.kind={kind!r} is non-price; "
                "supply manual_outcome + manual_evidence"
            ),
            "forecast": serialize_forecast(row),
        }

    brier = brier_score(float(row.probability), outcome)
    computed = {
        "outcome": outcome,
        "observed_value": observed,
        "brier_score": round(brier, 5),
        "resolution_source": resolution_source,
        "resolution_detail": detail,
    }
    if not persist:
        return {
            "status": "previewed",
            "changed": False,
            "computed": computed,
            "forecast": serialize_forecast(row),
        }

    row.outcome = outcome
    row.observed_value = _to_decimal(observed)
    row.brier_score = _to_decimal(round(brier, 5))
    row.resolution_source = resolution_source
    row.resolution_detail = detail
    row.resolved_at = resolved_now
    row.status = "closed"
    await db.flush()
    # Reload server-computed columns (updated_at onupdate) within the async
    # context so serialize_forecast doesn't trigger a lazy sync refresh.
    await db.refresh(row)
    return {
        "status": "resolved",
        "changed": True,
        "computed": computed,
        "forecast": serialize_forecast(row),
    }


def _group_key(r: TradeForecast, group_by: str) -> str:
    if group_by == "day":
        d = _kst_date(r.created_at)
        return d.isoformat() if d else "unknown"
    value = getattr(r, group_by, None)
    return value if value else "unlabeled"


async def _fetch_calibration_rows(
    db: AsyncSession, *, filters: list
) -> list[TradeForecast]:
    """Fetch closed+scored forecasts for calibration with the unused JSONB
    payload columns deferred. Calibration needs ALL matching rows (no LIMIT);
    it only reads brier_score/outcome/probability + the grouping attribute, so
    forecast_target / evidence_ids / resolution_detail are pure load waste."""
    result = await db.execute(
        select(TradeForecast)
        .where(*filters)
        .options(
            defer(TradeForecast.forecast_target),
            defer(TradeForecast.evidence_ids),
            defer(TradeForecast.resolution_detail),
        )
    )
    return list(result.scalars().all())


async def build_forecast_calibration_aggregate(
    db: AsyncSession,
    *,
    group_by: str = "created_by",
    created_by: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Calibration: Brier + hit-rate per label cohort (closed forecasts only).

    Groups closed, scored forecasts by ``created_by`` / ``session_label`` /
    ``model_label`` / KST ``day`` — the objective metric behind an operator's
    "does another LLM reach the same result" comparison. ``calibration_gap`` is
    ``avg_probability - hit_rate`` (positive = over-confident).
    """
    if group_by not in _GROUP_BY_FIELDS:
        group_by = "created_by"

    filters = [
        TradeForecast.status == "closed",
        TradeForecast.brier_score.isnot(None),
    ]
    if created_by is not None:
        filters.append(TradeForecast.created_by == created_by)
    if symbol is not None:
        filters.append(
            TradeForecast.symbol
            == _normalize_symbol_for_filter(symbol, instrument_type)
        )
    if instrument_type is not None:
        filters.append(TradeForecast.instrument_type == instrument_type)
    if days is not None:
        filters.append(TradeForecast.resolved_at >= now_kst() - timedelta(days=days))

    rows = await _fetch_calibration_rows(db, filters=filters)

    groups: dict[str, list[TradeForecast]] = {}
    for r in rows:
        groups.setdefault(_group_key(r, group_by), []).append(r)

    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        n = len(items)
        briers = [float(it.brier_score) for it in items if it.brier_score is not None]
        hits = sum(1 for it in items if it.outcome)
        probs = [float(it.probability) for it in items if it.probability is not None]
        avg_brier = sum(briers) / len(briers) if briers else None
        hit_rate = hits / n if n else None
        avg_prob = sum(probs) / len(probs) if probs else None
        calibration_gap = (
            avg_prob - hit_rate
            if (avg_prob is not None and hit_rate is not None)
            else None
        )
        out.append(
            {
                "group": key,
                "sample_size": n,
                "hits": hits,
                "misses": n - hits,
                "hit_rate": hit_rate,
                "avg_brier_score": avg_brier,
                "avg_probability": avg_prob,
                "calibration_gap": calibration_gap,
            }
        )
    out.sort(key=lambda g: -g["sample_size"])
    return {"group_by": group_by, "groups": out}
