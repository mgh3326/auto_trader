"""ROB-265 Plan 4 — investment_watch scanner.

Reads DB-backed ``investment_watch_alerts``, evaluates the trigger via
``watch_market_data.get_current_value``, persists triggered fires to
``investment_watch_events`` with the immutable trigger-identity
snapshot, transitions the alert to ``triggered``, and emits Hermes
review-trigger notifications.

Locked semantics:
* Watch is a **review trigger**, never an automatic order instruction.
* No broker / live order mutation from this path.
* Notification target is **Hermes**, never OpenClaw.
* ``investment_watch_events.idempotency_key`` is unique on
  ``event:{alert_uuid}:{kst_date}:{threshold_key}`` — a re-fire on the
  same KST day at the same threshold is a DB-level no-op (the scanner
  records it as ``ignored`` for that loop iteration and moves on).
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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.jobs.watch_market_data import (
    get_current_value,
    is_market_open,
    is_triggered,
)
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
)
from app.services.investment_reports.idempotency import watch_event_key
from app.services.investment_reports.repository import InvestmentReportsRepository

logger = logging.getLogger(__name__)


# action_mode → starting outcome on the event row. Hermes delivery
# success/failure is tracked separately (logs + scan summary); it does
# NOT mutate this column after the fact (Plan 4 is fire-and-log;
# a Plan 5+ follow-up could add an outbox/retry).
_OUTCOME_BY_ACTION_MODE: dict[str, str] = {
    "notify_only": "notified",
    "preview_only": "preview_attached",
    "approval_required": "review_required",
}


@dataclass
class _ScanStats:
    alerts_seen: int = 0
    triggered: int = 0
    notified: int = 0
    duplicates: int = 0
    failed_lookups: int = 0
    failed_delivery: int = 0
    skipped_delivery: int = 0
    skipped_closed: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def summary(self, market: str, *, market_open: bool) -> dict[str, Any]:
        return {
            "market": market,
            "market_open": market_open,
            "alerts_seen": self.alerts_seen,
            "triggered": self.triggered,
            "notified": self.notified,
            "duplicates": self.duplicates,
            "failed_lookups": self.failed_lookups,
            "failed_delivery": self.failed_delivery,
            "skipped_delivery": self.skipped_delivery,
            "skipped_closed": self.skipped_closed,
            "details": self.details,
        }


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class InvestmentWatchScanner:
    """Per-market scan of ``investment_watch_alerts`` → events → Hermes."""

    def __init__(
        self,
        *,
        hermes_client: HermesNotificationClient | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._hermes = hermes_client or HermesNotificationClient()
        self._session_factory: SessionFactory = session_factory or AsyncSessionLocal

    async def scan_market(self, market: str) -> dict[str, Any]:
        normalized_market = str(market).strip().lower()
        market_open = is_market_open(normalized_market)
        stats = _ScanStats()

        async with self._session_factory() as db:
            repo = InvestmentReportsRepository(db)
            now_utc = datetime.now(UTC)
            alerts = await repo.list_active_alerts(
                market=normalized_market, valid_at=now_utc
            )
            stats.alerts_seen = len(alerts)

            if not alerts:
                return stats.summary(normalized_market, market_open=market_open)

            for alert in alerts:
                # FX watches keep firing while regular markets are closed.
                if not market_open and alert.target_kind != "fx":
                    stats.skipped_closed += 1
                    continue

                try:
                    current_value = await get_current_value(
                        target_kind=alert.target_kind,
                        metric=alert.metric,
                        symbol=alert.symbol,
                        market=alert.market,
                    )
                except Exception as exc:
                    logger.warning(
                        "investment-watch lookup failed: "
                        "alert_uuid=%s market=%s symbol=%s metric=%s error=%s",
                        alert.alert_uuid,
                        alert.market,
                        alert.symbol,
                        alert.metric,
                        exc,
                    )
                    stats.failed_lookups += 1
                    continue

                threshold_value = float(alert.threshold)
                if not is_triggered(current_value, alert.operator, threshold_value):
                    continue

                # Trigger detected — persist the event + transition the alert.
                emission = await self._emit_event(
                    db=db,
                    repo=repo,
                    alert=alert,
                    current_value=current_value,
                )
                if emission is None:
                    stats.duplicates += 1
                    continue

                stats.triggered += 1
                stats.details.append(emission["detail"])

                # Hermes delivery is best-effort. Event row already persisted.
                delivery = await self._hermes.send_review_trigger(emission["payload"])
                if delivery.status == "success":
                    stats.notified += 1
                elif delivery.status == "skipped":
                    stats.skipped_delivery += 1
                else:
                    stats.failed_delivery += 1
                    logger.warning(
                        "Hermes review-trigger delivery failed: "
                        "alert_uuid=%s event_uuid=%s reason=%s",
                        alert.alert_uuid,
                        emission["event_uuid"],
                        delivery.reason,
                    )

            return stats.summary(normalized_market, market_open=market_open)

    async def _emit_event(
        self,
        *,
        db: AsyncSession,
        repo: InvestmentReportsRepository,
        alert: Any,
        current_value: float,
    ) -> dict[str, Any] | None:
        outcome = _OUTCOME_BY_ACTION_MODE.get(alert.action_mode, "notified")
        correlation_id = uuid4().hex
        kst_date = now_kst().date().isoformat()
        idempotency_key = watch_event_key(
            alert_uuid=str(alert.alert_uuid),
            kst_date=kst_date,
            threshold_key=alert.threshold_key,
        )
        current_value_decimal = Decimal(str(current_value))
        scanner_snapshot: dict[str, Any] = {
            "metric": alert.metric,
            "operator": alert.operator,
            "current_value": current_value,
            "threshold": float(alert.threshold),
        }

        try:
            event = await repo.insert_event(
                event_uuid=uuid4(),
                idempotency_key=idempotency_key,
                alert_id=alert.id,
                source_report_uuid=alert.source_report_uuid,
                source_item_uuid=alert.source_item_uuid,
                market=alert.market,
                target_kind=alert.target_kind,
                symbol=alert.symbol,
                metric=alert.metric,
                operator=alert.operator,
                threshold=alert.threshold,
                threshold_key=alert.threshold_key,
                intent=alert.intent,
                action_mode=alert.action_mode,
                current_value=current_value_decimal,
                scanner_snapshot=scanner_snapshot,
                outcome=outcome,
                correlation_id=correlation_id,
                kst_date=kst_date,
            )
            await repo.update_alert_status(alert.id, "triggered")
            await db.commit()
        except IntegrityError:
            # Same-day re-fire — already emitted. No-op.
            await db.rollback()
            return None

        payload = ReviewTriggerPayload(
            event_uuid=event.event_uuid,
            alert_uuid=alert.alert_uuid,
            source_report_uuid=alert.source_report_uuid,
            source_item_uuid=alert.source_item_uuid,
            correlation_id=correlation_id,
            kst_date=kst_date,
            market=alert.market,
            target_kind=alert.target_kind,
            symbol=alert.symbol,
            metric=alert.metric,
            operator=alert.operator,
            threshold=alert.threshold,
            threshold_key=alert.threshold_key,
            intent=alert.intent,
            action_mode=alert.action_mode,
            current_value=current_value_decimal,
            scanner_snapshot=scanner_snapshot,
            outcome=outcome,
        )
        return {
            "event_uuid": event.event_uuid,
            "payload": payload,
            "detail": {
                "alert_uuid": str(alert.alert_uuid),
                "event_uuid": str(event.event_uuid),
                "outcome": outcome,
                "symbol": alert.symbol,
                "current_value": current_value,
                "threshold": float(alert.threshold),
            },
        }

    async def run(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for market in ("crypto", "kr", "us"):
            try:
                results[market] = await self.scan_market(market)
            except Exception as exc:
                logger.exception(
                    "investment_watch scan_market raised: market=%s", market
                )
                results[market] = {
                    "market": market,
                    "status": "failed",
                    "reason": "scan_aborted",
                    "error": str(exc),
                }
        return results

    async def close(self) -> None:
        await self._hermes.close()
