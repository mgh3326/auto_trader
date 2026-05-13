"""Dry-run-first broker execution ledger reconciler (ROB-211)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.core.config import settings
from app.schemas.execution_ledger import (
    ExecutionLedgerCommitDisabledError,
    ExecutionLedgerRead,
    ExecutionLedgerUpsert,
    ReconcileDiff,
    ReconcileRunRecord,
)
from app.services.execution_ledger.normalizers import to_execution_ledger_upsert
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.n8n_filled_orders_service import fetch_filled_orders

Broker = Literal["kis", "upbit"]
FilledOrdersFetcher = Callable[..., Awaitable[dict[str, Any]]]


class ExecutionLedgerReconciler:
    def __init__(
        self,
        repo: ExecutionLedgerRepository,
        fetcher: FilledOrdersFetcher | None = None,
    ):
        self.repo = repo
        self.fetcher = fetcher or fetch_filled_orders

    async def run(
        self, broker: Broker, *, window_hours: int = 24, dry_run: bool = True
    ) -> ReconcileDiff:
        if not dry_run and not settings.EXECUTION_LEDGER_COMMIT_ENABLED:
            raise ExecutionLedgerCommitDisabledError(
                "EXECUTION_LEDGER_COMMIT_ENABLED is false; commit mode is disabled"
            )
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        window_start = now - timedelta(hours=window_hours)
        window_end = now
        diff = ReconcileDiff(source_run_id=run_id)
        error_summary: str | None = None
        try:
            fills = await self._fetch_normalized(
                broker, window_hours=window_hours, source_run_id=run_id
            )
            for fill in fills:
                status = await self.repo.classify_fill(fill)
                sample = ExecutionLedgerRead(
                    id=None, **fill.model_dump(exclude={"raw_payload_json"})
                )
                if status == "inserted":
                    diff.would_insert += 1
                    diff.add_insert_sample(sample)
                elif status == "updated":
                    diff.would_update += 1
                    diff.add_update_sample(sample)
                else:
                    diff.unchanged += 1
                if not dry_run and status != "unchanged":
                    committed_status, _row_id = await self.repo.upsert_fill(fill)
                    if committed_status == "inserted":
                        diff.committed_insert += 1
                    elif committed_status == "updated":
                        diff.committed_update += 1
        except Exception as exc:
            error_summary = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await self.repo.record_run(
                ReconcileRunRecord(
                    run_id=run_id,
                    broker=broker,
                    window_start=window_start,
                    window_end=window_end,
                    finished_at=datetime.now(UTC),
                    dry_run=dry_run,
                    would_insert=diff.would_insert,
                    would_update=diff.would_update,
                    unchanged=diff.unchanged,
                    committed_insert=diff.committed_insert,
                    committed_update=diff.committed_update,
                    error_summary=error_summary,
                    notes="commit disabled" if dry_run else None,
                )
            )
        return diff

    async def _fetch_normalized(
        self, broker: Broker, *, window_hours: int, source_run_id: uuid.UUID
    ) -> list[ExecutionLedgerUpsert]:
        days = max(1, int((window_hours + 23) / 24))
        markets = "crypto" if broker == "upbit" else "kr,us"
        result = await self.fetcher(
            days=days, markets=markets, min_amount=0, include_indicators=False
        )
        rows = result.get("orders") or result.get("items") or []
        upserts: list[ExecutionLedgerUpsert] = []
        for row in rows:
            upsert = to_execution_ledger_upsert(
                row, broker=broker, source_run_id=source_run_id
            )
            upserts.append(upsert)
        return upserts
