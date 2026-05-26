"""ROB-329 — ingest a ``validated_run_card.v1`` artifact as an InvestmentSnapshot.

Producer→consumer path: ``run_card.json`` → sanitize → immutable
``InvestmentSnapshot(snapshot_kind="validated_run_card")`` → cite by
``snapshot_uuid`` from a report item's ``evidence_snapshot`` (see
``app.schemas.validated_run_card.build_run_card_evidence``).

The persisted ``payload_json`` is the **sanitized** run card (non-finite
metrics → ``null``) so it is strict-JSON / Postgres-jsonb safe. ``source_kind``
is ``manual`` — this is an operator/CLI-driven ingest of a research artifact,
not a live feed; the gitignored ``results/`` path is intentionally never
recorded as a ``source_uri``.

Safety: writes go only through ``InvestmentSnapshotsRepository`` (append-only).
No broker/order/watch/order-intent mutation, no scheduler, no
``/invest/screener`` scoring change.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.models.investment_snapshots import InvestmentSnapshot
from app.schemas.investment_snapshots import (
    SnapshotAccountScope,
    SnapshotCreate,
    SnapshotMarket,
    SnapshotRequestedBy,
    SnapshotRunCreate,
)
from app.schemas.validated_run_card import (
    RunCardCitation,
    build_run_card_citation,
    sanitize_non_finite,
)
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

#: Run/bundle policy label for run-card ingests (echoes the artifact schema).
RUN_CARD_POLICY_VERSION = "validated_run_card.v1"


def _parse_generated_at(payload: dict[str, Any]) -> dt.datetime | None:
    raw = payload.get("generated_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


class RunCardSnapshotIngestor:
    """Ingest a run-card payload as an immutable ``validated_run_card`` snapshot."""

    def __init__(self, repository: InvestmentSnapshotsRepository) -> None:
        self._repo = repository

    async def ingest(
        self,
        *,
        run_card_payload: dict[str, Any],
        market: SnapshotMarket,
        account_scope: SnapshotAccountScope | None = None,
        as_of: dt.datetime | None = None,
        requested_by: SnapshotRequestedBy = "claude_code",
        refresh_reason: str | None = None,
    ) -> tuple[InvestmentSnapshot, RunCardCitation]:
        """Persist the sanitized run card and return ``(snapshot, citation)``.

        ``as_of`` defaults to the run card's ``generated_at`` (or now). The
        snapshot ``symbol`` is set only when the run card targets a single
        symbol, so single-symbol cards remain indexable. Idempotent on the
        canonical payload via the repository's dedup (re-ingest reuses the row).
        """
        citation = build_run_card_citation(run_card_payload)
        sanitized_payload = sanitize_non_finite(dict(run_card_payload))

        effective_as_of = (
            as_of or _parse_generated_at(run_card_payload) or dt.datetime.now(dt.UTC)
        )
        symbol = citation.symbols[0] if len(citation.symbols) == 1 else None

        run = await self._repo.insert_run(
            SnapshotRunCreate(
                purpose="manual_refresh",
                market=market,
                account_scope=account_scope,
                requested_by=requested_by,
                policy_version=RUN_CARD_POLICY_VERSION,
                refresh_reason=refresh_reason,
            )
        )
        snapshot = await self._repo.insert_snapshot(
            SnapshotCreate(
                run_uuid=run.run_uuid,
                snapshot_kind="validated_run_card",
                market=market,
                account_scope=account_scope,
                symbol=symbol,
                source_kind="manual",
                payload_json=sanitized_payload,
                as_of=effective_as_of,
                freshness_status="fresh",
            )
        )
        return snapshot, citation
