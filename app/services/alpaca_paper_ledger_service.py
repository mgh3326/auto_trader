"""Alpaca Paper execution state ledger service (ROB-84/ROB-90).

Pure record-keeping only. Must not import or call broker mutation services,
KIS, Upbit, watch alerts, order intents, or scheduler code.
All writes receive already-produced payload data and persist state only.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.schemas.preopen import (
    PreopenBriefingArtifact,
    PreopenPaperApprovalBridge,
    PreopenPaperApprovalCandidate,
)

# ---------------------------------------------------------------------------
# ROB-90 canonical lifecycle state constants
# ---------------------------------------------------------------------------
LIFECYCLE_PLANNED = "planned"
LIFECYCLE_PREVIEWED = "previewed"
LIFECYCLE_VALIDATED = "validated"
LIFECYCLE_SUBMITTED = "submitted"
LIFECYCLE_FILLED = "filled"
LIFECYCLE_POSITION_RECONCILED = "position_reconciled"
LIFECYCLE_SELL_VALIDATED = "sell_validated"
LIFECYCLE_CLOSED = "closed"
LIFECYCLE_FINAL_RECONCILED = "final_reconciled"
LIFECYCLE_ANOMALY = "anomaly"
LIFECYCLE_STALE_PREVIEW_CLEANUP_REQUIRED = "stale_preview_cleanup_required"

CANONICAL_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        LIFECYCLE_PLANNED,
        LIFECYCLE_PREVIEWED,
        LIFECYCLE_VALIDATED,
        LIFECYCLE_SUBMITTED,
        LIFECYCLE_FILLED,
        LIFECYCLE_POSITION_RECONCILED,
        LIFECYCLE_SELL_VALIDATED,
        LIFECYCLE_CLOSED,
        LIFECYCLE_FINAL_RECONCILED,
        LIFECYCLE_ANOMALY,
        LIFECYCLE_STALE_PREVIEW_CLEANUP_REQUIRED,
    }
)

# ROB-91: post-submit executed states used for idempotency checks.
# Excludes pre-submit (planned/previewed/validated) and anomaly.
EXECUTED_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        LIFECYCLE_SUBMITTED,
        LIFECYCLE_FILLED,
        LIFECYCLE_POSITION_RECONCILED,
        LIFECYCLE_SELL_VALIDATED,
        LIFECYCLE_CLOSED,
        LIFECYCLE_FINAL_RECONCILED,
    }
)

# ROB-90 record_kind constants
RECORD_KIND_PLAN = "plan"
RECORD_KIND_PREVIEW = "preview"
RECORD_KIND_VALIDATION_ATTEMPT = "validation_attempt"
RECORD_KIND_EXECUTION = "execution"
RECORD_KIND_RECONCILE = "reconcile"
RECORD_KIND_ANOMALY = "anomaly"

# ---------------------------------------------------------------------------
# Sensitive key patterns — redact before any JSON persistence
# ---------------------------------------------------------------------------
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|secret|authorization|token|account[_-]?no|"
    r"account[_-]?number|account[_-]?id|account[_-]?identifier|"
    r"email|passwd|password|credential)",
    re.IGNORECASE,
)


_SENSITIVE_TEXT_VALUE_RE = re.compile(
    r"(?P<prefix>\b(api[_-]?key|secret|token|account[_-]?no|"
    r"account[_-]?number|account[_-]?id|account[_-]?identifier|"
    r"email|passwd|password|credential)\b\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_AUTHORIZATION_TEXT_VALUE_RE = re.compile(
    r"(?P<prefix>\bauthorization\b\s*[:=]\s*)"
    r"(?P<value>[^,;]+?)(?=\s+\w+(?:[_-]?\w+)*\s*[:=]|$|[,;])",
    re.IGNORECASE,
)


def _redact_sensitive_keys(payload: Any) -> Any:
    """Recursively redact sensitive keys from dicts/lists before persistence."""
    if isinstance(payload, dict):
        return {
            k: "[REDACTED]"
            if _SENSITIVE_KEY_RE.search(str(k))
            else _redact_sensitive_keys(v)
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_sensitive_keys(item) for item in payload]
    if isinstance(payload, str):
        return _redact_sensitive_text(payload)
    return payload


def _redact_sensitive_text(text: str | None) -> str | None:
    """Redact key=value/key: value secrets from operator narrative text."""
    if text is None:
        return None
    redacted = _AUTHORIZATION_TEXT_VALUE_RE.sub(r"\g<prefix>[REDACTED]", text)
    return _SENSITIVE_TEXT_VALUE_RE.sub(r"\g<prefix>[REDACTED]", redacted)


# ---------------------------------------------------------------------------
# Lifecycle state derivation from broker order status (ROB-90 canonical)
# ---------------------------------------------------------------------------
_OPEN_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "held",
        "done_for_day",
        "pending_cancel",
        "pending_replace",
        "replaced",
        "stopped",
        "calculated",
    }
)
_ANOMALY_STATUSES = frozenset({"rejected", "expired", "suspended"})


def _derive_lifecycle_state(
    order_status: str | None,
    filled_qty: Decimal | float | None = None,
) -> str:
    """Map broker order_status to ROB-90 canonical lifecycle state.

    Mapping:
    - filled → filled
    - partially_filled → submitted (broker status preserved in order_status)
    - open statuses (new/accepted/…) → submitted
    - open status with filled_qty > 0 → anomaly
    - canceled → anomaly (ROB-90: no benign cancel state)
    - rejected/expired/suspended/unknown → anomaly
    """
    if order_status is None:
        return LIFECYCLE_ANOMALY
    status = order_status.lower()
    if status == "filled":
        return LIFECYCLE_FILLED
    if status == "partially_filled":
        return LIFECYCLE_SUBMITTED
    if status in _OPEN_STATUSES:
        if filled_qty is not None:
            try:
                qty = float(filled_qty)
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0:
                return LIFECYCLE_ANOMALY
        return LIFECYCLE_SUBMITTED
    if status in _ANOMALY_STATUSES or status == "canceled":
        return LIFECYCLE_ANOMALY
    return LIFECYCLE_ANOMALY


# ---------------------------------------------------------------------------
# ApprovalProvenance dataclass
# ---------------------------------------------------------------------------
@dataclass
class ApprovalProvenance:
    candidate_uuid: uuid.UUID | None = None
    signal_symbol: str | None = None
    signal_venue: str | None = None
    execution_asset_class: str | None = None
    workflow_stage: str | None = None
    purpose: str | None = None
    briefing_artifact_run_uuid: uuid.UUID | None = None
    briefing_artifact_status: str | None = None
    qa_evaluator_status: str | None = None
    approval_bridge_generated_at: datetime | None = None
    approval_bridge_status: str | None = None


def from_approval_bridge(
    bridge: PreopenPaperApprovalBridge,
    candidate: PreopenPaperApprovalCandidate,
    briefing_artifact: PreopenBriefingArtifact | None = None,
    qa_evaluator_status: str | None = None,
) -> ApprovalProvenance:
    """Build ApprovalProvenance from approval bridge/candidate objects.

    Tolerates missing briefing_artifact and qa_evaluator_status.
    """
    briefing_run_uuid: uuid.UUID | None = None
    briefing_status: str | None = None
    if briefing_artifact is not None:
        if briefing_artifact.run_uuid is not None:
            briefing_run_uuid = briefing_artifact.run_uuid
        briefing_status = briefing_artifact.status

    return ApprovalProvenance(
        candidate_uuid=candidate.candidate_uuid,
        signal_symbol=candidate.signal_symbol,
        signal_venue=candidate.signal_venue,
        execution_asset_class=candidate.execution_asset_class,
        workflow_stage=candidate.workflow_stage,
        purpose=candidate.purpose,
        briefing_artifact_run_uuid=briefing_run_uuid,
        briefing_artifact_status=briefing_status,
        qa_evaluator_status=qa_evaluator_status,
        approval_bridge_generated_at=bridge.generated_at,
        approval_bridge_status=bridge.status,
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LedgerNotFoundError(Exception):
    """Raised when a ledger row with the given client_order_id does not exist."""


# ---------------------------------------------------------------------------
# AlpacaPaperLedgerService
# ---------------------------------------------------------------------------
class AlpacaPaperLedgerService:
    """Pure record-keeping service for Alpaca Paper order lifecycle.

    No broker calls. Receives already-produced payload data.
    All sensitive keys in JSONB payloads are redacted before persistence.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> AlpacaPaperOrderLedger | None:
        stmt = (
            select(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.client_order_id == client_order_id)
            .order_by(
                AlpacaPaperOrderLedger.created_at.desc(),
                AlpacaPaperOrderLedger.id.desc(),
            )
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, ledger_id: int) -> AlpacaPaperOrderLedger | None:
        stmt = select(AlpacaPaperOrderLedger).where(
            AlpacaPaperOrderLedger.id == ledger_id
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_recent(
        self,
        limit: int = 50,
        lifecycle_state: str | None = None,
    ) -> list[AlpacaPaperOrderLedger]:
        stmt = select(AlpacaPaperOrderLedger).order_by(
            AlpacaPaperOrderLedger.created_at.desc()
        )
        if lifecycle_state is not None:
            stmt = stmt.where(AlpacaPaperOrderLedger.lifecycle_state == lifecycle_state)
        stmt = stmt.limit(limit)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_correlation_id(
        self,
        lifecycle_correlation_id: str,
    ) -> list[AlpacaPaperOrderLedger]:
        """Return all ledger rows sharing a lifecycle_correlation_id, ordered by created_at, id."""
        if not lifecycle_correlation_id or not lifecycle_correlation_id.strip():
            raise ValueError("lifecycle_correlation_id must not be empty")
        stmt = (
            select(AlpacaPaperOrderLedger)
            .where(
                AlpacaPaperOrderLedger.lifecycle_correlation_id
                == lifecycle_correlation_id
            )
            .order_by(
                AlpacaPaperOrderLedger.created_at.asc(),
                AlpacaPaperOrderLedger.id.asc(),
            )
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_candidate_uuid(
        self,
        candidate_uuid: uuid.UUID,
    ) -> list[AlpacaPaperOrderLedger]:
        """Return all ledger rows for a candidate UUID, ordered by created_at, id."""
        stmt = (
            select(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.candidate_uuid == candidate_uuid)
            .order_by(
                AlpacaPaperOrderLedger.created_at.asc(),
                AlpacaPaperOrderLedger.id.asc(),
            )
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_briefing_artifact_run_uuid(
        self,
        briefing_artifact_run_uuid: uuid.UUID,
    ) -> list[AlpacaPaperOrderLedger]:
        """Return all ledger rows for a briefing artifact UUID, ordered by created_at, id."""
        stmt = (
            select(AlpacaPaperOrderLedger)
            .where(
                AlpacaPaperOrderLedger.briefing_artifact_run_uuid
                == briefing_artifact_run_uuid
            )
            .order_by(
                AlpacaPaperOrderLedger.created_at.asc(),
                AlpacaPaperOrderLedger.id.asc(),
            )
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def find_executed_by_client_order_id(
        self,
        client_order_id: str,
    ) -> AlpacaPaperOrderLedger | None:
        """Return the execution row for client_order_id if it is in an executed lifecycle state.

        Returns None if the row does not exist or is in a pre-submit / preview-only state.
        Filters to record_kind='execution' and lifecycle_state in EXECUTED_LIFECYCLE_STATES.
        """
        stmt = (
            select(AlpacaPaperOrderLedger)
            .where(
                AlpacaPaperOrderLedger.client_order_id == client_order_id,
                AlpacaPaperOrderLedger.record_kind == RECORD_KIND_EXECUTION,
                AlpacaPaperOrderLedger.lifecycle_state.in_(EXECUTED_LIFECYCLE_STATES),
            )
            .order_by(
                AlpacaPaperOrderLedger.created_at.desc(),
                AlpacaPaperOrderLedger.id.desc(),
            )
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _require_row(self, client_order_id: str) -> AlpacaPaperOrderLedger:
        row = await self.get_by_client_order_id(client_order_id)
        if row is None:
            raise LedgerNotFoundError(
                f"No ledger row found for client_order_id={client_order_id!r}"
            )
        return row

    async def _accumulate_raw_response(
        self,
        client_order_id: str,
        event_key: str,
        raw_response: dict[str, Any] | None,
    ) -> None:
        if raw_response is None:
            return
        sanitized = _redact_sensitive_keys(raw_response)
        row = await self._require_row(client_order_id)
        existing: dict[str, Any] = dict(row.raw_responses or {})
        target_key = event_key
        suffix = 2
        while target_key in existing:
            target_key = f"{event_key}_{suffix}"
            suffix += 1
        existing[target_key] = sanitized
        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.id == row.id)
            .values(raw_responses=existing)
        )

    def _build_provenance_values(self, prov: ApprovalProvenance) -> dict[str, Any]:
        return {
            "signal_symbol": prov.signal_symbol,
            "signal_venue": prov.signal_venue,
            "execution_asset_class": prov.execution_asset_class,
            "workflow_stage": prov.workflow_stage,
            "purpose": prov.purpose,
            "briefing_artifact_run_uuid": prov.briefing_artifact_run_uuid,
            "briefing_artifact_status": prov.briefing_artifact_status,
            "qa_evaluator_status": prov.qa_evaluator_status,
            "approval_bridge_generated_at": prov.approval_bridge_generated_at,
            "approval_bridge_status": prov.approval_bridge_status,
            "candidate_uuid": prov.candidate_uuid,
        }

    # ------------------------------------------------------------------
    # Lifecycle write methods
    # ------------------------------------------------------------------

    async def record_plan(
        self,
        *,
        client_order_id: str,
        lifecycle_correlation_id: str | None = None,
        execution_symbol: str,
        execution_venue: str,
        instrument_type: InstrumentType,
        side: str,
        order_type: str = "limit",
        time_in_force: str | None = None,
        requested_qty: Decimal | float | None = None,
        requested_notional: Decimal | float | None = None,
        requested_price: Decimal | float | None = None,
        currency: str = "USD",
        leg_role: str | None = None,
        provenance: ApprovalProvenance | None = None,
        notes: str | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Insert a planned row (lifecycle_state='planned', record_kind='plan')."""
        if not client_order_id or not client_order_id.strip():
            raise ValueError("client_order_id must not be empty")

        prov = provenance or ApprovalProvenance()
        correlation_id = lifecycle_correlation_id or client_order_id

        values: dict[str, Any] = {
            "client_order_id": client_order_id,
            "lifecycle_correlation_id": correlation_id,
            "record_kind": RECORD_KIND_PLAN,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "lifecycle_state": LIFECYCLE_PLANNED,
            "execution_symbol": execution_symbol,
            "execution_venue": execution_venue,
            "instrument_type": instrument_type,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "requested_qty": requested_qty,
            "requested_notional": requested_notional,
            "requested_price": requested_price,
            "currency": currency,
            "leg_role": leg_role,
            "confirm_flag": None,
            "notes": _redact_sensitive_text(notes),
            **self._build_provenance_values(prov),
        }

        stmt = (
            pg_insert(AlpacaPaperOrderLedger)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["client_order_id", "record_kind"],
                index_where=text("validation_attempt_no IS NULL"),
            )
        )
        await self._db.execute(stmt)
        await self._db.commit()

        row = await self.get_by_client_order_id(client_order_id)
        if row is None:
            raise LedgerNotFoundError(
                f"No ledger row found for client_order_id={client_order_id!r}"
            )
        return row

    async def record_preview(
        self,
        *,
        client_order_id: str,
        lifecycle_correlation_id: str | None = None,
        execution_symbol: str,
        execution_venue: str,
        instrument_type: InstrumentType,
        side: str,
        order_type: str = "limit",
        time_in_force: str | None = None,
        requested_qty: Decimal | float | None = None,
        requested_notional: Decimal | float | None = None,
        requested_price: Decimal | float | None = None,
        currency: str = "USD",
        preview_payload: dict[str, Any] | None = None,
        validation_summary: dict[str, Any] | None = None,
        lifecycle_state: str = LIFECYCLE_PREVIEWED,
        leg_role: str | None = None,
        provenance: ApprovalProvenance | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Insert a preview row (record_kind='preview'); idempotent on duplicate."""
        if not client_order_id or not client_order_id.strip():
            raise ValueError("client_order_id must not be empty")

        prov = provenance or ApprovalProvenance()
        correlation_id = lifecycle_correlation_id or client_order_id
        sanitized_preview = (
            _redact_sensitive_keys(preview_payload) if preview_payload else None
        )
        sanitized_validation = (
            _redact_sensitive_keys(validation_summary) if validation_summary else None
        )
        initial_raw = {}
        if raw_response is not None:
            initial_raw["preview"] = _redact_sensitive_keys(raw_response)

        values: dict[str, Any] = {
            "client_order_id": client_order_id,
            "lifecycle_correlation_id": correlation_id,
            "record_kind": RECORD_KIND_PREVIEW,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "lifecycle_state": lifecycle_state,
            "execution_symbol": execution_symbol,
            "execution_venue": execution_venue,
            "instrument_type": instrument_type,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "requested_qty": requested_qty,
            "requested_notional": requested_notional,
            "requested_price": requested_price,
            "currency": currency,
            "preview_payload": sanitized_preview,
            "validation_summary": sanitized_validation,
            "leg_role": leg_role,
            "confirm_flag": None,
            "raw_responses": initial_raw if initial_raw else None,
            **self._build_provenance_values(prov),
        }

        stmt = (
            pg_insert(AlpacaPaperOrderLedger)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["client_order_id", "record_kind"],
                index_where=text("validation_attempt_no IS NULL"),
            )
        )
        await self._db.execute(stmt)
        await self._db.commit()

        return await self._require_row(client_order_id)

    async def record_validation_attempt(
        self,
        *,
        client_order_id: str,
        lifecycle_correlation_id: str | None = None,
        execution_symbol: str,
        execution_venue: str,
        instrument_type: InstrumentType,
        side: str,
        order_type: str = "limit",
        time_in_force: str | None = None,
        requested_qty: Decimal | float | None = None,
        requested_notional: Decimal | float | None = None,
        requested_price: Decimal | float | None = None,
        currency: str = "USD",
        validation_attempt_no: int = 1,
        validation_outcome: str = "failed",
        validation_summary: dict[str, Any] | None = None,
        leg_role: str | None = None,
        provenance: ApprovalProvenance | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Insert a validation attempt row (confirm=false).

        Each attempt is distinguished by validation_attempt_no.
        lifecycle_state is set to 'validated' for passed, 'anomaly' for failed.
        """
        if not client_order_id or not client_order_id.strip():
            raise ValueError("client_order_id must not be empty")
        if validation_attempt_no < 1:
            raise ValueError("validation_attempt_no must be >= 1")

        prov = provenance or ApprovalProvenance()
        correlation_id = lifecycle_correlation_id or client_order_id
        sanitized_validation = (
            _redact_sensitive_keys(validation_summary) if validation_summary else None
        )
        initial_raw: dict[str, Any] = {}
        if raw_response is not None:
            initial_raw[f"validation_{validation_attempt_no}"] = _redact_sensitive_keys(
                raw_response
            )

        if validation_outcome == "passed":
            lc_state = LIFECYCLE_VALIDATED
        else:
            lc_state = LIFECYCLE_ANOMALY

        values: dict[str, Any] = {
            "client_order_id": client_order_id,
            "lifecycle_correlation_id": correlation_id,
            "record_kind": RECORD_KIND_VALIDATION_ATTEMPT,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "lifecycle_state": lc_state,
            "execution_symbol": execution_symbol,
            "execution_venue": execution_venue,
            "instrument_type": instrument_type,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "requested_qty": requested_qty,
            "requested_notional": requested_notional,
            "requested_price": requested_price,
            "currency": currency,
            "validation_attempt_no": validation_attempt_no,
            "validation_outcome": validation_outcome,
            "validation_summary": sanitized_validation,
            "leg_role": leg_role,
            "confirm_flag": False,
            "raw_responses": initial_raw if initial_raw else None,
            **self._build_provenance_values(prov),
        }

        stmt = (
            pg_insert(AlpacaPaperOrderLedger)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    "lifecycle_correlation_id",
                    "side",
                    "validation_attempt_no",
                ],
                index_where=text("record_kind = 'validation_attempt'"),
            )
        )
        await self._db.execute(stmt)
        await self._db.commit()

        row = await self.get_by_client_order_id(client_order_id)
        if row is None:
            raise LedgerNotFoundError(
                f"No ledger row found for client_order_id={client_order_id!r}"
            )
        return row

    async def record_submit(
        self,
        client_order_id: str,
        order: dict[str, Any],
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Record a confirmed submit as a distinct execution row."""
        source_row = await self._require_row(client_order_id)

        broker_order_id = (
            order.get("id") or order.get("order_id") or order.get("broker_order_id")
        )
        order_status = order.get("status")
        filled_qty_raw = order.get("filled_qty") or order.get("filled_quantity")
        filled_avg_price_raw = order.get("filled_avg_price") or order.get(
            "avg_fill_price"
        )

        filled_qty: Decimal | None = None
        if filled_qty_raw is not None:
            try:
                filled_qty = Decimal(str(filled_qty_raw))
            except Exception:
                filled_qty = None

        filled_avg_price: Decimal | None = None
        if filled_avg_price_raw is not None:
            try:
                filled_avg_price = Decimal(str(filled_avg_price_raw))
            except Exception:
                filled_avg_price = None

        lifecycle_state = _derive_lifecycle_state(order_status, filled_qty)
        raw_responses = None
        if raw_response is not None:
            raw_responses = {"submit": _redact_sensitive_keys(raw_response)}

        values: dict[str, Any] = {
            "client_order_id": client_order_id,
            "lifecycle_correlation_id": source_row.lifecycle_correlation_id
            or client_order_id,
            "record_kind": RECORD_KIND_EXECUTION,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "lifecycle_state": lifecycle_state,
            "execution_symbol": source_row.execution_symbol,
            "execution_venue": source_row.execution_venue,
            "instrument_type": source_row.instrument_type,
            "side": source_row.side,
            "order_type": source_row.order_type,
            "time_in_force": source_row.time_in_force,
            "requested_qty": source_row.requested_qty,
            "requested_notional": source_row.requested_notional,
            "requested_price": source_row.requested_price,
            "currency": source_row.currency,
            "leg_role": source_row.leg_role,
            "preview_payload": source_row.preview_payload,
            "validation_summary": source_row.validation_summary,
            "signal_symbol": source_row.signal_symbol,
            "signal_venue": source_row.signal_venue,
            "execution_asset_class": source_row.execution_asset_class,
            "workflow_stage": source_row.workflow_stage,
            "purpose": source_row.purpose,
            "briefing_artifact_run_uuid": source_row.briefing_artifact_run_uuid,
            "briefing_artifact_status": source_row.briefing_artifact_status,
            "qa_evaluator_status": source_row.qa_evaluator_status,
            "approval_bridge_generated_at": source_row.approval_bridge_generated_at,
            "approval_bridge_status": source_row.approval_bridge_status,
            "candidate_uuid": source_row.candidate_uuid,
            "order_status": order_status,
            "broker_order_id": str(broker_order_id) if broker_order_id else None,
            "submitted_at": datetime.now(UTC),
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
            "confirm_flag": True,
            "raw_responses": raw_responses,
        }
        if lifecycle_state == LIFECYCLE_ANOMALY:
            values["error_summary"] = (
                f"anomaly: order_status={order_status!r} after submit"
            )

        update_vals = {
            k: v
            for k, v in values.items()
            if k not in {"client_order_id", "record_kind"} and v is not None
        }
        stmt = (
            pg_insert(AlpacaPaperOrderLedger)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["client_order_id", "record_kind"],
                index_where=text("validation_attempt_no IS NULL"),
                set_=update_vals,
            )
        )
        await self._db.execute(stmt)
        await self._db.commit()
        return await self._require_row(client_order_id)

    async def record_status(
        self,
        client_order_id: str,
        order: dict[str, Any],
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Update lifecycle state from a status-check response."""
        target_row = await self._require_row(client_order_id)

        order_status = order.get("status")
        filled_qty_raw = order.get("filled_qty") or order.get("filled_quantity")

        filled_qty: Decimal | None = None
        if filled_qty_raw is not None:
            try:
                filled_qty = Decimal(str(filled_qty_raw))
            except Exception:
                filled_qty = None

        filled_avg_price_raw = order.get("filled_avg_price") or order.get(
            "avg_fill_price"
        )
        filled_avg_price: Decimal | None = None
        if filled_avg_price_raw is not None:
            try:
                filled_avg_price = Decimal(str(filled_avg_price_raw))
            except Exception:
                filled_avg_price = None

        lifecycle_state = _derive_lifecycle_state(order_status, filled_qty)

        update_vals: dict[str, Any] = {
            "lifecycle_state": lifecycle_state,
            "order_status": order_status,
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
        }
        if lifecycle_state == LIFECYCLE_ANOMALY:
            update_vals["error_summary"] = (
                f"anomaly: order_status={order_status!r} during status check"
            )

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.id == target_row.id)
            .values(**update_vals)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(client_order_id, "status", raw_response)

        await self._db.commit()
        return await self._require_row(client_order_id)

    async def record_cancel(
        self,
        client_order_id: str,
        cancel_status: str,
        raw_response: dict[str, Any] | None = None,
        error_summary: str | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Record cancel metadata. Lifecycle state is set by record_status, not here."""
        target_row = await self._require_row(client_order_id)

        update_vals: dict[str, Any] = {
            "cancel_status": cancel_status,
            "canceled_at": datetime.now(UTC),
        }
        if error_summary is not None:
            update_vals["error_summary"] = _redact_sensitive_text(error_summary)

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.id == target_row.id)
            .values(**update_vals)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(client_order_id, "cancel", raw_response)

        await self._db.commit()
        return await self._require_row(client_order_id)

    async def record_position_snapshot(
        self,
        client_order_id: str,
        position: dict[str, Any] | None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Record position snapshot and advance lifecycle to position_reconciled.

        position=None means no position found (explicit zero).
        position=dict means record qty and avg_entry_price from broker response.
        """
        target_row = await self._require_row(client_order_id)

        if position is None:
            snapshot: dict[str, Any] = {
                "qty": "0",
                "avg_entry_price": None,
                "fetched_at": datetime.now(UTC).isoformat(),
            }
        else:
            sanitized = _redact_sensitive_keys(position)
            snapshot = {
                "qty": str(sanitized.get("qty") or sanitized.get("quantity") or "0"),
                "avg_entry_price": sanitized.get("avg_entry_price")
                or sanitized.get("avg_cost"),
                "fetched_at": datetime.now(UTC).isoformat(),
                **{
                    k: v
                    for k, v in sanitized.items()
                    if k not in {"qty", "quantity", "avg_entry_price", "avg_cost"}
                },
            }

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.id == target_row.id)
            .values(
                position_snapshot=snapshot,
                lifecycle_state=LIFECYCLE_POSITION_RECONCILED,
            )
        )

        if raw_response is not None:
            await self._accumulate_raw_response(
                client_order_id, "position", raw_response
            )

        await self._db.commit()
        return await self._require_row(client_order_id)

    async def record_sell_validation(
        self,
        *,
        client_order_id: str,
        lifecycle_correlation_id: str | None = None,
        execution_symbol: str,
        execution_venue: str,
        instrument_type: InstrumentType,
        side: str = "sell",
        order_type: str = "limit",
        time_in_force: str | None = None,
        requested_qty: Decimal | float | None = None,
        requested_notional: Decimal | float | None = None,
        requested_price: Decimal | float | None = None,
        currency: str = "USD",
        validation_attempt_no: int = 1,
        validation_outcome: str = "passed",
        validation_summary: dict[str, Any] | None = None,
        leg_role: str | None = "sell",
        provenance: ApprovalProvenance | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Insert a sell-side validation attempt row (lifecycle_state='sell_validated')."""
        if not client_order_id or not client_order_id.strip():
            raise ValueError("client_order_id must not be empty")

        prov = provenance or ApprovalProvenance()
        correlation_id = lifecycle_correlation_id or client_order_id
        sanitized_validation = (
            _redact_sensitive_keys(validation_summary) if validation_summary else None
        )
        initial_raw: dict[str, Any] = {}
        if raw_response is not None:
            initial_raw[f"sell_validation_{validation_attempt_no}"] = (
                _redact_sensitive_keys(raw_response)
            )

        values: dict[str, Any] = {
            "client_order_id": client_order_id,
            "lifecycle_correlation_id": correlation_id,
            "record_kind": RECORD_KIND_VALIDATION_ATTEMPT,
            "broker": "alpaca",
            "account_mode": "alpaca_paper",
            "lifecycle_state": LIFECYCLE_SELL_VALIDATED,
            "execution_symbol": execution_symbol,
            "execution_venue": execution_venue,
            "instrument_type": instrument_type,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "requested_qty": requested_qty,
            "requested_notional": requested_notional,
            "requested_price": requested_price,
            "currency": currency,
            "validation_attempt_no": validation_attempt_no,
            "validation_outcome": validation_outcome,
            "validation_summary": sanitized_validation,
            "leg_role": leg_role,
            "confirm_flag": False,
            "raw_responses": initial_raw if initial_raw else None,
            **self._build_provenance_values(prov),
        }

        stmt = (
            pg_insert(AlpacaPaperOrderLedger)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    "lifecycle_correlation_id",
                    "side",
                    "validation_attempt_no",
                ],
                index_where=text("record_kind = 'validation_attempt'"),
            )
        )
        await self._db.execute(stmt)
        await self._db.commit()

        row = await self.get_by_client_order_id(client_order_id)
        if row is None:
            raise LedgerNotFoundError(
                f"No ledger row found for client_order_id={client_order_id!r}"
            )
        return row

    async def record_close(
        self,
        client_order_id: str,
        *,
        qty_delta: Decimal | float | None = None,
        notes: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Advance lifecycle to 'closed' after sell execution."""
        target_row = await self._require_row(client_order_id)

        update_vals: dict[str, Any] = {
            "lifecycle_state": LIFECYCLE_CLOSED,
        }
        if qty_delta is not None:
            update_vals["qty_delta"] = qty_delta
        if notes is not None:
            update_vals["notes"] = _redact_sensitive_text(notes)

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.id == target_row.id)
            .values(**update_vals)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(client_order_id, "close", raw_response)

        await self._db.commit()
        return await self._require_row(client_order_id)

    async def record_reconcile(
        self,
        client_order_id: str,
        reconcile_status: str,
        notes: str | None = None,
        error_summary: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Record reconciliation result."""
        target_row = await self._require_row(client_order_id)

        update_vals: dict[str, Any] = {
            "reconcile_status": reconcile_status,
            "reconciled_at": datetime.now(UTC),
            "error_summary": _redact_sensitive_text(error_summary)
            if error_summary is not None
            else None,
        }
        if notes is not None:
            update_vals["notes"] = _redact_sensitive_text(notes)

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.id == target_row.id)
            .values(**update_vals)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(
                client_order_id, "reconcile", raw_response
            )

        await self._db.commit()
        return await self._require_row(client_order_id)

    async def record_final_reconcile(
        self,
        client_order_id: str,
        *,
        reconcile_status: str = "ok",
        settlement_status: str = "n_a",
        qty_delta: Decimal | float | None = None,
        notes: str | None = None,
        error_summary: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Record final roundtrip reconciliation (lifecycle_state='final_reconciled')."""
        target_row = await self._require_row(client_order_id)

        update_vals: dict[str, Any] = {
            "lifecycle_state": LIFECYCLE_FINAL_RECONCILED,
            "record_kind": RECORD_KIND_RECONCILE,
            "reconcile_status": reconcile_status,
            "reconciled_at": datetime.now(UTC),
            "settlement_status": settlement_status,
            "error_summary": _redact_sensitive_text(error_summary)
            if error_summary is not None
            else None,
        }
        if qty_delta is not None:
            update_vals["qty_delta"] = qty_delta
        if notes is not None:
            update_vals["notes"] = _redact_sensitive_text(notes)

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.id == target_row.id)
            .values(**update_vals)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(
                client_order_id, "final_reconcile", raw_response
            )

        await self._db.commit()
        return await self._require_row(client_order_id)


__all__ = [
    "AlpacaPaperLedgerService",
    "ApprovalProvenance",
    "CANONICAL_LIFECYCLE_STATES",
    "EXECUTED_LIFECYCLE_STATES",
    "LIFECYCLE_ANOMALY",
    "LIFECYCLE_CLOSED",
    "LIFECYCLE_FILLED",
    "LIFECYCLE_FINAL_RECONCILED",
    "LIFECYCLE_PLANNED",
    "LIFECYCLE_POSITION_RECONCILED",
    "LIFECYCLE_PREVIEWED",
    "LIFECYCLE_SELL_VALIDATED",
    "LIFECYCLE_STALE_PREVIEW_CLEANUP_REQUIRED",
    "LIFECYCLE_SUBMITTED",
    "LIFECYCLE_VALIDATED",
    "LedgerNotFoundError",
    "RECORD_KIND_ANOMALY",
    "RECORD_KIND_EXECUTION",
    "RECORD_KIND_PLAN",
    "RECORD_KIND_PREVIEW",
    "RECORD_KIND_RECONCILE",
    "RECORD_KIND_VALIDATION_ATTEMPT",
    "_derive_lifecycle_state",
    "_redact_sensitive_keys",
    "_redact_sensitive_text",
    "from_approval_bridge",
]
