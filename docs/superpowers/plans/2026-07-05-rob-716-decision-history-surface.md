# ROB-716 Decision History Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 종목상세 "최근 분석" 패널을 죽은 `stock_analysis_results` 대신 ROB-711 `build_decision_context`(라이브 판단→결과 학습루프)로 재연결한다.

**Architecture:** 기존 `build_stock_detail` provider 패턴에 provider 1개 + 응답 필드(`decisionHistory`) + 프론트 카드(`DecisionHistoryCard`)를 추가하고, 죽은 `latestAnalysis` 계열(provider·스키마·프론트 타입·카드)을 제거한다. 데이터는 `app/services/decision_history.py::build_decision_context(db, symbol, market)`를 그대로 호출 — join 로직 재구현 없음.

**Tech Stack:** Python 3.13 / FastAPI / Pydantic v2 (`ConfigDict(extra="forbid")`) / SQLAlchemy async / pytest (`@pytest.mark.unit`, `@pytest.mark.asyncio`) / React + TypeScript / vitest.

## Global Constraints

- Migration 0 — DB 스키마 변경 금지. read-only.
- 브로커/주문/감시/order-intent mutation 무접촉.
- In-process LLM provider import 금지 (ROB-501; `app/**` 정적 가드).
- 모든 신규 Pydantic 모델은 `model_config = ConfigDict(extra="forbid")`.
- 데이터 소스는 `build_decision_context` 재사용 — 새 join 로직 작성 금지.
- 테스트 실행: `uv run pytest <path> -v`. 프론트: `cd frontend/invest && npm test`.
- 커밋 트레일러: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- `app/schemas/invest_stock_detail.py` — `StockDetailLatestAnalysis` 제거, `StockDetailDecisionHistory*` 5개 모델 추가, `StockDetailResponse` 필드 교체.
- `app/services/invest_view_model/stock_detail_providers.py` — `stock_detail_latest_analysis_provider`+`_reasons_top3` 제거, `stock_detail_decision_history_provider` 추가.
- `app/services/invest_view_model/stock_detail_service.py` — `StockDetailProviders`/`build_stock_detail` 배선 교체.
- `frontend/invest/src/types/stockDetail.ts` — 타입 교체.
- `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx` — `AnalysisCard`→`DecisionHistoryCard`.
- `frontend/invest/src/__tests__/StockDetailPage.test.tsx` — fixture 교체.
- Tests: `tests/test_invest_stock_detail_schemas.py`, `tests/test_stock_detail_providers.py`, `tests/test_stock_detail_service.py`.

Task 순서 = 의존 순서: 스키마(1) → provider(2) → 서비스 배선(3) → 프론트(4).

---

### Task 1: `decisionHistory` 스키마 (신규 모델 + 응답 필드 교체)

**Files:**
- Modify: `app/schemas/invest_stock_detail.py` (제거 `StockDetailLatestAnalysis` @387-397, 제거 `StockDetailResponse.latestAnalysis` @514; 신규 모델 추가)
- Test: `tests/test_invest_stock_detail_schemas.py`

