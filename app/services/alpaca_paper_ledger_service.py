"""Alpaca Paper execution state ledger service (ROB-84).

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

from sqlalchemy import select, update
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
# Lifecycle state derivation from broker order status
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
_UNEXPECTED_STATUSES = frozenset({"rejected", "expired", "suspended"})


def _derive_lifecycle_state(
    order_status: str | None,
    filled_qty: Decimal | float | None = None,
) -> str:
    if order_status is None:
        return "unexpected"
    status = order_status.lower()
    if status == "canceled":
        return "canceled"
    if status == "filled":
        return "filled"
    if status == "partially_filled":
        return "partially_filled"
    if status in _OPEN_STATUSES:
        # If there is nonzero filled qty with an open status, treat as unexpected
        if filled_qty is not None:
            try:
                qty = float(filled_qty)
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0:
                return "unexpected"
        return "open"
    if status in _UNEXPECTED_STATUSES:
        return "unexpected"
    return "unexpected"


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
        stmt = select(AlpacaPaperOrderLedger).where(
            AlpacaPaperOrderLedger.client_order_id == client_order_id
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
            .where(AlpacaPaperOrderLedger.client_order_id == client_order_id)
            .values(raw_responses=existing)
        )

    # ------------------------------------------------------------------
    # Lifecycle write methods
    # ------------------------------------------------------------------

    async def record_preview(
        self,
        *,
        client_order_id: str,
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
        lifecycle_state: str = "previewed",
        provenance: ApprovalProvenance | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Insert a previewed row; idempotent on duplicate client_order_id."""
        if not client_order_id or not client_order_id.strip():
            raise ValueError("client_order_id must not be empty")

        prov = provenance or ApprovalProvenance()
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
            "raw_responses": initial_raw if initial_raw else None,
        }

        stmt = (
            pg_insert(AlpacaPaperOrderLedger)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_alpaca_paper_ledger_client_order_id")
        )
        await self._db.execute(stmt)
        await self._db.commit()

        return await self._require_row(client_order_id)

    async def record_submit(
        self,
        client_order_id: str,
        order: dict[str, Any],
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Update with broker order id, status, and filled quantities after submit."""
        await self._require_row(client_order_id)

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

        update_vals: dict[str, Any] = {
            "lifecycle_state": lifecycle_state,
            "order_status": order_status,
            "broker_order_id": str(broker_order_id) if broker_order_id else None,
            "submitted_at": datetime.now(UTC),
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
        }
        if lifecycle_state == "unexpected":
            update_vals["error_summary"] = (
                f"unexpected order_status={order_status!r} after submit"
            )

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.client_order_id == client_order_id)
            .values(**update_vals)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(client_order_id, "submit", raw_response)

        await self._db.commit()
        return await self._require_row(client_order_id)

    async def record_status(
        self,
        client_order_id: str,
        order: dict[str, Any],
        raw_response: dict[str, Any] | None = None,
    ) -> AlpacaPaperOrderLedger:
        """Update lifecycle state from a status-check response."""
        await self._require_row(client_order_id)

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
        if lifecycle_state == "unexpected":
            update_vals["error_summary"] = (
                f"unexpected order_status={order_status!r} during status check"
            )

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.client_order_id == client_order_id)
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
        await self._require_row(client_order_id)

        update_vals: dict[str, Any] = {
            "cancel_status": cancel_status,
            "canceled_at": datetime.now(UTC),
        }
        if error_summary is not None:
            update_vals["error_summary"] = _redact_sensitive_text(error_summary)

        await self._db.execute(
            update(AlpacaPaperOrderLedger)
            .where(AlpacaPaperOrderLedger.client_order_id == client_order_id)
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
        """Record position snapshot.

        position=None means no position found (explicit zero).
        position=dict means record qty and avg_entry_price from broker response.
        """
        await self._require_row(client_order_id)

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
            .where(AlpacaPaperOrderLedger.client_order_id == client_order_id)
            .values(position_snapshot=snapshot)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(
                client_order_id, "position", raw_response
            )

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
        await self._require_row(client_order_id)

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
            .where(AlpacaPaperOrderLedger.client_order_id == client_order_id)
            .values(**update_vals)
        )

        if raw_response is not None:
            await self._accumulate_raw_response(
                client_order_id, "reconcile", raw_response
            )

        await self._db.commit()
        return await self._require_row(client_order_id)


__all__ = [
    "AlpacaPaperLedgerService",
    "ApprovalProvenance",
    "LedgerNotFoundError",
    "_derive_lifecycle_state",
    "_redact_sensitive_keys",
    "_redact_sensitive_text",
    "from_approval_bridge",
]
