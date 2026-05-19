"""ROB-271 — adapter that turns existing /invest coverage + readiness state into
the product-facing Toss/Naver benchmark gap matrix.

Read-only. No broker/order/watch/scheduler side effects. Never imports broker or
order modules. Never writes to the DB.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable

from app.schemas.invest_benchmark_gap import (
    BenchmarkGapMatrixResponse,
    BenchmarkGapMatrixSummary,
    BenchmarkGapPriority,
    BenchmarkGapRow,
    CoverageProductStatus,
    NextSourcingCandidate,
)
from app.schemas.invest_coverage import (
    CoverageMarket,
    CoverageState,
    InvestCoverageResponse,
)

_COVERAGE_TO_PRODUCT: dict[CoverageState, CoverageProductStatus] = {
    "fresh": "covered",
    "stale": "stale",
    "partial": "partial",
    "missing": "missing",
    "unsupported": "unsupported",
    "error": "blocked_by_auth_or_policy",
    "provider_unwired": "candidate_unwired",
}


def coverage_state_to_product_status(state: CoverageState) -> CoverageProductStatus:
    """Map legacy CoverageState into the new product-facing status vocabulary.

    Raises ValueError for unknown values so callers fail loud rather than
    silently emit a default. Two product statuses have no legacy source and are
    only assignable explicitly by a row author:
        - benchmark_only
        - intentionally_excluded
    """
    if state not in _COVERAGE_TO_PRODUCT:
        raise ValueError(f"unknown coverage state: {state!r}")
    return _COVERAGE_TO_PRODUCT[state]


def build_mvp_benchmark_rows() -> list[BenchmarkGapRow]:
    """MVP hardcoded set: Toss 5 + Naver 5 + Internal/KIS 5 = 15 rows.

    Statuses on Toss/Naver rows default to product-facing values that do not need
    a live coverage lookup. Internal/KIS rows that mirror existing /invest
    surfaces default to ``covered``; they will be promoted/demoted by the
    coverage adapter (Task 4) when /invest/api/coverage data is available.
    """
    return [
        # ── Toss ───────────────────────────────────────────────────────
        BenchmarkGapRow(
            id="toss.screener",
            featureArea="screener",
            benchmarkProvider="toss",
            benchmarkSurface="screener.presets",
            benchmarkLabelKo="골라보기",
            sourceRole="benchmark_only",
            coverageStatus="partial",
            priority="P2",
            whyNeeded="screener parity로 종목 후보 폭 보장",
            nextAction="auto_trader screener presets를 Toss preset 분류와 정합되도록 매핑",
            autoTraderApi="/invest/api/screener/presets",
            autoTraderReadModel="screener_snapshots",
            dataKind="snapshot",
            relatedLinearIssue="ROB-192",
        ),
        BenchmarkGapRow(
            id="toss.stock_detail.chart",
            featureArea="stock-detail",
            benchmarkProvider="toss",
            benchmarkSurface="stock_detail.chart",
            benchmarkLabelKo="종목 차트",
            sourceRole="benchmark_only",
            coverageStatus="candidate_unwired",
            priority="P1",
            whyNeeded="종목 분석 전 가격/이평선 시각화 필수",
            nextAction="ohlcv read-model 후보 평가 + provider contract 검토",
            autoTraderApi="/invest/api/stock-detail/candles",
            autoTraderTable="ohlcv",
            dataKind="raw",
            gapReason="durable ohlcv read-model 미연결",
            newIssueCandidate=True,
        ),
        BenchmarkGapRow(
            id="toss.stock_detail.orderbook",
            featureArea="stock-detail",
            benchmarkProvider="toss",
            benchmarkSurface="stock_detail.orderbook",
            benchmarkLabelKo="호가/체결",
            sourceRole="benchmark_only",
            coverageStatus="candidate_unwired",
            priority="P1",
            whyNeeded="주문 결정 전 호가/스프레드 확인",
            nextAction="quotes/orderbook provider contract 검토",
            autoTraderTable="quotes",
            dataKind="raw",
            newIssueCandidate=True,
        ),
        BenchmarkGapRow(
            id="toss.account.holdings",
            featureArea="account",
            benchmarkProvider="toss",
            benchmarkSurface="account.holdings",
            benchmarkLabelKo="우측패널 보유",
            sourceRole="broker_authority",
            coverageStatus="covered",
            priority="P0",
            whyNeeded="액션 리포트의 사전 조건 — KIS live broker authority",
            nextAction="유지: KIS live 권위 보존",
            autoTraderApi="/invest/api/account-panel",
            autoTraderReadModel="holdings",
            dataKind="account",
        ),
        BenchmarkGapRow(
            id="toss.account.pending_orders",
            featureArea="account",
            benchmarkProvider="toss",
            benchmarkSurface="account.pending_orders",
            benchmarkLabelKo="미체결 주문",
            sourceRole="broker_authority",
            coverageStatus="covered",
            priority="P0",
            whyNeeded="액션 리포트가 주문 상태에 의존",
            nextAction="유지: pending_order reconciliation 신선도 모니터",
            autoTraderTable="pending_orders",
            dataKind="account",
        ),
        # ── Naver ──────────────────────────────────────────────────────
        BenchmarkGapRow(
            id="naver.market.kr",
            featureArea="market",
            benchmarkProvider="naver",
            benchmarkSurface="market.kr",
            benchmarkLabelKo="국내 시장",
            sourceRole="reference",
            coverageStatus="covered",
            priority="P2",
            whyNeeded="KR 시장 개요 parity",
            nextAction="market_dashboard read-model을 Naver 항목과 정합 점검",
            autoTraderApi="/invest/api/market-dashboard",
            dataKind="derived",
        ),
        BenchmarkGapRow(
            id="naver.market.major_indices",
            featureArea="market",
            benchmarkProvider="naver",
            benchmarkSurface="market.major_indices",
            benchmarkLabelKo="주요 지수",
            sourceRole="reference",
            coverageStatus="covered",
            priority="P2",
            whyNeeded="시장 컨텍스트 parity",
            nextAction="market parity card 확장 평가",
            autoTraderApi="/invest/api/market-parity",
            dataKind="derived",
        ),
        BenchmarkGapRow(
            id="naver.stock_detail.price",
            featureArea="stock-detail",
            benchmarkProvider="naver",
            benchmarkSurface="stock_detail.price",
            benchmarkLabelKo="종목 시세",
            sourceRole="candidate",
            coverageStatus="candidate_unwired",
            priority="P1",
            whyNeeded="시세 freshness 확인",
            nextAction="quote snapshot provider 평가",
            autoTraderTable="quotes",
            dataKind="raw",
            newIssueCandidate=True,
        ),
        BenchmarkGapRow(
            id="naver.stock_detail.finance_overview",
            featureArea="stock-detail",
            benchmarkProvider="naver",
            benchmarkSurface="stock_detail.finance_overview",
            benchmarkLabelKo="재무개요",
            sourceRole="candidate",
            coverageStatus="candidate_unwired",
            priority="P1",
            whyNeeded="밸류에이션/재무 컨텍스트",
            nextAction="valuation snapshot 후보 평가",
            autoTraderTable="valuation_fundamentals",
            dataKind="snapshot",
            newIssueCandidate=True,
        ),
        BenchmarkGapRow(
            id="naver.stock_detail.investment_info",
            featureArea="stock-detail",
            benchmarkProvider="naver",
            benchmarkSurface="stock_detail.investment_info",
            benchmarkLabelKo="투자정보/컨센서스",
            sourceRole="candidate",
            coverageStatus="partial",
            priority="P2",
            whyNeeded="research consensus 보조 신호",
            nextAction="research_consensus 확장 평가",
            autoTraderApi="/invest/api/stock-detail/research-consensus",
            dataKind="derived",
            relatedLinearIssue="ROB-201",
        ),
        # ── Internal/KIS (broker authority + product authority) ───────
        BenchmarkGapRow(
            id="internal.kis_live_holdings",
            featureArea="account",
            benchmarkProvider="kis",
            benchmarkSurface="kis_live_holdings",
            benchmarkLabelKo="KIS 실시간 보유",
            sourceRole="broker_authority",
            coverageStatus="covered",
            priority="P0",
            whyNeeded="액션 리포트의 broker authority",
            nextAction="유지",
            autoTraderApi="/invest/api/account-panel",
            dataKind="broker_authority",
        ),
        BenchmarkGapRow(
            id="internal.kis_live_cash_orderable",
            featureArea="account",
            benchmarkProvider="kis",
            benchmarkSurface="kis_live_cash_orderable",
            benchmarkLabelKo="KIS 주문가능현금",
            sourceRole="broker_authority",
            coverageStatus="covered",
            priority="P0",
            whyNeeded="매수 가능 여부 판정",
            nextAction="유지",
            dataKind="broker_authority",
        ),
        BenchmarkGapRow(
            id="internal.kis_live_open_orders",
            featureArea="account",
            benchmarkProvider="kis",
            benchmarkSurface="kis_live_open_orders",
            benchmarkLabelKo="KIS 미체결",
            sourceRole="broker_authority",
            coverageStatus="covered",
            priority="P0",
            whyNeeded="중복 주문 방지 / 정합성",
            nextAction="유지",
            dataKind="broker_authority",
        ),
        BenchmarkGapRow(
            id="internal.kis_live_sellable_quantity",
            featureArea="account",
            benchmarkProvider="kis",
            benchmarkSurface="kis_live_sellable_quantity",
            benchmarkLabelKo="KIS 매도가능수량",
            sourceRole="broker_authority",
            coverageStatus="covered",
            priority="P0",
            whyNeeded="매도 가능 여부 판정",
            nextAction="유지",
            dataKind="broker_authority",
        ),
        BenchmarkGapRow(
            id="internal.kr_action_readiness_summary",
            featureArea="action-readiness",
            benchmarkProvider="internal",
            benchmarkSurface="kr_action_readiness_summary",
            benchmarkLabelKo="KR 액션 리포트 준비도 요약",
            sourceRole="owned_read_model",
            coverageStatus="covered",
            priority="P1",
            whyNeeded="액션 리포트 차단/준비 보조 진단",
            nextAction="보조 섹션 위치 유지",
            autoTraderApi="/invest/api/kr/action-readiness",
            dataKind="derived",
            relatedLinearIssue="ROB-256",
        ),
    ]


def build_benchmark_gap_summary(
    rows: list[BenchmarkGapRow],
) -> BenchmarkGapMatrixSummary:
    return BenchmarkGapMatrixSummary(
        totalRows=len(rows),
        byStatus=dict(Counter(row.coverageStatus for row in rows)),
        byPriority=dict(Counter(row.priority for row in rows)),
        byProvider=dict(Counter(row.benchmarkProvider for row in rows)),
    )


_PRIORITY_ORDER: list[BenchmarkGapPriority] = ["P0", "P1", "P2", "P3"]

_SOURCE_POLICY: list[str] = [
    "KIS live = holdings/cash/open orders/sellable quantity broker authority",
    "auto_trader DB/read-models = /invest product authority",
    "Toss = benchmark/reference only — never sourceOfTruth",
    "Naver = candidate/reference unless promoted to owned read-model",
    "community/discussion = aggregate signal only — raw text cloning prohibited",
]


def _surface_state_index(
    coverage: InvestCoverageResponse | None,
) -> dict[str, CoverageState]:
    if coverage is None:
        return {}
    return {surface.surface: surface.state for surface in coverage.surfaces}


def _row_keys_for_coverage_lookup(row: BenchmarkGapRow) -> list[str]:
    """Which coverage surface names should overlay this row's status."""
    candidates: list[str] = []
    if row.autoTraderReadModel:
        candidates.append(row.autoTraderReadModel)
    if row.autoTraderTable:
        candidates.append(row.autoTraderTable)
    return candidates


