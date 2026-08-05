# [DRAFT — 운영자 확정 대기] KR 엔진 amendment 추가분 A10~A12

```
작성: claude-mock (상류 분석, wB:p4R) · 2026-08-05 09:5x KST
지위: ✅ 확정 — 운영자가 claude-mock 대화에서 2026-08-05 10:0x 승인.
      결정 정본 operator-decisions-20260805-0830.md §4차 확정에 편입됨.
근거: codex-mock 2차 적대검증 answer-codexmock-golden-xcheck2-0935.md —
      "C10 은 운영자 승인 범위(A1~A9)를 넘어선 새 정본" 판정 + empty-baseline·
      field-level validity 결정 공백 지적. 결과를 보기 전(첫 ㉠ 백테스트 전) 봉인.
전제: no-fill 차순위 보충 금지는 결정이 아니라 **동결 YAML 위반 수정**(2단계 선발)이므로
      이 문서에 없다 — v3 에서 구현 수정.
```

## A10 — temporal semantics (구 C10 의 ratification)

1. **선발 2단계**: 신호 세션 t 에 랭킹 순회로 (held 스킵하며) 가용 슬롯 수만큼 **선발 확정** →
   t+1 open 에서 체결 평가. **no-fill 은 슬롯 소멸 — 차순위 보충 없음** (YAML literal).
   held 재신호만 선발 단계에서 차순위로 대체된다 ("재신호 무시 후 차순위로 슬롯 충원").
2. held/capacity 판정 시점 = **entry 세션(t+1) open**. 세션 s 에 만기(exit_due=s)인 포지션은
   s open 에 아직 슬롯 점유, s close 청산, **s+1 open 부터 해제**.
3. 상폐 terminal: event 세션 d 에 종료 기록, 슬롯은 **d+1 부터 해제**.
4. **A6-1 censoring 판정이 held/capacity 판정에 선행** (달력 판정이 포트폴리오 판정보다 먼저).
5. 보유 중간 세션의 bar 결측·무효는 **허용** — entry 와 만기(및 상폐 최종 유효가 탐색)만
   가격 유효성을 요구한다.
6. **A6-1 의 경계는 명시 입력** `exploration_end_session` (2024 마지막 XKRX 세션) — "corpus
   파일 끝"으로 유도 금지. D+N > 경계 → RIGHT_CENSORED_NOT_TRADEABLE (사전 제외).

## A11 — verdict 합성·방어 규칙

1. 후보 최종 verdict 우선순위: `RUN_INVALID_* > UNIDENTIFIABLE_*(식별성 floor 미달) >
   FALSIFIED > PASS`. 식별성 floor = 진입 세션(cluster) ≥ 30 (A5) — 미달 시 개별 게이트
   판정값은 참고로 병기하되 PASS/FALSIFIED 를 선언하지 않는다.
2. YAML 4 게이트(filled≥300 · 평균 초과>0 · 군집 90% LCB>0 · 양수 연도≥6)는 **모두 실제
   fills 에서 합성**되어 단일 verdict 로 이어져야 한다 (단위 수식만 맞추는 구현 불인정).
3. **empty baseline**: A4 자기 포함 규칙상 체결된 전략 거래의 cohort 는 공집합이 될 수 없다
   (자기 자신이 항상 구성원 — 상폐 거래도 C8 로 포함). 따라서 공집합 발생 = 구현 오류 신호 —
   방어적으로 `RUN_INVALID_EMPTY_BASELINE` fail-closed (조용한 분모 제거 금지).

## A12 — field-level 가격 유효성 predicate

`valid_bar`(전 필드 finite·양수 — **volume 도 finite**) 는 **적격성/신호 계산(A8)** 에만 적용.
실행 단계는 필드별 predicate 로 분리한다:

| 단계 | 요구 필드 | 미충족 시 |
|---|---|---|
| entry (t+1 open) | `open` finite·>0 ∧ `volume` >0 (미거래 방지) | NO_FILL |
| 만기 (D+N close) | `close` finite·>0 ∧ `volume` >0 | 상폐 증거 있으면 C8, 없으면 RUN_INVALID_MATURITY_PRICE |
| 상폐 최종 유효가 탐색 | `close` finite·>0 ∧ `volume` >0 (event 세션 포함해 역탐색) | 없으면 −100% |
| liq (close×volume) | A8 전체 bar 유효성에 포함됨 | 모집단 제외 |

## A9 보강 (fail-closed 구체화 — 신규 결정 아님, 검증 벡터 의무화)

- 동일 symbol 이 양시장에 동시 존재하는 fixture + 시장별 독립 처리 golden.
- duplicate `(market,symbol,session)` 입력 → `RUN_INVALID_DUPLICATE_ROW` fail-closed.
