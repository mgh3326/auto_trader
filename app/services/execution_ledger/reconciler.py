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


def _format_fetch_errors(errors: list[Any]) -> str:
    parts = []
    for error in errors:
        if isinstance(error, dict):
            market = error.get("market") or "unknown"
            message = error.get("error") or error
            parts.append(f"{market}: {message}")
        else:
            parts.append(str(error))
    return "; ".join(parts)


def _resolve_run_window(
    *,
    window_hours: int = 24,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    window_end = end_at or now
    window_start = start_at or (window_end - timedelta(hours=window_hours))
    if window_start >= window_end:
        raise ValueError("start_at must be before end_at")
    return window_start, window_end


class ExecutionLedgerReconciler:
    def __init__(
        self,
        repo: ExecutionLedgerRepository,
        fetcher: FilledOrdersFetcher | None = None,
    ):
        self.repo = repo
        self.fetcher = fetcher or fetch_filled_orders

    async def run(  # NOSONAR
        self,
        broker: Broker,
        *,
        window_hours: int = 24,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        max_pages: int = 100,
        dry_run: bool = True,
    ) -> ReconcileDiff:
        if not dry_run and not settings.EXECUTION_LEDGER_COMMIT_ENABLED:
            raise ExecutionLedgerCommitDisabledError(
                "EXECUTION_LEDGER_COMMIT_ENABLED is false; commit mode is disabled"
            )
        run_id = uuid.uuid4()
        window_start, window_end = _resolve_run_window(
            window_hours=window_hours,
            start_at=start_at,
            end_at=end_at,
        )
        diff = ReconcileDiff(source_run_id=run_id)
        error_summary: str | None = None
        try:
            fills = await self._fetch_normalized(
                broker,
                window_hours=window_hours,
                start_at=window_start,
                end_at=window_end,
                max_pages=max_pages,
                source_run_id=run_id,
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
            self.repo.record_run(
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
        self,
        broker: Broker,
        *,
        window_hours: int,
        start_at: datetime,
        end_at: datetime,
        max_pages: int,
        source_run_id: uuid.UUID,
    ) -> list[ExecutionLedgerUpsert]:
        days = max(1, int(((end_at - start_at).total_seconds() + 86399) / 86400))
        markets = "crypto" if broker == "upbit" else "kr,us"
        result = await self.fetcher(
            days=days,
            markets=markets,
            min_amount=0,
            include_indicators=False,
            start_at=start_at,
            end_at=end_at,
            max_pages=max_pages,
        )
        errors = result.get("errors") or []
        if errors:
            raise RuntimeError(
                "Filled-orders fetch returned errors "
                f"broker={broker} markets={markets} "
                f"start_at={start_at.isoformat()} end_at={end_at.isoformat()}: "
                f"{_format_fetch_errors(errors)}"
            )
        rows = result.get("orders") or result.get("items") or []
        upserts: list[ExecutionLedgerUpsert] = []
        for row in rows:
            upsert = to_execution_ledger_upsert(
                row, broker=broker, source_run_id=source_run_id
            )
            upserts.append(upsert)
        return upserts
