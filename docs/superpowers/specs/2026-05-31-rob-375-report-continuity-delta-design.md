# ROB-375 — 리포트 연속성/델타 레이어 버그 3건 설계

- **이슈:** ROB-375 (Bug, Medium). `market="us"`, `account_scope="kis_live"`. ROB-287 계열.
- **분리:** 기능성 항목(advisory watch/decision → triggered_events, intraday 델타 리포트 타입/API)은 ROB-376으로 분리됨. 본 작업은 **조회/저장 버그만** 다룬다.
- **패키징:** 3개 독립 PR(슬라이스), 우선순위 순서 **Slice 1(Bug 3) → Slice 2(Bug 1) → Slice 3(Bug 2)**.

## 공통 원칙 / 제약

- 순수 조회·저장 버그만 수정. ROB-376 기능 영역(watch/decision 기록, intraday_update 타입)은 손대지 않는다.
- **마이그레이션 0건** — 관련 컬럼(`market_snapshot`/`portfolio_snapshot` JSONB, `status`)은 이미 존재.
- read-mostly. 어떤 경로도 broker/order/watch mutation에 도달하지 않는다.
- **하위호환:** 기존 기본 동작을 보존. 새 동작은 전부 opt-in 파라미터(기본값 = 현행 동작).

---

## Slice 1 — Bug 3: `get_trade_journal` opt-in 라이브 enrich (최우선)

### 문제
`app/mcp_server/tooling/trade_journal_tools.py:302-307` — `current_price`/`pnl_pct_live`/`target_reached`/`stop_reached`가 하드코딩 `None`("too slow for bulk queries"). `target_price`/`stop_loss`는 저널 행에 존재하지만 라이브 판정을 못 해 가장 강한 델타 신호("목표/손절 터치")를 못 뽑음. summary의 `near_target`/`near_stop`도 항상 0.

### 변경
- `get_trade_journal(...)`에 파라미터 `enrich_live: bool = False` 추가.
- `enrich_live=True`일 때만 entry별 라이브 시세 조회·계산. `False`(기본)면 현행과 100% 동일(필드 null 유지).
- **시세 소스:** `app/services/market_data/service.py::get_quote(symbol, market)` 재사용. `market`은 `instrument_type`에서 파생:
  - `equity_us → "us"`, `equity_kr → "kr"`, `crypto → "crypto"`.
- **계산 (side 고려):**
  - `current_price` = quote.price
  - `pnl_pct_live` = `(current - entry_price) / entry_price * 100` (entry_price 존재 시; side가 sell/short면 부호 반전)
  - `target_reached`: long이면 `target_price is not None and current >= target_price`; short면 `current <= target_price`
  - `stop_reached`: long이면 `stop_loss is not None and current <= stop_loss`; short면 `current >= stop_loss`
  - summary `near_target`/`near_stop`: 임계 근접(목표/손절 가격의 ±1.5% 이내)인 active entry 카운트
- **안전/graceful:** quote 조회 실패·심볼 미해석·entry_price 없음 → 해당 entry 필드는 null로 두고 계속 진행(전체 호출 실패 금지). enrich는 조회된 journals(이미 `limit`로 bounded)에 대해서만 수행.

### 영향 범위
- `app/mcp_server/tooling/trade_journal_tools.py` (핸들러 + 필요한 헬퍼)
- `app/mcp_server/tooling/trade_journal_registration.py` (docstring/파라미터 노출)

### 테스트
- quote stub으로: target_reached True/False, stop_reached True/False, near_target/near_stop 집계, side=sell 부호/판정 반전, quote 실패 graceful(필드 null + success True), `enrich_live=False` 시 현행 동일.

---

## Slice 2 — Bug 1: `investment_report_context_get`의 draft 제외로 컨텍스트가 항상 빈 배열

### 문제
`app/services/investment_reports/query_service.py:275`:
```python
prior_reports = [r for r in prior_reports if r.status != "draft"]
```
ROB-352 Slice B에서 smoke boilerplate(항상 draft) 제거 목적으로 추가됨. 그러나 advisory 플로우는 리포트가 **항상 `status="draft"`**(publish 단계 없음, `status` 서버 기본값 `'draft'`)라 이 필터가 prior_reports를 통째로 비우고, 이어서 `unresolved_deferred_items`/`active_watches`/`triggered_events`/`recent_decisions`(모두 prior_report_ids 의존)까지 연쇄로 빈 배열이 됨.

### 변경 — `include_draft` 파라미터 (결정됨)
- `ReportQueryService.previous_report_context(...)`에 `include_draft: bool = False` 추가.
  - `include_draft=False`(기본): 현행대로 `status != "draft"` 필터 적용(smoke-safe).
  - `include_draft=True`: draft 제외 필터를 건너뛰어 최신 draft도 prior로 인정.
