# kr-golden-vectors-v1 — 소비 계약 (golden v6)

```
작성: claude-mock (상류 분석, wB:p4R) · 2026-08-05 (v6 — A15 반영: 만기 결측 = 거래 단위 missing_exit, §10차 확정)
대상: KR-A0 구현·검증 워커
후보 정본: prior-art-map-v1/02-active-candidates.yaml
  sha256 = 0f5e92bf7d10dd77588fa08ad949811a68004cf71dd7f2efd232306b22d82d85
convention 정본: 운영자 확정 amendment A1~A9 + A10~A12 + A13~A14 + **A15** (전부 ~/work/herdr-inbox/,
  지위 ✅확정) — SHA 는 golden_v6.json `convention_sha256` 결속 (generator 자체 포함)
⚠️ v1~v5 산출물 superseded — 소비 금지. 정본 = golden_v6.json.
🔴 정본 재생성 런타임 = auto_trader .venv **Python 3.13.x, plain python (-O 금지)** — byte-identity 는 이 런타임 기준
   (3.9 는 statistics 내부 차이로 float leaf 4개 상이 — cross-version 재현은 비계약).
```

## 파일

```
5b5bde57f83ad39ddf3bd6a077ed7d22370d338f85c2765873c1e767354dd220  golden_v6.json
e9e744932594da41…  fixture_bars.csv        9907044e1f54d0a1…  fixture_sessions.csv
280f829080580d4c…  fixture_config.csv      88709819a088145e…  fixture_membership.csv
586151af9101f47d…  fixture_delist_events.csv
variants/<name>/   자기완결 variant 디렉토리 21종 — RunInvalid 6종은 지정 라벨로 **실패해야
                   하고**, 완주 15종은 지정 결과로 **완주해야 한다** (A15 로 invalid_maturity 가
                   실패군 → 완주군 이동. 5파일 전체 SHA 가 golden 에 결속):
  invalid_maturity → **A15-3: 완주 + missing_exit 1건·16거래 + 최종 verdict = INCONCLUSIVE_MISSING_EXITS (1/17=5.88%) 결속 + 존재-but-무효 자매 6종(close blank/0/Inf·volume blank/0/음수) 동일 기대 + entry_open_zero(진입 open=0 → NO_FILL·capacity ripple) + E2E.predicate_oracles(입력 bar 자기완결 14케이스 — 비유한 값은 {"special_float": "+inf"} tag, golden 은 RFC strict JSON·allow_nan=False·strict 파서 검사 내장 — 소양수 0.01/1 유효로 exact >0 하한 고정, 실함수 배선)** (구 RUN_INVALID 기대 폐기 —
  증거 없는 만기 결측은 거래 단위 제외+공개, 5% 초과 시 INCONCLUSIVE_MISSING_EXITS) · data_gap → RUN_INVALID_DATA_GAP ·
  duplicate_row → RUN_INVALID_DUPLICATE_ROW (로더) · nofill_no_substitution →
  QA6=NO_FILL·KA5=SKIPPED 유지·16거래 (차순위 대체 구현 격추) ·
  identity_conflict → RUN_INVALID_IDENTITY_CONFLICT (동시 양시장 membership) ·
  market_transfer → RUN_INVALID_MARKET_TRANSFER (보유 중 이전상장 — 가격 해석 전
  membership_end < exit_due 게이트 — **stale-valid 만기 bar 보존**: 만기-무효-시에만 transfer 검사하는 mutant 는 완주해버려 격추됨) ·
  market_transfer_missing_maturity → 같은 transfer 인데 만기 bar 결측 — 그래도
  RUN_INVALID_MARKET_TRANSFER (만기-먼저 mutant 는 MATURITY_PRICE 를 내어 격추 —
  두 variant 가 우선순위 양방향을 봉인) · data_gap_control_all_invalid →
  전부-무효-but-존재 rows 는 gap 아님(완주) · stale_delist_bar → **§12차 반전: 유효 만기 bar 가 결정공시 증거에 우선**(KA4 정상 청산
  s34·close 1000.00 + QA5 cohort terminal_included=0 exact 결속 — evidence-first mutant 격추) · holdout_leg_control →
  s53+ 시작 membership leg 는 구조적으로 불가시(base 와 결과 동일·oob 0·clamped
  symbol/membership key set 부재가 golden 에 직렬화됨)
```

## 핵심 판별기

- **경계 봉인 (A14-5)**: `exploration_end=52` 는 명시 입력, 파일은 s55 까지 존재.
  **membership 메타데이터도 World 구성 시 경계로 clamp** — identity/transfer 검사가
  holdout leg 를 구조적으로 볼 수 없음. 엔진의 모든
  iteration·gap검사·신호계산은 `t ≤ 52`. **access-spy: 경계 밖 bar read == 0** 이 acceptance
  조건 (`golden_v6.json.access_spy_oob_reads = 0`). KA2@47·KH1@49 는 bar 존재해도 censored.
