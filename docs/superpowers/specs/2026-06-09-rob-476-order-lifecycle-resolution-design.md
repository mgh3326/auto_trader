# ROB-476 — 주문 lifecycle 해소 + 라우팅 가시성 설계

- **Linear**: ROB-476 `[Bug/DX] 주문 lifecycle 불명확 — 만료 day order가 pending 무한 잔류 + SOR/NXT 라우팅 상태 미표시`
- **Priority**: Medium (label: Bug)
- **날짜**: 2026-06-09
- **선행/인접**: ROB-395 (fill-evidence gate), ROB-463 (NXT venue/TIF — 보완관계)
- **PR**: 단독 PR (ROB-475와 분리)

## 문제

1. **만료 day order가 pending 무한 잔류**: `reconcile`이 KRX 마감(15:30)을 지난
   미체결 day order를 계속 `verdict:"pending"`, `action:"noop_pending"`로 남긴다
   (만료/취소로 해소 안 됨). 증거(2026-06-09): 09:43 주문 4건이 15:38 reconcile
   시점(마감 후)에도 pending.
2. **place_order 응답에 라우팅 가시성 없음**: 스키마 설명엔 "auto-route via SOR
   (NXT-eligible)"라 돼 있으나 실제 응답엔 venue/session/order_validity/예상
   만료시각이 없다 → 운영자가 "NXT에서 살아있나 phantom인가"를 추정만 함.

## 근본 원인 (코드 기준)

- `_reconcile_one_ledger_row` (`kis_live_ledger.py:407`): 매칭된 broker daily-order
  row가 있으나 `filled_qty<=0`이면 `classify_fill_evidence`가 verdict `PENDING` →
  `action="noop_pending"`, ledger 무변경. **연령/만료/세션 마감 로직이 전무.**
  (매칭 row가 **없으면** 이미 `cancelled`로 해소됨 — 이 버그는 row가 **있는데**
  미체결로 남는 PENDING 경로.)
- `_record_kis_live_order` (`kis_live_ledger.py:155`)의 응답
  (`kis_live_ledger.py:219-242`)에 라우팅/만료 필드 없음.

## 접근 (결정됨)

### Part A — 만료 pending 해소 (Bug). Hybrid: evidence-first, time-guard fallback.

PENDING 분기에서 **만료 분류기**를 추가한다:

1. **Evidence-first**: broker daily-order row의 `prcs_stat_name`(처리상태명) /
   `rvse_cncl_dvsn_cd`(정정취소구분)이 명확히 취소/거부(terminal)를 가리키면 해소.
   (이 필드들은 응답에 존재하며 `orders_modify_cancel.py:154,178`에서 이미 소비됨.)
2. **Time-guard fallback**: order_validity=day(현재 전부 day)이고 해당 `ord_dt`의
   KRX 세션이 마감되었으며(해당 날짜의 15:30 KST 경과) 여전히 **완전 미체결**이면
   `expired`로 표시.
3. **Fail-closed**: 모호하면(당일 마감 전, 또는 NXT 이월 불확실) `pending` 유지.

reconcile 결과: `status="expired"`, `action="marked_expired"`(dry_run=False) /
`"would_mark_expired"`(dry_run=True). **부분체결 row는 범위 외** — 이미 booking
되어 "무한 pending" 버그가 아니다. 완전 미체결 PENDING만 expired로 해소한다.

`status`는 **CHECK 제약 없는 Text 컬럼**(migration `14fa36b85d0a`는 인덱스만) →
`expired` 값 추가는 **migration-0**. `get_order_history`는 ledger status를 그대로
읽으므로 자동으로 `expired` 반영(별도 코드 불필요, 확인만).

세션 마감 판정은 기존 KRX market-session 헬퍼(`market_session.py` /
`kr_market_data_state` 계열, ROB-464)를 재사용한다. 헬퍼가 "해당 날짜 마감 후"를
판정 못하면 plan 단계에서 명시적 15:30 KST 컷오프 헬퍼를 신설한다(순수 함수).

