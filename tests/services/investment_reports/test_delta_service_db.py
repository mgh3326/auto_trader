# tests/services/investment_reports/test_delta_service_db.py
"""ROB-376 — default baseline loader against a seeded report row (no bundle)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.delta_service import DeltaService
from app.services.investment_reports.repository import InvestmentReportsRepository


@pytest.mark.asyncio
async def test_default_loader_reads_report_market_and_marks_pnl_absent(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await repo.insert_report(
        idempotency_key="rob376:delta:1",
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="HERMES_ADVISOR",
        title="baseline",
        summary="s",
        status="published",
        report_metadata={},
        market_snapshot={"baseline": {"indices": {"^GSPC": {"current": 5500.0}}}},
        portfolio_snapshot={},
    )
    await session.commit()

    # Inject only the live fns so no network is hit; loader is the real default.
    async def journal_fn(*, account_type, market):
        return {"entries": []}

    async def holdings_fn(*, market):
        return {"accounts": []}

    async def index_fn():
        return {"indices": [{"symbol": "^GSPC", "current": 5533.0}]}

    svc = DeltaService(
        session=session,
        journal_fn=journal_fn,
        holdings_fn=holdings_fn,
        market_index_fn=index_fn,
    )
    out = await svc.compute_delta(report.report_uuid)
    assert out["success"] is True
    assert out["market"] == "us"
    # No snapshot_bundle_uuid on the seeded row -> per-symbol P/L baseline absent.
    assert out["holdings_pnl_delta"] is None
    assert out["unavailable"]["holdings"] == "baseline_snapshot_absent"
    # Index baseline IS present on the row -> index delta computed.
    assert round(out["index_delta"]["entries"][0]["change_pct"], 4) == 0.6


@pytest.mark.asyncio
async def test_default_loader_reads_portfolio_snapshot_json_when_no_bundle(
    session: AsyncSession,
) -> None:
    # ROB-456: with NO snapshot_bundle_uuid, the default loader falls back to the
    # create-time report.portfolio_snapshot JSON column for the P/L baseline.
    repo = InvestmentReportsRepository(session)
    report = await repo.insert_report(
        idempotency_key="rob456:delta:1",
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="HERMES_ADVISOR",
        title="baseline",
        summary="s",
        status="published",
        report_metadata={},
        market_snapshot={"baseline": {"indices": {"^GSPC": {"current": 5500.0}}}},
        portfolio_snapshot={"holdings": [{"ticker": "AAPL", "pnl_rate": 1.0}]},
    )
    await session.commit()

    async def journal_fn(*, account_type, market):
        return {"entries": []}

    async def holdings_fn(*, market):
        return {"accounts": [{"positions": [{"symbol": "AAPL", "profit_rate": 4.1}]}]}

    async def index_fn():
        return {"indices": [{"symbol": "^GSPC", "current": 5533.0}]}

    svc = DeltaService(
        session=session,
        journal_fn=journal_fn,
        holdings_fn=holdings_fn,
        market_index_fn=index_fn,
    )
    out = await svc.compute_delta(report.report_uuid)
    assert out["success"] is True
    assert "holdings" not in out.get("unavailable", {})
    entry = out["holdings_pnl_delta"]["entries"][0]
    assert entry["symbol"] == "AAPL"
    assert entry["baseline_pnl_pct"] == 1.0
    assert entry["live_pnl_pct"] == 4.1
    assert entry["delta_pp"] == 3.1


@pytest.mark.asyncio
async def test_default_loader_baseline_not_found(session: AsyncSession) -> None:
    svc = DeltaService(session=session)
    out = await svc.compute_delta("11111111-1111-1111-1111-111111111111")
    assert out == {"success": False, "error": "baseline_not_found"}
