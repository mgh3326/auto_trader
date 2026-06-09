# ROB-346 — US 신규매수 후보 universe 필터·우선순위 고도화 (Design)

- **Issue**: ROB-346 (Improvement / High) — parent ROB-336
- **Date**: 2026-06-09
- **Scope**: PR-B (A→B→C 배치). Migration-0. **US-only**(KR 경로 무회귀).
- **Status**: design + adversarial spec-review 반영. pending user review → plan
- **Ordering**: PR-A(345)와 독립. PR-C(347)와 같은 candidate 루프를 건드리므로 **B 먼저
  머지**. 단, 충돌을 구조적으로 없애기 위해 **classify_candidate_symbol signature는
  바꾸지 않고**, 후처리 demotion 헬퍼를 추가한다(§3.3).

## 1. Context / 현황 (코드 검증)

US 신규매수 후보는 `invest_screener_snapshots` top-gainers에서 **순수 모멘텀**으로 수집:
- `collectors/candidate_universe.py:302-355` `_collect_top_gainers`(US 분기;
  `_collect_equity:291-300` 이 wrapper). 품질 필터 **전무**.
- `invest_screener_snapshots/repository.py:123-141` `list_top_candidates`:
  `ORDER BY change_rate DESC nullslast, symbol ASC LIMIT :limit`. 내부 pool == 표시 pool.
- 후보 verdict `action_verdict.py:74-98` `classify_candidate_symbol(quote, *,
  universe_useful, quote_snapshot_present, candidate_fresh)` → `data_gap`/`watch_only`/
  `buy_review` (호가 actionability + freshness만; 가격/유동성/연령 입력 없음). **candidate
  전용**(held는 `classify_held_symbol`).
- auto_emit candidate 루프(`auto_emit.py:464-518`): verdict → reason if/elif(`:476-490`,
  현 reasons `quote_missing`/`low_liquidity`/`beyond_candidate_budget`(=개수 cap)/
  `screener_stale`) → `_candidate_item(..., verdict, priority, reject_or_wait_reason)`.
  candidate dict은 `payload["candidates"]` 에서 옴(collector의 `build_candidate_evidence`).

### 1.1 가용 데이터 (필터 feasibility)
`invest_screener_snapshots`: `latest_close`(Decimal, 가격), `daily_volume`(BigInt, 주),
`change_rate`(Decimal, **percent** e.g. 12.0=+12%, ratio 아님), `week_change_rate`, `closes_window`(JSONB),
`consecutive_up_days`. **`market_cap` 없음.**
`us_symbol_universe`: `is_common_stock`(nullable Bool, ROB-204), `is_active`, `exchange`.
**`market_cap`/`sector` 없음.**
→ penny/illiquid/abnormal-spike/stale는 snapshot으로 직접 계산. **true 시총 마이크로캡은
추가 fetch 없이는 불가 → 가격·달러볼륨 proxy + is_common_stock 으로 대체(정직하게
"size/liquidity proxy" 명명).**

## 2. Goal

예산보다 넓은 pool에서 priority 순으로 신규매수 후보를 산출하되, 저품질 후보는 무조건
제거가 아니라 `watch_only`/`rejected`/`data_gap` + 사유로 분류. stale-only 후보가 live buy로
노출되지 않음.

## 3. Design (migration-0, US-only gate)

### 3.1 용어 (일관 사용)
- **quality_flags**: 후보별로 *감지된* 품질 이슈 이름의 `frozenset[str]`. 가능한 멤버:
  `{"penny","illiquid","abnormal_spike","non_common_stock","screener_stale",
  "common_stock_unknown"}`. (true 플래그만 포함; 없으면 empty set.)
- **verdict**: locked decision_bucket의 sub-verdict(`buy_review`/`watch_only`/`rejected`/
  `data_gap`).
- **reason**: auto_emit가 evidence에 기록하는 문자열(예: `penny`).

