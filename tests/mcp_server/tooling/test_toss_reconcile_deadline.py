"""hk 6288 — the between-rows deadline of the Toss reconcile loop (no DB)."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling import toss_live_ledger as mod

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _run(
    *, deadline: float | None, now: float = 0.0, row_seconds: float = 100.0
) -> tuple[dict, list[int]]:
    """Three open rows; each takes ``row_seconds`` on a fake clock."""
    clock = [now]
    processed: list[int] = []

    async def _one(row, **kwargs):
        clock[0] += row_seconds
        processed.append(row.id)
        return {"ledger_id": row.id, "verdict": "filled", "action": "booked"}

    rows = [
        SimpleNamespace(id=index, created_at=datetime(2026, 11, 25, tzinfo=UTC))
        for index in (1, 2, 3)
    ]
    count = MagicMock()
    count.scalar_one.return_value = len(rows)
    db = MagicMock()
    db.execute = AsyncMock(return_value=count)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=None)
    service = SimpleNamespace(
        reopen_anomalies_for_reconcile=AsyncMock(return_value={"rows": []}),
        list_open=AsyncMock(return_value=rows),
    )
    source = SimpleNamespace(
        aclose=AsyncMock(),
        window_from="2026-11-25",
        window_to="2026-11-25",
        closed_pages_capped=False,
        single_fetch_count=0,
    )
    patches = (
        patch.object(mod, "_monotonic", new=lambda: clock[0]),
        patch.object(mod, "_reconcile_one_toss_row", new=_one),
        patch.object(mod, "_order_session_factory", return_value=lambda: session),
        patch.object(mod, "TossLiveOrderLedgerService", return_value=service),
        patch.object(
            mod,
            "_repair_terminal_toss_proposal_projections",
            new=AsyncMock(return_value={}),
        ),
        patch.object(mod, "run_resting_rung_sweep", new=AsyncMock(return_value={})),
        patch.object(
            mod.TossReadClient,
            "from_settings",
            return_value=SimpleNamespace(aclose=AsyncMock()),
        ),
        patch.object(
            mod.TossBatchEvidenceSource, "build", new=AsyncMock(return_value=source)
        ),
    )
    kwargs = {} if deadline is None else {"deadline_monotonic": deadline}
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        result = await mod.toss_reconcile_orders_impl(dry_run=False, **kwargs)
    source.aclose.assert_awaited_once()
    return result, processed


async def test_deadline_passed_after_the_first_row_stops_before_the_next():
    result, processed = await _run(deadline=50.0)

    # The first row ran past the deadline and was still finished and reported.
    assert processed == [1]
    assert [item["ledger_id"] for item in result["reconciled"]] == [1]
    assert result["counts"] == {"filled": 1}
    assert result["deadline"] == {"reached": True, "rows_left": 2}
    assert result["success"] is True


async def test_no_row_starts_exactly_at_the_deadline():
    result, processed = await _run(deadline=200.0)

    assert processed == [1, 2]
    assert result["deadline"] == {"reached": True, "rows_left": 1}


async def test_deadline_already_passed_starts_no_row():
    result, processed = await _run(deadline=10.0, now=11.0)

    assert processed == []
    assert result["reconciled"] == []
    assert result["deadline"] == {"reached": True, "rows_left": 3}


async def test_deadline_passing_during_the_last_row_leaves_nothing():
    result, processed = await _run(deadline=250.0)

    assert processed == [1, 2, 3]
    assert "deadline" not in result


async def test_without_a_deadline_every_row_runs():
    result, processed = await _run(deadline=None, row_seconds=1_000_000.0)

    assert processed == [1, 2, 3]
    assert result["counts"] == {"filled": 3}
    assert "deadline" not in result