def _overlay_status_from_coverage(
    row: BenchmarkGapRow, state_index: dict[str, CoverageState]
) -> BenchmarkGapRow:
    for key in _row_keys_for_coverage_lookup(row):
        legacy = state_index.get(key)
        if legacy is None:
            continue
        try:
            row = row.model_copy(
                update={"coverageStatus": coverage_state_to_product_status(legacy)}
            )
        except ValueError:
            continue
        break
    return row


def _build_next_candidates(
    rows: Iterable[BenchmarkGapRow],
) -> list[NextSourcingCandidate]:
    candidates: list[NextSourcingCandidate] = []
    for row in rows:
        if row.coverageStatus == "covered":
            continue
        if row.coverageStatus in {"intentionally_excluded", "unsupported"}:
            continue
        candidates.append(
            NextSourcingCandidate(
                rowId=row.id,
                priority=row.priority,
                featureArea=row.featureArea,
                benchmarkProvider=row.benchmarkProvider,
                gap=row.gapReason or row.whyNeeded,
                currentAutoTrader=row.autoTraderApi
                or row.autoTraderReadModel
                or row.autoTraderTable,
                whyItMatters=row.whyNeeded,
                currentStatus=row.coverageStatus,
                nextAction=row.nextAction,
                relatedLinearIssue=row.relatedLinearIssue,
                newIssueCandidate=row.newIssueCandidate,
            )
        )
    candidates.sort(key=lambda c: _PRIORITY_ORDER.index(c.priority))
    return candidates


def build_benchmark_gap_matrix_from_coverage(
    coverage: InvestCoverageResponse | None,
    *,
    market: CoverageMarket = "kr",
    as_of: dt.datetime | None = None,
) -> BenchmarkGapMatrixResponse:
    """Pure function used by both the router and tests.

    The router passes a freshly-built ``InvestCoverageResponse``; tests can pass
    a hand-built one. ``None`` keeps every row at its declared default.
    """
    when = as_of or dt.datetime.now(dt.UTC)
    state_index = _surface_state_index(coverage)
    rows = [
        _overlay_status_from_coverage(row, state_index)
        for row in build_mvp_benchmark_rows()
    ]
    return BenchmarkGapMatrixResponse(
        market=market,
        asOf=when,
        rows=rows,
        nextCandidates=_build_next_candidates(rows),
        summary=build_benchmark_gap_summary(rows),
        sourcePolicy=_SOURCE_POLICY,
        notes=[
            "first-screen view: 토스·네이버 대비 auto_trader 데이터 수급 현황",
            "Toss/Naver는 reference/candidate only — never sourceOfTruth",
            "downstream collector 구현은 본 이슈의 non-goal",
        ],
    )
