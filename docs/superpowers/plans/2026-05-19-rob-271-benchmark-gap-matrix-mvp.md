# ROB-271 — /invest/coverage 토스·네이버 데이터 수급 갭 매트릭스 MVP

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/invest/coverage` 첫 화면을 "action-readiness-first"에서 "토스·네이버 대비 데이터 수급 갭" 중심으로 재구성하는 MVP를 추가한다. 새 product-facing status vocabulary 와 benchmark gap row schema 를 도입하고, MVP 분량의 Toss/Naver/Internal surface 만 매핑한다. 기존 `/invest/api/coverage` 및 `/invest/api/kr/action-readiness` 응답 스키마는 깨지 않는다.

**Architecture:** 새 read-only 엔드포인트 `/invest/api/coverage/benchmark-gap` 을 추가하고, 백엔드에서 기존 `build_invest_coverage` 결과(+ `build_kr_action_readiness`)를 입력으로 받아 product-facing benchmark gap row 로 매핑하는 얇은 어댑터 서비스를 둔다. 프론트엔드는 새 API 결과로 첫 화면 IA(summary → next candidates → Toss → Naver → internal/KIS → action-readiness 보조 → 디버그 테이블 collapsed)를 재배치한다. 기존 surface table 과 ActionReadinessCard 는 detail/debug section 으로 보존된다.

**Tech Stack:** Python 3.13 / FastAPI / Pydantic v2 (`extra="forbid"`) / pytest-asyncio (백엔드) · React + Vite + Vitest + Testing Library (프론트). 기존 `superpowers:test-driven-development` 패턴을 따른다.

---

## File Structure

### Created
- `app/schemas/invest_benchmark_gap.py` — new product-facing types (`BenchmarkProvider`, `SourceRole`, `CoverageProductStatus`, `BenchmarkGapPriority`, `BenchmarkGapRow`, `NextSourcingCandidate`, `BenchmarkGapMatrixSummary`, `BenchmarkGapMatrixResponse`).
- `app/services/invest_benchmark_gap_service.py` — `build_benchmark_gap_matrix(db, *, market, as_of)` 어댑터. legacy → product-facing status 매핑 헬퍼 + MVP 행 정의 + summary 계산.
- `tests/test_invest_benchmark_gap.py` — pydantic 검증 + 매핑 + MVP 행 + 우선순위/카테고리별 카운트 + router 컨트랙트 테스트.
- `frontend/invest/src/types/benchmarkGap.ts` — TS mirror.
- `frontend/invest/src/api/benchmarkGap.ts` — fetch 클라이언트.
- `frontend/invest/src/components/coverage/BenchmarkGapSection.tsx` — summary + next candidates + provider section 렌더링.
- `frontend/invest/src/__tests__/DesktopCoveragePage.benchmarkGap.test.tsx` — UI smoke.

### Modified
- `app/routers/invest_api.py` — 새 `GET /coverage/benchmark-gap` 엔드포인트 추가. 기존 `/coverage`, `/kr/action-readiness` 응답은 변경하지 않는다.
- `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx` — IA 재배치. 기존 surface table → `<details>` collapsed. 기존 `ActionReadinessCard` → secondary 위치(첫 화면 아래쪽). 신규 컴포넌트 호출 추가.
- `frontend/invest/src/__tests__/DesktopCoveragePage.test.tsx` — 기존 테스트는 깨지 않도록 새 API 도 모킹. 기존 assertion 유지.
- `docs/invest-coverage-dashboard.md` — 새 endpoint, product-facing status vocabulary, 새 IA 순서, source authority 재명시, action-readiness 보조 위치, non-goals 추가.

### Untouched (반드시)
- `app/schemas/invest_coverage.py`, `app/schemas/invest_action_readiness.py` 의 Literal/필드는 변경/삭제 금지. (추가는 금지 — 새 vocabulary 는 별도 모듈에 둔다.)
- `app/services/invest_coverage_service.py`, `app/services/invest_view_model/action_readiness_service.py` 의 시그니처/리턴 타입 변경 금지.
- 모든 broker/order/watch/scheduler 모듈.

---

## Status Vocabulary Mapping (불변 계약)

새 product-facing status 는 별도 enum `CoverageProductStatus` 로 정의하고, 기존 `CoverageState`/`ActionReadinessState` 는 그대로 둔다. 매핑은 어댑터 서비스의 순수 함수에서만 수행한다.

| Source (legacy) | Source value | → Product-facing status |
|---|---|---|
| `CoverageState` | `fresh` | `covered` |
| `CoverageState` | `stale` | `stale` |
| `CoverageState` | `partial` | `partial` |
| `CoverageState` | `missing` | `missing` |
| `CoverageState` | `unsupported` | `unsupported` |
| `CoverageState` | `error` | `blocked_by_auth_or_policy` |
| `CoverageState` | `provider_unwired` | `candidate_unwired` |
| (no source — benchmark-only declared row) | n/a | `benchmark_only` |
| (no source — declared excluded row) | n/a | `intentionally_excluded` |

`ActionReadinessState` 는 MVP 에서 직접 매핑하지 않는다(action-readiness 는 첫 화면 보조 카드로만 노출). 추후 확장 시 별도 매핑 함수를 추가한다.

---

## MVP Benchmark Rows (Toss 5 / Naver 5 / Internal 5)

ID 는 `kebab-case` 로, 모든 행은 hardcoded list 로 시작한다. 추후 service-derived 로 확장 가능한 seam 만 남긴다.

### Toss
1. `toss.screener` — 골라보기 / ranking presets (`internal.invest_screener_snapshots` 와 대조)
2. `toss.stock_detail.chart` — 종목 차트 (`internal.ohlcv` provider_unwired 대조)
3. `toss.stock_detail.orderbook` — 호가/체결 (`internal.quotes` provider_unwired 대조)
4. `toss.account.holdings` — 우측패널 보유 (`internal.kis_live_holdings` broker_authority)
5. `toss.account.pending_orders` — 미체결/체결 내역 (`internal.kis_live_open_orders` broker_authority)

### Naver
1. `naver.market.kr` — KR 시장 페이지 (`internal.kr_market_dashboard`)
2. `naver.market.major_indices` — 주요지수 (`internal.invest_market_parity`)
3. `naver.stock_detail.price` — 종목 시세 (`internal.quotes` provider_unwired 대조)
4. `naver.stock_detail.finance_overview` — 재무개요 (`internal.valuation_fundamentals` provider_unwired 대조)
5. `naver.stock_detail.investment_info` — 투자정보/컨센서스 (`internal.research_consensus` 후보)

### Internal/KIS (auto_trader 자체 surface — broker authority + product authority)
1. `internal.kis_live_holdings` — broker authority
2. `internal.kis_live_cash_orderable` — broker authority
3. `internal.kis_live_open_orders` — broker authority
4. `internal.kis_live_sellable_quantity` — broker authority
5. `internal.kr_action_readiness_summary` — 기존 ROB-256 readiness 의 요약 포인터(보조)

---

## Task Decomposition

각 Task 는 self-contained 하며 끝나면 commit 한다. 모든 Task 는 TDD(failing test → minimal impl → green → commit).

---

### Task 1: Backend schema — `invest_benchmark_gap.py`

**Files:**
- Create: `app/schemas/invest_benchmark_gap.py`
- Test: `tests/test_invest_benchmark_gap.py`

- [ ] **Step 1: Write the failing schema validation test**

```python
# tests/test_invest_benchmark_gap.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_benchmark_gap.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.invest_benchmark_gap'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/schemas/invest_benchmark_gap.py
"""ROB-271 — read-only Toss/Naver benchmark data-sourcing gap matrix schemas.

Product-facing types live here so the existing CoverageState/ActionReadinessState
contracts remain untouched. The gap matrix is an additive read-only view layered
on top of /invest/api/coverage and /invest/api/kr/action-readiness.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BenchmarkProvider = Literal[
    "toss",
    "naver",
    "internal",
    "kis",
    "upbit",
    "news_ingestor",
]

SourceRole = Literal[
    "source_of_truth",
    "broker_authority",
    "owned_read_model",
    "reference",
    "candidate",
    "benchmark_only",
    "excluded",
    "unsupported",
]

CoverageProductStatus = Literal[
    "covered",
    "partial",
    "stale",
    "missing",
    "candidate_unwired",
    "benchmark_only",
    "intentionally_excluded",
    "unsupported",
    "blocked_by_auth_or_policy",
]

BenchmarkGapPriority = Literal["P0", "P1", "P2", "P3"]

BenchmarkGapDataKind = Literal[
    "raw",
    "snapshot",
    "derived",
    "ui_only",
    "account",
    "broker_authority",
    "reference",
]


class BenchmarkGapRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required (MVP)
    id: str
    featureArea: str
    benchmarkProvider: BenchmarkProvider
    benchmarkSurface: str
    benchmarkLabelKo: str
    sourceRole: SourceRole
    coverageStatus: CoverageProductStatus
    priority: BenchmarkGapPriority
    whyNeeded: str
    nextAction: str

    # Optional (expansion seams)
    benchmarkUrl: str | None = None
    autoTraderSurface: str | None = None
    autoTraderApi: str | None = None
    autoTraderReadModel: str | None = None
    autoTraderTable: str | None = None
    dataKind: BenchmarkGapDataKind | None = None
    freshnessAt: datetime | None = None
    gapReason: str | None = None
    relatedLinearIssue: str | None = None
    newIssueCandidate: bool = False
    notes: list[str] = Field(default_factory=list)


class NextSourcingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rowId: str
    priority: BenchmarkGapPriority
    featureArea: str
    benchmarkProvider: BenchmarkProvider
    gap: str
    currentAutoTrader: str | None = None
    whyItMatters: str
    currentStatus: CoverageProductStatus
    nextAction: str
    relatedLinearIssue: str | None = None
    newIssueCandidate: bool = False


class BenchmarkGapMatrixSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totalRows: int
    byStatus: dict[str, int] = Field(default_factory=dict)
    byPriority: dict[str, int] = Field(default_factory=dict)
    byProvider: dict[str, int] = Field(default_factory=dict)


class BenchmarkGapMatrixResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["kr", "us", "crypto", "all"]
    asOf: datetime
    rows: list[BenchmarkGapRow]
    nextCandidates: list[NextSourcingCandidate]
    summary: BenchmarkGapMatrixSummary
    sourcePolicy: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_benchmark_gap.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/invest_benchmark_gap.py tests/test_invest_benchmark_gap.py
git commit -m "feat(rob-271): add benchmark gap matrix schema (MVP)"
```

---

### Task 2: Status mapping helper (pure function)

**Files:**
- Create: `app/services/invest_benchmark_gap_service.py`
- Modify: `tests/test_invest_benchmark_gap.py` (append cases)

- [ ] **Step 1: Append failing mapping tests**

Append to `tests/test_invest_benchmark_gap.py`:

```python
from app.services.invest_benchmark_gap_service import (
    coverage_state_to_product_status,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_benchmark_gap.py::test_coverage_state_to_product_status_mapping -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.invest_benchmark_gap_service'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/invest_benchmark_gap_service.py
"""ROB-271 — adapter that turns existing /invest coverage + readiness state into
the product-facing Toss/Naver benchmark gap matrix.

