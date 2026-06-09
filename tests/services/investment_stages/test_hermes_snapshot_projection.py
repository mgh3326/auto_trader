# tests/services/investment_stages/test_hermes_snapshot_projection.py
"""ROB-470 — Hermes ingest projects portfolio/market snapshot columns from the
bundle so delta_get holdings/index deltas work for Hermes-created reports."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_stages.hermes_ingest import (
    HermesCompositionIngestService,
    _project_market_snapshot,
    _project_portfolio_snapshot,
)


def _pair(kind: str, payload: dict):
    return (object(), SimpleNamespace(snapshot_kind=kind, payload_json=payload))


# ---------------------------------------------------------------------------
# Pure projector helpers
# ---------------------------------------------------------------------------
def test_project_market_snapshot_wraps_indices_into_baseline():
    pairs = [
        _pair("news", {"articles": []}),
        _pair("market", {"indices": {"^GSPC": {"current": 5500.0, "name": "S&P"}}}),
    ]
    assert _project_market_snapshot(pairs) == {
        "baseline": {"indices": {"^GSPC": {"current": 5500.0, "name": "S&P"}}}
    }


def test_project_market_snapshot_empty_when_absent_or_no_indices():
    assert _project_market_snapshot([]) == {}
    assert _project_market_snapshot([_pair("market", {})]) == {}  # no indices key
    assert _project_market_snapshot([_pair("market", {"indices": {}})]) == {}  # empty


def test_project_portfolio_snapshot_lightweight_holdings():
    pairs = [
        _pair(
            "portfolio",
            {
                "holdings": [
                    {"ticker": "AAPL", "pnl_rate": 3.1, "qty": "drop-me"},
                    {"ticker": "MSFT", "pnl_rate": -2.0},
                    {"pnl_rate": 9.0},  # no ticker -> skipped
                ],
                "reference_holdings": [],  # not projected
            },
        ),
    ]
    assert _project_portfolio_snapshot(pairs) == {
        "holdings": [
            {"ticker": "AAPL", "pnl_rate": 3.1},
            {"ticker": "MSFT", "pnl_rate": -2.0},
        ]
    }


def test_project_portfolio_snapshot_empty_when_absent_or_no_holdings():
    assert _project_portfolio_snapshot([]) == {}
    assert _project_portfolio_snapshot([_pair("portfolio", {})]) == {}
    assert _project_portfolio_snapshot([_pair("portfolio", {"holdings": []})]) == {}


# ---------------------------------------------------------------------------
# Integration — ingest_composition populates the report columns from the bundle
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_composition_projects_snapshot_columns(
    session: AsyncSession,
) -> None:
    from datetime import UTC, datetime

    from app.schemas.hermes_composition import (
        HermesCompositionIngestRequest,
        HermesCompositionResult,
    )
    from app.schemas.investment_snapshots import (
        BundleCreate,
        BundleItemCreate,
        SnapshotCreate,
        SnapshotRunCreate,
    )
    from app.services.investment_reports.repository import InvestmentReportsRepository
    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    now = datetime.now(tz=UTC)
    snapshots_repo = InvestmentSnapshotsRepository(session)
    run = await snapshots_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            requested_by="claude_code",
            policy_version="intraday_action_report_v1",
        )
    )
    bundle = await snapshots_repo.insert_bundle(
        BundleCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )
    portfolio_snap = await snapshots_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="us",
            account_scope="kis_live",
            source_kind="kis_mcp",
            payload_json={
                "holdings": [{"ticker": "AAPL", "pnl_rate": 3.1}],
                "reference_holdings": [],
            },
            as_of=now,
            freshness_status="fresh",
        )
    )
    market_snap = await snapshots_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="market",
            market="us",
            account_scope=None,
            source_kind="auto_trader_mcp",
            payload_json={"indices": {"^GSPC": {"current": 5500.0, "name": "S&P 500"}}},
            as_of=now,
            freshness_status="fresh",
        )
    )
    for snap in (portfolio_snap, market_snap):
        await snapshots_repo.link_bundle_item(
            bundle_uuid=bundle.bundle_uuid,
            item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="optional"),
        )
    await session.commit()

    composition = HermesCompositionResult(
        snapshot_bundle_uuid=bundle.bundle_uuid,
        hermes_run_id="run-proj",
        title="Advisory",
        summary="synth",
        items=[],
        news_citations=[],
    )
    request = HermesCompositionIngestRequest(
        composition=composition,
        kst_date="2026-05-23",
        market="us",
        account_scope="kis_live",
        status="draft",
    )

    report = await HermesCompositionIngestService(session).ingest_composition(request)
    await session.commit()

    reports_repo = InvestmentReportsRepository(session)
    refreshed = await reports_repo.get_report_by_id(report.id)
    assert refreshed is not None
    # portfolio: lightweight holdings projection
    assert refreshed.portfolio_snapshot == {
        "holdings": [{"ticker": "AAPL", "pnl_rate": 3.1}]
    }
    # market: bundle top-level indices wrapped into baseline.indices for the loader
    assert refreshed.market_snapshot == {
        "baseline": {"indices": {"^GSPC": {"current": 5500.0, "name": "S&P 500"}}}
    }

    # End-to-end: delta_get now computes BOTH signals for the Hermes report —
    # index from the freshly-populated market_snapshot (the real gap), holdings
    # from the bundle's portfolio snapshot. Neither is baseline_snapshot_absent.
    from app.services.investment_reports.delta_service import DeltaService

    async def journal_fn(*, account_type, market):
        return {"entries": []}

    async def holdings_fn(*, market):
        return {"accounts": [{"positions": [{"symbol": "AAPL", "profit_rate": 5.0}]}]}

    async def index_fn():
        return {"indices": [{"symbol": "^GSPC", "current": 5533.0}]}

    delta = await DeltaService(
        session,
        journal_fn=journal_fn,
        holdings_fn=holdings_fn,
        market_index_fn=index_fn,
    ).compute_delta(report.report_uuid)
    assert "holdings" not in delta.get("unavailable", {})
    assert "index" not in delta.get("unavailable", {})
    assert delta["holdings_pnl_delta"]["entries"][0]["symbol"] == "AAPL"
    assert round(delta["index_delta"]["entries"][0]["change_pct"], 4) == 0.6
