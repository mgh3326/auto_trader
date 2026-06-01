"""ROB-265 — Watch activation service.

Copies an approved watch :class:`InvestmentReportItem` into the
immutable activation snapshot held in :class:`InvestmentWatchAlert`.
Items are the source of truth; once activated, the alert's snapshot
fields are not mutated again (the scanner re-wire in Plan 4 only
flips ``status`` between active / triggered / expired / canceled).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentWatchAlert
from app.schemas.investment_reports import ActivateWatchRequest
from app.services.investment_reports.idempotency import watch_activation_key
from app.services.investment_reports.repository import InvestmentReportsRepository


class WatchActivationService:
    """Activate an approved watch item into ``investment_watch_alerts``."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def activate(self, request: ActivateWatchRequest) -> InvestmentWatchAlert:
        item = await self._repo.get_item_by_uuid(request.item_uuid)
        if item is None:
            raise ValueError(f"item not found: {request.item_uuid}")

        idempotency_key = request.idempotency_key or watch_activation_key(
            source_item_uuid=str(item.item_uuid)
        )

        # Idempotent re-activate: a subsequent call after the item has
        # already transitioned to ``activated`` returns the existing alert
        # without re-validating.
        existing = await self._repo.get_alert_by_idempotency_key(idempotency_key)
        if existing is not None:
            if existing.source_item_uuid != item.item_uuid:
                # Caller-supplied idempotency_key collided with an alert
                # already sourced from a different item — reject loudly
                # rather than silently aliasing.
                raise ValueError(
                    f"idempotency_key {idempotency_key!r} already used for a "
                    f"different watch item (existing source_item_uuid="
                    f"{existing.source_item_uuid}, requested source_item_uuid="
                    f"{item.item_uuid})"
                )
            return existing

        if item.item_kind != "watch":
            raise ValueError(
                f"only watch items can be activated; item_kind={item.item_kind}"
            )
        if item.status != "approved":
            raise ValueError(
                f"only approved items can be activated; status={item.status}"
            )
        # ROB-393 — operation='review' watches are created without a
        # watch_condition/valid_until (schema + DB CHECK both exempt them).
        # Allow supplying them at activation time; persist back onto the item
        # so the item stays the source of truth and re-activation is idempotent.
        watch_condition = item.watch_condition
        valid_until = item.valid_until

        if request.watch_condition is not None:
            if watch_condition is not None:
                raise ValueError(
                    "watch_condition already set on item; refusing to override "
                    "at activation"
                )
            watch_condition = request.watch_condition.model_dump(mode="json")
        if request.valid_until is not None:
            if valid_until is not None:
                raise ValueError(
                    "valid_until already set on item; refusing to override "
                    "at activation"
                )
            valid_until = request.valid_until

        if watch_condition is None:
            raise ValueError(
                "watch_condition not set (operation='review' watch); pass "
                "watch_condition to activate, or recreate the watch with a "
                "condition"
            )
        if valid_until is None:
            raise ValueError(
                "valid_until not set (operation='review' watch); pass "
                "valid_until to activate, or recreate the watch with an expiry"
            )
        if item.symbol is None:
            raise ValueError("symbol missing on watch item")

        # Persist any injected fields before building the alert.
        await self._repo.update_item_watch_condition(
            item.id,
            watch_condition=(watch_condition if item.watch_condition is None else None),
            valid_until=(valid_until if item.valid_until is None else None),
        )

        report = await self._repo.get_report_by_id(item.report_id)
        if report is None:
            raise ValueError(f"report not found for item: {item.report_id}")

        # ROB-393 supplies ``watch_condition`` (possibly injected from the
        # activation request for operation='review' watches); ROB-403 maps it
        # to the normalized clauses + flat primary summary.
        condition: dict[str, Any] = watch_condition
        clauses: list[dict[str, Any]] = list(condition.get("conditions") or [])
        if not clauses:
            # legacy flat payload that predates normalization
            clauses = [
                {
                    "metric": condition["metric"],
                    "op": condition["operator"],
                    "threshold": condition.get("threshold"),
                }
            ]
        combine = condition.get("combine", "and")
        primary = clauses[0]
        primary_metric = primary["metric"]
        if primary["op"] == "between":
            primary_operator = "between"
            primary_threshold = _to_decimal(primary.get("low"))
            primary_threshold_high: Decimal | None = _to_decimal(primary.get("high"))
        else:
            primary_operator = primary["op"]
            primary_threshold = _to_decimal(primary.get("threshold"))
            primary_threshold_high = None
        threshold_key = condition.get("threshold_key") or str(primary_threshold)

        alert = await self._repo.insert_alert(
            alert_uuid=None,  # default from PG
            idempotency_key=idempotency_key,
            source_report_uuid=report.report_uuid,
            source_item_uuid=item.item_uuid,
            market=report.market,
            target_kind=item.target_kind,
            symbol=item.symbol,
            metric=primary_metric,
            operator=primary_operator,
            threshold=primary_threshold,
            threshold_high=primary_threshold_high,
            threshold_key=threshold_key,
            conditions=clauses,
            combine=combine,
            intent=item.intent,
            action_mode=condition.get("action_mode", "notify_only"),
            rationale=item.rationale,
            trigger_checklist=list(item.trigger_checklist),
            max_action=dict(item.max_action),
            valid_until=valid_until,
        )

        await self._repo.update_item_status(item.id, "activated")
        await self._session.flush()
        return alert


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("threshold is required in watch_condition")
    return Decimal(str(value))