Read-only. No broker/order/watch/scheduler side effects. Never imports broker or
order modules. Never writes to the DB.
"""

from __future__ import annotations

from app.schemas.invest_benchmark_gap import CoverageProductStatus
from app.schemas.invest_coverage import CoverageState

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_benchmark_gap.py -v`
Expected: all passed (previous 3 + 8 new = 11).

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_benchmark_gap_service.py tests/test_invest_benchmark_gap.py
git commit -m "feat(rob-271): add coverage→product status mapping helper"
```

---

### Task 3: MVP rows + summary builder (no DB)

**Files:**
- Modify: `app/services/invest_benchmark_gap_service.py`
- Modify: `tests/test_invest_benchmark_gap.py`

- [ ] **Step 1: Append failing test for MVP rows + summary aggregation**

Append to `tests/test_invest_benchmark_gap.py`:

```python
from app.services.invest_benchmark_gap_service import (
    build_mvp_benchmark_rows,
    build_benchmark_gap_summary,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_benchmark_gap.py::test_build_mvp_benchmark_rows_returns_at_least_minimum_set -v`
Expected: FAIL with `ImportError: cannot import name 'build_mvp_benchmark_rows'`

- [ ] **Step 3: Extend the service**

Append to `app/services/invest_benchmark_gap_service.py`:

```python
from collections import Counter

from app.schemas.invest_benchmark_gap import (
    BenchmarkGapMatrixSummary,
    BenchmarkGapRow,
)


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
            autoTraderReadModel="invest_screener_snapshots",
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
            autoTraderTable="(ohlcv: provider_unwired)",
            dataKind="raw",
            gapReason="durable ohlcv read-model 미연결",
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
            autoTraderTable="(quotes: provider_unwired)",
            dataKind="raw",
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
            autoTraderReadModel="kis_live_holdings",
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
            autoTraderTable="(quotes: provider_unwired)",
            dataKind="raw",
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
            autoTraderTable="(valuation_fundamentals: provider_unwired)",
            dataKind="snapshot",
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_invest_benchmark_gap.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_benchmark_gap_service.py tests/test_invest_benchmark_gap.py
git commit -m "feat(rob-271): add MVP benchmark rows and summary"
```

---

### Task 4: Top-level builder that overlays live coverage state

**Files:**
- Modify: `app/services/invest_benchmark_gap_service.py`
- Modify: `tests/test_invest_benchmark_gap.py`

The builder accepts an optional `InvestCoverageResponse`; if provided, it remaps any MVP row whose `autoTraderReadModel` or `autoTraderTable` references a known coverage `surface` value to that surface's current state. Rows without a matching surface keep their declared default.

- [ ] **Step 1: Append failing builder test**

Append to `tests/test_invest_benchmark_gap.py`:

```python
import datetime as dt

from app.schemas.invest_coverage import (
    InvestCoverageCounts,
    InvestCoverageResponse,
    InvestCoverageSurface,
)
from app.services.invest_benchmark_gap_service import (
    build_benchmark_gap_matrix_from_coverage,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_benchmark_gap.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_benchmark_gap_matrix_from_coverage'`

- [ ] **Step 3: Implement builder + next-candidate derivation**

Append to `app/services/invest_benchmark_gap_service.py`:

```python
import datetime as dt
from typing import Iterable

from app.schemas.invest_benchmark_gap import (
    BenchmarkGapMatrixResponse,
    BenchmarkGapPriority,
    BenchmarkGapRow,
    NextSourcingCandidate,
)
from app.schemas.invest_coverage import (
    CoverageMarket,
    InvestCoverageResponse,
    InvestCoverageSurface,
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
) -> dict[str, str]:
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
    # ID convention: trailing segment after final dot also matches some
    # coverage surfaces (e.g., toss.screener -> invest_screener_snapshots is
    # already declared; this branch is intentionally minimal).
    return candidates


def _overlay_status_from_coverage(
    row: BenchmarkGapRow, state_index: dict[str, str]
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_invest_benchmark_gap.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_benchmark_gap_service.py tests/test_invest_benchmark_gap.py
git commit -m "feat(rob-271): overlay live coverage state on benchmark gap rows"
```

---

### Task 5: Router endpoint `/coverage/benchmark-gap`

**Files:**
- Modify: `app/routers/invest_api.py`
- Modify: `tests/test_invest_benchmark_gap.py`

- [ ] **Step 1: Append failing router test**

Append to `tests/test_invest_benchmark_gap.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_invest_benchmark_gap.py::test_benchmark_gap_endpoint_returns_shape -v`
Expected: FAIL with 404 or similar.

- [ ] **Step 3: Add the endpoint**

In `app/routers/invest_api.py`, add the import block (next to existing coverage import):

```python
from app.schemas.invest_benchmark_gap import BenchmarkGapMatrixResponse
from app.services.invest_benchmark_gap_service import (
    build_benchmark_gap_matrix_from_coverage,
)
```

And add the route right after the existing `/coverage` endpoint (around line 301):

```python
@router.get("/coverage/benchmark-gap")
async def get_invest_coverage_benchmark_gap(
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: CoverageMarket = Query("kr"),
    as_of: Annotated[date | None, Query(alias="asOf")] = None,
) -> BenchmarkGapMatrixResponse:
    """ROB-271 — read-only Toss/Naver benchmark data-sourcing gap matrix.

    Layered adapter over /invest/api/coverage. No broker/order/watch/scheduler
    side effects. Does not mutate or backfill anything.
    """
    _ = user
    try:
        coverage = await build_invest_coverage(db, market=market, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_benchmark_gap_matrix_from_coverage(coverage, market=market)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_invest_benchmark_gap.py -v`
Expected: all passed.

- [ ] **Step 5: Confirm no regression on the existing coverage endpoint**

Run: `uv run pytest tests/test_invest_coverage.py tests/test_invest_action_readiness.py -v`
Expected: all passed (no schema additions to the existing modules).

- [ ] **Step 6: Commit**

```bash
git add app/routers/invest_api.py tests/test_invest_benchmark_gap.py
git commit -m "feat(rob-271): expose GET /invest/api/coverage/benchmark-gap"
```

---

### Task 6: Frontend types

**Files:**
- Create: `frontend/invest/src/types/benchmarkGap.ts`

- [ ] **Step 1: Write the types directly (no test needed; structural mirror of `app/schemas/invest_benchmark_gap.py`)**

```typescript
// frontend/invest/src/types/benchmarkGap.ts
export type BenchmarkProvider =
  | "toss"
  | "naver"
  | "internal"
  | "kis"
  | "upbit"
  | "news_ingestor";

export type SourceRole =
  | "source_of_truth"
  | "broker_authority"
  | "owned_read_model"
  | "reference"
  | "candidate"
  | "benchmark_only"
  | "excluded"
  | "unsupported";

export type CoverageProductStatus =
  | "covered"
  | "partial"
  | "stale"
  | "missing"
  | "candidate_unwired"
  | "benchmark_only"
  | "intentionally_excluded"
  | "unsupported"
  | "blocked_by_auth_or_policy";

export type BenchmarkGapPriority = "P0" | "P1" | "P2" | "P3";

export type BenchmarkGapDataKind =
  | "raw"
  | "snapshot"
  | "derived"
  | "ui_only"
  | "account"
  | "broker_authority"
  | "reference";

export interface BenchmarkGapRow {
  id: string;
  featureArea: string;
  benchmarkProvider: BenchmarkProvider;
  benchmarkSurface: string;
  benchmarkLabelKo: string;
  sourceRole: SourceRole;
  coverageStatus: CoverageProductStatus;
  priority: BenchmarkGapPriority;
  whyNeeded: string;
  nextAction: string;
  benchmarkUrl?: string | null;
  autoTraderSurface?: string | null;
  autoTraderApi?: string | null;
  autoTraderReadModel?: string | null;
  autoTraderTable?: string | null;
  dataKind?: BenchmarkGapDataKind | null;
  freshnessAt?: string | null;
  gapReason?: string | null;
  relatedLinearIssue?: string | null;
  newIssueCandidate: boolean;
  notes: string[];
}

export interface NextSourcingCandidate {
  rowId: string;
  priority: BenchmarkGapPriority;
  featureArea: string;
  benchmarkProvider: BenchmarkProvider;
  gap: string;
  currentAutoTrader?: string | null;
  whyItMatters: string;
  currentStatus: CoverageProductStatus;
  nextAction: string;
  relatedLinearIssue?: string | null;
  newIssueCandidate: boolean;
}

export interface BenchmarkGapMatrixSummary {
  totalRows: number;
  byStatus: Record<string, number>;
  byPriority: Record<string, number>;
  byProvider: Record<string, number>;
}

export interface BenchmarkGapMatrixResponse {
  market: "kr" | "us" | "crypto" | "all";
  asOf: string;
  rows: BenchmarkGapRow[];
  nextCandidates: NextSourcingCandidate[];
  summary: BenchmarkGapMatrixSummary;
  sourcePolicy: string[];
  notes: string[];
}
```

- [ ] **Step 2: Run typecheck**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npm run typecheck` (or whichever script exists — check `package.json` first; if no `typecheck`, run `npx tsc --noEmit`).

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/types/benchmarkGap.ts
git commit -m "feat(rob-271): add benchmark gap matrix frontend types"
```

---

### Task 7: Frontend API client

**Files:**
- Create: `frontend/invest/src/api/benchmarkGap.ts`

- [ ] **Step 1: Write the client (mirror of `coverage.ts` style)**

```typescript
// frontend/invest/src/api/benchmarkGap.ts
import type { BenchmarkGapMatrixResponse } from "../types/benchmarkGap";

export async function fetchBenchmarkGapMatrix(
  params: {
    market?: "kr" | "us" | "crypto" | "all";
    asOf?: string;
    signal?: AbortSignal;
  } = {},
): Promise<BenchmarkGapMatrixResponse> {
  const q = new URLSearchParams();
  if (params.market) q.set("market", params.market);
  if (params.asOf) q.set("asOf", params.asOf);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  const res = await fetch(`/invest/api/coverage/benchmark-gap${suffix}`, {
    credentials: "include",
    signal: params.signal,
  });
  if (!res.ok) {
    throw new Error(`/invest/api/coverage/benchmark-gap ${res.status}`);
  }
  return res.json();
}
```

- [ ] **Step 2: Run typecheck**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/api/benchmarkGap.ts
git commit -m "feat(rob-271): add benchmark gap matrix fetch client"
```

---

### Task 8: New `BenchmarkGapSection` component

**Files:**
- Create: `frontend/invest/src/components/coverage/BenchmarkGapSection.tsx`

- [ ] **Step 1: Write the component**

```tsx
// frontend/invest/src/components/coverage/BenchmarkGapSection.tsx
import { Card } from "../../ds";
import type {
  BenchmarkGapMatrixResponse,
  BenchmarkGapPriority,
  BenchmarkGapRow,
  CoverageProductStatus,
} from "../../types/benchmarkGap";

const PRIORITY_COLOR: Record<BenchmarkGapPriority, string> = {
  P0: "#dc2626",
  P1: "#d97706",
  P2: "#ca8a04",
  P3: "#64748b",
};

const STATUS_LABEL: Record<CoverageProductStatus, string> = {
  covered: "수급됨",
  partial: "부분",
  stale: "오래됨",
  missing: "없음",
  candidate_unwired: "후보 · 미연결",
  benchmark_only: "벤치마크만",
  intentionally_excluded: "의도적 제외",
  unsupported: "미지원",
  blocked_by_auth_or_policy: "차단 (auth/정책)",
};

const STATUS_COLOR: Record<CoverageProductStatus, string> = {
  covered: "#16a34a",
  partial: "#ca8a04",
  stale: "#d97706",
  missing: "#dc2626",
  candidate_unwired: "#7c3aed",
  benchmark_only: "#64748b",
  intentionally_excluded: "#475569",
  unsupported: "#94a3b8",
  blocked_by_auth_or_policy: "#b91c1c",
};

function StatusPill({ status }: { status: CoverageProductStatus }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 999,
        padding: "3px 8px",
        fontSize: 12,
        fontWeight: 800,
        color: "white",
        background: STATUS_COLOR[status],
        whiteSpace: "nowrap",
      }}
    >
      {STATUS_LABEL[status]}
    </span>
  );
}

function PriorityChip({ priority }: { priority: BenchmarkGapPriority }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: 6,
        padding: "1px 6px",
        fontSize: 11,
        fontWeight: 900,
        color: "white",
        background: PRIORITY_COLOR[priority],
      }}
    >
      {priority}
    </span>
  );
}

function RowCard({ row }: { row: BenchmarkGapRow }) {
  return (
    <div
      style={{
        border: "1px solid var(--divider)",
        borderRadius: 10,
        padding: 12,
        display: "grid",
        gap: 6,
      }}
    >
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <PriorityChip priority={row.priority} />
        <strong style={{ fontSize: 14 }}>{row.benchmarkLabelKo}</strong>
        <StatusPill status={row.coverageStatus} />
      </div>
      <div style={{ color: "var(--fg-3)", fontSize: 12, fontFamily: "var(--font-mono)" }}>
        {row.benchmarkProvider} · {row.benchmarkSurface}
      </div>
      <div style={{ fontSize: 12 }}>
        <span style={{ color: "var(--fg-2)" }}>왜 필요:</span> {row.whyNeeded}
      </div>
      <div style={{ fontSize: 12 }}>
        <span style={{ color: "var(--fg-2)" }}>다음 액션:</span> {row.nextAction}
      </div>
      {(row.autoTraderApi || row.autoTraderReadModel || row.autoTraderTable) && (
        <div style={{ color: "var(--fg-3)", fontSize: 11, fontFamily: "var(--font-mono)" }}>
          auto_trader: {row.autoTraderApi ?? row.autoTraderReadModel ?? row.autoTraderTable}
        </div>
      )}
      {row.relatedLinearIssue && (
        <div style={{ color: "var(--fg-3)", fontSize: 11 }}>관련 이슈: {row.relatedLinearIssue}</div>
      )}
      {row.newIssueCandidate && (
        <div style={{ color: "#7c3aed", fontSize: 11, fontWeight: 700 }}>new_issue_candidate</div>
      )}
    </div>
  );
}

function rowsByProvider(
  rows: BenchmarkGapRow[],
  provider: BenchmarkGapRow["benchmarkProvider"] | BenchmarkGapRow["benchmarkProvider"][],
) {
  const providers = Array.isArray(provider) ? provider : [provider];
  return rows.filter((row) => providers.includes(row.benchmarkProvider));
}

export function BenchmarkGapSection({ data }: { data: BenchmarkGapMatrixResponse }) {
  const tossRows = rowsByProvider(data.rows, "toss");
  const naverRows = rowsByProvider(data.rows, "naver");
  const internalRows = rowsByProvider(data.rows, ["internal", "kis", "upbit", "news_ingestor"]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card>
        <h2 style={{ margin: 0, fontSize: 20 }}>토스·네이버 대비 데이터 수급 현황</h2>
        <p style={{ margin: "6px 0 12px", color: "var(--fg-2)", fontSize: 13 }}>
          이 화면은 “무슨 데이터를 다음에 수급해야 하는가?” 를 보여주는 read-only 벤치마크 갭 매트릭스입니다.
          Toss/Naver 는 reference/candidate 신호이며 sourceOfTruth 가 아닙니다.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 8 }}>
          {Object.entries(data.summary.byStatus).map(([status, count]) => (
            <div
              key={status}
              style={{
                border: "1px solid var(--divider)",
                borderRadius: 8,
                padding: "6px 10px",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: 8,
              }}
            >
              <StatusPill status={status as CoverageProductStatus} />
              <strong>{count}</strong>
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>다음 수급 후보</h2>
        <p style={{ margin: "4px 0 12px", color: "var(--fg-2)", fontSize: 12 }}>
          우선순위 P0 → P3 순. 이미 `covered` 인 surface 는 제외됩니다.
        </p>
        <div style={{ display: "grid", gap: 8 }}>
          {data.nextCandidates.length === 0 && (
            <span style={{ color: "var(--fg-3)" }}>현재 추가 수급 후보 없음</span>
          )}
          {data.nextCandidates.map((c) => (
            <div
              key={c.rowId}
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr auto",
                gap: 10,
                alignItems: "center",
                padding: "8px 10px",
                border: "1px solid var(--divider)",
                borderRadius: 8,
              }}
            >
              <PriorityChip priority={c.priority} />
              <div>
                <div style={{ fontWeight: 800 }}>
                  {c.featureArea} · {c.benchmarkProvider}
                </div>
                <div style={{ fontSize: 12, color: "var(--fg-2)" }}>{c.gap}</div>
                <div style={{ fontSize: 11, color: "var(--fg-3)" }}>
                  현재 auto_trader: {c.currentAutoTrader ?? "(없음)"} · 다음 액션: {c.nextAction}
                  {c.relatedLinearIssue ? ` · 관련 ${c.relatedLinearIssue}` : ""}
                  {c.newIssueCandidate ? " · new_issue_candidate" : ""}
                </div>
              </div>
              <StatusPill status={c.currentStatus} />
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>Toss benchmark</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10, marginTop: 10 }}>
          {tossRows.map((row) => (
            <RowCard key={row.id} row={row} />
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>Naver benchmark</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10, marginTop: 10 }}>
          {naverRows.map((row) => (
            <RowCard key={row.id} row={row} />
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 18 }}>auto_trader 내부 / KIS</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 10, marginTop: 10 }}>
          {internalRows.map((row) => (
            <RowCard key={row.id} row={row} />
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: 0, fontSize: 14 }}>Source authority</h2>
        <ul style={{ margin: "6px 0 0", paddingLeft: 18, color: "var(--fg-2)", fontSize: 12 }}>
          {data.sourcePolicy.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
```

- [ ] **Step 2: Run typecheck**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/components/coverage/BenchmarkGapSection.tsx
git commit -m "feat(rob-271): add BenchmarkGapSection UI component"
```

---

### Task 9: Reorder `DesktopCoveragePage.tsx` (IA change)

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx`

Change the render order so the first screen is data-sourcing-gap-first:

1. PageSafetyNote (unchanged)
2. Header (unchanged title; subtitle re-worded)
3. Market/symbols filter (unchanged)
4. **NEW: `<BenchmarkGapSection data={benchmarkGap} />`** (summary → next candidates → Toss → Naver → internal/KIS)
5. **MOVED: `<ActionReadinessCard>` rendered inside `<details>` with `summary="KR 액션 리포트 준비도 (보조)"`**, default collapsed
6. **MOVED: existing coverage state summary grid + surfaces table + symbol section wrapped in `<details>` with `summary="개발자 / 디버그 — 원 raw 커버리지"`** , default collapsed

Top-level component must add a second data fetch for the benchmark gap matrix.

- [ ] **Step 1: Add the new fetch state and effect**

Find the existing `useState<KrActionReadinessResponse | undefined>` block (line ~346) and add right after it:

```tsx
import { fetchBenchmarkGapMatrix } from "../../api/benchmarkGap";
import { BenchmarkGapSection } from "../../components/coverage/BenchmarkGapSection";
import type { BenchmarkGapMatrixResponse } from "../../types/benchmarkGap";

// inside CoverageRoute, near other useStates:
const [benchmarkGap, setBenchmarkGap] = useState<BenchmarkGapMatrixResponse | undefined>();
const [benchmarkLoading, setBenchmarkLoading] = useState(true);
const [benchmarkErr, setBenchmarkErr] = useState<string | null>(null);

useEffect(() => {
  const controller = new AbortController();
  setBenchmarkLoading(true);
  setBenchmarkErr(null);
  fetchBenchmarkGapMatrix({ market, signal: controller.signal })
    .then((response) => {
      setBenchmarkGap(response);
      setBenchmarkLoading(false);
    })
    .catch((e) => {
      if (controller.signal.aborted) return;
      setBenchmarkErr(String(e?.message ?? e));
      setBenchmarkLoading(false);
    });
  return () => controller.abort();
}, [market]);
```

- [ ] **Step 2: Update the subtitle copy**

Find the line containing `/invest 소유 read-model의 freshness와 Toss/Naver 기준·후보 신호를 구분해 확인합니다.` and replace it with:

```tsx
<p style={{ margin: "6px 0 0", color: "var(--fg-2)", fontSize: 14 }}>
  토스·네이버 대비 auto_trader 데이터 수급 현황. “다음에 어떤 데이터를 수급해야 하는가?” 에 답하는 read-only 갭 매트릭스입니다.
</p>
```

- [ ] **Step 3: Insert BenchmarkGapSection above the existing readiness card**

Find the block `{market === "kr" && readiness && !readinessLoading && <ActionReadinessCard data={readiness} />}` (around line 444). Replace the whole block from the `{loading && <Card>…</Card>}` line through the closing `)` of `{data && !loading && (…)}` with:

```tsx
{benchmarkLoading && <Card>벤치마크 갭 매트릭스 로딩 중…</Card>}
{benchmarkErr && (
  <Card>
    <span style={{ color: STATE_COLOR.error }}>벤치마크 갭 API 오류: {benchmarkErr}</span>
  </Card>
)}
{benchmarkGap && !benchmarkLoading && <BenchmarkGapSection data={benchmarkGap} />}

<details>
  <summary style={{ cursor: "pointer", fontWeight: 700, padding: "8px 0" }}>
    KR 액션 리포트 준비도 (보조)
  </summary>
  <div style={{ marginTop: 8 }}>
    {market === "kr" && readinessLoading && <Card>KR 액션 리포트 준비도 로딩 중…</Card>}
    {market === "kr" && readinessErr && (
      <Card>
        <span style={{ color: STATE_COLOR.error }}>액션 준비도 API 오류: {readinessErr}</span>
      </Card>
    )}
    {market === "kr" && readiness && !readinessLoading && <ActionReadinessCard data={readiness} />}
    {market !== "kr" && <Card>KR 마켓일 때만 노출됩니다.</Card>}
  </div>
</details>

<details>
  <summary style={{ cursor: "pointer", fontWeight: 700, padding: "8px 0" }}>
    개발자 · 디버그 raw 커버리지
  </summary>
  <div style={{ marginTop: 8, display: "grid", gap: 12 }}>
    {loading && <Card>커버리지 로딩 중…</Card>}
    {err && <Card><span style={{ color: STATE_COLOR.error }}>커버리지 API 오류: {err}</span></Card>}
    {data && !loading && (
      <>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 12 }}>
          {summary.map(({ state, count }) => (
            <Card key={state}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                <StatePill state={state} />
                <strong style={{ fontSize: 22 }}>{count}</strong>
              </div>
            </Card>
          ))}
        </div>

        <Card>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 1160 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--fg-3)", fontSize: 12 }}>
                  <th style={{ padding: "0 10px 8px" }}>Surface</th>
                  <th style={{ padding: "0 10px 8px" }}>State</th>
                  <th style={{ padding: "0 10px 8px" }}>Market</th>
                  <th style={{ padding: "0 10px 8px" }}>Source of truth</th>
                  <th style={{ padding: "0 10px 8px" }}>Latest</th>
                  <th style={{ padding: "0 10px 8px" }}>Counts</th>
                  <th style={{ padding: "0 10px 8px" }}>Actionability</th>
                  <th style={{ padding: "0 10px 8px" }}>Gap / note</th>
                </tr>
              </thead>
              <tbody>
                {data.surfaces.map((surface, idx) => (
                  <SurfaceRow key={`${surface.surface}-${surface.market}-${idx}`} surface={surface} />
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        {data.symbols.length > 0 && (
          <Card>
            <h2 style={{ margin: "0 0 12px", fontSize: 18 }}>Symbol coverage</h2>
            <div style={{ display: "grid", gap: 10 }}>
              {data.symbols.map((symbol) => (
                <div key={symbol.symbol} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                  <strong style={{ width: 84 }}>{symbol.symbol}</strong>
                  <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{symbol.market}</span>
                  {Object.entries(symbol.surfaces).map(([name, state]) => (
                    <span key={name} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                      <span style={{ color: "var(--fg-3)", fontSize: 12 }}>{name}</span>
                      <StatePill state={state} />
                    </span>
                  ))}
                  <ActionabilityBadge actionability={symbol.actionability} />
                </div>
              ))}
            </div>
          </Card>
        )}
      </>
    )}
  </div>
</details>
```

- [ ] **Step 4: Run typecheck**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/pages/desktop/DesktopCoveragePage.tsx
git commit -m "feat(rob-271): reorder /invest/coverage IA to gap-first; collapse legacy panels"
```

---

### Task 10: Frontend smoke test for new IA

**Files:**
- Modify: `frontend/invest/src/__tests__/DesktopCoveragePage.test.tsx`
- Create: `frontend/invest/src/__tests__/DesktopCoveragePage.benchmarkGap.test.tsx`

We append the benchmark gap mock to the existing test so it stays green, and add a new test asserting the new IA elements render.

- [ ] **Step 1: Update existing test to also mock the new API**

In `frontend/invest/src/__tests__/DesktopCoveragePage.test.tsx`, add imports near the top:

```tsx
import * as benchmarkGapApi from "../api/benchmarkGap";
import type { BenchmarkGapMatrixResponse } from "../types/benchmarkGap";
```

Add a stub payload above `function wrap`:

```tsx
const BENCHMARK_GAP_PAYLOAD: BenchmarkGapMatrixResponse = {
  market: "kr",
  asOf: "2026-05-19T00:00:00Z",
  rows: [],
  nextCandidates: [],
  summary: { totalRows: 0, byStatus: {}, byPriority: {}, byProvider: {} },
  sourcePolicy: ["KIS live = broker authority"],
  notes: [],
};
```

Inside the existing `beforeEach`, add:

```tsx
vi.spyOn(benchmarkGapApi, "fetchBenchmarkGapMatrix").mockResolvedValue(BENCHMARK_GAP_PAYLOAD);
```

- [ ] **Step 2: Verify existing smoke test still passes**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npx vitest run src/__tests__/DesktopCoveragePage.test.tsx`
Expected: pass.

- [ ] **Step 3: Add a new smoke test for the gap-first IA**

```tsx
// frontend/invest/src/__tests__/DesktopCoveragePage.benchmarkGap.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import * as benchmarkGapApi from "../api/benchmarkGap";
import * as coverageApi from "../api/coverage";
import { CoverageRoute } from "../pages/desktop/DesktopCoveragePage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";
import type { BenchmarkGapMatrixResponse } from "../types/benchmarkGap";
import type { InvestCoverageResponse } from "../types/coverage";

const COVERAGE_EMPTY: InvestCoverageResponse = {
  market: "kr",
  asOf: "2026-05-19T00:00:00Z",
  tradingDate: "2026-05-19",
  states: ["fresh"],
  surfaces: [],
  symbols: [],
  gaps: [],
  notes: [],
};

const GAP_PAYLOAD: BenchmarkGapMatrixResponse = {
  market: "kr",
  asOf: "2026-05-19T00:00:00Z",
  rows: [
    {
      id: "toss.screener",
      featureArea: "screener",
      benchmarkProvider: "toss",
      benchmarkSurface: "screener.presets",
      benchmarkLabelKo: "골라보기",
      sourceRole: "benchmark_only",
      coverageStatus: "partial",
      priority: "P2",
      whyNeeded: "screener parity",
      nextAction: "map presets",
      newIssueCandidate: false,
      notes: [],
    },
    {
      id: "naver.market.kr",
      featureArea: "market",
      benchmarkProvider: "naver",
      benchmarkSurface: "market.kr",
      benchmarkLabelKo: "국내 시장",
      sourceRole: "reference",
      coverageStatus: "covered",
      priority: "P2",
      whyNeeded: "kr market parity",
      nextAction: "monitor",
      newIssueCandidate: false,
      notes: [],
    },
  ],
  nextCandidates: [
    {
      rowId: "toss.screener",
      priority: "P2",
      featureArea: "screener",
      benchmarkProvider: "toss",
      gap: "missing toss-style presets",
      currentAutoTrader: "/invest/api/screener/presets",
      whyItMatters: "parity baseline",
      currentStatus: "partial",
      nextAction: "map presets",
      newIssueCandidate: false,
    },
  ],
  summary: { totalRows: 2, byStatus: { partial: 1, covered: 1 }, byPriority: { P2: 2 }, byProvider: { toss: 1, naver: 1 } },
  sourcePolicy: ["Toss = benchmark/reference only — never sourceOfTruth"],
  notes: ["first-screen view"],
};

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/coverage"]}>{ui}</MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  vi.spyOn(coverageApi, "fetchInvestCoverage").mockResolvedValue(COVERAGE_EMPTY);
  vi.spyOn(benchmarkGapApi, "fetchBenchmarkGapMatrix").mockResolvedValue(GAP_PAYLOAD);
});

test("benchmark gap section is the first visible section after header/filter", async () => {
  render(wrap(<CoverageRoute />));
  await waitFor(() =>
    expect(screen.getByText(/토스·네이버 대비 데이터 수급 현황/)).toBeInTheDocument(),
  );
  expect(screen.getByText(/다음 수급 후보/)).toBeInTheDocument();
  expect(screen.getByText(/Toss benchmark/)).toBeInTheDocument();
  expect(screen.getByText(/Naver benchmark/)).toBeInTheDocument();
  expect(screen.getByText(/auto_trader 내부 · KIS|auto_trader 내부/)).toBeInTheDocument();
  // legacy panels live under collapsed details
  expect(screen.getByText(/KR 액션 리포트 준비도 \(보조\)/)).toBeInTheDocument();
  expect(screen.getByText(/개발자 · 디버그 raw 커버리지/)).toBeInTheDocument();
  // source policy is rendered
  expect(screen.getByText(/Toss = benchmark/)).toBeInTheDocument();
});
```

- [ ] **Step 4: Run the new test**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npx vitest run src/__tests__/DesktopCoveragePage.benchmarkGap.test.tsx`
Expected: pass.

- [ ] **Step 5: Run the full frontend test suite**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npx vitest run`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/__tests__/DesktopCoveragePage.test.tsx frontend/invest/src/__tests__/DesktopCoveragePage.benchmarkGap.test.tsx
git commit -m "test(rob-271): smoke-test new gap-first IA and keep legacy test green"
```

---

### Task 11: Docs update

**Files:**
- Modify: `docs/invest-coverage-dashboard.md`

- [ ] **Step 1: Append a new top section in `docs/invest-coverage-dashboard.md` (after the H1, before the existing endpoint section)**

Insert after the H1 line (`# ROB-203 /invest coverage actionability`):

```markdown
> **ROB-271 update:** `/invest/coverage` is now a Toss/Naver benchmark data-sourcing gap matrix first. The KR action-readiness card and the raw coverage surface table remain available as collapsed/secondary sections. The new product-facing endpoint is read-only and does not replace the existing `/invest/api/coverage` or `/invest/api/kr/action-readiness` contracts.

## ROB-271 — Benchmark gap matrix (data-sourcing-gap-first)

Endpoint: `GET /invest/api/coverage/benchmark-gap`

Query parameters:
- `market`: `kr`, `us`, `crypto`, `all` (default `kr`)
- `asOf`: optional trading date override

Purpose: answer “토스·네이버 대비 auto_trader 가 어떤 데이터를 다음에 수급해야 하는가?” without estimating, without scraping, and without proposing buy/sell logic.

Source authority (explicit, unchanged):
- **KIS live** = holdings / cash / orderable cash / open orders / sellable quantity broker authority.
- **auto_trader DB/read-models** = product authority for `/invest` surfaces (market, screener, news, calendar, valuation, flow, ledger, action-report snapshots).
- **Toss** = benchmark / reference only. Never `sourceOfTruth`.
- **Naver** = candidate / reference unless explicitly promoted to an owned read-model.
- **community / discussion** = aggregate-signal-only candidates. Raw text cloning is prohibited.

Product-facing status vocabulary (additive — legacy `CoverageState`/`ActionReadinessState` are preserved):

| Status | Meaning |
| --- | --- |
| `covered` | already available and mapped in auto_trader |
| `partial` | partially available; needs more fields/better mapping |
| `stale` | data exists but too old |
| `missing` | no owned read-model/source yet |
| `candidate_unwired` | source candidate exists but ingest/read-model/UI not wired |
| `benchmark_only` | visible in Toss/Naver, used only for comparison |
| `intentionally_excluded` | intentionally not collected (e.g., community text cloning) |
| `unsupported` | outside current scope |
| `blocked_by_auth_or_policy` | blocked by login/private API/robots/rate limit/licensing |

Legacy developer states (`blocked`, `missing`, `unknown`, `확인 불가`) remain in the action-readiness API and in the raw coverage surface table, both of which are now rendered under collapsed secondary sections.

UI information architecture (data-sourcing-gap-first):
1. Benchmark gap summary
2. 다음 수급 후보 list (priority-ordered)
3. Toss benchmark coverage
4. Naver benchmark coverage
5. auto_trader 내부 / KIS coverage
6. (collapsed) KR 액션 리포트 준비도 — secondary
7. (collapsed) 개발자 · 디버그 raw 커버리지 — original surfaces table + symbol diagnostics

Non-goals of this issue:
- No broker/order/watch/order-intent mutation.
- No buy/sell recommendation logic.
- No production DB writes, backfills, or scheduler activation.
- No live Toss/Naver scraping in request paths.
- No promotion of Toss/Naver to `sourceOfTruth`.
- No cloning of public community text.
- Implementing every downstream data collector is **out of scope**. This issue identifies and prioritizes gaps; collection work belongs to follow-up Linear issues.

New rows discovered during work that do not yet have a Linear issue should be marked with `newIssueCandidate=true` in the row payload. **Do not auto-create Linear issues from the dashboard.** Promotion to a real Linear issue is a separate human-approved handoff step.
```

- [ ] **Step 2: Commit**

```bash
git add docs/invest-coverage-dashboard.md
git commit -m "docs(rob-271): document benchmark gap matrix + product-facing vocabulary"
```

---

### Task 12: Final verification + summary

- [ ] **Step 1: Run full backend coverage and readiness tests one more time**

Run: `uv run pytest tests/test_invest_coverage.py tests/test_invest_action_readiness.py tests/test_invest_benchmark_gap.py -v`
Expected: all green.

- [ ] **Step 2: Run targeted frontend tests**

Run: `cd /Users/mgh3326/work/auto_trader.rob-271/frontend/invest && npx vitest run src/__tests__/DesktopCoveragePage.test.tsx src/__tests__/DesktopCoveragePage.benchmarkGap.test.tsx`
Expected: pass.

- [ ] **Step 3: Run lint / typecheck on the whole repo (best-effort)**

Run: `make lint` (backend) and `cd frontend/invest && npx tsc --noEmit` (frontend).
Expected: no new errors introduced by this branch.

- [ ] **Step 4: Browser smoke** (manual)

Start dev server (`make dev`), open `/invest/coverage` for KR mode, confirm:
- 첫 화면이 benchmark gap section
- 다음 수급 후보 카드가 P0/P1 우선순위 순으로 보임
- Toss / Naver / 내부 섹션이 차례로 보임
- KR 액션 리포트 준비도 카드는 collapsed details 안에 있음
- 개발자 · 디버그 raw 커버리지 carded section 도 collapsed
- 기존 raw surface table 의 내용은 펼치면 그대로 노출

Record results in the PR description (screenshot or text smoke). If dev server cannot be started in this environment, document that explicitly.

- [ ] **Step 5: Compose PR / handoff summary**

Include:
- branch name (`rob-271`)
- migration status (none — additive code only)
- backend tests run + results
- frontend tests run + results
- /invest/coverage smoke result (or note if not testable in this environment)
- new follow-up gap candidates discovered during MVP work (text list — do **not** auto-create Linear issues)
- deployment cautions: no DB migration, no scheduler activation, no broker calls, only an additive read-only endpoint and one frontend route IA change

---

## Self-Review

**Spec coverage:**
- ✅ 1차 PR 목표 (gap-first IA): Tasks 8–10
- ✅ 1) 현행 구조 파악: pre-plan reading
- ✅ 2) Benchmark gap row schema (required minimum + optional): Task 1
- ✅ 3) MVP mapping (Toss 5 / Naver 5 / Internal/KIS 5): Task 3
- ✅ 4) Product-facing vocabulary (9 values): Task 1 + Task 2 (mapping)
- ✅ 5) UI IA reorder: Task 9
- ✅ 6) 다음 수급 후보 list: Task 4 (builder) + Task 8 (UI)
- ✅ 7) Source authority preserved in code + docs + UI copy: Task 4 (`_SOURCE_POLICY`) + Task 8 (rendered) + Task 11 (docs)
- ✅ 8) Non-goals 엄수: explicit in router + service docstrings + docs section
- ✅ 9) Tests: backend Tasks 1–5, frontend Task 10
- ✅ 10) Docs update: Task 11
- ✅ Handoff content (branch, migrations, tests, smoke, follow-ups, deployment cautions): Task 12

**Placeholder scan:**
- All code blocks contain real, runnable code.
- All file paths absolute.
- All test commands exact.
- No "TBD" / "TODO" / "similar to Task N" without inlined code.

**Type consistency:**
- `BenchmarkGapRow` field names identical in Python and TS (`benchmarkProvider`, `benchmarkSurface`, `benchmarkLabelKo`, `sourceRole`, `coverageStatus`, `priority`, `whyNeeded`, `nextAction`, `relatedLinearIssue`, `newIssueCandidate`, etc.).
- `CoverageProductStatus` literal values match across both languages and the docs table.
- `BenchmarkGapPriority` is `"P0" | "P1" | "P2" | "P3"` in both.
- `BenchmarkGapMatrixResponse.market` uses the same `"kr" | "us" | "crypto" | "all"` union as the existing coverage response.

**Untouched contracts:**
- `app/schemas/invest_coverage.py`, `app/schemas/invest_action_readiness.py`: no changes.
- `app/services/invest_coverage_service.py`, `app/services/invest_view_model/action_readiness_service.py`: no changes.
- Existing `/invest/api/coverage` and `/invest/api/kr/action-readiness` routes: unchanged.
- Existing `tests/test_invest_coverage.py`, `tests/test_invest_action_readiness.py`: unchanged.
- Existing `DesktopCoveragePage.test.tsx`: a single mock added; assertions preserved.
