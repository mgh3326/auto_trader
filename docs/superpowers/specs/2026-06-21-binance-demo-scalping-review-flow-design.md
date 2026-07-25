# Binance Demo 스캘핑 일별 리뷰+벤치마크 자동화 (Phase 2 / Design)

- **작성일**: 2026-06-21
- **상태**: 승인됨 (브레인스토밍 → 스펙)
- **브랜치**: `feature/binance-demo-scalping-review-flow`
- **선행**: Phase 1(변경 A/B) merged·라이브 (PR #1342); 트레이딩 틱은 외부 Prefect(robin-prefect-automations)가 이미 매일 kick 중

## 1. 배경 / 동기

Binance USD-M Futures Demo "매일 자동 모의루프"에서 **트레이딩 틱은 이미 자동**(스케줄러/외부 Prefect)이지만, **일별 리뷰 draft + buy&hold 벤치마크는 자동 생성되지 않는다**(스케줄러는 거래만 돌리고 `ScalpingReviewService.build_draft` / `benchmark_runner`를 호출하지 않음). 그래서 `/invest/scalping`에 "전략 net vs 패시브(buy&hold)"가 매일 자동으로 채워지지 않는다.

Phase 2 = **이 측정/리뷰 자동화**. recurrence는 **Prefect로 통일** — 트레이딩 틱이 이미 Prefect에 있으므로, 리뷰/벤치마크도 Prefect flow로 두어 **스캘핑 루프 전체(거래+측정)를 하나의 control plane에서 스케줄·관측·일시정지**할 수 있게 한다. (읽기전용 리뷰는 in-repo TaskIQ cron도 가능하나, 루프 응집/단일 가시성을 위해 Prefect 채택.)

## 2. 범위

**In scope:** 매일 자동으로 데모 product별 (1) 리뷰 draft 롤업 + (2) buy&hold 벤치마크를 생성·저장하는 **Prefect flow** 추가. 기존 `app/flows/*_flow.py` 패턴(env-gate default-off, 배포 등록은 외부/deferred) 준수.

**Out of scope:**
- 트레이딩 틱 (외부 Prefect, 무변경)
- verdict/회고 자동 초안 (Phase 3)
- LLM 시그널 (Phase 3)
- **실제 Prefect 배포 등록** — robin-prefect-automations(외부 레포), operator, paused-by-default (이 PR 아님)
- UI 변경 (`/invest/scalping`은 이미 `benchmarkReturnBps` 노출 — Phase 1에서 완료)

## 3. 컴포넌트

### 3.1 신규: `app/flows/binance_demo_scalping_review_flow.py`

기존 `invest_screener_snapshots_us_flow.py` 패턴(순수 helper + `@task` + `@flow` + dict 반환)을 따른다.

```python
async def run_demo_scalping_review_refresh(
    *,
    review_date: dt.date | None = None,        # 기본: 현재 UTC date
    products: Sequence[str] = ("spot", "usdm_futures"),
    now: dt.datetime | None = None,
) -> dict[str, Any]
```

- **게이트**: `settings.binance_demo_scalping_review_flow_enabled` False면 즉시 `{"status": "disabled"}` 반환(작업 0, DB 쓰기 0).
- 활성 시:
  - `now` 기본 = 현재 UTC(`dt.datetime.now(dt.UTC)`), `review_date` 기본 = `now.date()`.
  - `market_data = DemoScalpingMarketData()` 1회 생성, `async with AsyncSessionLocal() as session:` 1회.
  - **product별 루프** (try/except 격리):
    - `service.build_draft(review_date=..., product=p, now=now)` (그날 `scalp_trade_analytics` → `scalping_daily_reviews` 롤업)
    - `await compute_and_store_daily_benchmark(session=session, market_data=market_data, review_date=..., product=p, now=now)` (1d klines → `benchmark_return_bps`)
    - product 결과 요약(trade_count / net_return_bps / benchmark_return_bps) 수집; 예외는 errors에 기록하고 다음 product 계속.
  - `await session.commit()`; `finally: await market_data.aclose()`.
  - 반환: `{"status": "ran", "reviewDate": ..., "products": [...요약...], "errors": [...]}`.
- `@task(name="binance_demo_scalping_review")` + `@flow(name="binance_demo_scalping_review")` 래퍼.

### 3.2 `app/core/config.py`

신규 설정 필드:
```python
binance_demo_scalping_review_flow_enabled: bool = False
```
(env `BINANCE_DEMO_SCALPING_REVIEW_FLOW_ENABLED`, pydantic case-insensitive. 기본 False = default-off.)

### 3.3 (이 PR 아님) Prefect 배포 등록

robin-prefect-automations에 deployment 등록, **paused-by-default**. **권장 cron: 매일 ~23:55 UTC** (그날 거래 완료 + 1d 캔들 거의 완성 시점 반영). 운영자가 (a) `BINANCE_DEMO_SCALPING_REVIEW_FLOW_ENABLED=true` 설정 + (b) deployment unpause 시 자동 동작.

## 4. 데이터 흐름

```
[외부 Prefect cron ~23:55 UTC] → binance_demo_scalping_review_flow
   → [게이트 off → {"status":"disabled"} no-op]
   → 게이트 on: 각 product(spot, usdm_futures):
        build_draft  (scalp_trade_analytics 그날 롤업 → scalping_daily_reviews)
        benchmark    (DemoScalpingMarketData 1d klines → benchmark_return_bps)
   → /invest/scalping 에서 전략 net_return_bps vs benchmark_return_bps 자동 표시
```

## 5. 경계 / 안전

- flow는 `DemoScalpingMarketData` + `ScalpingReviewService` + `benchmark_runner`를 import — **flow/worker 컨텍스트라 허용**(`/invest/api/scalping` 라우터 정적 import-guard와 무관; flow는 그 라우터가 아님).
- **브로커/주문 mutation 없음**: `scalp_trade_analytics`(읽기) + klines(읽기) + `scalping_daily_reviews`/`scalping_reviews` 쓰기만.
- **default-off**: env flag 미설정 시 no-op. 실제 recurrence는 외부 deployment unpause(operator)까지 비활성.
- product별 try/except 격리: 한 product 실패가 다른 product를 막지 않음.
- 벤치마크는 `benchmark_runner`가 이미 market-data 실패 시 None(best-effort) — 리뷰는 전략 net만으로 정상.

## 6. 에러 처리

- 게이트 off → `{"status": "disabled"}`.
- product별 예외 → 해당 product 요약 대신 `errors`에 `{product, error}` 기록, 나머지 product 계속, flow는 성공 반환(부분 성공).
- `market_data.aclose()`는 finally(생성 실패 시에도 안전).
- DB 오류(build_draft/commit)는 전파(데이터 정합 보호) — best-effort 대상은 market-data fetch뿐.

## 7. 테스트

- **게이트 off**: `binance_demo_scalping_review_flow_enabled=False` → `{"status":"disabled"}`, `scalping_daily_reviews` 행 0 (DB 무변경).
- **게이트 on (fake market_data + db_session + seeded `scalp_trade_analytics`)**: product별 `scalping_daily_reviews` 행 생성 + `net_return_bps` + `benchmark_return_bps` 채워짐. 반환 dict에 product별 요약.
- **product 격리**: 한 product의 market_data fetch가 raise → 그 product benchmark NULL(또는 errors 기록)이지만 다른 product는 정상 완료.
- **review_date 기본값**: `now` 미지정 시 현재 UTC date로 롤업.
- (순수 단위) `run_demo_scalping_review_refresh`를 직접 호출하는 테스트(Prefect flow 런타임 없이 helper 단위 테스트 — 스크리너 flow의 `run_*` helper 테스트 패턴).

## 8. 위험 / 함정

- **중복 스케줄 금지**: in-repo TaskIQ cron으로도 만들지 말 것(Prefect로 통일). flow에 `@broker.task(schedule=...)` 부착 금지.
- **read-only 보장**: flow가 실수로 주문 경로(executor/execution_client)를 import하지 않도록 — `benchmark_runner`/`service`/`market_data`만.
- **시점**: review_date=현재 UTC date이므로, cron이 UTC 일경계 직전(~23:55)이 아니면 "그날" 데이터가 부분적일 수 있음 — 권장 cron 준수 또는 operator가 prior-day로 `review_date` 지정 가능.
- **default-off**: 배포만으로는 안 돎(env flag + 외부 deployment unpause 필요) — 의도된 안전장치.

## 9. 산출물 / 완료 기준

- `app/flows/binance_demo_scalping_review_flow.py` (helper + task + flow) + `config.py` flag.
- 게이트 off no-op / 게이트 on product별 리뷰+벤치마크 생성 / product 격리 테스트 green.
- 마이그레이션 없음(Phase 1에서 `benchmark_return_bps` 컬럼 이미 추가됨).
- 배포 등록(외부, paused)은 operator 후속 — 런북/Linear 코멘트로 활성화 절차 안내.
