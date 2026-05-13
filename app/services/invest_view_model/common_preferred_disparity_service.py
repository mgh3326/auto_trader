"""Read-only common/preferred-share disparity view model service."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import mean, pstdev

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.schemas.invest_common_preferred_disparity import (
    CommonPreferredDisparityCard,
    CommonPreferredDisparityResponse,
    DisparityPeriodWindow,
    DisparitySource,
)
from app.services.invest_view_model.kr_preferred_pairs import (
    KRSymbolRow,
    discover_common_preferred_pairs,
)

_PERIOD_DAYS: dict[str, int] = {"1d": 1, "5d": 5, "20d": 20, "60d": 60}
_PRIMARY_WINDOW = "20d"
_DISPARITY_FORMULA = "((commonPrice - preferredPrice) / commonPrice) * 100"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_float(value: Decimal | int | float | None) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _disparity_pct(common_price: float | None, preferred_price: float | None) -> float | None:
    if common_price is None or preferred_price is None or common_price <= 0:
        return None
    return ((common_price - preferred_price) / common_price) * 100.0


def _state_from_cards(cards: list[CommonPreferredDisparityCard]) -> str:
    if not cards:
        return "missing"
    states = {card.dataState for card in cards}
    if states == {"fresh"}:
        return "fresh"
    if states <= {"missing"}:
        return "missing"
    if states <= {"stale", "missing"} and "stale" in states:
        return "stale"
    return "partial"


def _latest_by_source(
    rows: list[MarketQuoteSnapshot],
) -> dict[str, MarketQuoteSnapshot]:
    latest: dict[str, MarketQuoteSnapshot] = {}
    for row in rows:
        existing = latest.get(row.source)
        if existing is None or row.snapshot_at > existing.snapshot_at:
            latest[row.source] = row
    return latest


def _choose_same_source_quote(
    common_rows: list[MarketQuoteSnapshot],
    preferred_rows: list[MarketQuoteSnapshot],
) -> tuple[str | None, MarketQuoteSnapshot | None, MarketQuoteSnapshot | None]:
    common_latest = _latest_by_source(common_rows)
    preferred_latest = _latest_by_source(preferred_rows)
    shared = set(common_latest) & set(preferred_latest)
    if not shared:
        return None, None, None
    best_source = max(
        shared,
        key=lambda source: min(
            common_latest[source].snapshot_at,
            preferred_latest[source].snapshot_at,
        ),
    )
    return best_source, common_latest[best_source], preferred_latest[best_source]


def _window_values(
    *,
    common_rows: list[MarketQuoteSnapshot],
    preferred_rows: list[MarketQuoteSnapshot],
    source: str,
    as_of: datetime,
    days: int,
) -> list[float]:
    since = as_of - timedelta(days=days)
    common_by_at = {
        _as_aware(row.snapshot_at): _to_float(row.price)
        for row in common_rows
        if row.source == source and (_as_aware(row.snapshot_at) or as_of) >= since
    }
    preferred_by_at = {
        _as_aware(row.snapshot_at): _to_float(row.price)
        for row in preferred_rows
        if row.source == source and (_as_aware(row.snapshot_at) or as_of) >= since
    }
    values: list[float] = []
    for snapshot_at in sorted(set(common_by_at) & set(preferred_by_at)):
        value = _disparity_pct(common_by_at[snapshot_at], preferred_by_at[snapshot_at])
        if value is not None:
            values.append(value)
    return values


def _build_windows(
    *,
    current_disparity: float | None,
    common_rows: list[MarketQuoteSnapshot],
    preferred_rows: list[MarketQuoteSnapshot],
    source: str | None,
    as_of: datetime,
) -> tuple[list[DisparityPeriodWindow], float | None]:
    windows: list[DisparityPeriodWindow] = []
    primary_z: float | None = None
    for period, days in _PERIOD_DAYS.items():
        values = (
            _window_values(
                common_rows=common_rows,
                preferred_rows=preferred_rows,
                source=source,
                as_of=as_of,
                days=days,
            )
            if source
            else []
        )
        if not values:
            windows.append(
                DisparityPeriodWindow(
                    period=period, sampleCount=0, dataState="missing", emptyReason="quote_window_missing"
                )
            )
            continue
        avg = mean(values)
        sigma = pstdev(values) if len(values) > 1 else 0.0
        z_score = None
        if current_disparity is not None and sigma > 0:
            z_score = (current_disparity - avg) / sigma
        if period == _PRIMARY_WINDOW:
            primary_z = z_score
        windows.append(
            DisparityPeriodWindow(
                period=period,
                sampleCount=len(values),
                meanDisparityPct=_round(avg),
                minDisparityPct=_round(min(values)),
                maxDisparityPct=_round(max(values)),
                zScore=_round(z_score),
                dataState="fresh",
            )
        )
    return windows, _round(primary_z)


async def _load_active_universe(db: AsyncSession) -> list[KRSymbolRow]:
    result = await db.execute(
        sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name, KRSymbolUniverse.exchange)
        .where(KRSymbolUniverse.is_active.is_(True))
        .order_by(KRSymbolUniverse.symbol.asc())
    )
    return [KRSymbolRow(symbol=row.symbol, name=row.name, exchange=row.exchange) for row in result.all()]


async def _load_quote_rows(
    db: AsyncSession,
    *,
    symbols: set[str],
    as_of: datetime,
) -> dict[str, list[MarketQuoteSnapshot]]:
    if not symbols:
        return {}
    since = as_of - timedelta(days=max(_PERIOD_DAYS.values()) + 1)
    result = await db.execute(
        sa.select(MarketQuoteSnapshot)
        .where(
            MarketQuoteSnapshot.market == "kr",
            MarketQuoteSnapshot.symbol.in_(sorted(symbols)),
            MarketQuoteSnapshot.snapshot_at <= as_of,
            MarketQuoteSnapshot.snapshot_at >= since,
        )
        .order_by(
            MarketQuoteSnapshot.symbol.asc(),
            MarketQuoteSnapshot.source.asc(),
            MarketQuoteSnapshot.snapshot_at.desc(),
        )
    )
    grouped: dict[str, list[MarketQuoteSnapshot]] = defaultdict(list)
    for row in result.scalars().all():
        grouped[row.symbol].append(row)
    return grouped


async def build_common_preferred_disparity(
    *,
    db: AsyncSession,
    symbols: list[str] | None = None,
    as_of: datetime | None = None,
    limit: int = 10,
    max_stale_days: int = 1,
) -> CommonPreferredDisparityResponse:
    """Build common/preferred disparity cards from persisted read-only snapshots."""

    now = _utc_now()
    effective_as_of = _as_aware(as_of) or now
    wanted = {s.strip() for s in symbols or [] if s.strip()}
    universe_rows = await _load_active_universe(db)
    pairs = discover_common_preferred_pairs(universe_rows, symbols=wanted or None)[:limit]
    if not pairs:
        return CommonPreferredDisparityResponse(
            state="missing",
            asOf=effective_as_of,
            cards=[],
            emptyReason="common_preferred_pair_missing",
            warnings=["preferred_pair_mapping_heuristic_no_match"],
        )

    quote_symbols = {pair.common_symbol for pair in pairs} | {pair.preferred_symbol for pair in pairs}
    quotes = await _load_quote_rows(db, symbols=quote_symbols, as_of=effective_as_of)
    cards: list[CommonPreferredDisparityCard] = []
    global_warnings = ["preferred_pair_mapping_heuristic"]
    stale_after = timedelta(days=max_stale_days)

    for pair in pairs:
        common_rows = quotes.get(pair.common_symbol, [])
        preferred_rows = quotes.get(pair.preferred_symbol, [])
        source, common_quote, preferred_quote = _choose_same_source_quote(common_rows, preferred_rows)
        warnings = [f"pair_mapping:{pair.mapping_source}"]
        empty_reason = None
        data_state = "fresh"
        freshness_sec = None
        source_as_of = None
        common_price = preferred_price = disparity = premium = None

        if source is None or common_quote is None or preferred_quote is None:
            data_state = "missing"
            empty_reason = "same_source_quote_pair_missing"
            warnings.append("same_source_quote_pair_missing")
        else:
            common_ts = _as_aware(common_quote.snapshot_at) or effective_as_of
            preferred_ts = _as_aware(preferred_quote.snapshot_at) or effective_as_of
            source_as_of = min(common_ts, preferred_ts)
            freshness_sec = max(0, int((effective_as_of - source_as_of).total_seconds()))
            common_price = _to_float(common_quote.price)
            preferred_price = _to_float(preferred_quote.price)
            disparity = _disparity_pct(common_price, preferred_price)
            premium = _disparity_pct(preferred_price, common_price)
            if disparity is None:
                data_state = "missing"
                empty_reason = "quote_price_invalid"
                warnings.append("quote_price_invalid")
            elif effective_as_of - source_as_of > stale_after:
                data_state = "stale"
                empty_reason = "quote_snapshot_stale"
                warnings.append("quote_snapshot_stale")

        windows, z_score = _build_windows(
            current_disparity=disparity,
            common_rows=common_rows,
            preferred_rows=preferred_rows,
            source=source,
            as_of=effective_as_of,
        )
        if data_state == "fresh" and any(window.dataState != "fresh" for window in windows):
            data_state = "partial"
            warnings.append("quote_window_partial")

        tone = "unknown"
        if disparity is not None:
            if disparity > 0.25:
                tone = "discount"
            elif disparity < -0.25:
                tone = "premium"
            else:
                tone = "parity"

        cards.append(
            CommonPreferredDisparityCard(
                id=f"{pair.common_symbol}-{pair.preferred_symbol}",
                commonSymbol=pair.common_symbol,
                commonName=pair.common_name,
                preferredSymbol=pair.preferred_symbol,
                preferredName=pair.preferred_name,
                exchange=pair.exchange,
                commonPrice=_round(common_price, 2),
                preferredPrice=_round(preferred_price, 2),
                disparityPct=_round(disparity),
                preferredDiscountPct=_round(disparity),
                preferredPremiumPct=_round(premium),
                zScore=z_score,
                primaryWindow=_PRIMARY_WINDOW,
                windows=windows,
                tone=tone,  # type: ignore[arg-type]
                dataState=data_state,  # type: ignore[arg-type]
                emptyReason=empty_reason,
                formula=_DISPARITY_FORMULA,
                source=DisparitySource(
                    source=source or "market_quote_snapshots",
                    sourceOfTruth="market_quote_snapshots",
                    asOf=source_as_of,
                    stale=data_state == "stale",
                    freshnessSec=freshness_sec,
                    warnings=warnings,
                ),
                warnings=warnings,
            )
        )

    state = _state_from_cards(cards)
    empty_reason = None if cards else "common_preferred_pair_missing"
    if cards and state in {"missing", "stale"}:
        empty_reason = "quote_snapshot_unavailable"
    return CommonPreferredDisparityResponse(
        state=state,  # type: ignore[arg-type]
        asOf=effective_as_of,
        cards=cards,
        emptyReason=empty_reason,
        warnings=global_warnings,
    )