- **2단계 선발 (A10-1)**: held 스킵은 선발 대체 허용, no-fill 은 슬롯 소멸 — variant 가 증인.
- **A13 identity**: `"000420"` = 이전상장 모양 (KOSDAQ 0..20 → KOSPI 23..55, disjoint,
  신규 leg 의 pre-membership bar 없음) — **적격성은 21세션 전 구간 membership** 이라
  KOSPI 쪽 최초 신호는 s43 (s42 에는 비적격 — pre-list history 사용 구현 격추). symbol-only 키는 붕괴. 동시 중복·보유 중 이동은 variant 로 격추.
- **A12 field-level**: KG1 entry (high blank) 체결 · KA3 만기 (open/high/low blank) 정상 청산 ·
  KA4/QD1 상폐 역탐색 close+volume-only·**예정 만기일부터 역방향**(QD1 은 event 세션 bar 사용 — d−1 구현 격추, exit_session=예정 만기일) ·
  QD2 = entry close blank 로 체결 후 유효 종가 없음 → **gross −100%** · KE5 blank volume 무효.
- **exact 경계 (float 정확)**: KB1@45 `clv == 0.65 ∧ ratio == 1.5` (정수 프라이스 13/20) ·
  KB2@38 `clv == 0.75 ∧ ratio == 1.25` · KH1@49 `r3_pct == 0.05` (모집단 20) · s49 min
  `liq_pct == 0.5` · KD4@37 `margin == 0.0` · QH1@31 `r20 == 0.0` · LV5 900180@44
  `vol20_pct == 0.3` (모집단 20, rank 6).
- **A4**: baseline 이 거래와 동일한 순서 (**§12차: 유효 만기 bar → 결정공시 증거 → 제외** —
  evidence_type=decision_disclosure 는 bar 부재의 분류 전용). `cohort_excluded_entry ≥ 1`
  (QA1) · `cohort_terminal_included ≥ 1` (QA5) 카운트 공개.
- **A11+A14 합성**: 17거래 × 43/83bp 4게이트 실제 산출, 12 클러스터 → top-level
  `UNIDENTIFIABLE_CLUSTERS`(축약 금지) + `cost_sensitive` flag. **compose oracle**: 실제 compose 함수 통과 — PASS / FALSIFIED /
  COST_SENSITIVE / RUN_INVALID_EMPTY_BASELINE + **정확 경계**(filled 300/299 ·
  클러스터 30/29 · 초과 0 · **양수연도 6/5 가 유일 gate 차이인 full-profile** ·
  세션동일가중 vs 거래가중 연도 판별 · **Dec진입/Jan청산 entry-year 귀속**) 고정.
- lowvol: ml {9(이력부족)·29(8진입)·31(held 5 무시 유지+2충원+2 SKIP)·44(전부 censored)} ·
  Q01 s50 만기까지 강제청산 없음.

- **§12차 판별기 4종 + cohort oracle**: `c8_backscan_due_anchor`(event s28 < 유효 s30 <
  무효 due s31 → C8 종가는 **예정 만기일부터 역탐색**한 s30 — event 기점 mutant 격추) ·
  `slot_release_capacity_ripple`(event s29 + 정원 포화 → 슬롯은 예정 만기까지 유지, KA5 SKIP
  보존 — occupy=min(due,event) mutant 격추) · `evidence_after_due`(event s40 > 예정 만기 s30
  → 증거 아님·missing_exit — cutoff 제거 mutant 격추) · `unknown_evidence_type`(→
  `RUN_INVALID_UNKNOWN_EVIDENCE_TYPE` — fixture 가 evidence_type 을 선언·검증) ·
  **E2E.predicate_oracles (cohort 3종 + maturity_entry, 총 4종)**(baseline 단독 호출 — transfer-first 게이트·C8 due-anchor·
  evidence cutoff 가 baseline 에도 있음을 각각 exact 결속. baseline-only mutant 격추)

🔴 acceptance 원칙: «격추» 는 **불변 pinned golden_v6.json 과 구현 산출의 exact 비교**로만
   성립한다 — generator 를 다시 돌려 동적 기대값을 재생성하는 것으로 대체 금지 (17차 지적).

## 검증 요구

1. fixture 양자화 — 재계산 exact 일치. `==` 명시 항목은 float 정확 일치.
2. **양방향 증명**: mutation test (strict `<`·raw high·global percentile·no-fill 대체·full-bar
   entry/maturity validity·symbol-only identity·경계 밖 read — 각각 검출) + tamper test.
   실제 CI 테스트로 제공. **access-spy 테스트 필수.**
3. variant 21종은 지정 결과와 정확히 일치해야 한다 (RunInvalid 6종의 라벨·완주 15종의 결과 —
   golden_v6.json `E2E.variants` 가 정본).
4. 의도된 결측·오류는 시나리오 — "수리" 금지.
5. golden 미통과 시 registry 기동 거부 경로 증명.
6. convention 충돌 → `NEEDS_UPSTREAM` — 조용한 적응 금지.
