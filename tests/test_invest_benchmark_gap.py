from __future__ import annotations

import pytest

from app.schemas.invest_benchmark_gap import (
    BenchmarkGapMatrixResponse,
    BenchmarkGapMatrixSummary,
    BenchmarkGapRow,
    NextSourcingCandidate,
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
