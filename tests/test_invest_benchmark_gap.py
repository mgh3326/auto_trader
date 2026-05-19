from __future__ import annotations

import pytest

from app.schemas.invest_benchmark_gap import (
    BenchmarkGapMatrixResponse,
    BenchmarkGapMatrixSummary,
    BenchmarkGapRow,
    NextSourcingCandidate,
)
from app.services.invest_benchmark_gap_service import (
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
