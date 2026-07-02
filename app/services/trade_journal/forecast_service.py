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
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
_GROUP_BY_FIELDS = {"created_by", "session_label", "model_label", "day"}


class ForecastValidationError(ValueError):
    """Raised when a forecast payload violates a typed constraint."""


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


def _validate_forecast_target(target: Any) -> None:
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
                if existing.status == "closed":
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

    _validate_forecast_target(forecast_target)

    review = _parse_date(review_date, "review_date")
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
    if instrument_type == "equity_kr":
        market, partition = MarketKey.KR, "KRX"
    elif instrument_type == "crypto":
        market, partition = MarketKey.CRYPTO, "upbit_krw"
    elif instrument_type == "equity_us":
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        try:
            partition = await get_us_exchange_by_symbol(symbol, db=db)
        except Exception:
            return None
        if not partition:
            return None
        market = MarketKey.US
    else:
        return None

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


async def resolve_forecast(
    db: AsyncSession,
    *,
    forecast_id: str | uuid.UUID,
    persist: bool,
    manual_outcome: bool | None = None,
    manual_observed_value: float | None = None,
    manual_evidence: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve one forecast. Idempotent: a closed forecast is never re-scored.

    ``persist=False`` computes the outcome/Brier and returns a preview without
    mutating the row (the dry-run default at the tool boundary). Price-target
    forecasts resolve against loaded daily OHLCV; other kinds require
    ``manual_outcome`` + ``manual_evidence``.
    """
    repo = ForecastRepository(db)
    row = await repo.get_by_forecast_id(_coerce_forecast_id(forecast_id))
    if row is None:
        raise ForecastValidationError(f"forecast not found: {forecast_id}")
    if row.status == "closed":
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

    rows = (await db.execute(select(TradeForecast).where(*filters))).scalars().all()

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
