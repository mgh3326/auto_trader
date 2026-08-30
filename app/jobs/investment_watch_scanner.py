"""ROB-265 Plan 4 — investment_watch scanner.

Reads DB-backed ``investment_watch_alerts``, evaluates the trigger via
``watch_market_data.get_current_value``, persists triggered fires to
``investment_watch_events`` with the immutable trigger-identity
snapshot, transitions the alert to ``triggered``, and emits Hermes
review-trigger notifications.

Locked semantics:
* Watch is a **review trigger** unless the report activation explicitly uses
  ``auto_execute_mock``; that mode is permanently limited to ``kis_mock``.
* No broker / live order mutation from this path.
* Notification target is **Hermes**, never the agent gateway (the legacy
  agent-gateway watch-alert path has been removed).
* The alert only transitions to ``triggered`` after Hermes confirms
  delivery. ``skipped`` (HERMES_ENABLED=False) and ``failed`` deliveries
  leave the alert ``active`` so the next scan loop can re-attempt
  against the existing event row (looked up via the idempotency_key
  collision). The event row carries the delivery-tracking columns
  (``delivery_status`` / ``delivery_reason`` / ``delivered_at`` /
  ``delivery_attempts``) for audit + frontend visibility.
* ``investment_watch_events.idempotency_key`` is unique on
  ``event:{alert_uuid}:{kst_date}:{threshold_key}`` — same-day collisions
  trigger the retry path (NOT a silent no-op).

ROB-1110 — per-alert isolation. The scan loop writes to the DB while it
is still iterating, so a single alert used to be able to take out every
alert behind it in the same cycle:

* The rows returned by ``list_active_alerts`` are ORM objects owned by
  the scan session. ``AsyncSession.rollback()`` **always** expires the
  identity map (unlike ``commit()``, which this app configures with
  ``expire_on_commit=False``), so after the very first rollback — e.g.
  the routine same-day ``IntegrityError`` retry path in ``_upsert_event``
  — the next ``alert.conditions`` read became an implicit lazy refresh
  and raised ``MissingGreenlet`` under asyncio.
* A DB error inside one alert's handling left the transaction in an
  aborted state, so every later alert's ``commit()`` failed too.

The fix is structural rather than a targeted eager-load: every alert row
is materialized into an immutable ``_AlertSnapshot`` *before* the first
write, so the loop holds no ORM state at all, and each alert is processed
inside its own try/rollback boundary. A failure is counted
(``failed_alerts``) and described in ``details`` — never silently
swallowed — and the cycle carries on to the remaining alerts.
"""

from __future__ import annotations

import copy
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
    evaluate_alert_conditions,
    get_current_value,
    is_market_open,
    is_triggered,
)
from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    planned_action_from_max_action,
    price_guidance_from_watch_recommendation,
    trigger_checklist_from_raw,
)
from app.services.investment_reports.idempotency import watch_event_key
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_auto_execute import maybe_auto_execute

logger = logging.getLogger(__name__)


# action_mode → starting outcome on the event row. Hermes delivery
# success/failure is tracked separately via the delivery_status /
# delivery_reason / delivered_at / delivery_attempts columns; the
# original ``outcome`` is the operator-facing classification of what
# the watch represented. Auto-execution starts pending and is finalized
# only after maybe_auto_execute returns evidence (ROB-843 / ROB-1109).
_OUTCOME_BY_ACTION_MODE: dict[str, str] = {
    "notify_only": "notified",
    "preview_only": "preview_attached",
    "approval_required": "review_required",
    "auto_execute_mock": "pending",
}