### 3.2 pool 확장 + is_common_stock 조인 (collector, US-only)
- `repository.py` 에 **신규** `list_candidate_pool(market, limit=None)`:
  최신 partition 후보를 wide하게 반환(change_rate DESC nullslast, symbol ASC; limit None=
  partition 전체 또는 cap). US는 `us_symbol_universe.is_common_stock` LEFT JOIN(없으면 None).
  *기존 `list_top_candidates` 는 그대로 두고 신규 메서드 추가(타 호출자 무영향).*
- `_collect_top_gainers` (US 분기, `request.market=="us"` 게이트): pool =
  `max(candidate_limit × 5, 50)` 를 `list_candidate_pool` 로 수집.
- 후보별 **quality_flags + 원지표**를 candidate evidence dict에 적재(`build_candidate_evidence`/
  `_equity_row_to_input` 확장, additive):
  `latest_close`, `daily_volume`, `dollar_volume_usd`, `change_rate`, `week_change_rate`,
  `is_common_stock`, `quality_flags`(list), `priority_score`(float), `confidence_cap`(int|None).
- **KR 경로 무변경**: `_collect_kr_presets` 및 KR top_gainers fallback은 신규 quality 로직
  미적용(US-only gate). 기존 KR 테스트 green 유지.

### 3.3 품질 게이트 (Conservative 기본; spec review에서 튜닝 가능)
`dollar_volume_usd = float(latest_close) * daily_volume` (US는 `latest_close` USD 가정).
후보별 quality_flags 산출:
| flag | 기준(US) |
|------|----------|
| `penny` | `latest_close < 5.0` |
| `illiquid` | `dollar_volume_usd < 5_000_000` |
| `abnormal_spike` | `change_rate > 15.0` OR `week_change_rate > 50.0` (percent) |
| `non_common_stock` | `is_common_stock is False` |
| `common_stock_unknown` | `is_common_stock is None` (미분류) |
| `screener_stale` | collector `days_stale > 0` 또는 usefulness != "useful" |

**후처리 demotion(서명 충돌 회피 핵심):** `classify_candidate_symbol` 은 **그대로** 두고,
`action_verdict.py` 에 **순수 헬퍼** 추가:
```
def demote_for_quality(verdict: str, quality_flags: frozenset[str]) -> tuple[str, str | None]:
    # 우선순위: rejected > data_gap(미분류) > watch_only > 원 verdict
    if "non_common_stock" in quality_flags:      return "rejected", "non_common_stock"
    if verdict != "buy_review":                  return verdict, None  # 이미 honest 하향 — budget/품질로 끌어올리지 않음
    if "common_stock_unknown" in quality_flags:  return "data_gap", "common_stock_unknown"
    for f in ("penny","illiquid","abnormal_spike","screener_stale"):
        if f in quality_flags:                   return "watch_only", f
    return "buy_review", None
```
- 규칙: 품질 verdict가 **우선**; 품질은 **하향만**(절대 상향 없음). `non_common_stock` 은
  buy 여부와 무관히 reject(거짓 확정이므로). `is_common_stock None` 은 reject 아닌 data_gap
  (확정 거짓 아님).
- auto_emit candidate 루프: `base = classify_candidate_symbol(...)` →
  `verdict, q_reason = demote_for_quality(base, quality_flags)`. 기존 count-cap
  (`beyond_candidate_budget`)은 **최종 buy_review 집합에 마지막으로** 적용.
- reason 기록: 기존 단일 `reject_or_wait_reason` 은 primary로 유지(`q_reason` 우선),
  추가로 evidence에 `quality_reasons: list[str]`(전체 flag) 적재 → UI rich 표시.
- `auto_emit.py:476-490` if/elif에 신규 분기/헬퍼 호출 삽입(reason 매핑 확장).

### 3.4 priority ranking (결정적 공식)
buy_review 후보 정렬용 `priority_score`(높을수록 우선; collector가 계산해 evidence에 저장):
```
liquidity_term = min(1.0, log10(max(dollar_volume_usd, 1.0)) / 9.0)   # 9 ≈ log10($1B)
momentum_term  = clamp(change_rate, -5.0, 10.0) / 10.0               # percent; 적정 양(+) 선호, 상한
spike_penalty  = 1.0 if "abnormal_spike" in quality_flags else 0.0
stale_penalty  = 1.0 if "screener_stale" in quality_flags else 0.0
priority_score = 1.0*liquidity_term + 0.5*momentum_term - 0.5*spike_penalty - 0.3*stale_penalty
```
정렬: `priority_score` DESC, tiebreak `dollar_volume_usd` DESC, `symbol` ASC(결정적).
(가중치는 상수 모듈로 분리해 튜닝/테스트 용이.)

