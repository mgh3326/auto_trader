"""Service for /invest watch panel (ROB-591)."""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchAlert, InvestmentWatchEvent
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.schemas.invest_watches import (
    WatchAlertRow,
    WatchDataState,
    WatchesResponse,
    WatchEventSummary,
    WatchProximityBand,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names
from app.services.us_symbol_universe_service import get_us_names_by_symbols
from app.services.watch_proximity import compute_price_proximity

logger = logging.getLogger(__name__)


def _to_decimal(val: object) -> Decimal | None:
    if val is None:
        return None
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except (TypeError, ValueError):
        return None


class WatchPanelService:
    def __init__(
        self,
        *,
        db: AsyncSession,
        clock: dt.datetime | None = None,
    ) -> None:
        self._db = db
        self._clock = (
            (lambda: clock)
            if clock is not None
            else (lambda: dt.datetime.now(tz=dt.UTC))
        )

    async def list_watches(
        self,
        *,
        market: Literal["all", "kr", "us", "crypto"] = "all",
        status: Literal["all", "active", "triggered", "expired", "canceled"] = "all",
    ) -> WatchesResponse:
        repo = InvestmentReportsRepository(self._db)
        now = self._clock()

        # 1. Fetch alerts
        db_market = None if market == "all" else market
        db_status = None if status == "all" else status
        alerts = await repo.list_alerts(
            market=db_market,
            status=db_status,
            limit=250,
        )

        # 2. Enrich symbol names (fail-open)
        symbol_names = await self._attach_symbol_names(alerts)

        # 3. Fetch latest quotes for active price-metric alerts
        active_price_alerts = [
            a
            for a in alerts
            if a.status == "active" and a.metric in ("price_above", "price_below")
        ]

        snapshots: dict[tuple[str, str], MarketQuoteSnapshot] = {}
        pairs = list({(a.market, a.symbol) for a in active_price_alerts})
        if pairs:
            try:
                subq = (
                    sa.select(
                        MarketQuoteSnapshot.market,
                        MarketQuoteSnapshot.symbol,
                        sa.func.max(MarketQuoteSnapshot.snapshot_at).label("max_at"),
                    )
                    .where(
                        sa.tuple_(
                            MarketQuoteSnapshot.market, MarketQuoteSnapshot.symbol
                        ).in_(pairs)
                    )
                    .group_by(MarketQuoteSnapshot.market, MarketQuoteSnapshot.symbol)
                    .subquery()
                )
                stmt = sa.select(MarketQuoteSnapshot).join(
                    subq,
                    sa.and_(
                        MarketQuoteSnapshot.market == subq.c.market,
                        MarketQuoteSnapshot.symbol == subq.c.symbol,
                        MarketQuoteSnapshot.snapshot_at == subq.c.max_at,
                    ),
                )
                res = await self._db.execute(stmt)
                for snapshot in res.scalars().all():
                    snapshots[(snapshot.market, snapshot.symbol)] = snapshot
            except Exception:
                logger.exception("Failed to query market quote snapshots")

        # Determine data_state
        active_price_alert_count = len(active_price_alerts)
        resolved_count = 0
        for a in active_price_alerts:
            if (a.market, a.symbol) in snapshots:
                resolved_count += 1

        if active_price_alert_count > 0:
            if resolved_count == active_price_alert_count:
                data_state: WatchDataState = "ok"
            elif resolved_count > 0:
                data_state: WatchDataState = "degraded"
            else:
                data_state: WatchDataState = "unavailable"
        else:
            data_state: WatchDataState = "ok"

        # 4. Fetch last events for triggered/expired/canceled alerts
        non_active_alerts = [a for a in alerts if a.status != "active"]
        alert_ids = [a.id for a in non_active_alerts]
        last_events: dict[int, InvestmentWatchEvent] = {}
        if alert_ids:
            try:
                subq = (
                    sa.select(
                        InvestmentWatchEvent.alert_id,
                        sa.func.max(InvestmentWatchEvent.created_at).label(
                            "max_created"
                        ),
                    )
                    .where(InvestmentWatchEvent.alert_id.in_(alert_ids))
                    .group_by(InvestmentWatchEvent.alert_id)
                    .subquery()
                )
                stmt = sa.select(InvestmentWatchEvent).join(
                    subq,
                    sa.and_(
                        InvestmentWatchEvent.alert_id == subq.c.alert_id,
                        InvestmentWatchEvent.created_at == subq.c.max_created,
                    ),
                )
                res = await self._db.execute(stmt)
                for event in res.scalars().all():
                    if event.alert_id is not None:
                        last_events[event.alert_id] = event
            except Exception:
                logger.exception("Failed to query investment watch events")

        # 5. Build items
        items: list[WatchAlertRow] = []
        for alert in alerts:
            symbol_name = symbol_names.get((alert.market, alert.symbol))

            # Near expiry logic for active alerts
            near_expiry = False
            if alert.status == "active" and alert.valid_until - now <= dt.timedelta(
                days=2
            ):
                near_expiry = True

            # Proximity calculation for active price alerts
            current_price: Decimal | None = None
            proximity_band: WatchProximityBand | None = None
            if alert.status == "active" and alert.metric in (
                "price_above",
                "price_below",
            ):
                snapshot = snapshots.get((alert.market, alert.symbol))
                if snapshot is not None:
                    current_price = snapshot.price
                    try:
                        prox_res = compute_price_proximity(
                            market=alert.market,
                            target_kind=alert.target_kind,
                            symbol=alert.symbol,
                            condition_type=alert.metric,
                            threshold=float(alert.threshold),
                            current=float(snapshot.price),
                        )
                        # We classification match: band literal
                        proximity_band = prox_res.band
                    except Exception:
                        logger.warning(
                            "Failed to compute proximity for alert %s",
                            alert.id,
                            exc_info=True,
                        )

            # Last event for triggered/expired/canceled alerts
            last_event_summary: WatchEventSummary | None = None
            if alert.status != "active":
                event = last_events.get(alert.id)
                if event is not None:
                    last_event_summary = WatchEventSummary(
                        event_uuid=event.event_uuid,
                        outcome=event.outcome,
                        current_value=_to_decimal(event.current_value),
                        created_at=event.created_at,
                    )

            # Build trigger_checklist & max_action defaults
            trigger_checklist = alert.trigger_checklist or []
            max_action = alert.max_action or {}

            items.append(
                WatchAlertRow(
                    alert_uuid=alert.alert_uuid,
                    source_report_uuid=alert.source_report_uuid,
                    market=alert.market,  # type: ignore[arg-type]
                    symbol=alert.symbol,
                    symbol_name=symbol_name,
                    target_kind=alert.target_kind,
                    metric=alert.metric,
                    operator=alert.operator,  # type: ignore[arg-type]
                    threshold=_to_decimal(alert.threshold),
                    threshold_high=_to_decimal(alert.threshold_high),
                    status=alert.status,  # type: ignore[arg-type]
                    valid_until=alert.valid_until,
                    intent=alert.intent,
                    action_mode=alert.action_mode,
                    rationale=alert.rationale,
                    trigger_checklist=trigger_checklist,
                    max_action=max_action,
                    current_price=current_price,
                    proximity_band=proximity_band,
                    last_event=last_event_summary,
                    near_expiry=near_expiry,
                )
            )

        warnings: list[str] = []
        if data_state == "degraded":
            warnings.append(
                "Some active price watch items could not retrieve current prices from the database."
            )
        elif data_state == "unavailable":
            warnings.append(
                "All active price watch items failed to retrieve current prices from the database."
            )

        empty_reason = None
        if not items:
            if status == "all":
                empty_reason = "No watch alerts found."
            else:
                empty_reason = f"No {status} watch alerts found."

        return WatchesResponse(
            market=market,
            status=status,
            count=len(items),
            data_state=data_state,
            as_of=now,
            items=items,
            warnings=warnings,
            empty_reason=empty_reason,
        )

    async def _attach_symbol_names(
        self, alerts: list[InvestmentWatchAlert]
    ) -> dict[tuple[str, str], str]:
        """Best-effort display-name enrichment."""
        if not alerts:
            return {}

        kr_symbols = sorted({alert.symbol for alert in alerts if alert.market == "kr"})
        us_symbols = sorted({alert.symbol for alert in alerts if alert.market == "us"})
        crypto_markets = sorted(
            {
                alert.symbol.strip().upper()
                for alert in alerts
                if alert.market == "crypto"
            }
        )

        async def _safe(coro, label: str):
            try:
                return await coro
            except Exception:  # noqa: BLE001 - display names must fail open
                logger.warning(
                    "watch-panel symbol-name resolution failed for %s",
                    label,
                    exc_info=True,
                )
                return {}

        kr_names = (
            await _safe(get_kr_names_by_symbols(kr_symbols, self._db), "kr")
            if kr_symbols
            else {}
        )
        us_names = (
            await _safe(get_us_names_by_symbols(us_symbols, self._db), "us")
            if us_symbols
            else {}
        )
        crypto_names = (
            await _safe(
                get_upbit_market_display_names(crypto_markets, self._db), "crypto"
            )
            if crypto_markets
            else {}
        )

        symbol_names: dict[tuple[str, str], str] = {}
        for alert in alerts:
            name: str | None = None
            if alert.market == "kr":
                name = kr_names.get(alert.symbol)
            elif alert.market == "us":
                name = us_names.get(alert.symbol)
            elif alert.market == "crypto":
                display = crypto_names.get(alert.symbol.strip().upper())
                if display:
                    name = display.get("korean_name") or display.get("english_name")

            if name and name != alert.symbol:
                symbol_names[(alert.market, alert.symbol)] = name
        return symbol_names
