# Binance Demo 스캘핑 LLM 결정 주입 (Phase 3 / Design)

- **작성일**: 2026-06-22
- **상태**: 승인됨 (브레인스토밍 → 스펙)
- **브랜치**: `feature/binance-demo-scalping-llm-decision`
- **선행**: Phase 1(측정 신뢰화, PR #1342) + Phase 2(일별 리뷰+벤치마크 자동화, PR #1345) merged·라이브

## 1. 배경 / 동기

매일 자동 데모 스캘핑 루프는 **규칙기반 시그널**(SMA 브레이크아웃)로 돌고 있고, Phase 1/2로 거래마다 net PnL + 일별 buy&hold 벤치마크가 측정된다. 측정 결과: 규칙기반은 예상대로 **수수료만 내는 baseline**(net ≈ -10bps/거래). 사용자 원래 비전의 핵심 = **"LLM이 시장을 보고 전략을 결정 → 투자 → 회고"**.

런타임은 in-process LLM을 못 부른다(AST guard, LLM 판단은 out-of-process). 따라서 Phase 3 = **out-of-process LLM(MCP 세션)이 내린 결정을 데모 스캘핑에 주입하는 결정론적 surface(MCP 도구)**. LLM은 도구의 *호출자*이고, 도구 자체엔 LLM 호출이 없다(경계 준수). 첫 슬라이스는 **반자동**: 사람이 매일 MCP 세션을 트리거 → LLM이 읽고 결정 → 도구로 주입.

## 2. 범위

### 2.1 In scope (D-PR1)
1. **session_tag 배선** — `DemoScalpingExecutor.execute`/`execute_monitored`가 `session_tag`를 받아 `_finalize_analytics`/`_record_partial_analytics` → `analytics.record(session_tag=...)`로 전달. (Phase 1에서 미룬 배선; LLM 트레이드를 `"llm"`으로 태깅해 규칙 baseline과 분리 측정하기 위해 필요.)
2. **결정 주입 MCP 도구** `binance_demo_scalping_submit_decision` — LLM이 결정(symbol/side/rationale[+선택])을 제출하면 `execute_monitored(session_tag="llm")` 1 라운드트립 실행 + 결정·근거를 `strategy_events`에 기록. dry_run 기본 + confirm 이중게이트.
3. **일별 LLM 세션 런북** `docs/runbooks/binance-demo-scalping-llm-session.md` — 매일 MCP 세션 절차.

### 2.2 Out of scope
- **D-PR2 (후속)**: Phase 2 flow를 session_tag별("", "llm") 반복 + 각 buy&hold 벤치마크 → `/invest/scalping`에 LLM vs 규칙 baseline 리뷰행 나란히 surfacing.
- LLM 자율 실행(Hermes) — 첫 슬라이스는 반자동(사람 트리거 MCP 세션).
- 회고 → 전략 자동 수정 루프(코드가 자동으로 전략을 바꾸지 않음; 사람/세션이 회고를 읽고 다음 결정에 반영).
- 신규 시장-읽기 도구 — LLM은 기존 `get_crypto_*` + 스캘핑 리뷰 read + `strategy_events` list로 컨텍스트 확보.

## 3. 컴포넌트

### 3.1 session_tag 배선 (executor → analytics)
- `DemoScalpingExecutor.execute(*, ..., session_tag: str | None = None)` 및 `execute_monitored(*, ..., session_tag: str | None = None)` 추가.
- `_finalize_analytics`/`_record_partial_analytics`가 `session_tag`를 `analytics.record(session_tag=...)`로 전달(`ScalpTradeAnalyticsService.record`는 이미 `session_tag` 인자 보유 — Phase 1 미배선 부분).
- 기본값 `None` → 기존 호출자(스케줄러 규칙기반)는 `session_tag=None`(=NULL, 규칙 baseline) 유지(무회귀).

### 3.2 MCP 도구 `binance_demo_scalping_submit_decision`
- 위치: `app/mcp_server/tooling/` (신규 모듈), 기존 broker order MCP 도구의 dry_run+confirm 패턴 준수.
- **입력**: `symbol`(allowlist XRP/DOGE/SOL only), `side`(BUY|SELL), `rationale`(비어있지 않은 str — LLM 근거, 필수), 선택 `tp_bps`/`sl_bps`(기본=executor 기본 30/20), `notional_usdt`(기본=risk cap 10), `product`(기본 `usdm_futures`), `dry_run`(기본 True), `confirm`(기본 False).
- **동작**:
  1. 게이트: `BINANCE_DEMO_SCALPING_ENABLED` + futures demo enabled. 미설정 → 명확한 disabled 응답(주문 0).
  2. allowlist/side 검증(네트워크 전).
  3. `build_order_intent`로 OrderIntent 구성(notional cap 핀).
  4. **dry_run(=not confirm)**: 계획(intent 요약)만 반환, 주문/기록 0.
  5. **confirm=True**: `execute_monitored(intent, confirm=True, session_tag="llm", ...)` 1 라운드트립(open→bounded TP/SL→reduceOnly close→reconcile) → 결정+근거+결과(status/realized_pnl/cids)를 `strategy_events`에 기록 → 결과 dict 반환.
- **반환**: `{status, dry_run, symbol, side, rationale, session_tag, ...실행결과 or 계획}`.

### 3.3 런북
매일 MCP 세션 절차: LLM이 (a) `get_crypto_*`(funding/OI/캔들) + (b) 최근 스캘핑 리뷰/벤치마크 + (c) 과거 `strategy_events` 결정·결과를 읽고 → 결정 + 근거 → `binance_demo_scalping_submit_decision(confirm=True)`. 안전: dry_run으로 먼저 확인 권장.

## 4. 데이터 흐름

```
[사람이 매일 MCP 세션 트리거]
 LLM: get_crypto_* + 스캘핑 리뷰/벤치마크 + strategy_events 읽기 → 결정(symbol/side/근거)
   → binance_demo_scalping_submit_decision(confirm=True)
        → execute_monitored(session_tag="llm")   # 기존 안전경계: demo-fapi/1x/cap/Phase1 손실게이트
        → scalp_trade_analytics 행(session_tag="llm") + 손실게이트 realized_pnl
        → strategy_events 기록(결정 + 근거 + realized_pnl)
 (D-PR2) 일별 리뷰가 session_tag="llm" 별도 행 → /invest/scalping에서 규칙 baseline과 비교
```

## 5. 경계 / 안전
- **LLM 호출 없음**: 도구는 LLM이 제출한 결정을 결정론적으로 실행만. LLM 판단은 MCP 호출자(세션) 소유 — 런타임 in-process LLM 경계 준수.
- **데모 전용**: 기존 `BinanceFuturesDemoExecutionClient` + `DemoScalpingExecutor` 재사용 → host allowlist(demo-fapi only)/1x/심볼 allowlist/notional cap/Phase1 손실게이트 전부 상속. 새 주문 mutation 경로 없음.
- **이중 게이트**: dry_run 기본 + `confirm=True` 필수(실 데모 주문). `BINANCE_DEMO_SCALPING_ENABLED` default-off.
- **ROB-285 audit**: 새 binance-참조 파일(MCP 도구 모듈)을 `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` ALLOWED_LEGACY_FILES에 등재(Phase 2 교훈).
- **무회귀**: `session_tag` 기본 None → 스케줄러 규칙기반 트레이드는 NULL 태그 유지.

## 6. 에러 처리
- 게이트 off / 자격증명 없음 → disabled/명확 에러(주문 0).
- allowlist 외 symbol / 잘못된 side / 빈 rationale → 검증 거부(네트워크 전).
- dry_run → 계획만(주문·기록 0).
- execute_monitored가 blocked(리스크 게이트, 예: 손실예산/cap) → 그 상태를 반환(주문 안 나감); anomaly → executor가 이미 처리(가짜성공 없음).
- strategy_events 기록 실패는 best-effort(거래는 이미 reconcile됨; 기록 실패를 로깅하되 거래 결과는 반환).

## 7. 테스트
- **session_tag 배선**: `execute_monitored(session_tag="llm")` → `scalp_trade_analytics` 행의 `session_tag == "llm"`; 미지정 시 NULL(무회귀). (fake client + db_session)
- **MCP 도구**:
  - dry_run(기본) → 주문 0, 계획 반환, strategy_events 0.
  - confirm → execute_monitored 호출(fake executor/client) + strategy_events 기록 + session_tag="llm".
  - 게이트 off → disabled(주문 0).
  - allowlist 외 symbol / 빈 rationale → 거부.
- 기존 executor/analytics 회귀(session_tag 추가가 기존 동작 무변경).

## 8. 위험 / 함정
- **measurability**: LLM 트레이드와 스케줄러 규칙 트레이드가 같은 날 섞이면 측정 혼란 → session_tag="llm"로 분리(리뷰 grain이 session_tag 포함). 비교 surfacing은 D-PR2.
- **dry_run 우회 금지**: confirm 없이는 절대 실주문 안 나가게(도구 기본 dry_run).
- **백테스트 교훈**: LLM도 baseline(-10bps 수수료)을 net으로 못 이기면 의미 없음 → D-PR2의 LLM vs 규칙 비교가 판정 도구. "며칠 좋은 결과 = single-fold 우연" 경계.
- **ROB-285 audit**: 새 파일 allowlist 등재 누락 시 CI fail(Phase 2에서 겪음).

## 9. 산출물 / 완료 기준
- executor `session_tag` 배선 + `analytics` 기록(태깅 테스트 green, 무회귀).
- `binance_demo_scalping_submit_decision` MCP 도구(dry_run/confirm/게이트/allowlist/기록 테스트 green).
- 런북.
- ROB-285 audit allowlist 갱신. 마이그레이션 없음.
- D-PR2(리뷰 비교 surfacing)는 후속.
