"""ROB-337 Slice 2 — watch validity review job (read-only, dry-run default).

Mirrors :class:`app.jobs.investment_watch_scanner.InvestmentWatchScanner`
read patterns but classifies each active watch's continued validity
(keep/reprice/expire/review_now/data_gap) instead of firing triggers.

Locked semantics:
* NO broker / order / order-intent mutation.
* alert.status / watch_condition / watch_recommendation are NOT mutated;
  the only write (dry_run=False) is the ``last_review`` block in
  alert_metadata, used for material-change notification throttling.
* Notification reuses the Hermes review-trigger contract; verdict + reason
  ride in ``scanner_snapshot`` with ``outcome='review_required'``. No
  investment_watch_events rows are created.
* HERMES_ENABLED default False -> deliveries are skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.jobs.watch_market_data import get_current_value
from app.services import market_data as market_data_service
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    planned_action_from_max_action,
    price_guidance_from_watch_recommendation,
    trigger_checklist_from_raw,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_recommendation_policy import (
    ATR_PERIOD,
    LOOKBACK_DAYS,
    WatchPolicyInput,
    compute_watch_recommendation,
)
from app.services.investment_reports.watch_validity_policy import (
    WatchValidityInput,
    classify_watch_validity,
)

logger = logging.getLogger(__name__)

_ACTIONABLE = {"review_now", "expire", "data_gap"}
_MD_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _normalize_symbol(symbol: str, market: str) -> str:
    s = str(symbol or "").strip()
    if market == "crypto":
        up = s.upper()
        return up if "-" in up else f"KRW-{up}"
    if market == "us":
        return s.upper()
    return s


@dataclass
class _ReviewStats:
    market: str
    alerts_seen: int = 0
    notified: int = 0
    failed_lookups: int = 0
    verdict_counts: dict[str, int] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)

    def record(self, verdict: str) -> None:
        self.verdict_counts[verdict] = self.verdict_counts.get(verdict, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "alerts_seen": self.alerts_seen,
            "notified": self.notified,
            "failed_lookups": self.failed_lookups,
            "verdict_counts": self.verdict_counts,
            "details": self.details,
        }


class WatchValidityReviewService:
    def __init__(
        self,
        hermes_client: HermesNotificationClient | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._hermes = hermes_client or HermesNotificationClient()
        self._session_factory = session_factory or AsyncSessionLocal

    async def run(self, *, dry_run: bool = True) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for market in ("crypto", "kr", "us"):
            try:
                results[market] = await self.review_market(market, dry_run=dry_run)
            except Exception as exc:
                logger.exception("watch validity review_market raised: %s", market)
                results[market] = {
                    "market": market,
                    "status": "failed",
                    "error": str(exc),
                }
        return results

    async def review_market(
        self, market: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        stats = _ReviewStats(market=market)
        now_utc = datetime.now(UTC)
        async with self._session_factory() as db:
            repo = InvestmentReportsRepository(db)
            alerts = await repo.list_active_alerts(market=market, valid_at=now_utc)
            for alert in alerts:
                stats.alerts_seen += 1
                item = await repo.get_item_by_uuid(alert.source_item_uuid)
                stored = item.watch_recommendation if item is not None else None

                current_price = await self._current_price(alert)
                if current_price is None:
                    stats.failed_lookups += 1
                recomputed = await self._recompute(alert, now_utc)

                result = classify_watch_validity(
                    WatchValidityInput(
                        stored_recommendation=stored,
                        current_price=current_price,
                        recomputed=recomputed,
                        valid_until=alert.valid_until,
                        now=now_utc,
                    )
                )
                stats.record(result.verdict)
                stats.details.append(
                    {
                        "alert_uuid": str(alert.alert_uuid),
                        "symbol": alert.symbol,
                        "verdict": result.verdict,
                        "reason": result.reason,
                    }
                )

                if dry_run:
                    continue

                kst_date = now_kst().date().isoformat()
                last = (alert.alert_metadata or {}).get("last_review") or {}
                material = result.verdict != last.get(
                    "verdict"
                ) or kst_date != last.get("kst_date")
                if result.verdict in _ACTIONABLE and material:
                    if await self._notify(
                        alert,
                        result,
                        current_price,
                        kst_date,
                        stored_recommendation=stored,
                    ):
                        stats.notified += 1

                new_meta = dict(alert.alert_metadata or {})
                new_meta["last_review"] = {
                    "verdict": result.verdict,
                    "kst_date": kst_date,
                    "computed_at": now_utc.isoformat(),
                }
                await repo.update_alert_metadata(alert.id, new_meta)
            if not dry_run:
                await db.commit()
        return stats.summary()

    async def _current_price(self, alert: Any) -> Decimal | None:
        try:
            val = await get_current_value(
                target_kind=alert.target_kind,
                metric="price",
                symbol=alert.symbol,
                market=alert.market,
            )
        except Exception:
            logger.exception("validity current-price lookup failed: %s", alert.symbol)
            return None
        return Decimal(str(val)) if val is not None else None

    async def _recompute(self, alert: Any, now_utc: datetime):
        md_market = _MD_MARKET.get(alert.market)
        if md_market is None:
            return None
        try:
            candles = await market_data_service.get_ohlcv(
                symbol=_normalize_symbol(alert.symbol, alert.market),
                market=md_market,
                period="day",
                count=LOOKBACK_DAYS + ATR_PERIOD + 6,
            )
        except Exception:
            logger.exception("validity recompute fetch failed: %s", alert.symbol)
            return None
        ordered = sorted(candles, key=lambda c: c.timestamp)
        closes = [Decimal(str(c.close)) for c in ordered]
        return compute_watch_recommendation(
            WatchPolicyInput(
                reference_price=closes[-1] if closes else None,
                best_bid=None,
                best_ask=None,
                daily_highs=[Decimal(str(c.high)) for c in ordered],
                daily_lows=[Decimal(str(c.low)) for c in ordered],
                daily_closes=closes,
            ),
            computed_at=now_utc,
            valid_until=alert.valid_until,
        )

    async def _notify(
        self,
        alert: Any,
        result: Any,
        current_price: Decimal | None,
        kst_date: str,
        *,
        stored_recommendation: dict[str, Any] | None = None,
    ) -> bool:
        payload = ReviewTriggerPayload(
            event_uuid=uuid4(),
            alert_uuid=alert.alert_uuid,
            source_report_uuid=alert.source_report_uuid,
            source_item_uuid=alert.source_item_uuid,
            correlation_id=uuid4().hex,
            kst_date=kst_date,
            market=alert.market,
            target_kind=alert.target_kind,
            symbol=alert.symbol,
            metric=alert.metric,
            operator=alert.operator,
            threshold=Decimal(str(alert.threshold)),
            threshold_key=alert.threshold_key,
            intent=alert.intent,
            action_mode=alert.action_mode,
            current_value=current_price,
            scanner_snapshot={
                "validity_verdict": result.verdict,
                "reason": result.reason,
                "signals": result.signals,
            },
            outcome="review_required",
            # ROB-500 — no event row on this path, so no event anchor;
            # the alert row anchor is the operator's landing point.
            invest_links=build_invest_links(
                market=alert.market,
                symbol=alert.symbol,
                source_report_uuid=alert.source_report_uuid,
                alert_uuid=alert.alert_uuid,
            ),
            operator_action_guidance=build_operator_action_guidance(
                action_mode=alert.action_mode, outcome="review_required"
            ),
            price_guidance=price_guidance_from_watch_recommendation(
                stored_recommendation
            ),
            planned_action=planned_action_from_max_action(dict(alert.max_action or {})),
            trigger_checklist=trigger_checklist_from_raw(
                list(alert.trigger_checklist or [])
            ),
        )
        try:
            res = await self._hermes.send_review_trigger(payload)
        except Exception:
            logger.exception("validity hermes send failed: %s", alert.symbol)
            return False
        return res.status == "success"

    async def close(self) -> None:
        await self._hermes.close()
