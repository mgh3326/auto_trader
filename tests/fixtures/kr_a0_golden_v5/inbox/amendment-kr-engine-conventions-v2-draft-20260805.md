# [DRAFT — 운영자 확정 대기] KR 엔진 convention amendment v2

```
작성: claude-mock (상류 분석, wB:p4R) · 2026-08-05 09:0x KST
지위: ✅ 확정 — 운영자가 claude-mock 대화에서 2026-08-05 09:1x 승인 (A5 = z + floor 30 채택).
      결정 정본 operator-decisions-20260805-0830.md §3차 확정에 편입됨.
근거: answer-codexmock-golden-xcheck-0853.md — 동결 YAML 은 이름·의도만 정의하고
      exact 수치 convention 을 정의하지 않는 항목 7개 확인. 결과를 보기 전에
      (첫 ㉠ 백테스트 실행 전에) 봉인해야 post-hoc 오염이 없다.
```

## A1 (=C1) percentile 정의
`pct_asc(x_i) = |{j ∈ 모집단 : x_j ≤ x_i}| / N` — 자기 포함, 동값 동percentile, 최솟값 pct = 1/N.
소표본 경계: 모집단 N < 20 인 세션·시장은 해당 후보 신호 미계산 (r3_pct ≤ 0.05 가 표현 불가능한
규모에서 우연 통과를 방지).

## A2 (=C3) 기술 통계
median = 짝수 표본 중앙 2값 산술평균 · 표준편차 = ddof=1. (vol20 ddof=1 은 YAML 명시 — 나머지를
일반 ratify.)

## A3 (=C4) 비용 적용
전략 거래: `net = gross − 0.0043` (base) / `− 0.0083` (sensitivity), 정확히 이 값·측면 분해 없음·
화폐 반올림 없음. 상폐 terminal 거래에도 동일 차감. baseline cohort 는 gross (비용 미차감).

## A4 (=C5) baseline — 명칭 `candidate_counterfactual_baseline_v1`
#1777 은 pipeline smoke 이며 baseline 정본이 아님 (codex 확정). 정의:
- cohort = 신호 세션 t 의 **시장별** liq_pct 로 `decile = min(9, floor(pct×10))` 산출 후
  **두 시장 병합**, 거래 종목과 동일 decile 인 A9 적격 종목 전체 (**자기 포함**).
- 수익 = t+1 open → 동일 D+N close, 동일가중 gross 평균.
- cohort = 신호 세션 t 의 **A8 ①② 적격**(PIT + 21세션 유효 bar) 집합에서 산출 (liq 임계 이전 —
  decile 이 10분위 전체를 가져야 하므로).
- cohort 구성원 처리 (전략 거래와 구분): t+1 bar 없으면 제외 + `cohort_excluded_entry` 카운트,
  만기 bar 부재 + 상폐 증거 있으면 **C8 terminal 수익으로 포함**, 증거 없으면 제외 +
  `cohort_excluded_maturity` 카운트 (조용한 제거 금지 — 카운트 공개가 의무).
  **RUN_INVALID 는 전략 거래의 만기 결측(A6-3)에만 적용** — cohort 구성원까지 run 무효로 하면
  실코퍼스에서 정지 종목 1개가 모든 run 을 무효화한다 (2026-08-05 설계 중 발견, 운영자 확정
  범위 내 정밀화).
- excess = 전략 net − cohort gross 평균.

## A5 (=C6) 군집 LCB estimator  ← 유일한 실질 선택지
채택안(권고): **정규 z = 1.2815515655446004** (one-sided 90%, asymptotic 선언) +
세션 내 동일가중 → 세션 간 동일가중, sd ddof=1, **최소 진입 세션(cluster) 수 = 30** —
미달 시 verdict = `UNIDENTIFIABLE_CLUSTERS` (PASS/FAIL 판정 자체 거부).
대안(비채택 시): `t_{n-1, 0.90}` — 더 보수적이나 분위수 테이블 embed 필요.
bootstrap 은 v1 에서 배제 (seed·반복수 신규 결정 유발 — codex 권고 동일).

## A6 (=C7) 만기 결측 4분기 fail-closed (codex 안 그대로 채택)
1. D+N 이 pinned 달력상 2025+ (holdout) → `RIGHT_CENSORED_NOT_TRADEABLE` — 진입 전 제외,
   holdout 미접촉.
2. D+N 이 2015–2024 인데 시장 세션/partition 자체가 corpus 에 없음 → `RUN_INVALID_DATA_GAP`
   (조용히 분모에서 빼지 않음 — run 무효).
3. 세션은 존재·해당 symbol 만기 bar 부재·상폐 증거 없음 → `RUN_INVALID_MATURITY_PRICE`
   (suspension/결측을 임의 상폐 해석 금지).
4. **PIT 상폐 증거가 있을 때만** C8 적용.
기각: "마지막 corpus 종가 조기청산" — D+N 고정 청산의 사후 단축 + 끝단 선택편향 (codex 판정 수용).

## A7 (=C8) 상폐 처리 (문안 유지 + 증거 요건 명시)
corpus 상폐 이력(constraints §KR)에 **증거가 있는 경우에만**: 마지막 유효 종가 청산 +
`delisted_exit=true`, 유효가 없으면 −100%.

## A8 (=C9) 적격성·percentile 모집단 순서
① PIT membership(세션 t 기준) ∧ ② 21세션 연속 **유효** bar (finite ∧ OHLC 양수 ∧ volume 양수 —
YAML "결측·비양수면 미계산" 준수) → ③ 그 집합에서 시장별 `close_t×volume_t` 로 liq_pct (A1 식)
→ ④ `liq_pct ≥ 0.50` 통과 집합 = r3_pct·vol20_pct 모집단 (시장별).

## A9 identity
심볼 identity 는 `(market, symbol)` 복합키. 동일 symbol 의 시장 이동·중복은 fail-closed
(기존 백테스트 오류 `003670 multiple market session sets` 의 재발 방지 계약).

## golden v2 보강 목록 (amendment 확정 후 상류가 재조립)
- fixture 를 명시 입력 4종으로: bars(2dp 양자화 — CSV 왕복 exact) · 세션 달력(월말 flag 포함,
  달력이 입력이며 요일 유도 금지) · membership · 상폐 events
- P0: stateful max10 E2E(6세션 연속 신호 누적→슬롯 포화) · 재신호 무시/차순위 충원 ·
  lowvol 2개 월말 persistence · per-market vs global percentile 판별 fixture · invalid-value
  matrix · fills→gate 전 경로 집계 · V3 기대순서 독립 literal 화
- P1: brk20 clamp-high mutant fixture · 경계 등식(liq=.50, pct=.05, vol20_pct=.30, r20=0,
  margin=0) · baseline cohort 상폐/공집합 · golden 내 fixture SHA 결속 + CSV 재독 재현 검사
