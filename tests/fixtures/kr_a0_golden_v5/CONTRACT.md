# kr-golden-vectors-v1 — 소비 계약 (golden v5)

```
작성: claude-mock (상류 분석, wB:p4R) · 2026-08-05 (v5 — codex-mock 4차 적대검증 FIX_FIRST 반영)
대상: KR-A0 구현·검증 워커
후보 정본: prior-art-map-v1/02-active-candidates.yaml
  sha256 = 0f5e92bf7d10dd77588fa08ad949811a68004cf71dd7f2efd232306b22d82d85
convention 정본: 운영자 확정 amendment A1~A9 + A10~A12 + A13~A14 (전부 ~/work/herdr-inbox/,
  지위 ✅확정) — SHA 는 golden_v5.json `convention_sha256` 결속 (generator 자체 포함)
⚠️ v1~v4 산출물 superseded — 소비 금지. 정본 = golden_v5.json.
```

## 파일

```
fa9a3a1adbb47f80e0980e99e3f641ef0f331c21e4801bee296366f3735cb97b  golden_v5.json
e9e744932594da41…  fixture_bars.csv        9907044e1f54d0a1…  fixture_sessions.csv
280f829080580d4c…  fixture_config.csv      88709819a088145e…  fixture_membership.csv
588778f5e883b798…  fixture_delist_events.csv
variants/<name>/   자기완결 variant 디렉토리 10종 — 실패 7종은 지정 라벨로 **실패해야 하고**,
                   대조 3종은 지정 결과로 **완주해야 한다** (5파일 전체 SHA 가 golden 에 결속):
  invalid_maturity → RUN_INVALID_MATURITY_PRICE · data_gap → RUN_INVALID_DATA_GAP ·
  duplicate_row → RUN_INVALID_DUPLICATE_ROW (로더) · nofill_no_substitution →
  QA6=NO_FILL·KA5=SKIPPED 유지·16거래 (차순위 대체 구현 격추) ·
  identity_conflict → RUN_INVALID_IDENTITY_CONFLICT (동시 양시장 membership) ·
  market_transfer → RUN_INVALID_MARKET_TRANSFER (보유 중 이전상장 — 가격 해석 전
  membership_end < exit_due 게이트 — **stale-valid 만기 bar 보존**: 만기-무효-시에만 transfer 검사하는 mutant 는 완주해버려 격추됨) ·
  market_transfer_missing_maturity → 같은 transfer 인데 만기 bar 결측 — 그래도
  RUN_INVALID_MARKET_TRANSFER (만기-먼저 mutant 는 MATURITY_PRICE 를 내어 격추 —
  두 variant 가 우선순위 양방향을 봉인) · 대조 3종: data_gap_control_all_invalid →
  전부-무효-but-존재 rows 는 gap 아님(완주) · stale_delist_bar → 상폐 증거가
  stale-valid 만기 bar 에 우선(KA4 거래 base 와 동일 + QA5 cohort baseline exact 값 결속 —
  maturity-first baseline mutant 는 gross_mean 부호가 뒤집혀 격추) · holdout_leg_control →
  s53+ 시작 membership leg 는 구조적으로 불가시(base 와 결과 동일·oob 0·clamped
  symbol/membership key set 부재가 golden 에 직렬화됨)
```

## 핵심 판별기

- **경계 봉인 (A14-5)**: `exploration_end=52` 는 명시 입력, 파일은 s55 까지 존재.
  **membership 메타데이터도 World 구성 시 경계로 clamp** — identity/transfer 검사가
  holdout leg 를 구조적으로 볼 수 없음. 엔진의 모든
  iteration·gap검사·신호계산은 `t ≤ 52`. **access-spy: 경계 밖 bar read == 0** 이 acceptance
  조건 (`golden_v5.json.access_spy_oob_reads = 0`). KA2@47·KH1@49 는 bar 존재해도 censored.
- **2단계 선발 (A10-1)**: held 스킵은 선발 대체 허용, no-fill 은 슬롯 소멸 — variant 가 증인.
- **A13 identity**: `"000420"` = 이전상장 모양 (KOSDAQ 0..20 → KOSPI 23..55, disjoint,
  신규 leg 의 pre-membership bar 없음) — **적격성은 21세션 전 구간 membership** 이라
  KOSPI 쪽 최초 신호는 s43 (s42 에는 비적격 — pre-list history 사용 구현 격추). symbol-only 키는 붕괴. 동시 중복·보유 중 이동은 variant 로 격추.
- **A12 field-level**: KG1 entry (high blank) 체결 · KA3 만기 (open/high/low blank) 정상 청산 ·
  KA4/QD1 상폐 역탐색 close+volume-only (QD1 은 **event 세션 bar 를 사용** — d−1 구현 격추) ·
  QD2 = entry close blank 로 체결 후 유효 종가 없음 → **gross −100%** · KE5 blank volume 무효.
- **exact 경계 (float 정확)**: KB1@45 `clv == 0.65 ∧ ratio == 1.5` (정수 프라이스 13/20) ·
  KB2@38 `clv == 0.75 ∧ ratio == 1.25` · KH1@49 `r3_pct == 0.05` (모집단 20) · s49 min
  `liq_pct == 0.5` · KD4@37 `margin == 0.0` · QH1@31 `r20 == 0.0` · LV5 900180@44
  `vol20_pct == 0.3` (모집단 20, rank 6).
- **A4**: baseline 이 거래와 동일한 순서 (상폐 증거 → 만기 bar). `cohort_excluded_entry ≥ 1`
  (QA1) · `cohort_terminal_included ≥ 1` (QA5) 카운트 공개.
- **A11+A14 합성**: 17거래 × 43/83bp 4게이트 실제 산출, 12 클러스터 → top-level
  `UNIDENTIFIABLE_CLUSTERS`(축약 금지) + `cost_sensitive` flag. **compose oracle**: 실제 compose 함수 통과 — PASS / FALSIFIED /
  COST_SENSITIVE / RUN_INVALID_EMPTY_BASELINE + **정확 경계**(filled 300/299 ·
  클러스터 30/29 · 초과 0 · **양수연도 6/5 가 유일 gate 차이인 full-profile** ·
  세션동일가중 vs 거래가중 연도 판별 · **Dec진입/Jan청산 entry-year 귀속**) 고정.
- lowvol: ml {9(이력부족)·29(8진입)·31(held 5 무시 유지+2충원+2 SKIP)·44(전부 censored)} ·
  Q01 s50 만기까지 강제청산 없음.

## 검증 요구

1. fixture 양자화 — 재계산 exact 일치. `==` 명시 항목은 float 정확 일치.
2. **양방향 증명**: mutation test (strict `<`·raw high·global percentile·no-fill 대체·full-bar
   entry/maturity validity·symbol-only identity·경계 밖 read — 각각 검출) + tamper test.
   실제 CI 테스트로 제공. **access-spy 테스트 필수.**
3. variant 10종은 지정 결과와 정확히 일치해야 한다 (실패 7종의 라벨·대조 3종의 완주 결과 —
   golden_v5.json `E2E.variants` 가 정본).
4. 의도된 결측·오류는 시나리오 — "수리" 금지.
5. golden 미통과 시 registry 기동 거부 경로 증명.
6. convention 충돌 → `NEEDS_UPSTREAM` — 조용한 적응 금지.