- `exclude_report_uuid` 제외와 `[:n_prior]` 슬라이스는 그대로. `_DRAFT_FETCH_BUFFER` 버퍼 로직 유지(include_draft=True에서는 불필요하지만 무해).
- MCP 도구 `investment_report_context_get`(`app/mcp_server/tooling/investment_reports_handlers.py`)에 `include_draft` 파라미터를 노출하고 `previous_report_context`로 전달. docstring에 advisory(항상-draft) 연속성 용도임을 명시.

### 스코프 경계
- prior_reports를 비우던 근본만 수정. `active_watches`/`triggered_events`/`recent_decisions`의 **내용 채움**은 ROB-376 기능 영역 — 본 슬라이스는 prior_reports가 비지 않게 만들어 배선이 동작하게만 함.

### 영향 범위
- `app/services/investment_reports/query_service.py`
- `app/mcp_server/tooling/investment_reports_handlers.py`

### 테스트
- 전부 draft인 prior 집합 + `include_draft=True` → prior_reports 비지 않음; `include_draft=False`(기본) → 현행대로 빈 배열.
- 혼합(draft + active) → False는 active만, True는 둘 다(최신순 n_prior).
- `exclude_report_uuid`가 draft를 제외해도 정상.

---

## Slice 3 — Bug 2: snapshot-backed report row의 `market_snapshot`/`portfolio_snapshot`이 빈/포인터-only

### 문제
`app/services/action_report/snapshot_backed/generator.py:528-569` `_section_snapshot_descriptors`는 ROB-352 Slice B 원칙("pointer not payload")대로 **포인터 descriptor**(`snapshot_uuid`/`as_of`/`freshness`/`coverage`)만 기록하고 numeric payload는 안 뽑음. 결과적으로 델타 계산용 개장 베이스라인(지수·NAV·USD현금·비중·종목 종가)을 report row만으로 회수 불가 → 번들로 우회 필요. 구 수동 리포트(6d805c85)는 `portfolio_snapshot{usd_cash, usd_orderable, unrealized_pl_pct, concentration_notes}`를 직접 동결 저장했었음.

### 사전 조사 (구현 1단계)
- 재현 행(dfda9a04·7004e783)이 `{}`(빈)인지, descriptor(`{"status":"unavailable",...}`)인지 실제 write 경로 확인. descriptor 코드상 최소 unavailable dict가 들어가야 하므로 `{}`는 (a) Slice B 이전 행이거나 (b) 다른 생성 경로(mock_preview 등) 가능성. 어느 경로가 `market_snapshot`/`portfolio_snapshot`을 비우는지 특정한 뒤 설계 확정.

### 변경 — descriptor 유지 + numeric baseline 동결
- pointer/freshness descriptor는 **그대로 유지**(Slice B 원칙 보존). 거기에 델타용 **소수 numeric baseline**을 추가.
- bundle item의 `snapshot.payload_json`(generator는 이미 line 607 등에서 접근)에서 market/portfolio kind payload를 읽어 작은 화이트리스트 필드만 추출:
  - **market:** 주요 지수 값(가능 시), as_of.
  - **portfolio:** `usd_cash`, `usd_orderable`, `unrealized_pl_pct`, `concentration_notes`, (가능 시) per-symbol 종가/평가액.
- 저장 구조(예): `market_snapshot = {"provenance": <descriptor>, "baseline": <numeric subset>}` / `portfolio_snapshot` 동일. 정확한 키 네이밍은 기존 소비자(있다면)와의 호환을 조사 후 확정.
- payload에 해당 numeric이 없으면 baseline은 부분/빈으로 두되 fabricate 금지(없는 값 만들지 않음).

### 스코프 경계
- 전체 payload 복사 금지(Slice B 위반). 델타에 필요한 화이트리스트 numeric만.
- 마이그레이션 없음(JSONB 컬럼 재사용).

### 영향 범위
- `app/services/action_report/snapshot_backed/generator.py` (+ 필요 시 mock_preview/runner 경로)

### 테스트
- market/portfolio payload가 있는 번들 → row의 baseline에 numeric 동결, provenance 유지.
- payload에 numeric 없음 → fabricate 없이 부분/빈 baseline.
- 재현 경로 회귀(빈 `{}`가 더 이상 나오지 않음 확인).

---

## 통합 검증 (각 슬라이스 공통)

- `ruff check app/ tests/` + `ruff format --check app/ tests/` + import guards.
- 관련 단위 테스트 통과, Test 워크플로 green 확인 후 머지(pre-merge full-CI gate).
- 운영 활성화·라이브 render smoke는 operator-gated(별도). 본 작업 범위는 코드 + 테스트.
