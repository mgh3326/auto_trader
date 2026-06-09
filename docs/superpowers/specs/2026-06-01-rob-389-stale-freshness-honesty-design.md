# ROB-389 — stale 스냅샷이 `fresh`로 라벨되는 문제 차단 설계

- **이슈:** ROB-389 (오케스트레이션 ROB-394의 2번)
- **날짜:** 2026-06-01
- **상태:** 설계 승인됨 → 구현 계획 대상
- **base:** `origin/main` `79e53c1c` (ROB-388 머지 직후)

## 목표

2026-06-01 NXT 개장 리포트 작업 중, stale screener/momentum 데이터가 `fresh`로 표기되어 운영자/리포트를
오도했다. `get_momentum_candidates(market="kr")`가 2.5주 전(`trading_date=2026-05-13`) 데이터를
`data_state:"fresh"`로 반환했다. stale을 정직하게 라벨링하고 경과일·진단을 표면화한다.

**범위:** Medium (Bug A+B+C+D). 리프레시 파이프라인 근본원인은 진단 로그만 추가하고 코드 트리거는 넣지
않는다(운영/데이터 영역).

## 증상 (재확인)

* `get_momentum_candidates(market="kr")`: `data_state:"fresh"`인데 `trading_date:"2026-05-13"`,
  `latest_snapshot_at:2026-05-13` → 2.5주 전 데이터를 `fresh`로 표기.
* 동일 스냅샷이 invest_report 번들 `candidate_universe`를 오염: `data_health: fresh_count=0,
  stale_count=11638`. (이 경로는 이미 stale로 보고하지만 경과일/진단이 부족.)

## 코드 현황 (근거)

* `app/mcp_server/tooling/momentum_candidates.py:68` —
  `"data_state": "fresh" if rows else "missing"`. **동어반복**: 행이 있기만 하면 `trading_date`가
  얼마나 오래됐든 `fresh`. `classify_state`/baseline 비교를 전혀 안 함. → **주 버그.**
* `app/services/action_report/snapshot_backed/collectors/candidate_universe.py` —
  `coverage()` → `_classify_usefulness`(actionable=fresh_count) → `_FRESHNESS_BY_USEFULNESS`로
  `fresh_count=0, stale>0` → `stale_only` → `stale` + `_missing_data`(`cap 40`). **이미 정직하게
  stale 보고** (증상 리포트의 confidence cap 40이 증거). 단, 경과일/baseline 미노출.
* `candidate_universe._collect_top_gainers:300` — `coverage(today_trading_date=now.date())`로
  **raw UTC date** 전달(세션 비인식). 주말/자정 롤오버에서 baseline 오분류 가능.
* `app/services/invest_screener_snapshots/repository.py:92` `coverage()` —
  fresh=`snapshot_date == today_trading_date`, stale=`snapshot_date < today_trading_date`.
  baseline은 호출자가 결정.
* `app/services/invest_screener_snapshots/freshness.py` — `expected_baseline_date(market, now)`
  (kr→`expected_kr_baseline_date`, us→`expected_us_baseline_date`, else→`today_trading_date`),
  `classify_state`, `classify_investor_flow_partition`(closes_window 없는 패턴). trading-date
  로직의 단일 소스(ROB-277 §D4).
* `app/services/invest_momentum_events/repository.py` — `MomentumCandidateSignal`은
  `trading_date: dt.date` + `latest_snapshot_at: dt.datetime` 보유. `list_candidate_signals`는
  최신 스냅샷의 단일 `trading_date` 행들을 반환 → `rows[0].trading_date`가 최신 거래일.
* crypto coverage(`InvestCryptoScreenerSnapshotsRepository.coverage(today=...)`)는 24/7 시장이라
  `now.date()`가 적절 — 세션 롤백을 적용하면 안 됨.

## 설계

### 변경 1 — momentum_candidates 동어반복 제거 (Bug A, 주 버그)

`momentum_candidates.py`의 `get_momentum_candidates_impl`에서 `data_state`를 실제 trading_date 기반으로
분류:

* 빈 행 → `missing` (기존 `empty_reason` 유지).
* `rows[0].trading_date` vs `expected_kr_baseline_date(now)`:
  * `latest >= expected` → `fresh`
  * `latest < expected` → `stale`
* 응답에 `expected_baseline_date`(ISO), `latest_trading_date`(ISO), `days_stale`(int) 추가.
* `now`는 `dt.datetime.now(dt.UTC)`를 분류 시점에 사용.

### 변경 2 — freshness 분류 헬퍼 (Bug A 지원)

