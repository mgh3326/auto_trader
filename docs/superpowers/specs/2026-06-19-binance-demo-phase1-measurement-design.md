# Binance Demo 스캘핑 Phase 1 — 측정 신뢰화 (Design)

- **작성일**: 2026-06-19
- **상태**: 승인됨 (브레인스토밍 → 스펙)
- **브랜치**: `feature/binance-demo-phase1-measurement`
- **선행 분석**: 본 세션의 2개 조사 워크플로 (히스토리/실패원인 + Phase 0 심층 정독)

## 1. 배경 / 동기

사용자 목표: Binance USD-M Futures Demo(`demo-fapi`)에서 **매일 자동 모의투자 루프**를
돌리며 시행착오로 전략을 고쳐나간다. 과거 백테스트 라인(ROB-316/320/353/362/382/383)은
모두 실패했고 ROB-384가 "단기 크립토 OHLCV 전략 라인 CLOSED"로 공식 종료했다. 실패의
근본원인은 method가 아니라 **search-space(탐색한 전략 공간에 gross 엣지 없음)**였다.

따라서 모의 루프의 가치는 "없던 엣지를 만드는 것"이 아니라 **인프라+회고 루프를 빠르게
돌리며 다른 시그널 소스(LLM/이벤트/펀더멘털)를 싸게 시험**하는 데 있다. 이를 위해서는
루프의 수치를 **신뢰**할 수 있어야 한다. 백테스트 실패의 4대 교훈을 모의에 그대로 반영한다:

1. net(수수료+슬리피지 차감) 측정 필수 — "모의니까 비용 무시"하면 라이브 전환 때 동일 실패
2. buy&hold 벤치마크 내장 — 능동 전략이 passive를 net으로 이기는지 처음부터 측정
3. single-fold 자기기만 경계 — 며칠 좋은 결과 ≠ 엣지
4. score-hacking 경계 — 단일 지표 최대화 금지(net+거래수+time_in_market 동시 관찰)

## 2. 범위

Phase 1 = **매일 모의 루프의 수치(손실 통제 + "유의미한가" 판단)를 신뢰 가능하게 만든다.**
신규 기능이 아니라 **demo_scalping 인프라가 설계상 비워둔 자리 2곳을 채우는 것**이다.

대상 경로: `app/services/brokers/binance/demo_scalping*` (executor/scheduler/analytics/rollup).
Phase 0의 `scripts/binance_futures_demo_smoke.py`는 ledger만 쓰고 analytics/회고가 없으므로
Phase 1은 **executor 경로**(`scripts/binance_demo_scalping_execute.py` / `run_scalping_tick`)를 대상으로 한다.

### 2.1 In Scope (두 변경)