### Part B — place_order 응답 라우팅 가시성 (정직한 surface).

`_record_kis_live_order` 응답에 추가:
- `order_validity: "day"` (현재 항상 day).
- `routing: {"requested_venue": "auto", "note": "SOR auto-route (KRX; NXT-eligible)"}`.
- `expected_expiry`: 주문일 KRX 마감(15:30 KST), 산출 불가 시 `null`.
- `broker_exchange`: raw broker 응답의 거래소 필드(`EXCG_ID_DVSN_CD` 등)가 **있으면**
  그대로 반영, **없으면** omit/`null`. **절대 날조하지 않음.**

런북에 결정적 사실(day order는 KRX 마감에 만료)을 명문화하고, NXT 세션 이월
동작은 **operator-to-confirm**으로 표시(우리가 단정 못 하는 부분은 정직하게 미상).
ROB-463(NXT venue 파라미터 추가)과의 보완관계 명시.

## 컴포넌트

| 파일 | 변경 |
|------|------|
| `app/.../fill_evidence.py` 또는 신규 `kis_live_expiry.py` | 순수 만료 분류기 `classify_day_order_expiry(row, broker_rows, now_kst)` → `expired` \| `pending` |
| `app/mcp_server/tooling/kis_live_ledger.py` | `_reconcile_one_ledger_row` PENDING 분기에 분류기 배선; `_update_ledger_outcome(status="expired")`; `_record_kis_live_order` 응답에 routing/expiry 필드 |
| `app/.../market_session.py` (재사용 or 헬퍼 추가) | 해당 날짜 KRX 15:30 KST 마감 경과 판정 (필요 시 신규 순수 함수) |
| `docs/runbooks/kis-live-order-reconcile.md` | `expired` verdict + Part B 라우팅/만료 surface + NXT 이월 operator-to-confirm |
| 도구 설명 (`orders_kis_variants.py`) | place_order 응답 필드 + reconcile `expired` verdict 언급 |

## 데이터 흐름 (Part A)

```
reconcile → _reconcile_one_ledger_row(row)
  └─> classify_fill_evidence → verdict PENDING
        └─> classify_day_order_expiry(row, broker_rows, now_kst)
              ├─ evidence terminal(prcs_stat/rvse_cncl) → expired
              ├─ day + ord_dt 세션 마감 + 완전 미체결 → expired
              └─ else → pending (fail-closed, 기존 noop)
        └─> expired면 _update_ledger_outcome(status="expired"), action="marked_expired"
```

## 안전 경계

- **kis_live KR 한정**.
- **Fail-closed**: 모호하면 pending 유지(거짓 expired 금지).
- **No fabrication**: broker_exchange는 실제 응답 있을 때만; venue 추정/날조 없음.
- **부분체결 불변**: 이미 booking된 partial은 건드리지 않음.
- **Migration: 0** (status CHECK 없음; 응답 필드는 비영속).
- broker mutation/order 전송 경로 무변경 (reconcile 읽기 + ledger 갱신만; place_order는 응답 필드 추가만).

## 테스트

- 분류기 units:
  - time-guard로 expired (day + ord_dt 마감 + 미체결).
  - evidence로 expired (prcs_stat/rvse_cncl terminal).
  - 당일 마감 전 → pending 유지.
  - NXT 이월 가능/모호 → pending 유지 (fail-closed).
  - 부분체결 → 분류기 미적용(이미 partial booking).
- reconcile 통합: expired 표시 + action 값(dry_run T/F).
- place_order 응답: order_validity/routing/expected_expiry 포함; broker_exchange는
  raw 응답에 있을 때만 등장(없으면 부재/null).
- `get_order_history`가 expired 반영(확인 테스트).

## 미해결/후속

- 실제 NXT 세션 이월 동작 확정 = operator + KIS 문서 (런북에 to-confirm).
- venue 영속 컬럼 + 실 라우팅 캡처 = ROB-463 Phase 2 (별도, 이 PR 범위 외).
