from __future__ import annotations

import datetime as dt

import pytest

from app.schemas.invest_benchmark_gap import (
    BenchmarkGapMatrixResponse,
    BenchmarkGapMatrixSummary,
    BenchmarkGapRow,
    NextSourcingCandidate,
)
from app.schemas.invest_coverage import (
    InvestCoverageCounts,
    InvestCoverageResponse,
    InvestCoverageSurface,
)
from app.services.invest_benchmark_gap_service import (
    build_benchmark_gap_matrix_from_coverage,
    build_benchmark_gap_summary,
    build_mvp_benchmark_rows,
    coverage_state_to_product_status,
)


def test_benchmark_gap_row_minimum_required_fields():
    row = BenchmarkGapRow(
        id="toss.screener",
        featureArea="screener",
        benchmarkProvider="toss",
        benchmarkSurface="screener.presets",
        benchmarkLabelKo="골라보기",
        sourceRole="benchmark_only",
        coverageStatus="benchmark_only",
        priority="P2",
        whyNeeded="screener parity baseline",
        nextAction="map auto_trader screener presets to Toss presets",
    )
    assert row.benchmarkProvider == "toss"
    assert row.coverageStatus == "benchmark_only"
    assert row.relatedLinearIssue is None
    assert row.benchmarkUrl is None


def test_benchmark_gap_row_rejects_unexpected_fields():
    with pytest.raises(ValueError):
        BenchmarkGapRow(
            id="toss.x",
            featureArea="x",
            benchmarkProvider="toss",
            benchmarkSurface="x",
            benchmarkLabelKo="x",
            sourceRole="benchmark_only",
            coverageStatus="benchmark_only",
            priority="P2",
            whyNeeded="x",
            nextAction="x",
            unexpected="boom",  # type: ignore[call-arg]
        )


def test_benchmark_gap_matrix_response_minimum_shape():
    summary = BenchmarkGapMatrixSummary(
        totalRows=1,
        byStatus={"benchmark_only": 1},
        byPriority={"P2": 1},
        byProvider={"toss": 1},
    )
    candidate = NextSourcingCandidate(
        rowId="toss.screener",
        priority="P2",
        featureArea="screener",
        benchmarkProvider="toss",
        gap="missing toss-style presets",
        currentAutoTrader="invest_screener_snapshots presets",
        whyItMatters="parity baseline",
        currentStatus="partial",
        nextAction="enumerate Toss preset taxonomy",
    )
    resp = BenchmarkGapMatrixResponse(
        market="kr",
        asOf="2026-05-19T00:00:00Z",
        rows=[],
        nextCandidates=[candidate],
        summary=summary,
        sourcePolicy=["KIS live = broker authority"],
        notes=[],
    )
    assert resp.summary.totalRows == 1
    assert resp.nextCandidates[0].rowId == "toss.screener"


@pytest.mark.parametrize(
    "legacy,expected",
    [
        ("fresh", "covered"),
        ("stale", "stale"),
        ("partial", "partial"),
        ("missing", "missing"),
        ("unsupported", "unsupported"),
        ("error", "blocked_by_auth_or_policy"),
        ("provider_unwired", "candidate_unwired"),
    ],
)
def test_coverage_state_to_product_status_mapping(legacy, expected):
    assert coverage_state_to_product_status(legacy) == expected


def test_coverage_state_to_product_status_unknown_raises():
    with pytest.raises(ValueError):
        coverage_state_to_product_status("invalid_state")  # type: ignore[arg-type]


def test_build_mvp_benchmark_rows_returns_at_least_minimum_set():
    rows = build_mvp_benchmark_rows()
    ids = {row.id for row in rows}
    # MVP minimum: Toss 5, Naver 5, Internal 5
    assert {
        "toss.screener",
        "toss.stock_detail.chart",
        "toss.stock_detail.orderbook",
        "toss.account.holdings",
        "toss.account.pending_orders",
        "naver.market.kr",
        "naver.market.major_indices",
        "naver.stock_detail.price",
        "naver.stock_detail.finance_overview",
        "naver.stock_detail.investment_info",
        "internal.kis_live_holdings",
        "internal.kis_live_cash_orderable",
        "internal.kis_live_open_orders",
        "internal.kis_live_sellable_quantity",
        "internal.kr_action_readiness_summary",
    } <= ids
    # MVP non-goal: every row must use one of the documented sourceRoles and statuses
    providers = {row.benchmarkProvider for row in rows}
    assert providers <= {"toss", "naver", "internal", "kis"}


def test_build_benchmark_gap_summary_counts_correctly():
    rows = build_mvp_benchmark_rows()
    summary = build_benchmark_gap_summary(rows)
    assert summary.totalRows == len(rows)
    assert sum(summary.byProvider.values()) == len(rows)
    assert sum(summary.byPriority.values()) == len(rows)
    assert sum(summary.byStatus.values()) == len(rows)


def _surface(name: str, state: str) -> InvestCoverageSurface:
    return InvestCoverageSurface(
        surface=name,
        label=name,
        state=state,  # type: ignore[arg-type]
        sourceOfTruth=name,
        counts=InvestCoverageCounts(),
    )


def test_build_matrix_overlays_screener_state_from_coverage():
    coverage = InvestCoverageResponse(
        market="kr",
        asOf=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        tradingDate=dt.date(2026, 5, 19),
        states=["fresh", "stale", "missing"],
        surfaces=[
            _surface("invest_screener_snapshots", "stale"),
        ],
    )
    matrix = build_benchmark_gap_matrix_from_coverage(coverage, market="kr")
    by_id = {row.id: row for row in matrix.rows}
    assert by_id["toss.screener"].coverageStatus == "stale"
    # untouched row keeps declared default
    assert by_id["toss.stock_detail.chart"].coverageStatus == "candidate_unwired"
    # summary reflects updated row
    assert matrix.summary.totalRows == len(matrix.rows)
    assert matrix.summary.byStatus.get("stale", 0) >= 1


def test_build_matrix_emits_next_candidates_in_priority_order():
    coverage = InvestCoverageResponse(
        market="kr",
        asOf=dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        tradingDate=dt.date(2026, 5, 19),
        states=["fresh"],
        surfaces=[],
    )
    matrix = build_benchmark_gap_matrix_from_coverage(coverage, market="kr")
    priorities = [c.priority for c in matrix.nextCandidates]
    # candidates are sorted P0 < P1 < P2 < P3
    assert priorities == sorted(priorities, key=lambda p: ["P0", "P1", "P2", "P3"].index(p))
    # covered rows do not appear as next candidates
    assert all(c.currentStatus != "covered" for c in matrix.nextCandidates)
    # source policy is non-empty
    assert matrix.sourcePolicy


from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router


@pytest.fixture
def app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_benchmark_gap_endpoint_returns_shape(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/invest/api/coverage/benchmark-gap?market=kr")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["market"] == "kr"
    assert "rows" in payload and len(payload["rows"]) >= 15
    assert "nextCandidates" in payload
    assert "summary" in payload
    assert payload["summary"]["totalRows"] == len(payload["rows"])
    assert payload["sourcePolicy"]
