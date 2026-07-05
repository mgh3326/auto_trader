# ROB-716 — 종목상세 판단→결과 학습루프 노출 (decision_history surface)

- **Date**: 2026-07-05
- **Linear**: ROB-716 (Medium, blocked by ROB-711 → **해소**: ROB-711 merged `99508870`)
- **Branch**: `rob-716`
- **Related**: ROB-715 (웹 형제), ROB-692 (추천 카드, merged), ROB-705 (correlation_id 스파인), ROB-490 (dead writer), ROB-714 (place-time provenance)

## 문제

종목상세 "최근 분석"(`AnalysisCard`) 패널은 front~back 완성돼 있으나 **죽은 저장소 `stock_analysis_results`를 읽어 비어있다**. 이 테이블은 openclaw HTTP 콜백/research pipeline만 쓰고 오퍼레이터 경로엔 안 돎(ROB-490 확정). 오퍼레이터의 실제 분석은 `investment_report_items` + `trade_forecasts` + `trade_retrospectives`(review 스키마)에 적재된다.

오퍼레이터 요청(2026-07-05): "종목상세에서 분석할 때마다 분석 데이터가 잘 적재되고, 매수/매도 근거(뉴스 포함)와 맞았는지 평가가 보였으면 한다."

## 접근

죽은 "최근 분석" 패널을 **라이브 학습루프 소스로 교체**한다. 데이터는 ROB-711이 이미 배선한 `build_decision_context(db, symbol, market)`(`app/services/decision_history.py`)를 **그대로 재사용** — join 로직 재구현 없음(ROB-711 노트가 경고한 "세 번째 병렬 저장소" 발산 회피).

기존 `build_stock_detail` provider 패턴에 **provider 1개 + 응답 필드 1개 + 프론트 카드 1개**를 추가하고, 죽은 `latestAnalysis` 계열(provider·스키마·프론트 타입·카드)을 제거한다.

### 결정 사항 (brainstorm)

1. **패널 처리 = 교체.** `AnalysisCard`(`stock_analysis_results`)를 제거하고 그 슬롯에 `DecisionHistoryCard`(판단 이력)를 렌더. `stock_analysis_results` 의존 완전 제거(성공 기준 직결).
2. **결과 표현 = 섹션 분리 (정직).** `report_item_uuid`가 ledger/forecast/retro에 ~0% 채워져 `link_quality="symbol_window"` — 특정 판단↔특정 결과 직접 join 불가. 따라서 개별 판단 행에 `✓적중/✗빗나감` 인라인 배지를 달지 **않고**, `build_decision_context` payload를 섹션으로 렌더(과거 판단 / 실현된 결과 / 진행중 예측 / 종목 Brier / 교훈). "symbol 기준 집계" 명시.
3. **시장 = 전 시장(kr/us/crypto).** `build_decision_context`가 시장별 symbol 정규화 + 무신호 None 처리. 추가 비용 미미(인덱스된 쿼리 몇 개).

### 안전/제약

- Migration 0, read-only. 브로커/주문/감시 무접촉. LLM 무접촉(ROB-501 결정론).
- `latestAnalysis`는 프론트 view-model(`StockDetailResponse`) + 프론트 페이지 외 소비처 없음(MCP/타 백엔드 없음) — 제거 blast radius 국소.

## 컴포넌트

### 1. Provider — `app/services/invest_view_model/stock_detail_providers.py`

신규 `stock_detail_decision_history_provider(market, symbol, db)`:
- `hasattr(db, "execute")` 가드 → `build_decision_context(db, symbol, market)` 호출.
- 반환 dict(snake_case) → `StockDetailDecisionHistory`(camelCase) 매핑. `None`이면 그대로 `None`(무신호 = 패널 빈 상태).

제거: `stock_detail_latest_analysis_provider`, `_reasons_top3`, `StockAnalysisService` import, `StockDetailLatestAnalysis` import, `__all__`의 `stock_detail_latest_analysis_provider`.

### 2. 스키마 — `app/schemas/invest_stock_detail.py`

제거: `StockDetailLatestAnalysis`, `StockDetailResponse.latestAnalysis`.

신규(모두 `extra="forbid"`):