**Interfaces:**
- Produces:
  - `StockDetailDecisionHistoryPriorDecision(date: str|None, intent: str|None, side: str|None, decisionBucket: str|None, confidence: float|None, rationale: str|None)`
  - `StockDetailDecisionHistoryOutcome(date: str|None, side: str|None, outcome: str|None, triggerType: str|None, pnlPct: float|None, realizedPnl: float|None)`
  - `StockDetailDecisionHistoryOpenClaim(probability: float|None, horizon: str|None, reviewDate: str|None, direction: str|None, targetPrice: float|None)`
  - `StockDetailDecisionHistoryBrier(n: int, meanBrier: float|None, flag: Literal["ok","insufficient_sample"])`
  - `StockDetailDecisionHistory(symbol: str, market: str, linkQuality: str, priorDecisions: list[...], priorLessons: list[str], realizedOutcomes: list[...], openClaims: list[...], runningBrierSymbol: Brier, runningBrierGlobal: Brier, cautionLabel: str)`
  - `StockDetailResponse.decisionHistory: StockDetailDecisionHistory | None`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_invest_stock_detail_schemas.py` 끝에 추가

```python
@pytest.mark.unit
def test_decision_history_schema_forbids_extra_and_maps_sections():
    from app.schemas.invest_stock_detail import (
        StockDetailDecisionHistory,
        StockDetailDecisionHistoryBrier,
    )

    model = StockDetailDecisionHistory(
        symbol="000660",
        market="kr",
        linkQuality="symbol_window",
        priorDecisions=[
            {
                "date": "2026-06-28",
                "intent": "buy_review",
                "side": "buy",
                "decisionBucket": "new_buy_candidate",
                "confidence": 0.7,
                "rationale": "HBM 수요 지속",
            }
        ],
        priorLessons=["과열 구간 추격 금지"],
        realizedOutcomes=[
            {
                "date": "2026-06-20",
                "side": "sell",
                "outcome": "stop_loss",
                "triggerType": "stop",
                "pnlPct": -3.1,
                "realizedPnl": -31000.0,
            }
        ],
        openClaims=[
            {
                "probability": 0.7,
                "horizon": "1w",
                "reviewDate": "2026-07-10",
                "direction": "up",
                "targetPrice": 82000.0,
            }
        ],
        runningBrierSymbol=StockDetailDecisionHistoryBrier(
            n=12, meanBrier=0.18, flag="ok"
        ),
        runningBrierGlobal=StockDetailDecisionHistoryBrier(
            n=4, meanBrier=None, flag="insufficient_sample"
        ),
    )
    assert model.priorDecisions[0].confidence == 0.7
    assert model.realizedOutcomes[0].outcome == "stop_loss"
    assert model.openClaims[0].targetPrice == 82000.0
    assert model.runningBrierGlobal.flag == "insufficient_sample"
    assert "직접 연결" in model.cautionLabel  # default caution present

    with pytest.raises(ValidationError):
        StockDetailDecisionHistoryBrier(n=1, meanBrier=0.1, flag="ok", extra="x")
```

`ValidationError`/`pytest` import가 파일 상단에 없으면 추가.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_invest_stock_detail_schemas.py::test_decision_history_schema_forbids_extra_and_maps_sections -v`
Expected: FAIL — `ImportError: cannot import name 'StockDetailDecisionHistory'`.

- [ ] **Step 3: 스키마 구현** — `app/schemas/invest_stock_detail.py`

`StockDetailLatestAnalysis` 클래스(@387-397)를 아래 5개 모델로 **교체**(삭제 후 삽입):

```python
class StockDetailDecisionHistoryPriorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    intent: str | None = None
    side: str | None = None
    decisionBucket: str | None = None
    confidence: float | None = None
    rationale: str | None = None


class StockDetailDecisionHistoryOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    side: str | None = None
    outcome: str | None = None
    triggerType: str | None = None
    pnlPct: float | None = None
    realizedPnl: float | None = None


class StockDetailDecisionHistoryOpenClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probability: float | None = None
    horizon: str | None = None
    reviewDate: str | None = None
    direction: str | None = None
    targetPrice: float | None = None


class StockDetailDecisionHistoryBrier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int = 0
    meanBrier: float | None = None
    flag: Literal["ok", "insufficient_sample"] = "insufficient_sample"


class StockDetailDecisionHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: str
    linkQuality: str = "symbol_window"
    priorDecisions: list[StockDetailDecisionHistoryPriorDecision] = Field(
        default_factory=list
    )
    priorLessons: list[str] = Field(default_factory=list)
    realizedOutcomes: list[StockDetailDecisionHistoryOutcome] = Field(
        default_factory=list
    )
    openClaims: list[StockDetailDecisionHistoryOpenClaim] = Field(
        default_factory=list
    )
    runningBrierSymbol: StockDetailDecisionHistoryBrier = Field(
        default_factory=StockDetailDecisionHistoryBrier
    )
    runningBrierGlobal: StockDetailDecisionHistoryBrier = Field(
        default_factory=StockDetailDecisionHistoryBrier
    )
    cautionLabel: str = (
        "종목 기준 집계이며 특정 판단과 특정 결과의 직접 연결이 아닙니다."
    )
```

`AnalysisDecision` 별칭(@44)은 그대로 둔다(Literal 별칭, 미사용이어도 lint 무해).

`StockDetailResponse`(@495~)에서 `latestAnalysis: StockDetailLatestAnalysis | None = None` 라인을 다음으로 교체:

```python
    decisionHistory: StockDetailDecisionHistory | None = None
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_invest_stock_detail_schemas.py -v`
Expected: PASS (신규 테스트 + 기존 스키마 테스트 전부).

- [ ] **Step 5: 커밋**