### 3.5 confidence cap
- `confidence_cap: int | None` 를 candidate evidence(JSONB)에 additive 저장(모델 변경 없음).
  `screener_stale` 또는 `common_stock_unknown` 시 `40`, 아니면 `None`. ActionPacket/Hermes/
  UI가 렌더 시 cap 적용. 결정적 경로가 값을 세팅(별도 모델/migration 불요).

### 3.6 표시 정책
- `buy_review`: `priority_score` 순, count-cap(`_max_buy_candidates`) 적용 후 상위.
- 데모션(`watch_only`/`rejected`): 사유 포함, `candidate_rank`(pool 순위) 기준 **상위 10개만**
  카드로, 나머지는 집계(`{watch_only_count, rejected_count, data_gap_count}`)만.
- pool 크기 ≠ 표시 수 명시: evidence/summary에 `pool_size`, `buy_review_count`,
  `demoted_shown`, `demoted_total`.

### 3.7 sector/concentration "최소 정책"
sector 데이터 부재 → 최소만: 이미 보유 종목 제외(auto_emit 기존 `sym in held` skip 유지) +
per-report `buy_review` cap. **true 섹터 분산은 deferred**(sector 소스 추가 후속).

## 4. Acceptance criteria (이슈 매핑)
- 예산보다 넓은 pool에서 priority 순 산출. → 3.2/3.4
- 저품질 후보는 제거 아닌 `rejected`/`watch_only` + 사유. → 3.3
- stale-only 후보가 live buy로 노출 안 됨. → 3.3(screener_stale)/3.5/3.6
- 카드에 priority, confidence cap, 주요 근거, 주요 제외/감점 사유 포함. → 3.4/3.5/3.6
- focused tests가 stale exclusion, microcap/liquidity filter, priority ordering,
  candidate_limit 표시 커버. → §5

## 5. Test plan
1. **demote_for_quality 단위(순수)**: 각 flag → 기대 verdict/reason; 비-buy_review는 미상향;
   non_common_stock는 항상 rejected; None은 data_gap.
2. **penny/illiquid 단위**: `latest_close<5` / `dollar_volume_usd<5e6` (예: 1M주×$3.50=$3.5M → illiquid).
3. **abnormal_spike**: `change_rate>0.15` / `week>0.50` → watch_only.
4. **non_common_stock / unknown**: False→rejected; None→data_gap; True→통과.
5. **priority ordering(결정적)**: 공식 + tiebreak; 고정 입력 → 고정 순서.
6. **pool > display**: 내부 pool(≥50) > 표시; 데모션 cap(10) + 집계 카운트.
7. **screener_stale**: stale partition → watch_only + confidence_cap=40, buy_review 아님.
8. **KR 무회귀**: KR 경로 quality 게이트 미적용(기존 KR 테스트 green).

## 6. Safety boundaries / Non-goals
- 자동 주문/주문 preview/submit/cancel/modify·broker/order/watch/order-intent mutation 금지.
- 외부 scraping 필수 경로화 금지(best-effort market_cap fetch 비채택, proxy 사용).
- 후보는 검토 대상이지 매수 지시 아님.
- decision_bucket enum(locked 5) 미변경. **classify_candidate_symbol signature 미변경**
  (후처리 헬퍼만 추가 → 모든 기존 caller·held 경로 무영향, PR-C와 무충돌).
- build-time `common_stocks_only`(`jobs/invest_screener_snapshots.py`) 흐름과 혼동 금지.
- migration 없음(품질은 동적 계산 + candidate evidence JSONB additive).

## 7. Out of scope / follow-up
- true 시총 마이크로캡(펀더멘털/valuation 소스); sector 분산; crypto/KR 후보 품질 — 별도.
