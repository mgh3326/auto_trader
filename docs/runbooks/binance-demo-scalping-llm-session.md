# Binance Demo 스캘핑 — 매일 LLM 결정 세션 (Phase 3, 반자동)

매일 사람이 MCP 세션을 트리거해 LLM이 데모 스캘핑 결정을 주입한다.

## 전제
- `BINANCE_DEMO_SCALPING_ENABLED=true` (MCP 도구 등록 게이트) + futures demo 자격증명.
- 데모 전용. 실 주문은 `confirm=true`에서만.

## 절차 (MCP 세션)
1. **시장 읽기** — `get_crypto_*`(funding/OI/캔들) 및 최근 스캘핑 리뷰/벤치마크(`/invest/scalping`)와
   과거 결정·결과(이전 `signal_snapshot`/리뷰)를 검토한다.
2. **결정** — symbol(XRPUSDT/DOGEUSDT/SOLUSDT 중)·side·근거(rationale)를 정한다.
3. **dry-run 확인** — `binance_demo_scalping_submit_decision(symbol, side, rationale, dry_run=true)`로 계획 확인.
4. **주입** — `...(dry_run=false, confirm=true)`로 실 데모 라운드트립 실행. 결과(status/realized_pnl)를 기록.
5. **회고** — 다음 세션에서 직전 결정의 결과(net vs buy&hold, LLM vs 규칙 baseline)를 보고 전략을 조정한다.

## 안전
- 1x · notional<=10 USDT · 손실예산 게이트(Phase 1) · demo-fapi only — executor가 강제.
- 같은 날 결과는 `session_tag="llm"`로 분리 집계(규칙 baseline과 비교; surfacing은 D-PR2).