```bash
git add app/schemas/invest_stock_detail.py tests/test_invest_stock_detail_schemas.py
git commit -m "feat(ROB-716): decisionHistory response schema, drop latestAnalysis

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `stock_detail_decision_history_provider` (provider 교체)

**Files:**
- Modify: `app/services/invest_view_model/stock_detail_providers.py` (제거 `stock_detail_latest_analysis_provider`+`_reasons_top3`+`StockAnalysisService`/`StockDetailLatestAnalysis` import; 신규 provider + `build_decision_context` import + `__all__` 갱신)
- Test: `tests/test_stock_detail_providers.py`

**Interfaces:**
- Consumes: `app.services.decision_history.build_decision_context(db, symbol, market) -> dict | None` (모듈에 import — 테스트가 `providers.build_decision_context`로 monkeypatch).
- Produces: `stock_detail_decision_history_provider(market: NewsMarket, symbol: str, db) -> StockDetailDecisionHistory | None`.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_stock_detail_providers.py`의 두 `latest_analysis` 테스트(@387-454)를 아래로 **교체**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_decision_history_provider_maps_payload(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    payload = {
        "symbol": "000660",
        "market": "kr",
        "link_quality": "symbol_window",
        "prior_decisions": [
            {
                "date": "2026-06-28",
                "intent": "buy_review",
                "side": "buy",
                "decision_bucket": "new_buy_candidate",
                "confidence": 0.7,
                "rationale": "HBM 수요",
            }
        ],
        "prior_lessons": ["추격 금지"],
        "realized_outcomes": [
            {
                "date": "2026-06-20",
                "side": "sell",
                "outcome": "stop_loss",
                "trigger_type": "stop",
                "pnl_pct": -3.1,
                "realized_pnl": -31000.0,
            }
        ],
        "recent_fills": [{"date": "2026-06-20", "side": "sell"}],  # 의도적 무시
        "open_claims": [
            {
                "probability": 0.7,
                "horizon": "1w",
                "review_date": "2026-07-10",
                "direction": "up",
                "target_price": 82000.0,
            }
        ],
        "running_brier_symbol": {"n": 12, "mean_brier": 0.18, "flag": "ok"},
        "running_brier_global": {"n": 4, "mean_brier": None, "flag": "insufficient_sample"},
    }

    async def fake_build(db, symbol, market):
        assert symbol == "000660"
        assert market == "kr"
        return payload

    monkeypatch.setattr(providers, "build_decision_context", fake_build)

    result = await providers.stock_detail_decision_history_provider(
        "kr", "000660", SimpleNamespace(execute=object())
    )

    assert result is not None
    assert result.linkQuality == "symbol_window"
    assert result.priorDecisions[0].decisionBucket == "new_buy_candidate"
    assert result.realizedOutcomes[0].triggerType == "stop"
    assert result.realizedOutcomes[0].pnlPct == -3.1
    assert result.openClaims[0].targetPrice == 82000.0
    assert result.runningBrierSymbol.n == 12
    assert result.runningBrierGlobal.flag == "insufficient_sample"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_decision_history_provider_returns_none(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fake_build(db, symbol, market):
        return None

    monkeypatch.setattr(providers, "build_decision_context", fake_build)

    # no db.execute → None (build_decision_context never called)
    assert (
        await providers.stock_detail_decision_history_provider("kr", "000660", object())
        is None
    )
    # db present but no signal → None
    assert (
        await providers.stock_detail_decision_history_provider(
            "kr", "000660", SimpleNamespace(execute=object())
        )
        is None
    )
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_stock_detail_providers.py -k decision_history -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'stock_detail_decision_history_provider'`.

- [ ] **Step 3: provider 구현** — `stock_detail_providers.py`

상단 import 교체: `StockDetailLatestAnalysis` 를 `StockDetailDecisionHistory`로 바꾸고, 파일 상단 import 블록에 추가:

```python
from app.services.decision_history import build_decision_context
```

`_reasons_top3`(@145-153) 및 `stock_detail_latest_analysis_provider`(@156-186) 전체를 아래로 **교체**:

```python
def _brier(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "n": raw.get("n", 0),
        "meanBrier": raw.get("mean_brier"),
        "flag": raw.get("flag", "insufficient_sample"),
    }


async def stock_detail_decision_history_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailDecisionHistory | None:
    if not hasattr(db, "execute"):
        return None
    ctx = await build_decision_context(db, symbol, market)
    if ctx is None:
        return None
    return StockDetailDecisionHistory(
        symbol=ctx.get("symbol", symbol),
        market=ctx.get("market", market),
        linkQuality=ctx.get("link_quality", "symbol_window"),
        priorDecisions=[
            {
                "date": d.get("date"),
                "intent": d.get("intent"),
                "side": d.get("side"),
                "decisionBucket": d.get("decision_bucket"),
                "confidence": d.get("confidence"),
                "rationale": d.get("rationale"),
            }
            for d in ctx.get("prior_decisions", [])
        ],
        priorLessons=list(ctx.get("prior_lessons", [])),
        realizedOutcomes=[
            {
                "date": o.get("date"),
                "side": o.get("side"),
                "outcome": o.get("outcome"),
                "triggerType": o.get("trigger_type"),
                "pnlPct": o.get("pnl_pct"),
                "realizedPnl": o.get("realized_pnl"),
            }
            for o in ctx.get("realized_outcomes", [])
        ],
        openClaims=[
            {
                "probability": c.get("probability"),
                "horizon": c.get("horizon"),
                "reviewDate": c.get("review_date"),
                "direction": c.get("direction"),
                "targetPrice": c.get("target_price"),
            }
            for c in ctx.get("open_claims", [])
        ],
        runningBrierSymbol=_brier(ctx.get("running_brier_symbol")),
        runningBrierGlobal=_brier(ctx.get("running_brier_global")),
    )
```

(`recent_fills`는 의도적으로 매핑하지 않음 — YAGNI, 주문 카드가 이미 노출.)

`__all__`에서 `"stock_detail_latest_analysis_provider"` 제거, `"stock_detail_decision_history_provider"` 추가(알파벳 순).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_stock_detail_providers.py -v`
Expected: PASS. 그리고 `uv run ruff check app/services/invest_view_model/stock_detail_providers.py` — 미사용 import 없음.

- [ ] **Step 5: 커밋**

```bash
git add app/services/invest_view_model/stock_detail_providers.py tests/test_stock_detail_providers.py
git commit -m "feat(ROB-716): decision_history provider reuses build_decision_context

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 서비스 배선 (`StockDetailProviders` + `build_stock_detail`)

**Files:**
- Modify: `app/services/invest_view_model/stock_detail_service.py` (import, `StockDetailProviders`, `build_stock_detail` 본문, `StockDetailResponse(...)`)
- Test: `tests/test_stock_detail_service.py`

**Interfaces:**
- Consumes: `stock_detail_decision_history_provider` (Task 2), `StockDetailDecisionHistory` (Task 1).
- Produces: `StockDetailResponse.decisionHistory` 배선; warning key `decision_history_unavailable` / `decision_history_timeout` (기존 `_run_optional_block` 규약).

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_stock_detail_service.py`에 추가. **기존 모듈 헬퍼 `_resolve_kr`(symbol `005930`, @35-44)를 재사용**한다(직접 `ResolvedSymbol`을 만들지 말 것 — 필드 값 오류 방지). 파일 상단에 이미 `StockDetailProviders`, `build_stock_detail`, `SimpleNamespace` import 있음. `StockDetailDecisionHistory`만 로컬 import.

```python
@pytest.mark.asyncio
async def test_build_stock_detail_wires_decision_history():
    from app.schemas.invest_stock_detail import StockDetailDecisionHistory

    async def decision_history(market, symbol, db):
        assert symbol == "005930"
        return StockDetailDecisionHistory(symbol="005930", market="kr")

    providers = StockDetailProviders(
        resolver=_resolve_kr, decision_history=decision_history
    )

    result = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(execute=object()),
        providers=providers,
    )

    assert result.decisionHistory is not None
    assert result.decisionHistory.symbol == "005930"
    assert "decision_history_unavailable" not in result.meta.warnings


@pytest.mark.asyncio
async def test_build_stock_detail_isolates_decision_history_failure():
    async def decision_history(market, symbol, db):
        raise RuntimeError("boom")

    providers = StockDetailProviders(
        resolver=_resolve_kr, decision_history=decision_history
    )

    result = await build_stock_detail(
        user_id=1,
        market="kr",
        symbol="005930",
        db=SimpleNamespace(execute=object()),
        providers=providers,
    )

    assert result.decisionHistory is None
    assert "decision_history_unavailable" in result.meta.warnings
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_stock_detail_service.py -k decision_history -v`
Expected: FAIL — `TypeError: StockDetailProviders.__init__ got unexpected keyword 'decision_history'` (아직 `latest_analysis` 필드).

- [ ] **Step 3: 서비스 배선** — `stock_detail_service.py`

import 블록(@32, @55-60): `StockDetailLatestAnalysis` → `StockDetailDecisionHistory`, `stock_detail_latest_analysis_provider` → `stock_detail_decision_history_provider`.

`StockDetailProviders`(@441): 
```python
    latest_analysis: Provider = stock_detail_latest_analysis_provider
```
→
```python
    decision_history: Provider = stock_detail_decision_history_provider
```

`build_stock_detail` 본문의 `latest_analysis_task`(@490-494)를:
```python
    decision_history_task = _run_optional_block(
        "decision_history",
        providers.decision_history(market, resolved.symbol_db, db),
        warnings,
    )
```

`asyncio.gather` 언팩(@534-558)에서 `latest_analysis` → `decision_history`, `latest_analysis_task` → `decision_history_task` (같은 위치).

후처리 isinstance 변환(@578-581)을 교체:
```python
    if decision_history is not None and not isinstance(
        decision_history, StockDetailDecisionHistory
    ):
        decision_history = StockDetailDecisionHistory.model_validate(decision_history)
```

`StockDetailResponse(...)`(@711)에서 `latestAnalysis=latest_analysis,` → `decisionHistory=decision_history,`.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_stock_detail_service.py tests/test_invest_stock_detail_router.py -v`
Expected: PASS. `uv run ruff check app/services/invest_view_model/stock_detail_service.py`.

- [ ] **Step 5: 백엔드 회귀 + 커밋**

Run: `uv run pytest tests/test_stock_detail_service.py tests/test_stock_detail_providers.py tests/test_invest_stock_detail_schemas.py tests/test_invest_stock_detail_router.py tests/services/test_decision_history.py -q`
Expected: PASS (전부).

```bash
git add app/services/invest_view_model/stock_detail_service.py tests/test_stock_detail_service.py
git commit -m "feat(ROB-716): wire decisionHistory into build_stock_detail

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 프론트엔드 (타입 + DecisionHistoryCard + 테스트)

**Files:**
- Modify: `frontend/invest/src/types/stockDetail.ts` (@214-222 인터페이스 교체, @320 필드 교체)
- Modify: `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx` (`AnalysisCard`@425-444 → `DecisionHistoryCard`; 사용처 @600)
- Modify: `frontend/invest/src/__tests__/StockDetailPage.test.tsx` (@125 fixture 교체)

**Interfaces:**
- Consumes: `StockDetailResponse.decisionHistory` (Task 1 wire 형식).

- [ ] **Step 1: 타입 교체** — `types/stockDetail.ts`

`StockDetailLatestAnalysis`(@214-222)를 교체:
```typescript
export interface StockDetailDecisionHistoryPriorDecision {
  date: string | null;
  intent: string | null;
  side: string | null;
  decisionBucket: string | null;
  confidence: number | null;
  rationale: string | null;
}

export interface StockDetailDecisionHistoryOutcome {
  date: string | null;
  side: string | null;
  outcome: string | null;
  triggerType: string | null;
  pnlPct: number | null;
  realizedPnl: number | null;
}

export interface StockDetailDecisionHistoryOpenClaim {
  probability: number | null;
  horizon: string | null;
  reviewDate: string | null;
  direction: string | null;
  targetPrice: number | null;
}

export interface StockDetailDecisionHistoryBrier {
  n: number;
  meanBrier: number | null;
  flag: "ok" | "insufficient_sample";
}

export interface StockDetailDecisionHistory {
  symbol: string;
  market: string;
  linkQuality: string;
  priorDecisions: StockDetailDecisionHistoryPriorDecision[];
  priorLessons: string[];
  realizedOutcomes: StockDetailDecisionHistoryOutcome[];
  openClaims: StockDetailDecisionHistoryOpenClaim[];
  runningBrierSymbol: StockDetailDecisionHistoryBrier;
  runningBrierGlobal: StockDetailDecisionHistoryBrier;
  cautionLabel: string;
}
```

`StockDetailResponse`(@320) `latestAnalysis: StockDetailLatestAnalysis | null;` → `decisionHistory: StockDetailDecisionHistory | null;`.

- [ ] **Step 2: 카드 교체** — `StockDetailPage.tsx`

`AnalysisCard`(@425-444)를 교체:
```tsx
function DecisionHistoryCard({ data }: { data: StockDetailResponse }) {
  const dh = data.decisionHistory;
  return (
    <Card data-testid="stock-detail-decision-history">
      <h2 style={{ margin: "0 0 8px", fontSize: 16 }}>판단 이력</h2>
      {!dh ? (
        <p style={{ margin: 0, color: "var(--fg-3)" }}>이 종목의 과거 판단 기록이 없습니다.</p>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
            <span style={{ fontWeight: 700 }}>
              종목 Brier {dh.runningBrierSymbol.meanBrier == null ? "−" : dh.runningBrierSymbol.meanBrier.toFixed(2)}
            </span>
            <span style={{ color: "var(--fg-3)", fontSize: 12 }}>n={dh.runningBrierSymbol.n}</span>
            {dh.runningBrierSymbol.flag === "insufficient_sample" ? (
              <Pill tone="paper">표본 부족</Pill>
            ) : null}
          </div>

          {dh.priorDecisions.length > 0 ? (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 12, color: "var(--fg-3)", marginBottom: 4 }}>과거 판단</div>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
                {dh.priorDecisions.map((d, i) => (
                  <li key={`pd-${i}`} style={{ fontSize: 13 }}>
                    <Pill tone={d.side === "buy" ? "gain" : d.side === "sell" ? "loss" : "paper"}>{d.side ?? d.intent ?? "판단"}</Pill>
                    <span style={{ color: "var(--fg-3)", margin: "0 6px" }}>{d.date ?? "-"}</span>
                    {d.confidence != null ? <span style={{ color: "var(--fg-3)" }}>conf {d.confidence.toFixed(2)}</span> : null}
                    {d.rationale ? <div style={{ color: "var(--fg-2)" }}>{d.rationale}</div> : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {dh.realizedOutcomes.length > 0 ? (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 12, color: "var(--fg-3)", marginBottom: 4 }}>실현된 결과</div>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }}>
                {dh.realizedOutcomes.map((o, i) => (
                  <li key={`ro-${i}`} style={{ fontSize: 13, color: "var(--fg-2)" }}>
                    {o.date ?? "-"} · {o.side ?? "-"} · {o.outcome ?? "-"}
                    {o.pnlPct != null ? ` · ${o.pnlPct.toFixed(1)}%` : ""}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {dh.openClaims.length > 0 ? (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 12, color: "var(--fg-3)", marginBottom: 4 }}>진행중 예측</div>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }}>
                {dh.openClaims.map((c, i) => (
                  <li key={`oc-${i}`} style={{ fontSize: 13, color: "var(--fg-2)" }}>
                    {c.direction ?? "-"}
                    {c.probability != null ? ` P${c.probability.toFixed(2)}` : ""}
                    {c.targetPrice != null ? ` · 목표 ${Math.round(c.targetPrice).toLocaleString("ko-KR")}` : ""}
                    {c.reviewDate ? ` · ${c.reviewDate} 해소` : ""}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {dh.priorLessons.length > 0 ? (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 12, color: "var(--fg-3)", marginBottom: 4 }}>교훈</div>
              <ul style={{ margin: 0, paddingLeft: 18, color: "var(--fg-2)", fontSize: 13 }}>
                {dh.priorLessons.map((l, i) => <li key={`pl-${i}`}>{l}</li>)}
              </ul>
            </div>
          ) : null}

          <p style={{ margin: "6px 0 0", color: "var(--fg-3)", fontSize: 11 }}>{dh.cautionLabel}</p>
        </>
      )}
    </Card>
  );
}
```

사용처(@600) `<AnalysisCard data={data} />` → `<DecisionHistoryCard data={data} />`.

- [ ] **Step 3: 테스트 fixture 교체** — `StockDetailPage.test.tsx`

`latestAnalysis: { ... }` 블록(@125~)을 교체:
```tsx
  decisionHistory: {
    symbol: "000660",
    market: "kr",
    linkQuality: "symbol_window",
    priorDecisions: [
      { date: "2026-06-28", intent: "buy_review", side: "buy", decisionBucket: "new_buy_candidate", confidence: 0.7, rationale: "HBM 수요 지속" },
    ],
    priorLessons: ["과열 구간 추격 금지"],
    realizedOutcomes: [
      { date: "2026-06-20", side: "sell", outcome: "stop_loss", triggerType: "stop", pnlPct: -3.1, realizedPnl: -31000 },
    ],
    openClaims: [
      { probability: 0.7, horizon: "1w", reviewDate: "2026-07-10", direction: "up", targetPrice: 82000 },
    ],
    runningBrierSymbol: { n: 12, meanBrier: 0.18, flag: "ok" },
    runningBrierGlobal: { n: 4, meanBrier: null, flag: "insufficient_sample" },
    cautionLabel: "종목 기준 집계이며 특정 판단과 특정 결과의 직접 연결이 아닙니다.",
  },
```

이 파일에 `latestAnalysis` / `최근 분석` / `stock-detail-analysis`를 assert하는 부분이 있으면 새 카드 기준으로 교체:
```tsx
    expect(screen.getByTestId("stock-detail-decision-history")).toBeInTheDocument();
    expect(screen.getByText("판단 이력")).toBeInTheDocument();
    expect(screen.getByText("HBM 수요 지속")).toBeInTheDocument();
```

- [ ] **Step 4: 프론트 테스트 실행**

Run: `cd frontend/invest && npm test -- StockDetailPage`
Expected: PASS. 그리고 `cd frontend/invest && npx tsc --noEmit` — 타입 에러 없음(제거된 `StockDetailLatestAnalysis`/`latestAnalysis` 잔존 참조 0).

- [ ] **Step 5: 커밋**

```bash
git add frontend/invest/src/types/stockDetail.ts frontend/invest/src/pages/stock-detail/StockDetailPage.tsx frontend/invest/src/__tests__/StockDetailPage.test.tsx
git commit -m "feat(ROB-716): DecisionHistoryCard replaces dead latestAnalysis panel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 최종 검증 (grep 잔존 참조 + 전체 스위트)

**Files:** 없음(검증 전용). 필요 시 잔존 참조 정리.

- [ ] **Step 1: 죽은 참조 완전 제거 확인**

Run: `grep -rn "latestAnalysis\|StockDetailLatestAnalysis\|stock_detail_latest_analysis_provider\|stock-detail-analysis" app/ frontend/invest/src/`
Expected: 결과 없음(0 라인). 남아있으면 해당 파일 정리 후 재실행.

- [ ] **Step 2: `stock_analysis_results` 의존 제거 확인 (stock detail 경로)**

Run: `grep -rn "get_latest_analysis_by_symbol\|StockAnalysisService" app/services/invest_view_model/`
Expected: 결과 없음.

- [ ] **Step 3: 백엔드 관련 스위트**

Run: `uv run pytest tests/test_stock_detail_service.py tests/test_stock_detail_providers.py tests/test_invest_stock_detail_schemas.py tests/test_invest_stock_detail_router.py tests/test_stock_detail_capability_contract.py tests/services/test_decision_history.py -q`
Expected: PASS.

- [ ] **Step 4: lint / typecheck**

Run: `uv run ruff check app/ && cd frontend/invest && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 5: (검증 커밋 불필요 — 코드 변경 없으면 skip)**

---

## Self-Review

**Spec coverage:**
- 패널 교체(dead→live) → Task 1(스키마)·2(provider)·3(서비스)·4(프론트). ✓
- section-separated 렌더(과거판단/실현결과/진행중예측/Brier/교훈) → Task 4 Step 2. ✓
- 전 시장 실행 → Task 3(시장 게이트 없이 항상 `decision_history_task` 실행); `build_decision_context`가 시장 정규화. ✓
- `link_quality`/cautionLabel 정직 표기 → Task 1 default + Task 4 각주. ✓
- `recent_fills` 제외(YAGNI) → Task 2 명시. ✓
- migration 0 / read-only / LLM 무접촉 → Global Constraints; 신규 코드 DB write·broker·LLM import 없음. ✓
- 성공 기준(죽은 테이블 의존 제거) → Task 5 Step 1-2 grep 게이트. ✓

**Placeholder scan:** 모든 코드 스텝에 실제 코드 포함, TBD/TODO 없음. ✓

**Type consistency:** `decision_history`(provider/필드), `decisionHistory`(응답/프론트), `StockDetailDecisionHistory`(모델) — Task 1 정의와 Task 2/3/4 사용 일치. `_brier` snake→camel(`meanBrier`) 매핑이 Task 1 Brier 모델 필드와 일치. ✓
