"""Job-level behaviour for analyst_consensus_snapshots (ROB-641).

Covers holdings ∪ watch symbol resolution (mocked resolvers), the
to_db_symbol override normalization, TaskIQ registration, and the
end-to-end upsert dedupe on duplicate payload keys.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from app.jobs import analyst_consensus_snapshots as snapshot_job
from app.jobs.analyst_consensus_snapshots import (
    AnalystConsensusSnapshotBuildRequest,
    resolve_symbols,
    run_analyst_consensus_snapshot_build,
)
from app.models.analyst_consensus_snapshot import AnalystConsensusSnapshot

_KST = ZoneInfo("Asia/Seoul")

_UNIQUE = 0


def _unique_symbol() -> str:
    global _UNIQUE
    _UNIQUE += 1
    return f"T641J{_UNIQUE:04d}"


def _patch_resolvers(
    monkeypatch,
    *,
    kis: list[dict[str, Any]],
    manual: list[Any],
    watch: list[str],
) -> None:
    async def fake_kis(market: str) -> list[Any]:
        return list(kis)

    async def fake_manual(market: str, user_id: int) -> list[Any]:
        return list(manual)

    async def fake_watch(market: str) -> list[str]:
        return list(watch)

    monkeypatch.setattr(snapshot_job, "_fetch_kis_holdings", fake_kis)
    monkeypatch.setattr(snapshot_job, "_fetch_manual_holdings", fake_manual)
    monkeypatch.setattr(snapshot_job, "_fetch_active_watch_symbols", fake_watch)


@pytest.mark.unit
def test_task_module_is_registered() -> None:
    """ROB-641: the TaskIQ module must be in the worker's explicit load list."""
    from app.tasks import TASKIQ_TASK_MODULES, analyst_consensus_snapshot_tasks

    assert analyst_consensus_snapshot_tasks in TASKIQ_TASK_MODULES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_symbols_override_normalized_with_to_db_symbol() -> None:
    symbols = await resolve_symbols("us", ["brk-b", " aapl ", "BRK/A", ""])
    assert symbols == ["BRK.B", "AAPL", "BRK.A"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_symbols_kr_holdings_union_watch(monkeypatch) -> None:
    _patch_resolvers(
        monkeypatch,
        kis=[{"pdno": "005930"}, {"pdno": "000660"}],
        manual=[SimpleNamespace(ticker="035420")],
        # "5930" normalizes to 005930 → duplicate collapses into the union.
        watch=["005380", "5930"],
    )
    symbols = await resolve_symbols("kr", [])
    assert symbols == ["000660", "005380", "005930", "035420"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_symbols_us_holdings_union_watch(monkeypatch) -> None:
    _patch_resolvers(
        monkeypatch,
        kis=[{"ovrs_pdno": "BRK/B"}],
        manual=[SimpleNamespace(ticker="aapl")],
        watch=["TSLA", "BRK-B"],
    )
    symbols = await resolve_symbols("us", [])
    assert symbols == ["AAPL", "BRK.B", "TSLA"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_symbols_limit_caps_resolved_set(monkeypatch) -> None:
    _patch_resolvers(
        monkeypatch,
        kis=[{"pdno": "005930"}, {"pdno": "000660"}],
        manual=[],
        watch=["035420"],
    )
    symbols = await resolve_symbols("kr", [], limit=2)
    assert symbols == ["000660", "005930"]


@pytest.mark.unit
def test_job_surface_has_no_full_universe_option() -> None:
    """The all_symbols full-universe scan was removed from the job surface."""
    import dataclasses

    field_names = {
        field.name for field in dataclasses.fields(AnalystConsensusSnapshotBuildRequest)
    }
    assert "all_symbols" not in field_names
    assert not hasattr(snapshot_job, "resolve_active_universe")


def _fake_build_with_fetcher(consensus: dict[str, Any]):
    """Wrap the real builder with a network-free fetcher."""
    from app.services.analyst_consensus_snapshots.builder import (
        build_consensus_snapshots,
    )

    async def fake_fetcher(market: str, symbol: str) -> dict[str, Any]:
        return {
            "source": "naver_finance",
            "consensus": consensus,
            "opinions": [],
            "opinions_limit": 30,
            "newest_opinion_date": None,
        }

    async def build(**kwargs: Any):
        return await build_consensus_snapshots(fetcher=fake_fetcher, **kwargs)

    return build


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_job_dedupes_duplicate_payload_keys_on_commit(
    db_session, monkeypatch
) -> None:
    """Duplicate symbol input → duplicatePayloadKeys surfaced, single DB row."""
    symbol = _unique_symbol()
    await db_session.execute(
        sa.delete(AnalystConsensusSnapshot).where(
            AnalystConsensusSnapshot.symbol == symbol
        )
    )
    await db_session.commit()

    consensus = {
        "buy_count": 4,
        "hold_count": 2,
        "sell_count": 1,
        "strong_buy_count": 1,
        "total_count": 7,
        "avg_target_price": 90000,
        "current_price": 80000,
    }
    monkeypatch.setattr(
        snapshot_job,
        "build_consensus_snapshots",
        _fake_build_with_fetcher(consensus),
    )

    result = await run_analyst_consensus_snapshot_build(
        AnalystConsensusSnapshotBuildRequest(
            market="kr",
            symbols=(symbol, symbol),
            commit=True,
            now=dt.datetime(2026, 7, 2, 0, 30, tzinfo=_KST),
        )
    )
    assert result.symbols_resolved == 2
    assert result.snapshots_built == 2
    assert result.idempotency["duplicatePayloadKeys"] == 1

    rows = (
        (
            await db_session.execute(
                sa.select(AnalystConsensusSnapshot).where(
                    AnalystConsensusSnapshot.symbol == symbol
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # KST-morning run books the KST calendar date.
    assert rows[0].snapshot_date == dt.date(2026, 7, 2)

    await db_session.execute(
        sa.delete(AnalystConsensusSnapshot).where(
            AnalystConsensusSnapshot.symbol == symbol
        )
    )
    await db_session.commit()
