# KR 엔진 amendment A15 — 만기 결측 의미론 정밀화 (✅ 확정)

```
작성: claude-mock (상류 분석, wB:p4R) · 2026-08-05 15:1x KST
지위: ✅ 확정 — 운영자가 claude-mock 대화에서 승인 (결정 정본 §10차).
     첫 KR-A1 실행 전 확정 — post-hoc 오염 없음 (KR-A1 은 0/2 실행 상태에서 fail-closed 중).
근거: KR-A1 pre-run admission refusal (orchmock-kra1-blocked-20260805-1425.md) —
     봉인 corpus membership 은 presence-only 이며 수집기 계약이 «부재 ≠ 상폐» 를 명시.
     기존 A6-3(증거 없는 만기 결측 = RUN_INVALID)은 실코퍼스에서 보유 중 정지/상폐 1건이
     10년 run 전체를 무효화 — 운영 불능.
```

## A15-1 — membership 입력 계약 (presence-native)
- 엔진 membership 입력 = **presence-only** (session × market × symbol 존재 여부).
- interval 은 presence 에서 기계 변환 (interval = 연속 presence 구간). 이는 **상장 상태가 아니라
  관측 구간**이다 — `status=delisted` 는 별도 상폐 이벤트 정본(`kr-delist-events-v1`) 에
  증거가 있을 때만 부여 (A7 원칙 유지).

## A15-2 — 상폐 이벤트 정본
- `kr-delist-events-v1`: `(market, symbol, delist_date, reason)` + 소스 증빙 + SHA 봉인.
  소스 primary = KRX 정보데이터시스템 (연구 자격증명). 봉인 corpus 무접촉 별도 artifact.
- coverage audit 의무: presence-종료 세션과 이벤트일 교차 대사 — 근사 일치율과 불일치
  목록 공개 (조용한 보정 금지).

## A15-3 — 만기 결측 처리 (A6-3 대체)
- 상폐 **증거 있음** (이벤트 ≤ exit_due): C8 terminal (마지막 유효 종가 / 없으면 −100%,
  `delisted_exit=true`) — 기존 A7 그대로.
- 증거 **없음** + 만기 bar 결측/무효: **해당 거래를 `missing_exit` 로 제외 + 카운트 공개**
  (수익 집계·게이트 분모에서 제외, 은폐 금지). run 은 계속.
- **상한**: `missing_exit / 총 체결 > 5%` 이면 해당 후보 verdict = `INCONCLUSIVE_MISSING_EXITS`
  (PASS/FALSIFIED 선언 금지).
- market transfer 감지(A13-4)는 missing_exit 판정보다 **선행** — 기존 순서 유지.
- crypto 반환 계약(`missing_exit=true 별도 집계`)과 동일 의미론 — 시장 간 정합.

## 적용 범위
- KR 전용. **US 는 불변** — US 후보 동결 문안이 «만기 close 부재 = run-invalid» 를 명시
  (survivor universe). crypto 는 자체 계약이 이미 A15-3 과 동일.
- golden: v5 의 `invalid_maturity` variant 기대와 maturity 분기·verdict 합성이 변경됨 →
  **golden v6 슬라이스 재조립 + codex-mock 재확인 후** 엔진 개정 job 발행 (상류 소유).
