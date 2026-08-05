# [DRAFT — 운영자 확정 대기] KR 엔진 amendment 추가분 A13~A14

```
작성: claude-mock (상류 분석, wB:p4R) · 2026-08-05 10:5x KST
지위: ✅ 확정 — 운영자가 claude-mock 대화에서 2026-08-05 11:0x 승인 (+ v4→4차검증→PASS 시
      추가 왕복 없는 KR-A0 릴레이 자동 진행 승인). 결정 정본 §5차 확정 편입.
근거: codex-mock 3차 적대검증 answer-codexmock-golden-xcheck3-1025.md — A9 문언 충돌
      (BLOCKER 2)과 verdict/연도 규약 공백(P1-6)은 구현 워커에게 넘길 수 없는 상류 결정.
      (경계 밖 접근 BLOCKER 1 은 결정이 아니라 구현 수정 — v4 에서 access-spy 로 봉인.)
```

## A13 — identity 의미론 단일화 (A9 문언 충돌 해소)

KRX 사실관계: 6자리 종목코드는 시점 기준 양시장 통틀어 유일하다. 같은 코드가 **동시에**
양시장에 존재 = 데이터 오염. 단 이전상장(KOSDAQ→KOSPI 등)으로 코드가 시장을 **이동**할 수
있다 (`003670` 사건의 실체).

1. identity = `(market, symbol)` 복합키 (A9 유지).
2. **동시 중복** (같은 symbol 이 같은 세션에 양시장 membership/bar 보유) =
   `RUN_INVALID_IDENTITY_CONFLICT` fail-closed.
3. **시장 이동** (membership 구간이 시장 간 disjoint): 두 `(market,symbol)` 인스턴스는
   독립 — cross-market 이력 연결(stitching) 금지. 이동 전 시장의 적격성은 그 시장 bar 만으로
   판정 (연속 이력이 끊기므로 이동 직후 자연 제외 — 보수적).
4. **보유 중 이동**: 전략 포지션의 만기 전에 해당 시장 membership 이 끝나고 같은 symbol 이
   타시장에 출현하며 상폐 증거가 없으면 `RUN_INVALID_MARKET_TRANSFER` fail-closed
   (조용한 stitching·조용한 상폐 해석 둘 다 금지, 발생 수 공개). 실코퍼스에서 이 사유
   run-invalid 가 유의미하게 발생하면 그때 운영자 에스컬레이션으로 정책 재결정.

## A14 — verdict·비용·연도 규약 pinning

1. **후보 top-level verdict = 43bp(base) 프로파일** 로 단일 선언. 83bp(sensitivity) 는
   병기 필수 — 83bp 에서 부호가 뒤집히면 `COST_SENSITIVE` 라벨을 붙인다 (veto 아님 —
   veto 승격은 별도 운영자 결정).
2. verdict 값은 축약 금지 — `UNIDENTIFIABLE_CLUSTERS` 등 전체 라벨 사용.
3. 양수 연도 게이트의 귀속·가중: 연도 = **진입 세션의 달력 연도**, 연도 내 집계 =
   entry-session 동일가중 평균 (A5 와 동일 가중 체계).
4. data-gap 의미 분리 (A6-2 정밀화): `RUN_INVALID_DATA_GAP` 은 **row/partition 부재**
   (존재성 검사) 에만. "행은 있으나 전부 무효" 는 A8 universe 제외 경로
   (→ `POPULATION_BELOW_FLOOR` 가능) — run-invalid 아님.
5. holdout 비접촉의 기계 증명: 엔진의 모든 iteration·데이터 검사·신호 계산은
   `t ≤ exploration_end` 로 닫고, **access-spy 검사** (경계 밖 bar read 카운트 == 0) 를
   golden acceptance 에 포함한다.