@dataclass(frozen=True)
class _AlertSnapshot:
    """Plain-Python copy of one ``investment_watch_alerts`` row.

    ROB-1110: the scan loop commits and rolls back while iterating, and a
    rollback expires the whole identity map. Reading any attribute off a
    live ORM row after that point is an implicit lazy refresh, which
    raises ``MissingGreenlet`` under asyncio. Materializing every field
    the scan needs — once, before the first write — removes the
    lazy-load surface entirely instead of eager-loading one column at a
    time and hoping no future field is added.

    Attribute names deliberately mirror ``InvestmentWatchAlert`` so this
    is a drop-in for the collaborators that take an ``alert`` (notably
    ``maybe_auto_execute``).
    """

    id: int
    alert_uuid: Any
    source_report_uuid: Any
    source_item_uuid: Any
    market: str
    target_kind: str
    symbol: str
    metric: str
    operator: str
    threshold: Any
    threshold_key: str
    threshold_high: Any
    intent: str
    action_mode: str
    combine: str
    conditions: list[Any]
    max_action: dict[str, Any]
    trigger_checklist: list[Any]
    alert_metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> _AlertSnapshot:
        # deepcopy the mutable JSON containers so the snapshot cannot be
        # aliased to (or invalidated with) the ORM row's attribute state.
        return cls(
            id=row.id,
            alert_uuid=row.alert_uuid,
            source_report_uuid=row.source_report_uuid,
            source_item_uuid=row.source_item_uuid,
            market=row.market,
            target_kind=row.target_kind,
            symbol=row.symbol,
            metric=row.metric,
            operator=row.operator,
            threshold=row.threshold,
            threshold_key=row.threshold_key,
            threshold_high=row.threshold_high,
            intent=row.intent,
            action_mode=row.action_mode,
            combine=row.combine,
            conditions=copy.deepcopy(row.conditions or []),
            max_action=copy.deepcopy(row.max_action or {}),
            trigger_checklist=copy.deepcopy(row.trigger_checklist or []),
            alert_metadata=copy.deepcopy(row.alert_metadata or {}),
        )


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
    # ROB-1110: alerts whose handling raised and was isolated, and alerts
    # the cycle never reached. Both are "not evaluated" — kept separate
    # from the zero-trigger case so a quiet scan can't be mistaken for a
    # clean one.
    failed_alerts: int = 0
    not_evaluated: int = 0
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
            "failed_alerts": self.failed_alerts,
            "not_evaluated": self.not_evaluated,
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
            rows = await repo.list_active_alerts(
                market=normalized_market, valid_at=now_utc
            )
            # ROB-1110: detach from the ORM *before* the first write. From
            # here on the loop touches nothing that a commit/rollback can
            # invalidate.
            alerts = [_AlertSnapshot.from_row(row) for row in rows]
            del rows
            stats.alerts_seen = len(alerts)

            if not alerts:
                return stats.summary(normalized_market, market_open=market_open)

            for index, alert in enumerate(alerts):
                # FX watches keep firing while regular markets are closed.
                if not market_open and alert.target_kind != "fx":
                    stats.skipped_closed += 1
                    continue

                try:
                    await self._process_alert(
                        db=db, repo=repo, alert=alert, stats=stats
                    )
                except Exception as exc:  # noqa: BLE001 - isolate, don't swallow
                    # ROB-1110: one alert's failure must not reach the next
                    # one. Record it loudly, then reset the session so the
                    # rest of the cycle still has a usable transaction.
                    stats.failed_alerts += 1
                    stats.details.append(
                        {
                            "alert_uuid": str(alert.alert_uuid),
                            "symbol": alert.symbol,
                            "status": "alert_failed",
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    )
                    logger.exception(
                        "investment-watch alert processing failed — "
                        "isolated, scan continues: alert_uuid=%s market=%s symbol=%s",
                        alert.alert_uuid,
                        alert.market,
                        alert.symbol,
                    )
                    if not await self._reset_session(db):
                        # The session itself is unusable; every remaining
                        # alert would fail identically. Stop and report
                        # exactly how many were never evaluated rather
                        # than emitting a wall of identical failures.
                        stats.not_evaluated = len(alerts) - index - 1
                        stats.details.append(
                            {
                                "status": "scan_aborted",
                                "reason": "session_unusable",
                                "not_evaluated": stats.not_evaluated,
                            }
                        )
                        break

            return stats.summary(normalized_market, market_open=market_open)

    @staticmethod
    async def _reset_session(db: AsyncSession) -> bool:
        """Roll the scan session back to a clean state. ROB-1110.

        Returns ``False`` when the rollback itself fails, which means the
        session can no longer be reused for the remaining alerts.
        """
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - reported by the caller
            logger.exception(
                "investment-watch: scan session rollback failed — "
                "remaining alerts in this market cannot be evaluated"
            )
            return False
        return True

    async def _process_alert(
        self,
        *,
        db: AsyncSession,
        repo: InvestmentReportsRepository,
        alert: _AlertSnapshot,
        stats: _ScanStats,
    ) -> None:
        """Evaluate one alert and, if triggered, emit + deliver it.

        Raises on unexpected failure; ``scan_market`` isolates and records
        it so the remaining alerts still get evaluated (ROB-1110).
        """
        try:
            if alert.conditions:
                triggered, current_value = await evaluate_alert_conditions(
                    target_kind=alert.target_kind,
                    symbol=alert.symbol,
                    market=alert.market,
                    conditions=alert.conditions,
                    combine=alert.combine,
                    get_value_fn=get_current_value,
                )
            else:
                current_value = await get_current_value(
                    target_kind=alert.target_kind,
                    metric=alert.metric,
                    symbol=alert.symbol,
                    market=alert.market,
                )
                triggered = is_triggered(
                    current_value, alert.operator, float(alert.threshold)
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
            return

        if not triggered:
            return

        # Insert (or look up existing) event row with delivery_status='pending'.
        emission = await self._upsert_event(
            db=db,
            repo=repo,
            alert=alert,
            current_value=current_value,
        )
        if emission is None:
            # Same-day collision and existing event is already delivered;
            # nothing left to do for this loop iteration.
            stats.duplicates += 1
            return

        is_first_attempt = emission["is_first_attempt"]

        if is_first_attempt:
            stats.triggered += 1
            stats.details.append(emission["detail"])
            if alert.action_mode == "auto_execute_mock":
                payload = emission["payload"]
                execution_outcome = "failed"
                try:
                    execution_result = await maybe_auto_execute(
                        db,
                        alert=alert,
                        correlation_id=payload.correlation_id,
                        kst_date=payload.kst_date,
                        scanner_snapshot=payload.scanner_snapshot,
                    )
                    if execution_result.get("executed") is True:
                        execution_outcome = "executed"
                except Exception:  # noqa: BLE001 - never kill the scan loop
                    logger.exception(
                        "auto_execute_mock failed for alert %s",
                        emission["alert_uuid"],
                    )
                    # ROB-1110: the event row is already committed, but a
                    # failed statement leaves the transaction aborted — roll
                    # back so this alert's own delivery bookkeeping (and
                    # every alert after it) can still commit.
                    await self._reset_session(db)
                await repo.update_event_outcome(
                    emission["event_id"],
                    outcome=execution_outcome,
                )
                await db.commit()
                payload.outcome = execution_outcome
                emission["detail"]["outcome"] = execution_outcome
        else:
            # No new event row this iteration — we found an existing
            # pending/failed/skipped row from earlier in the day and are
            # re-attempting delivery against it. Count as a duplicate so
            # the scan summary reflects "row reused, not created".
            stats.duplicates += 1

        # Attempt Hermes delivery and gate the alert transition on it.
        delivery = await self._hermes.send_review_trigger(emission["payload"])
        await self._record_delivery_outcome(
            db=db,
            repo=repo,
            alert_id=emission["alert_id"],
            alert_uuid=emission["alert_uuid"],
            event_id=emission["event_id"],
            event_uuid=emission["event_uuid"],
            delivery=delivery,
            stats=stats,
        )

    async def _upsert_event(
        self,
        *,
        db: AsyncSession,
        repo: InvestmentReportsRepository,
        alert: _AlertSnapshot,
        current_value: float,
    ) -> dict[str, Any] | None:
        """Insert a fresh event row, or load the existing one for retry.

        Returns ``None`` if a same-day event already exists and was
        already delivered (nothing to do). Otherwise returns the event
        row + payload + ``is_first_attempt`` flag.

        ``alert`` is an :class:`_AlertSnapshot`, i.e. already detached
        plain data — the ``db.rollback()`` on the same-day collision path
        below expires the ORM identity map, so nothing read here may be a
        live ORM attribute (ROB-1110).
        """
        alert_id = alert.id
        alert_uuid_value = alert.alert_uuid
        alert_source_report_uuid = alert.source_report_uuid
        alert_source_item_uuid = alert.source_item_uuid
        alert_market = alert.market
        alert_target_kind = alert.target_kind
        alert_symbol = alert.symbol
        alert_metric = alert.metric
        alert_operator = alert.operator
        alert_threshold = alert.threshold
        alert_threshold_key = alert.threshold_key
        alert_threshold_high = alert.threshold_high
        alert_intent = alert.intent
        alert_action_mode = alert.action_mode
        alert_max_action = alert.max_action
        alert_trigger_checklist = alert.trigger_checklist
        alert_metadata = alert.alert_metadata

        outcome = _OUTCOME_BY_ACTION_MODE.get(alert_action_mode, "notified")
        correlation_id = uuid4().hex
        kst_date = now_kst().date().isoformat()
        idempotency_key = watch_event_key(
            alert_uuid=str(alert_uuid_value),
            kst_date=kst_date,
            threshold_key=alert_threshold_key,
        )
        current_value_decimal = Decimal(str(current_value))
        scanner_snapshot: dict[str, Any] = {
            "metric": alert_metric,
            "operator": alert_operator,
            "current_value": current_value,
            "threshold": float(alert_threshold),
        }
        if "synthetic_canary" in alert_metadata:
            scanner_snapshot["synthetic_canary"] = copy.deepcopy(
                alert_metadata["synthetic_canary"]
            )

        is_first_attempt = True
        try:
            event = await repo.insert_event(
                event_uuid=uuid4(),
                idempotency_key=idempotency_key,
                alert_id=alert_id,
                source_report_uuid=alert_source_report_uuid,
                source_item_uuid=alert_source_item_uuid,
                market=alert_market,
                target_kind=alert_target_kind,
                symbol=alert_symbol,
                metric=alert_metric,
                operator=alert_operator,
                threshold=alert_threshold,
                threshold_high=alert_threshold_high,
                threshold_key=alert_threshold_key,
                intent=alert_intent,
                action_mode=alert_action_mode,
                current_value=current_value_decimal,
                scanner_snapshot=scanner_snapshot,
                outcome=outcome,
                correlation_id=correlation_id,
                kst_date=kst_date,
            )
            await db.commit()
        except IntegrityError:
            # Same-day re-fire — load the existing row and retry delivery
            # if it's still pending/skipped/failed.
            await db.rollback()
            event = await repo.get_event_by_idempotency_key(idempotency_key)
            if event is None:
                return None
            if event.delivery_status == "delivered":
                # Already delivered on a previous run — no work to do.
                return None
            is_first_attempt = False

        # ROB-500 — operator-facing price guidance from the source item's
        # advisory watch_recommendation. Fail-open: a lookup problem must
        # never block the trigger notification itself.
        price_guidance = None
        try:
            item = (
                await repo.get_item_by_uuid(event.source_item_uuid)
                if event.source_item_uuid is not None
                else None
            )
            price_guidance = price_guidance_from_watch_recommendation(
                item.watch_recommendation if item is not None else None
            )
        except Exception:  # noqa: BLE001 - guidance is advisory, never fatal
            logger.warning(
                "watch_recommendation lookup failed for item %s — "
                "sending trigger without price guidance",
                event.source_item_uuid,
            )

        # Build the Hermes payload from the event row's persisted fields so
        # a retry sends the exact same identity snapshot that's on disk.
        payload = ReviewTriggerPayload(
            event_uuid=event.event_uuid,
            alert_uuid=alert_uuid_value,
            source_report_uuid=event.source_report_uuid,
            source_item_uuid=event.source_item_uuid,
            correlation_id=event.correlation_id,
            kst_date=event.kst_date,
            market=event.market,
            target_kind=event.target_kind,
            symbol=event.symbol,
            metric=event.metric,
            operator=event.operator,
            threshold=Decimal(str(event.threshold)),
            threshold_high=(
                Decimal(str(event.threshold_high))
                if event.threshold_high is not None
                else None
            ),
            threshold_key=event.threshold_key,
            intent=event.intent,
            action_mode=event.action_mode,
            current_value=(
                Decimal(str(event.current_value))
                if event.current_value is not None
                else None
            ),
            scanner_snapshot=event.scanner_snapshot,
            outcome=event.outcome,
            invest_links=build_invest_links(
                market=event.market,
                symbol=event.symbol,
                source_report_uuid=event.source_report_uuid,
                event_uuid=event.event_uuid,
                alert_uuid=alert_uuid_value,
            ),
            operator_action_guidance=build_operator_action_guidance(
                action_mode=event.action_mode, outcome=event.outcome
            ),
            price_guidance=price_guidance,
            planned_action=planned_action_from_max_action(alert_max_action),
            trigger_checklist=trigger_checklist_from_raw(alert_trigger_checklist),
        )
        return {
            "event": event,
            "event_id": event.id,
            "event_uuid": event.event_uuid,
            "alert_id": alert_id,
            "alert_uuid": alert_uuid_value,
            "payload": payload,
            "is_first_attempt": is_first_attempt,
            "detail": {
                "alert_uuid": str(alert_uuid_value),
                "event_uuid": str(event.event_uuid),
                "outcome": event.outcome,
                "symbol": alert_symbol,
                "current_value": current_value,
                "threshold": float(alert_threshold),
            },
        }

    async def _record_delivery_outcome(
        self,
        *,
        db: AsyncSession,
        repo: InvestmentReportsRepository,
        alert_id: int,
        alert_uuid: Any,
        event_id: int,
        event_uuid: Any,
        delivery: Any,
        stats: _ScanStats,
    ) -> None:
        """Update event delivery columns and gate alert.status on success.

        Plan 4 hardening: ``alert.status`` only transitions to
        ``triggered`` when ``delivery.status == 'success'``. A
        ``skipped`` or ``failed`` delivery leaves the alert ``active``
        so the next scan loop can re-attempt against the existing event
        row (looked up via the idempotency_key collision).
        """
        delivered_at: datetime | None = None
        delivery_reason: str | None = None

        if delivery.status == "success":
            delivery_status = "delivered"
            delivered_at = datetime.now(UTC)
            stats.notified += 1
        elif delivery.status == "skipped":
            delivery_status = "skipped"
            delivery_reason = delivery.reason or "hermes_disabled"
            stats.skipped_delivery += 1
        else:
            delivery_status = "failed"
            delivery_reason = delivery.reason or "unknown"
            stats.failed_delivery += 1
            logger.warning(
                "Hermes review-trigger delivery failed: "
                "alert_uuid=%s event_uuid=%s reason=%s",
                alert_uuid,
                event_uuid,
                delivery_reason,
            )

        await repo.update_event_delivery(
            event_id,
            delivery_status=delivery_status,
            delivery_reason=delivery_reason,
            delivered_at=delivered_at,
        )
        if delivery_status == "delivered":
            await repo.update_alert_status(alert_id, "triggered")
        await db.commit()

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
