"""Wire contract for the fillwire P0 execution-ledger HTTP ingest (spec §3 P0).

Two token-authed endpoints share this module:

* ``POST /trading/api/execution-ledger/fills/ingest`` — batch fill upsert.
  Item-level validation failures are reported per item (``rejected``) instead
  of collapsing the whole batch into one ``422``; only *envelope* problems
  (unknown ``source``, empty/oversized batch, malformed JSON body) are
  ordinary FastAPI validation errors. ``fills`` is therefore typed as raw
  objects here and validated one-by-one against
  :class:`app.schemas.execution_ledger.ExecutionLedgerUpsert` in the handler.
* ``POST /trading/api/execution-ledger/reconcile/trigger`` — reconnect
  backfill trigger, dry-run by default.

The envelope ``source`` is *transport provenance* (who posted), deliberately
distinct from the per-row ``ExecutionLedgerUpsert.source`` the DB CHECK
constrains (``reconciler``/``websocket``/``manual_import``). A producer name
is never written into the ledger row.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Hard batch bound for one ingest request (spec §3 P0: "배치 ≤200").
MAX_INGEST_BATCH = 200

IngestSource = Literal["fillwire", "websocket_monitor"]
IngestItemStatus = Literal["inserted", "updated", "unchanged", "rejected"]
ReconcileMarket = Literal["kr", "us", "crypto"]
ReconcileReason = Literal["reconnect"]
ReconcileTriggerStatus = Literal["executed", "deduped", "failed"]

#: The ledger ``source`` every externally ingested fill is stored under. A
#: producer cannot claim ``reconciler``/``manual_import`` provenance for a row
#: it pushed over the wire — this transport *is* the websocket tap.
INGESTED_ROW_SOURCE = "websocket"

#: Upper bound for any operator-facing free text we echo back. Keeps a broker
#: or driver error from turning into an unbounded response body.
MAX_REASON_CHARS = 300


class ExecutionLedgerFillIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fills: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_INGEST_BATCH)
    source: IngestSource
    source_run_id: uuid.UUID | None = None


class ExecutionLedgerFillIngestItemResult(BaseModel):
    """Per-item outcome, positionally aligned with the request ``fills`` list."""

    model_config = ConfigDict(extra="forbid")

    status: IngestItemStatus
    row_id: int | None = None
    reason: str | None = None


class ExecutionLedgerFillIngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: IngestSource
    source_run_id: uuid.UUID | None = None
    received: int
    accepted: int
    rejected: int
    results: list[ExecutionLedgerFillIngestItemResult]


class ExecutionLedgerReconcileTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: ReconcileMarket
    dry_run: bool = True
    #: Exact literal. This endpoint exists for the reconnect backfill and
    #: nothing else; an arbitrary reason string is a 422, not a free-text slot.
    reason: ReconcileReason = "reconnect"


class ExecutionLedgerReconcileTriggerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: ReconcileMarket
    dry_run: bool
    reason: ReconcileReason
    status: ReconcileTriggerStatus
    deduped: bool
    #: Fills actually booked by this run. Always ``0`` for ``dry_run=True`` —
    #: a dry run commits nothing and must never be reported as backfilled.
    backfilled: int
    dedupe_window_seconds: float
    #: Bounded projection of the reconcile kernel result (counts + truncated
    #: message), never the raw unbounded ``reconciled`` list.
    kernel: dict[str, Any] | None = None
    error: str | None = None
