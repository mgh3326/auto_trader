"""Tests for common_stocks_only filter logic in the snapshot job (ROB-204)."""

from __future__ import annotations

import pytest

from app.jobs import invest_screener_snapshots as snapshot_job


@pytest.mark.asyncio
async def test_resolve_active_universe_kr_rejects_common_stocks_only() -> None:
    with pytest.raises(
        ValueError, match="common_stocks_only is only supported for market='us'"
    ):
        await snapshot_job.resolve_active_universe("kr", common_stocks_only=True)


@pytest.mark.asyncio
async def test_resolve_active_universe_us_raises_when_column_unpopulated(
    monkeypatch,
) -> None:
    """When is_common_stock column is unpopulated, common_stocks_only raises clearly."""
    from unittest.mock import AsyncMock

    async def _fake_not_populated(_session):
        return False

    monkeypatch.setattr(
        "app.jobs.invest_screener_snapshots._ensure_common_stock_flags_populated",
        AsyncMock(
            side_effect=ValueError(
                "US common-stock filter requested, but us_symbol_universe.is_common_stock "
                "has not been populated. Run scripts.sync_us_common_stock_flags first."
            )
        ),
    )

    with pytest.raises(ValueError, match="has not been populated"):
        await snapshot_job.resolve_active_universe("us", common_stocks_only=True)


@pytest.mark.asyncio
async def test_resolve_symbols_kr_rejects_common_stocks_only() -> None:
    with pytest.raises(
        ValueError, match="common_stocks_only is only supported for market='us'"
    ):
        await snapshot_job.resolve_symbols("kr", [], 20, common_stocks_only=True)


def test_snapshot_build_request_has_common_stocks_only_field() -> None:
    req = snapshot_job.SnapshotBuildRequest(market="us", common_stocks_only=True)
    assert req.common_stocks_only is True
    req_default = snapshot_job.SnapshotBuildRequest(market="us")
    assert req_default.common_stocks_only is False