- `StockDetailDecisionHistoryPriorDecision`: `date: str | None`, `intent: str | None`, `side: str | None`, `decisionBucket: str | None`, `confidence: float | None`, `rationale: str | None`
- `StockDetailDecisionHistoryOutcome`: `date`, `side`, `outcome`, `triggerType`, `pnlPct`, `realizedPnl` (모두 `| None`)
- `StockDetailDecisionHistoryOpenClaim`: `probability`, `horizon`, `reviewDate`, `direction`, `targetPrice` (모두 `| None`)
- `StockDetailDecisionHistoryBrier`: `n: int`, `meanBrier: float | None`, `flag: Literal["ok", "insufficient_sample"]`
- `StockDetailDecisionHistory`: `symbol: str`, `market: str`, `linkQuality: str`, `priorDecisions: list[...]`, `priorLessons: list[str]`, `realizedOutcomes: list[...]`, `openClaims: list[...]`, `runningBrierSymbol: StockDetailDecisionHistoryBrier`, `runningBrierGlobal: StockDetailDecisionHistoryBrier`, `cautionLabel: str = "종목 기준 집계이며 특정 판단과 특정 결과의 직접 연결이 아닙니다."`
- `StockDetailResponse.decisionHistory: StockDetailDecisionHistory | None = None`

**YAGNI 컷**: `recent_fills`(payload에 있으나) 제외 — 체결 내역은 페이지의 주문 카드(ROB-559 by-symbol order history)가 이미 노출.

### 3. 서비스 배선 — `app/services/invest_view_model/stock_detail_service.py`

- `StockDetailProviders`의 `latest_analysis` 필드 → `decision_history: Provider = stock_detail_decision_history_provider`로 교체.
- `latest_analysis_task` → `decision_history_task = _run_optional_block("decision_history", providers.decision_history(market, resolved.symbol_db, db), warnings)` (전 시장 실행, 실패 격리).
- `asyncio.gather` 언팩 + 후처리 isinstance 변환(`StockDetailDecisionHistory.model_validate`) 교체.
- `StockDetailResponse(... decisionHistory=decision_history ...)`, `latestAnalysis=` 제거.
- import 정리(`StockDetailLatestAnalysis` 제거, 신규 `StockDetailDecisionHistory` 추가).

### 4. 프론트 — `frontend/invest/src/`

- `types/stockDetail.ts`: `StockDetailLatestAnalysis` 제거, `StockDetailResponse.latestAnalysis` 제거. 신규 `StockDetailDecisionHistory` 계열 인터페이스 + `decisionHistory: StockDetailDecisionHistory | null`.
- `pages/stock-detail/StockDetailPage.tsx`: `AnalysisCard` → `DecisionHistoryCard`(제목 "판단 이력"). 섹션 렌더:
  1. **종목 Brier**: `runningBrierSymbol` — `mean_brier` + `n`, `flag==="insufficient_sample"`면 배지.
  2. **과거 판단**: `priorDecisions` — side/intent Pill + confidence + rationale(truncated).
  3. **실현된 결과**: `realizedOutcomes` — date/side/outcome/pnlPct.
  4. **진행중 예측**: `openClaims` — probability/direction/targetPrice/reviewDate.
  5. **교훈**: `priorLessons` 리스트.
  6. `cautionLabel` 각주.
  - `decisionHistory == null`이면 "이 종목의 과거 판단 기록이 없습니다."
- `__tests__/StockDetailPage.test.tsx`: `latestAnalysis` fixture → `decisionHistory` fixture 교체.

## 테스트

- **Provider 단위**: `build_decision_context` 반환 dict → `StockDetailDecisionHistory` 매핑 검증(모든 섹션); `None` 반환 시 `None`; `db` 무 execute 시 `None`.
- **서비스**: `decisionHistory`가 응답에 배선됨; provider 예외 시 `decision_history_unavailable` warning + 페이지 렌더 유지; 전 시장(kr/us/crypto) 실행.
- **스키마**: `extra="forbid"` 계약; `flag` Literal; Brier round-trip.
- **프론트**: 섹션 렌더(과거 판단/결과/예측/교훈/Brier); 빈 상태; `insufficient_sample` 배지.

## 성공 기준

- 종목상세에서 해당 심볼의 과거 판단(근거/confidence) + 예측 결과(실현/미해결) + 회고 lesson이 클릭 없이 보인다.
- 죽은 `stock_analysis_results` 의존 제거 — 재분석 심볼 80%+에서 패널이 비어있지 않다.

## 참고

- `docs/superpowers/specs/2026-07-05-symbol-analysis-capture-design.md` (write-loop 부분 SUPERSEDED; triggers 모델·Brier 채점·correlation_id 정합 근거만 유효).