`freshness.py`에 순수 헬퍼 추가(trading-date 로직 단일 소스 유지):

```python
def classify_momentum_freshness(
    *, latest_trading_date: dt.date, now: dt.datetime
) -> tuple[DataState, int]:
    """(state, days_stale) for a KR momentum partition.

    fresh  when latest_trading_date >= expected_kr_baseline_date(now)
    stale  otherwise; days_stale = (expected - latest).days (>=1)
    Callers handle the empty-rows -> 'missing' case before calling this.
    """
```

`days_stale`는 fresh일 때 0, stale일 때 `(expected - latest).days`(calendar days, 최소 1).

### 변경 3 — coverage 세션 인식 (Bug B)

`candidate_universe._collect_top_gainers`(equity kr/us)가 `coverage()`에 넘기는 baseline을
`now.date()` → `expected_baseline_date(request.market, now=now)`로 교체.

* **crypto 경로(`_collect_crypto`)는 불변** — `coverage(today=now.date())` 유지(24/7).

### 변경 4 — 경과일/baseline 페이로드 노출 (Bug C)

`candidate_universe`의 두 빌더(`_build_candidate_result`, `_build_preset_candidate_result`)
페이로드에 추가:

* `expected_baseline_date`(ISO 또는 None)
* `latest_partition_date`(ISO 또는 None) — `last_computed_at`과 별개로 데이터 거래일.
* `days_stale`(int) — `expected_baseline_date`와 `latest_partition_date` 차이(없으면 0).

stale일 때 `_missing_data`의 `what` 메시지에 경과일 반영:
`"{market_ko} 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 ({days_stale}일 지연, stale)."`

> preset 경로(`_build_preset_candidate_result`)는 `last_computed_at=None`이고 per-state 기반이라
> `latest_partition_date`를 별도 조회하지 않고 `expected_baseline_date`만 노출, `days_stale`은
> 계산 불가 시 0으로 둔다(JSON 추가만, 거짓값 금지).

### 변경 5 — 리프레시 갭 진단 로그 (Bug D)

equity coverage가 `fresh_count == 0 and stale_count > 0`을 반환하면 콜렉터에서 구조화
`logger.warning`:

```
"candidate_universe refresh gap: market=%s expected_baseline=%s latest_partition=%s "
"stale_count=%d (snapshot build did not produce a partition for the expected baseline)"
```

**스케줄러/트리거/백필 없음.** 진단 표면화만.

## 테스트 (fake / unit, read-only)

* **T1:** `classify_momentum_freshness` — (a) `latest == expected` → `("fresh", 0)`;
  (b) `latest < expected`(예: expected-14d) → `("stale", days_stale>=1)`.
* **T2:** `get_momentum_candidates_impl` — fake repository가 `trading_date`가 14일 전인 단일 행을
  반환할 때 `data_state == "stale"`, `days_stale >= 1`, `expected_baseline_date`/`latest_trading_date`
  키 존재. (정확한 ROB-389 증상 회귀 가드.) 빈 행 → `missing`.
* **T3:** `_collect_top_gainers` — fake equity repo의 `coverage`가 받은 `today_trading_date` 인자가
  `expected_baseline_date("kr", now=now)`와 같은지(세션 인식). `_collect_crypto`는 `now.date()` 유지.
* **T4:** `_missing_data`/페이로드 — stale일 때 `what`에 `"N일 지연"` 포함, 페이로드에
  `expected_baseline_date`/`latest_partition_date`/`days_stale` 키 존재.

## 안전 경계

* read-only. broker/order/watch/order-intent mutation 없음.
* **DB 마이그레이션 없음** — 컬럼 추가 없이 페이로드 JSON 필드만 추가.
* scheduler/Prefect 등록·활성화 없음. prod backfill/data rewrite 없음.
* `recommend_stocks` 무관. 좁은 수정 우선.

## 산출물 / 핸드오프

* 독립 PR (base: `origin/main` `79e53c1c`, worktree `auto_trader.rob-389`/branch `rob-389`).
* 검증 명령/결과 → PR + ROB-394 handoff 코멘트.
* 잔여(스냅샷 빌드 잡 운영 활성화)를 별도 operator issue로 명시 후 ROB-390 인계.

## 비목표 (Out of scope)

* 스냅샷 빌드 파이프라인의 운영 활성화/복구 (별도 operator issue — "11,638 stale"의 근본 운영 원인).
* US/crypto freshness 의미론 재설계 (KR 중심 수정만).
* `candidate_universe`의 이미 올바른 stale 판정 로직(`_classify_usefulness`) 재작성.
* report confidence cap 로직 변경 (이미 `cap 40` 동작).
