"""Read-only query service for market events (ROB-128).

NOTE: held / watched flags currently return None. Joining holdings / watchlist is
deferred to a follow-up — see docs/runbooks/market-events-ingestion.md.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_events import MarketEvent, MarketEventValue
from app.schemas.market_events import (
    MarketEventResponse,
    MarketEventsDayResponse,
    MarketEventsRangeResponse,
    MarketEventValueResponse,
)
from app.services.disclosures.timing import StoredDisclosureRow
from app.services.market_events.taxonomy import (
    validate_category,
    validate_market,
)


def _normalize_symbol_filter(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = str(symbol).strip().upper()
    if not normalized:
        return None
    if len(normalized) == 7 and normalized.startswith("A") and normalized[1:].isdigit():
        return normalized[1:]
    if normalized.isdigit() and len(normalized) < 6:
        return normalized.zfill(6)
    return normalized


def _value_response(value: MarketEventValue) -> MarketEventValueResponse:
    response = MarketEventValueResponse.model_validate(value)
    if value.value_details:
        return MarketEventValueResponse.model_validate(
            {**response.model_dump(), **value.value_details}
        )
    return response


def select_current_metric_value(
    event: MarketEventResponse, metric_name: str
) -> MarketEventValueResponse | None:
    """Select a metric row by current fiscal period, then a stable value key.

    Earnings revisions can move an event from one period to another. Prefer
    the value whose period matches the event's current fiscal year/quarter;
    if no such row exists, choose the greatest period label. Rows with the
    same period use their full serialized value fields as an explicit
    deterministic tie-break, independent of database/insertion order.
    """
    values = [value for value in event.values if value.metric_name == metric_name]
    if not values:
        return None

    current_period = None
    if event.fiscal_year is not None and event.fiscal_quarter is not None:
        current_period = f"Q{event.fiscal_quarter}-{event.fiscal_year}"

    value_fields = (
        "actual",
        "forecast",
        "previous",
        "revised_previous",
        "unit",
        "surprise",
        "surprise_pct",
        "released_at",
        "reason",
    )

    def order_key(
        value: MarketEventValueResponse,
    ) -> tuple[bool, str, tuple[tuple[str, bool, str], ...]]:
        stable_values = tuple(
            (
                field,
                item is not None,
                item.isoformat()
                if hasattr(item, "isoformat")
                else str(item)
                if item is not None
                else "",
            )
            for field in value_fields
            for item in (getattr(value, field, None),)
        )
        period = getattr(value, "period", None) or ""
        return (period == current_period, period, stable_values)

    return max(values, key=order_key)


def _disclosure_row(
    event: MarketEvent, *, receipt_text: str | None = None
) -> StoredDisclosureRow:
    return StoredDisclosureRow(
        source=event.source,
        published_at=event.published_at,
        published_date=event.published_date,
        published_offset=event.published_offset,
        published_precision=event.published_precision,
        provider_created_at=event.provider_created_at,
        provider_created_raw=event.provider_created_raw,
        first_received_at=event.first_received_at,
        receipt_text=receipt_text,
    )


class MarketEventsQueryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def dart_values_by_receipt(
        self, receipts: list[str]
    ) -> dict[str, list[dict[str, str | None]]]:
        """Read structured values for exact DART receipts already returned by MCP."""
        if not receipts:
            return {}
        stmt = (
            select(MarketEvent.source_event_id, MarketEventValue)
            .join(MarketEventValue, MarketEvent.id == MarketEventValue.event_id)
            .where(
                MarketEvent.source == "dart",
                MarketEvent.market == "kr",
                MarketEvent.category == "disclosure",
                MarketEvent.source_event_id.in_(receipts),
                MarketEventValue.value_details.is_not(None),
            )
            .order_by(MarketEventValue.id)
        )
        values: dict[str, list[dict[str, str | None]]] = {}
        for receipt, value in (await self.db.execute(stmt)).all():
            values.setdefault(receipt, []).append(
                {
                    "metric_name": value.metric_name,
                    "unit": value.unit,
                    **value.value_details,
                }
            )
        return values

    async def disclosure_rows_by_receipt(
        self, receipts: list[str], symbols: list[str]
    ) -> dict[str, list[StoredDisclosureRow]]:
        """Stored timing evidence for DART receipts (DART and linked Naver rows).

        Naver rows are looked up only for the given symbols: the collector links
        a receipt to the stock it fetched.
        """
        if not receipts:
            return {}
        rows: dict[str, list[StoredDisclosureRow]] = {}
        dart_stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.source == "dart",
                MarketEvent.market == "kr",
                MarketEvent.category.in_(("disclosure", "earnings")),
                MarketEvent.source_event_id.in_(receipts),
            )
            .order_by(MarketEvent.id)
        )
        for event in (await self.db.execute(dart_stmt)).scalars().all():
            receipt_text = (event.raw_payload_json or {}).get("rcept_dt")
            rows.setdefault(event.source_event_id, []).append(
                _disclosure_row(
                    event,
                    receipt_text=receipt_text
                    if isinstance(receipt_text, str)
                    else None,
                )
            )
        if not symbols:
            return rows
        link = MarketEvent.raw_payload_json["dart_rcept_no"].astext
        naver_stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.source == "naver_disclosure",
                MarketEvent.market == "kr",
                MarketEvent.category == "disclosure",
                MarketEvent.symbol.in_(symbols),
                link.in_(receipts),
            )
            .order_by(MarketEvent.id)
        )
        for event in (await self.db.execute(naver_stmt)).scalars().all():
            receipt = (event.raw_payload_json or {}).get("dart_rcept_no")
            rows.setdefault(receipt, []).append(_disclosure_row(event))
        return rows

    async def list_for_date(
        self,
        target_date: date,
        *,
        category: str | None = None,
        market: str | None = None,
        source: str | None = None,
        symbol: str | None = None,
    ) -> MarketEventsDayResponse:
        events = await self._query(
            from_date=target_date,
            to_date=target_date,
            category=category,
            market=market,
            source=source,
            symbol=symbol,
        )
        return MarketEventsDayResponse(date=target_date, events=events)

    async def list_for_range(
        self,
        from_date: date,
        to_date: date,
        *,
        category: str | None = None,
        market: str | None = None,
        source: str | None = None,
        symbol: str | None = None,
    ) -> MarketEventsRangeResponse:
        if from_date > to_date:
            raise ValueError("from_date must be <= to_date")
        events = await self._query(
            from_date=from_date,
            to_date=to_date,
            category=category,
            market=market,
            source=source,
            symbol=symbol,
        )
        return MarketEventsRangeResponse(
            from_date=from_date,
            to_date=to_date,
            count=len(events),
            events=events,
        )

    async def _query(
        self,
        *,
        from_date: date,
        to_date: date,
        category: str | None,
        market: str | None,
        source: str | None,
        symbol: str | None,
    ) -> list[MarketEventResponse]:
        if category is not None:
            validate_category(category)
        if market is not None:
            validate_market(market)

        normalized_symbol = _normalize_symbol_filter(symbol)

        stmt = (
            select(MarketEvent)
            .where(
                MarketEvent.event_date >= from_date,
                MarketEvent.event_date <= to_date,
            )
            .order_by(
                MarketEvent.event_date.asc(),
                MarketEvent.symbol.asc(),
                MarketEvent.id.asc(),
            )
        )
        if category is not None:
            stmt = stmt.where(MarketEvent.category == category)
        if market is not None:
            stmt = stmt.where(MarketEvent.market == market)
        if source is not None:
            stmt = stmt.where(MarketEvent.source == source)
        if normalized_symbol is not None:
            stmt = stmt.where(MarketEvent.symbol == normalized_symbol)

        rows = (await self.db.execute(stmt)).scalars().all()

        # ROB-1314 — bulk-load event values in one query. The previous loop
        # issued one SELECT per event row (SQL N+1) and dominated /invest
        # calendar latency on dense ranges.
        values_by_event_id: dict[int, list[MarketEventValue]] = {}
        event_ids = [row.id for row in rows]
        if event_ids:
            values_stmt = (
                select(MarketEventValue)
                .where(MarketEventValue.event_id.in_(event_ids))
                .order_by(MarketEventValue.id.asc())
            )
            value_rows = (await self.db.execute(values_stmt)).scalars().all()
            for value_row in value_rows:
                values_by_event_id.setdefault(value_row.event_id, []).append(value_row)

        out: list[MarketEventResponse] = []
        for row in rows:
            value_rows = values_by_event_id.get(row.id, [])
            out.append(
                MarketEventResponse(
                    category=row.category,
                    market=row.market,
                    country=row.country,
                    currency=row.currency,
                    symbol=row.symbol,
                    company_name=row.company_name,
                    title=row.title,
                    event_date=row.event_date,
                    release_time_utc=row.release_time_utc,
                    time_hint=row.time_hint,
                    importance=row.importance,
                    status=row.status,
                    source=row.source,
                    source_event_id=row.source_event_id,
                    source_url=row.source_url,
                    fiscal_year=row.fiscal_year,
                    fiscal_quarter=row.fiscal_quarter,
                    held=None,
                    watched=None,
                    values=[_value_response(v) for v in value_rows],
                )
            )
        return out
