from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.schemas.execution_ledger import (
    ExecutionLedgerCommitDisabledError,
    ExecutionLedgerUpsert,
)
from app.services.execution_ledger.reconciler import ExecutionLedgerReconciler


class FakeRepo:
    def __init__(self, status: str = "inserted") -> None:
        self.status = status
        self.upserts: list[ExecutionLedgerUpsert] = []
        self.runs = []

    async def classify_fill(self, fill: ExecutionLedgerUpsert) -> str:
        await asyncio.sleep(0)
        return self.status

    async def upsert_fill(self, fill: ExecutionLedgerUpsert) -> tuple[str, int]:
        await asyncio.sleep(0)
        self.upserts.append(fill)
        return self.status, 42

    def record_run(self, run) -> None:  # noqa: ANN001
        self.runs.append(run)


async def fake_fetcher(**_kwargs):  # noqa: ANN003
    await asyncio.sleep(0)
    return {
        "orders": [
            {
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "instrument_type": "crypto",
                "side": "buy",
                "price": 100,
                "quantity": 2,
                "total_amount": 200,
                "fee": 1,
                "currency": "KRW",
                "account": "upbit",
                "order_id": "ord-1",
                "filled_at": datetime(2026, 5, 13, tzinfo=UTC).isoformat(),
                "fill_seq": 0,
                "venue": "upbit_krw",
                "raw_payload_json": {"safe": True},
            }
        ]
    }


@pytest.mark.asyncio
async def test_reconciler_dry_run_classifies_without_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.execution_ledger.reconciler.settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )
    repo = FakeRepo(status="inserted")

    diff = await ExecutionLedgerReconciler(repo, fetcher=fake_fetcher).run(
        "upbit", dry_run=True
    )

    assert diff.would_insert == 1
    assert diff.committed_insert == 0
    assert repo.upserts == []
    assert len(repo.runs) == 1
    assert repo.runs[0].dry_run is True


@pytest.mark.asyncio
async def test_reconciler_commit_requires_disabled_by_default_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.execution_ledger.reconciler.settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )

    with pytest.raises(ExecutionLedgerCommitDisabledError):
        await ExecutionLedgerReconciler(FakeRepo(), fetcher=fake_fetcher).run(
            "upbit", dry_run=False
        )


@pytest.mark.asyncio
async def test_reconciler_commit_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.execution_ledger.reconciler.settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=True),
    )
    repo = FakeRepo(status="inserted")

    diff = await ExecutionLedgerReconciler(repo, fetcher=fake_fetcher).run(
        "upbit", dry_run=False
    )

    assert diff.committed_insert == 1
    assert len(repo.upserts) == 1
    assert repo.upserts[0].filled_qty == Decimal("2")


@pytest.mark.asyncio
async def test_reconciler_passes_explicit_window_to_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.execution_ledger.reconciler.settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )
    captured: dict[str, object] = {}

    async def fetcher(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"orders": []}

    start_at = datetime(2026, 2, 1, tzinfo=UTC)
    end_at = datetime(2026, 2, 8, tzinfo=UTC)
    repo = FakeRepo(status="inserted")

    await ExecutionLedgerReconciler(repo, fetcher=fetcher).run(
        "kis",
        start_at=start_at,
        end_at=end_at,
        max_pages=25,
        dry_run=True,
    )

    assert captured["start_at"] == start_at
    assert captured["end_at"] == end_at
    assert captured["max_pages"] == 25
    assert repo.runs[0].window_start == start_at
    assert repo.runs[0].window_end == end_at


@pytest.mark.asyncio
async def test_reconciler_rejects_fetch_errors_and_records_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.execution_ledger.reconciler.settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )

    async def fetcher(**_kwargs):  # noqa: ANN003
        await asyncio.sleep(0)
        return {
            "orders": [],
            "errors": [{"market": "crypto", "error": "truncated window"}],
        }

    repo = FakeRepo(status="inserted")

    with pytest.raises(RuntimeError, match="crypto.*truncated window"):
        await ExecutionLedgerReconciler(repo, fetcher=fetcher).run(
            "upbit", dry_run=True
        )

    assert len(repo.runs) == 1
    assert "crypto" in repo.runs[0].error_summary
    assert "truncated window" in repo.runs[0].error_summary