- **변경 A — `realized_pnl_usdt` 기록 (결함#1 수리, 접근법 1A)**
- **변경 B — 일별 buy&hold 벤치마크 (접근법 2A)**

### 2.2 Out of Scope (명시적 제외)

- A/B `session_tag`/`signal_snapshot` 배선 → Phase 3 (LLM 시그널 비교 시)
- `trade_retrospectives` binance account_mode 마이그레이션(결함#2) → 스캘핑은 `scalping_daily_reviews`
  를 쓰므로 불필요. 범용 회고 테이블로 통합할 때만 필요.
- 매일 자동 cron(TaskIQ daily schedule) → Phase 2
- LLM 시그널 주입(out-of-process) → Phase 3
- `daily_order_count_cap` "하루 1회" 튜닝 → operator config(코드 변경 아님)
- 5bps 수수료 추정을 실 Demo 커미션으로 교체 → 측정 caveat, 후속 가능

## 3. 변경 A — `realized_pnl_usdt` 기록 (1A)

### 3.1 문제 (검증된 결함#1)

손실예산 가드는 `LedgerSnapshot.realized_loss_today_usdt`에 의존한다
(`demo_scalping/contract.py:163` — `realized_loss_today_usdt >= daily_loss_budget_usdt`이면
`DAILY_LOSS_BUDGET_EXHAUSTED`). 이 값은 `ledger_state._realized_loss_today`가
**closed 행들의 `extra_metadata['realized_pnl_usdt']` 중 음수의 magnitude를 합산**해 만든다
(`demo_scalping/ledger_state.py:34-44`).

그러나 `demo_scalping_exec/executor.py` 어디에서도 `realized_pnl_usdt`를 ledger에 **쓰지 않는다**
(grep 0건). `ledger_state.py:9-11`의 docstring이 이를 명시한다:
*"Until PR2 writes that key, the sum is 0."* — 즉 설계상 비워둔 후속(PR2)이며,
현재 손실예산 가드는 **영구 비발화(inert)**다. 매일 자동 루프로 가면 손실 누적 차단 부재 = 위험.

### 3.2 설계

`build_round_trip_economics(...)`(현재 `_finalize_analytics` 내부, `executor.py:266-274`) 호출을
**close 행의 `record_reconciled` 전으로 1회 끌어올린다.** econ은 순수 계산(I/O 없음)이라 위치
이동이 안전하다. 산출된 `econ.net_pnl_usdt`(부호 포함, 5bps 차감 net)를:

1. **close 행의 reconcile `extra_metadata`에만** `realized_pnl_usdt`로 기록한다.
   `_exit_metadata`(`executor.py:104-113`)를 확장하거나, close 행 `record_reconciled` 호출에만
   해당 키를 병합한다. **open 행은 기록하지 않는다** → `_realized_loss_today`가 모든 closed 행을
   합산하므로 라운드트립당 **정확히 1회**만 집계되어 이중계상을 방지한다.
2. `_finalize_analytics`는 **같은 `econ`을 재사용**한다(재계산 없음) → 게이트↔분석 수치 drift 없음.

### 3.3 규약

- 저장 형식: 문자열(`str(Decimal)`) — `ledger_state.py:41`의 `Decimal(str(raw))`과 호환.
- 부호: **음수 = 손실**(`ledger_state.py:42`가 `pnl < 0`일 때 `-pnl` 누적). net_pnl_usdt를 부호
  그대로 저장한다.
- 통화: USDT (USD-M 결제통화).
- partial 행(`entry_fill is None`, `executor.py:261` → econ 계산 불가): `realized_pnl_usdt`
  **미기록**. 게이트가 그 라운드트립을 단순 미집계(체결 증거 없는 PnL 날조 금지, ROB-313/315 원칙 유지).

### 3.4 구현 전 검증 포인트

- `BinanceDemoLedgerService.closed_rows_since`가 **close 행을 포함**하는지 확인
  (포함해야 close 행 단독 기록으로 단일집계 성립). 플랜 1단계에서 확인.
- `record_reconciled`가 `extra_metadata`를 받아 병합하는 정확한 시그니처 확인.
- close 행 reconcile 호출 지점(`executor.py:911/918/1030/1037` 중 close 행에 해당하는 것) 식별.

## 4. 변경 B — 일별 buy&hold 벤치마크 (2A)

### 4.1 문제

코드 어디에도 거래 벤치마크(buy&hold/passive)가 없다(grep 결과 144건 전부 ROB-271
`invest_benchmark_gap`로 무관). 백테스트가 능동 전략이 BTC buy&hold(+359%)에 net으로
완패한 교훈상, **벤치마크 없이는 "엣지"를 판정할 수 없다.**

### 4.2 설계

- **Additive 마이그레이션**: `scalping_daily_reviews.benchmark_return_bps` (Numeric, nullable).
- **순수 함수**: `daily_buy_and_hold_return_bps(open_price, close_price) -> Decimal`
  = `(close_price / open_price - 1) * 10000`.
- **가격 출처**: 일별 리뷰 드래프트 시점(`ScalpingReviewService` / `rollup`)에 기존 `market_data`로
  그날(UTC 일경계, `ledger_state._start_of_day_utc`와 동일 규약) **시가/종가 캔들**을 fetch.
- **grain 정합**: rollup 단위(일별 집계 행)에 맞춘다. 다종목 거래 시 `benchmark_return_bps` =
  **notional 가중평균**(전략 `net_return_bps = sum(net)/sum(notional)`와 동일 규약,
  `rollup.py:69-134`). 종목별 상세는 `source_payload` JSONB에 audit용으로 저장.
- **노출**: 일별 리뷰에 전략 `net_return_bps` vs `benchmark_return_bps`(+델타)를 나란히.

### 4.3 구현 전 검증 포인트

- `scalping_daily_reviews`의 grain(일별 1행 / (일,종목) / (일,account_scope))을 확인해
  `benchmark_return_bps` 컬럼 의미와 가중 방식을 grain에 정합시킨다.
- `market_data`의 일봉(또는 1m first/last-of-day) fetch 인터페이스 확인.

## 5. 데이터 흐름

```
[매일 수동 tick] execute_monitored
   → (변경A) econ 1회 계산(reconcile 전)
     → close행 extra_metadata.realized_pnl_usdt(durable txn)
     → analytics행(같은 econ 재사용)
[같은 날 다음 tick] ledger_state._realized_loss_today = 실값
   → evaluate_risk가 손실 누적 ≥ 예산이면 DAILY_LOSS_BUDGET_EXHAUSTED로 차단
[일 마감] ScalpingReviewService 일별 리뷰 드래프트
   → (변경B) 그날 시가/종가 fetch → benchmark_return_bps → 전략 vs 패시브
```

## 6. 에러 처리

- econ 이동은 순수 계산 → 안전. `realized_pnl_usdt` 쓰기는 **durable reconcile 트랜잭션**의
  일부(의도적 — 손실 게이트가 권위 있으려면 best-effort savepoint가 아니어야 함).
- partial/no-fill → `realized_pnl_usdt` 미기록(게이트 미집계, 날조 없음).
- 벤치마크 klines fetch 실패 → `benchmark_return_bps` NULL, 리뷰는 전략 net만으로 정상 렌더
  (best-effort, 로그). 리뷰를 절대 차단하지 않는다.
- 마이그레이션 additive + nullable → 안전, operator `alembic upgrade head` 게이트.

## 7. 테스트

- **단위**
  - close 행에만 `realized_pnl_usdt` 기록(단일집계 단언, open 행 미기록 확인)
  - 같은 날 누적 손실 ≥ 예산 시 `DAILY_LOSS_BUDGET_EXHAUSTED` 발화
  - partial 행 → `realized_pnl_usdt` 미기록
  - `daily_buy_and_hold_return_bps` 순수함수(부호 / open==close → 0 / 음·양)
  - 벤치마크 klines 실패 → `benchmark_return_bps` NULL
  - 다종목 → notional 가중 벤치마크
- **통합**
  - 같은 날 2-tick 시퀀스: 1차 손실 → 2차가 손실예산으로 차단되는 end-to-end
- **회귀**
  - 기존 analytics/reconcile 테스트 그대로 통과(econ 값 불변, 위치만 이동)

## 8. 위험 / 함정

- `realized_pnl` 이중계상(open+close 둘 다 기록 시) → close 단독 기록 + 단일집계 테스트로 방지.
- 5bps 수수료 추정 + funding=0 가정(`cost.py`)이 USD-M net을 낙관 편향 → net을 "실제값"으로
  단정 금지(측정 caveat). 후속에서 실 커미션 교체 가능.
- 벤치마크 grain 불일치 → §4.3 검증으로 rollup grain에 정합.
- `closed_rows_since`가 close 행 미포함이면 집계 0 → §3.4 검증 필수.

## 9. 산출물 / 완료 기준

- executor가 close 행에 `realized_pnl_usdt`를 기록하고, 같은 날 누적 손실이 예산 초과 시
  다음 진입이 `DAILY_LOSS_BUDGET_EXHAUSTED`로 차단됨(통합 테스트 green).
- 일별 리뷰가 `benchmark_return_bps`를 채우고 전략 net vs passive를 나란히 노출.
- additive 마이그레이션 1개(operator `alembic upgrade head` 게이트).
- 전 테스트 green, 기존 회귀 없음.
