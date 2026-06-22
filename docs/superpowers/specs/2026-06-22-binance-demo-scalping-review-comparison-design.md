# Binance Demo 스캘핑 리뷰 LLM vs 규칙 비교 (Phase 3 D-PR2 / Design)

- **작성일**: 2026-06-22
- **상태**: 승인됨 (브레인스토밍 → 스펙)
- **브랜치**: `feature/binance-demo-scalping-review-comparison`
- **선행**: Phase 2(일별 리뷰+벤치마크 자동화, #1345) + Phase 3 D-PR1(LLM 결정 주입 `session_tag="llm"`, #1346) merged

## 1. 배경 / 동기

D-PR1로 LLM 트레이드는 `scalp_trade_analytics.session_tag="llm"`로 태깅된다. 그러나 일별 리뷰/벤치마크 생성 경로(`build_draft` / `compute_and_store_daily_benchmark`)는 **`session_tag` 필터가 없어** 그날·product의 **모든 트레이드(규칙 NULL + llm)를 한 리뷰 행에 혼합 집계**한다(latent — D-PR1 이전엔 트레이드가 전부 NULL이라 무해했음). 결과적으로 "LLM이 규칙 baseline을 이기는가"를 측정할 수 없다.

`scalping_daily_reviews` 리뷰 grain은 `(review_date, product, account_scope, session_tag)`로 **이미 session_tag를 포함**하고, `/invest/scalping` read는 그 행들을 그대로 반환·직렬화(`sessionTag`)한다. 따라서 리뷰/벤치마크를 **session_tag별로 분리 생성**하기만 하면, LLM(`"llm"`)과 규칙 baseline(`""`)이 별도 리뷰 행으로 분리되어 나란히 비교된다.

## 2. 범위

### 2.1 In scope
- **rollup session_tag 필터** — `list_analytics`/`_rollup_for`/`build_draft`가 session_tag로 집계 분리(혼합 수정).
- **benchmark session_tag 필터** — `compute_and_store_daily_benchmark`(이미 session_tag 인자 보유)가 내부 analytics 조회도 tag로 필터.
- **tag 열거** — 신규 `list_session_tags(review_date, product)` distinct.
- **flow tag 순회** — `_refresh_with_session`가 product마다 `{""} ∪ distinct tags` 순회하여 (product, tag)별 리뷰+벤치마크 생성.
- **UI 라벨** — `/invest/scalping` 리뷰 카드에 sessionTag 표시 라벨 매핑(`""`→"규칙", `"llm"`→"LLM").

### 2.2 Out of scope
- 전용 side-by-side 비교 컴포넌트(net_return_bps 델타·승패 표) — 후속.
- Hermes 자율 실행.
- 스케줄러(규칙) 트레이드에 명시 tag 부여(외부 코드; NULL 유지하고 `COALESCE`로 처리).

## 3. 컴포넌트

### 3.1 rollup session_tag 필터 (`app/services/scalping_reviews/service.py`)
- `list_analytics(*, review_date, product, session_tag: str | None = None)` — `session_tag=None`이면 기존처럼 전체(무회귀); 값이 있으면 `WHERE COALESCE(session_tag,'') = :session_tag` 추가.
- `_rollup_for(review_date, product, session_tag: str | None = None)` — `list_analytics`에 전달.
- `build_draft(*, ..., session_tag="")` — 자기 `session_tag`를 `_rollup_for`에 전달(현재는 전달 안 함). **결과: 각 리뷰 행이 자기 tag 트레이드만 집계.**

### 3.2 benchmark session_tag 필터 (`app/services/brokers/binance/demo_scalping_exec/benchmark_runner.py`)
- `compute_and_store_daily_benchmark(*, ..., session_tag="")`의 내부 `service.list_analytics(review_date, product)` 호출에 `session_tag=session_tag` 추가 → tag별 notional-weighted buy&hold.

### 3.3 tag 열거 (`service.py`)
- 신규 `list_session_tags(*, review_date, product) -> list[str]` — 그날·product의 `SELECT DISTINCT COALESCE(session_tag,'')` (정렬). 트레이드 없으면 빈 리스트.

### 3.4 flow tag 순회 (`app/jobs/binance_demo_scalping_review.py`)
- `_refresh_with_session`가 product마다 `tags = sorted({""} | set(list_session_tags(review_date, product)))` (규칙 baseline `""` 항상 포함) 순회 → 각 (product, tag) `build_draft(session_tag=tag)` + `compute_and_store_daily_benchmark(session_tag=tag)`. per-(product, tag) `begin_nested()` SAVEPOINT 격리(기존 per-product 패턴 확장).
- summary는 (product, tag)별 항목.

### 3.5 UI 라벨 (`/invest/scalping` 프런트)
- 리뷰 카드 컴포넌트가 sessionTag를 표시 라벨로 매핑: `""`(또는 falsy)→"규칙", `"llm"`→"LLM", 기타→그대로. (백엔드 직렬화 `sessionTag`는 불변.)

## 4. 데이터 흐름

```
일별 flow → product마다:
  tags = sorted({""} ∪ distinct(COALESCE(session_tag,'')))   # 규칙 항상 + 발견된 llm 등
  for tag in tags:
    build_draft(session_tag=tag)                  # COALESCE(session_tag,'')=tag 만 집계
    compute_and_store_daily_benchmark(session_tag=tag)
→ /invest/scalping: (date,product)당 tag별 행 → "규칙" vs "LLM" 나란히 (net_return_bps + buy&hold 벤치마크)
```

## 5. NULL/"" 정합
- 스케줄러(규칙) 트레이드 = `session_tag NULL`; 리뷰 baseline grain = `""`. 필터를 `COALESCE(session_tag,'')`로 통일 → `""` 리뷰가 NULL(규칙) 트레이드를 집계, `"llm"` 리뷰가 `"llm"` 트레이드를 집계.
- **의도된 동작 변경(de-mixing)**: 기존 `""` 리뷰는 rule+llm 혼합 집계였으나 이제 rule만. 측정 정확화가 목적.

## 6. 에러 처리
- flow의 (product, tag)별 실패는 SAVEPOINT 격리로 다른 (product, tag)에 전파 안 됨(기존 per-product 패턴 유지). 실패 항목은 summary에 에러 표기.
- 트레이드 없는 날: `list_session_tags`=빈 → `tags={""}` → `""` 리뷰만 생성(빈 rollup), 기존 Phase 2 무회귀.
- `session_tag=None`(기존 호출자) → 필터 미적용 전체 집계(무회귀).

## 7. 테스트
- **rollup 필터**: 같은 날·product에 rule(NULL)+llm 트레이드 혼재 → `build_draft(session_tag="")`=rule만, `build_draft(session_tag="llm")`=llm만 집계. `session_tag=None`이면 전체(무회귀).
- **list_session_tags**: NULL→"" coalesce + distinct + 정렬; 빈 날 빈 리스트.
- **flow**: 발견 tag별 리뷰+벤치마크 생성, `""` 항상 포함, 빈 날도 `""` 생성(무회귀), (product,tag) 격리.
- **benchmark 필터**: tag별 notional만으로 가중(혼합 아님).
- **UI 라벨**: sessionTag 매핑(""→규칙, llm→LLM) 단위 테스트(프런트 vitest).

## 8. 위험 / 함정
- **de-mixing 동작 변경**: 기존 `""` 리뷰 숫자가 바뀐다(llm 제외). 의도된 수정이나 명시.
- **무회귀**: `session_tag=None` 기본 경로(다른 호출자) 불변; 빈 날 `""` 리뷰 항상 생성.
- **`COALESCE` 일관**: 필터·distinct 양쪽 모두 `COALESCE(session_tag,'')` 동일 적용(NULL/"" 분기 방지).
- **마이그레이션 없음**(session_tag는 기존 컬럼).

## 9. 산출물 / 완료 기준
- `list_analytics`/`_rollup_for`/`build_draft` session_tag 필터 + `list_session_tags` + benchmark_runner 필터 + flow tag 순회 + UI 라벨.
- 테스트 green(rollup 분리, distinct, flow per-tag, benchmark 분리, UI 라벨, 무회귀).
- 마이그레이션 없음. 전용 비교 컴포넌트는 후속.
