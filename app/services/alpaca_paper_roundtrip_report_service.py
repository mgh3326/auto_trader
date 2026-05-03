"""Read-only Alpaca Paper roundtrip audit report assembler (ROB-92).

This module is deliberately side-effect free: it reads ledger rows already
provided by AlpacaPaperLedgerService and combines them with optional
caller-supplied snapshots. It must not call brokers or write to the database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.alpaca_paper_ledger import AlpacaPaperOrderLedgerRead
from app.schemas.alpaca_paper_roundtrip_report import (
    AlpacaPaperRoundtripReport,
    AlpacaPaperRoundtripReportListResponse,
    RoundtripAnomalyBlock,
    RoundtripApprovalPacketBlock,
    RoundtripCandidateBlock,
    RoundtripCompleteness,
    RoundtripFillBlock,
    RoundtripFinalPositionBlock,
    RoundtripLegBlock,
    RoundtripLookupKey,
    RoundtripOpenOrdersBlock,
    RoundtripOrderBlock,
    RoundtripQaBlock,
    RoundtripReconcileBlock,
)
from app.services.alpaca_paper_anomaly_checks import (
    build_paper_execution_preflight_report,
)
from app.services.alpaca_paper_ledger_service import (
    LIFECYCLE_ANOMALY,
    LIFECYCLE_CLOSED,
    LIFECYCLE_FILLED,
    LIFECYCLE_FINAL_RECONCILED,
    LIFECYCLE_PLANNED,
    LIFECYCLE_POSITION_RECONCILED,
    LIFECYCLE_PREVIEWED,
    LIFECYCLE_SELL_VALIDATED,
    LIFECYCLE_SUBMITTED,
    LIFECYCLE_VALIDATED,
    AlpacaPaperLedgerService,
)

_REQUIRED_STEPS = [
    LIFECYCLE_PLANNED,
    LIFECYCLE_PREVIEWED,
    LIFECYCLE_VALIDATED,
    LIFECYCLE_SUBMITTED,
    LIFECYCLE_FILLED,
    LIFECYCLE_POSITION_RECONCILED,
    LIFECYCLE_SELL_VALIDATED,
    LIFECYCLE_CLOSED,
    LIFECYCLE_FINAL_RECONCILED,
]


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _row_sort_key(row: Any) -> tuple[datetime, int]:
    created = _get(row, "created_at")
    if not isinstance(created, datetime):
        created = datetime.min.replace(tzinfo=UTC)
    row_id = _get(row, "id") or 0
    try:
        row_id_int = int(row_id)
    except (TypeError, ValueError):
        row_id_int = 0
    return created, row_id_int


def _safe_ledger_read(row: Any) -> AlpacaPaperOrderLedgerRead | None:
    try:
        return AlpacaPaperOrderLedgerRead.model_validate(row)
    except Exception:
        return None


def _latest(rows: Iterable[Any], predicate) -> Any | None:
    matching = [row for row in rows if predicate(row)]
    if not matching:
        return None
    return sorted(matching, key=_row_sort_key)[-1]


def _first(rows: list[Any]) -> Any | None:
    return rows[0] if rows else None


class AlpacaPaperRoundtripReportService:
    """Build read-only Alpaca Paper roundtrip reports from ledger rows."""

    def __init__(self, db: AsyncSession) -> None:
        self._ledger = AlpacaPaperLedgerService(db)

    async def build_report(
        self,
        *,
        lifecycle_correlation_id: str | None = None,
        client_order_id: str | None = None,
        candidate_uuid: uuid.UUID | None = None,
        briefing_artifact_run_uuid: uuid.UUID | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        positions: list[dict[str, Any]] | None = None,
        stale_after_minutes: int = 30,
        include_ledger_rows: bool = True,
        now: datetime | None = None,
    ) -> AlpacaPaperRoundtripReport:
        lookups = [
            lifecycle_correlation_id is not None,
            client_order_id is not None,
            candidate_uuid is not None,
            briefing_artifact_run_uuid is not None,
        ]
        if sum(lookups) != 1:
            raise ValueError("exactly one roundtrip lookup key is required")
        if stale_after_minutes < 1:
            raise ValueError("stale_after_minutes must be >= 1")

        lookup_key: RoundtripLookupKey
        rows: list[Any]
        if lifecycle_correlation_id is not None:
            value = lifecycle_correlation_id.strip()
            if not value:
                raise ValueError("lifecycle_correlation_id is required")
            lookup_key = RoundtripLookupKey(
                kind="lifecycle_correlation_id", value=value
            )
            rows = await self._ledger.list_by_correlation_id(value)
        elif client_order_id is not None:
            value = client_order_id.strip()
            if not value:
                raise ValueError("client_order_id is required")
            lookup_key = RoundtripLookupKey(kind="client_order_id", value=value)
            seed = await self._ledger.get_by_client_order_id(value)
            if seed is None:
                rows = []
            else:
                corr = _get(seed, "lifecycle_correlation_id") or value
                rows = await self._ledger.list_by_correlation_id(str(corr))
        elif candidate_uuid is not None:
            lookup_key = RoundtripLookupKey(
                kind="candidate_uuid", value=str(candidate_uuid)
            )
            rows = await self._ledger.list_by_candidate_uuid(candidate_uuid)
        else:
            assert briefing_artifact_run_uuid is not None
            lookup_key = RoundtripLookupKey(
                kind="briefing_artifact_run_uuid", value=str(briefing_artifact_run_uuid)
            )
            rows = await self._ledger.list_by_briefing_artifact_run_uuid(
                briefing_artifact_run_uuid
            )

        return self._build_report_from_rows(
            rows=rows,
            lookup_key=lookup_key,
            open_orders=open_orders or [],
            positions=positions or [],
            stale_after_minutes=stale_after_minutes,
            include_ledger_rows=include_ledger_rows,
            now=now,
        )

    async def build_reports_for_candidate_uuid(
        self,
        candidate_uuid: uuid.UUID,
        *,
        include_ledger_rows: bool = True,
        stale_after_minutes: int = 30,
        now: datetime | None = None,
    ) -> AlpacaPaperRoundtripReportListResponse:
        rows = await self._ledger.list_by_candidate_uuid(candidate_uuid)
        return self._build_grouped_response(
            rows=rows,
            lookup_key=RoundtripLookupKey(
                kind="candidate_uuid", value=str(candidate_uuid)
            ),
            include_ledger_rows=include_ledger_rows,
            stale_after_minutes=stale_after_minutes,
            now=now,
        )

    async def build_reports_for_briefing_artifact_run_uuid(
        self,
        briefing_artifact_run_uuid: uuid.UUID,
        *,
        include_ledger_rows: bool = True,
        stale_after_minutes: int = 30,
        now: datetime | None = None,
    ) -> AlpacaPaperRoundtripReportListResponse:
        rows = await self._ledger.list_by_briefing_artifact_run_uuid(
            briefing_artifact_run_uuid
        )
        return self._build_grouped_response(
            rows=rows,
            lookup_key=RoundtripLookupKey(
                kind="briefing_artifact_run_uuid",
                value=str(briefing_artifact_run_uuid),
            ),
            include_ledger_rows=include_ledger_rows,
            stale_after_minutes=stale_after_minutes,
            now=now,
        )

    def _build_grouped_response(
        self,
        *,
        rows: list[Any],
        lookup_key: RoundtripLookupKey,
        include_ledger_rows: bool,
        stale_after_minutes: int,
        now: datetime | None,
    ) -> AlpacaPaperRoundtripReportListResponse:
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            corr = str(_get(row, "lifecycle_correlation_id") or "")
            grouped.setdefault(corr, []).append(row)
        reports = [
            self._build_report_from_rows(
                rows=group_rows,
                lookup_key=lookup_key,
                open_orders=[],
                positions=[],
                stale_after_minutes=stale_after_minutes,
                include_ledger_rows=include_ledger_rows,
                now=now,
            )
            for _, group_rows in sorted(grouped.items())
        ]
        return AlpacaPaperRoundtripReportListResponse(
            lookup_key=lookup_key,
            count=len(reports),
            items=reports,
        )

    def _build_report_from_rows(
        self,
        *,
        rows: list[Any],
        lookup_key: RoundtripLookupKey,
        open_orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        stale_after_minutes: int,
        include_ledger_rows: bool,
        now: datetime | None,
    ) -> AlpacaPaperRoundtripReport:
        generated_at = now or datetime.now(UTC)
        ordered = sorted(rows, key=_row_sort_key)
        observed_steps = list(
            dict.fromkeys(str(_get(r, "lifecycle_state")) for r in ordered)
        )
        missing_steps = [step for step in _REQUIRED_STEPS if step not in observed_steps]
        completeness = RoundtripCompleteness(
            required_steps=list(_REQUIRED_STEPS),
            observed_steps=observed_steps,
            missing_steps=missing_steps,
            is_complete=not missing_steps,
        )
        first = _first(ordered)
        lifecycle_correlation_id = (
            _as_str(_get(first, "lifecycle_correlation_id")) if first else None
        )

        preflight = build_paper_execution_preflight_report(
            ledger_rows=[],
            open_orders=open_orders,
            positions=positions,
            stale_after_minutes=stale_after_minutes,
            now=generated_at,
        ).to_dict()
        row_anomalies = [
            {
                "check_id": "ledger_anomaly_row",
                "severity": "block",
                "summary": "Roundtrip ledger contains anomaly lifecycle row",
                "details": {
                    "client_order_id": _get(row, "client_order_id"),
                    "lifecycle_state": _get(row, "lifecycle_state"),
                    "error_summary": _get(row, "error_summary"),
                },
            }
            for row in ordered
            if _get(row, "lifecycle_state") == LIFECYCLE_ANOMALY
        ]
        anomalies_list = list(preflight.get("anomalies") or []) + row_anomalies
        should_block = bool(preflight.get("should_block")) or bool(row_anomalies)
        if not ordered:
            status = "not_found"
        elif should_block:
            status = "anomaly"
        elif completeness.is_complete:
            status = "complete"
        else:
            status = "incomplete"

        latest_validation = _latest(
            ordered, lambda r: _get(r, "validation_summary") is not None
        )
        latest_preview = _latest(
            ordered, lambda r: _get(r, "preview_payload") is not None
        )
        ledger_reads = (
            [_safe_ledger_read(r) for r in ordered] if include_ledger_rows else None
        )

        return AlpacaPaperRoundtripReport(
            lookup_key=lookup_key,
            lifecycle_correlation_id=lifecycle_correlation_id,
            generated_at=generated_at,
            status=status,
            completeness=completeness,
            candidate=self._candidate_block(first),
            qa_result=RoundtripQaBlock(
                briefing_artifact_run_uuid=_as_str(
                    _get(first, "briefing_artifact_run_uuid")
                ),
                briefing_artifact_status=_get(first, "briefing_artifact_status"),
                qa_evaluator_status=_get(first, "qa_evaluator_status"),
            ),
            approval_packet=RoundtripApprovalPacketBlock(
                approval_bridge_generated_at=_get(
                    first, "approval_bridge_generated_at"
                ),
                approval_bridge_status=_get(first, "approval_bridge_status"),
                preview_payload=_get(latest_preview, "preview_payload"),
                validation_summary=_get(latest_validation, "validation_summary"),
            ),
            buy_leg=self._leg_block(ordered, "buy"),
            sell_leg=self._leg_block(ordered, "sell"),
            final_position=self._final_position_block(ordered, positions),
            open_orders=RoundtripOpenOrdersBlock(
                source="caller_supplied" if open_orders else "missing",
                count=len(open_orders),
                orders=open_orders,
            ),
            anomalies=RoundtripAnomalyBlock(
                status=str(preflight.get("status") or "pass"),
                should_block=should_block,
                anomalies=anomalies_list,
                counts=dict(preflight.get("counts") or {}),
                preflight=preflight,
            ),
            ledger_rows=[r for r in ledger_reads or [] if r is not None]
            if include_ledger_rows
            else None,
        )

    def _candidate_block(self, row: Any | None) -> RoundtripCandidateBlock:
        return RoundtripCandidateBlock(
            candidate_uuid=_as_str(_get(row, "candidate_uuid")),
            signal_symbol=_get(row, "signal_symbol"),
            signal_venue=_get(row, "signal_venue"),
            execution_symbol=_get(row, "execution_symbol"),
            execution_venue=_get(row, "execution_venue"),
            execution_asset_class=_get(row, "execution_asset_class"),
            instrument_type=_as_str(_get(row, "instrument_type")),
            workflow_stage=_get(row, "workflow_stage"),
            purpose=_get(row, "purpose"),
        )

    def _leg_block(self, rows: list[Any], side: str) -> RoundtripLegBlock | None:
        leg_rows = [r for r in rows if str(_get(r, "side") or "").lower() == side]
        if not leg_rows:
            return None
        latest_order = (
            _latest(
                leg_rows,
                lambda r: (
                    _get(r, "broker_order_id") is not None
                    or _get(r, "submitted_at") is not None
                    or _get(r, "record_kind") == "execution"
                ),
            )
            or leg_rows[-1]
        )
        latest_fill = _latest(
            leg_rows,
            lambda r: (
                _get(r, "filled_qty") is not None
                or _get(r, "filled_avg_price") is not None
                or _get(r, "lifecycle_state") in {LIFECYCLE_FILLED, LIFECYCLE_CLOSED}
            ),
        )
        latest_reconcile = _latest(
            leg_rows,
            lambda r: (
                _get(r, "reconcile_status") is not None
                or _get(r, "position_snapshot") is not None
                or _get(r, "lifecycle_state")
                in {LIFECYCLE_POSITION_RECONCILED, LIFECYCLE_FINAL_RECONCILED}
            ),
        )
        return RoundtripLegBlock(
            side=side,  # type: ignore[arg-type]
            lifecycle_states=list(
                dict.fromkeys(str(_get(r, "lifecycle_state")) for r in leg_rows)
            ),
            record_kinds=list(
                dict.fromkeys(str(_get(r, "record_kind")) for r in leg_rows)
            ),
            order=RoundtripOrderBlock(
                client_order_id=_get(latest_order, "client_order_id"),
                broker_order_id=_get(latest_order, "broker_order_id"),
                order_status=_get(latest_order, "order_status"),
                order_type=_get(latest_order, "order_type"),
                time_in_force=_get(latest_order, "time_in_force"),
                requested_qty=_as_decimal(_get(latest_order, "requested_qty")),
                requested_notional=_as_decimal(
                    _get(latest_order, "requested_notional")
                ),
                requested_price=_as_decimal(_get(latest_order, "requested_price")),
                currency=_get(latest_order, "currency"),
                submitted_at=_get(latest_order, "submitted_at"),
            ),
            fill=RoundtripFillBlock(
                filled_qty=_as_decimal(_get(latest_fill, "filled_qty")),
                filled_avg_price=_as_decimal(_get(latest_fill, "filled_avg_price")),
                fee_amount=_as_decimal(_get(latest_fill, "fee_amount")),
                fee_currency=_get(latest_fill, "fee_currency"),
                qty_delta=_as_decimal(_get(latest_fill, "qty_delta")),
            ),
            reconcile=RoundtripReconcileBlock(
                reconcile_status=_get(latest_reconcile, "reconcile_status"),
                reconciled_at=_get(latest_reconcile, "reconciled_at"),
                settlement_status=_get(latest_reconcile, "settlement_status"),
                settlement_at=_get(latest_reconcile, "settlement_at"),
                position_snapshot=_get(latest_reconcile, "position_snapshot"),
                notes=_get(latest_reconcile, "notes"),
                error_summary=_get(latest_reconcile, "error_summary"),
            ),
            latest_row_created_at=_get(leg_rows[-1], "created_at"),
        )

    def _final_position_block(
        self, rows: list[Any], positions: list[dict[str, Any]]
    ) -> RoundtripFinalPositionBlock:
        if positions:
            first = positions[0]
            return RoundtripFinalPositionBlock(
                source="caller_supplied",
                symbol=_as_str(first.get("symbol")),
                qty=_as_decimal(first.get("qty") or first.get("quantity")),
                snapshot=first,
            )
        snapshot_row = _latest(rows, lambda r: _get(r, "position_snapshot") is not None)
        snapshot = _get(snapshot_row, "position_snapshot") if snapshot_row else None
        if isinstance(snapshot, dict):
            return RoundtripFinalPositionBlock(
                source="ledger_snapshot",
                symbol=_get(snapshot_row, "execution_symbol"),
                qty=_as_decimal(snapshot.get("qty") or snapshot.get("quantity")),
                snapshot=snapshot,
            )
        if LIFECYCLE_FINAL_RECONCILED in {
            str(_get(row, "lifecycle_state")) for row in rows
        }:
            return RoundtripFinalPositionBlock(
                source="ledger_snapshot", qty=Decimal("0")
            )
        return RoundtripFinalPositionBlock(source="missing")


__all__ = ["AlpacaPaperRoundtripReportService"]
