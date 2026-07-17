# Binance Demo 스캘핑 — 매일 LLM 결정 세션 (Phase 3, 반자동)

매일 사람이 MCP 세션을 트리거해 LLM이 데모 스캘핑 결정을 주입한다.

## 전제
- `BINANCE_DEMO_SCALPING_ENABLED=true` (MCP 도구 등록 게이트) + futures demo 자격증명.
- 데모 전용. 실 주문은 `confirm=true`에서만.

## 절차 (MCP 세션)
1. **시장 읽기** — `get_crypto_*`(funding/OI/캔들) 및 최근 스캘핑 리뷰/벤치마크(`/invest/scalping`)와
   과거 결정·결과(이전 `signal_snapshot`/리뷰)를 검토한다.
2. **결정** — symbol(XRPUSDT/DOGEUSDT/SOLUSDT 중)·side·근거(rationale)를 정한다.
3. **dry-run 예비판정** — `binance_demo_scalping_submit_decision(symbol, side, rationale, dry_run=true)`.
   dry-run은 단순 계획 에코가 아니다: 실 주문과 **동일하게** Demo 호스트에서 bookTicker + 최신 1m
   kline을 서버가 관측해 `MarketConditions`(spread/data-age)를 구성하고, DB 리스크 프리플라이트
   (ledger 스냅샷 읽기 + 사이징)를 수행한다. 단, **broker mutation과 ledger insert는 하지 않는다**.
   따라서 dry-run 응답 `status`는 다음 중 하나다:
   - `planned` — 서버 관측 조건 통과 + 사이징 성공(주입 가능). `market_conditions`(관측 spread/age) 동봉.
   - `blocked` — 기존 리스크 게이트 위반. `reason_codes`에 `spread_too_wide`/`stale_data`/
     notional·cooldown 등 사유가 그대로 노출된다(신규 게이트 아님).
   - `market_conditions_unavailable` — bookTicker/kline 수집 실패·빈/malformed·timestamp 부재·
     비유한(NaN/±Inf)·crossed 호가. 이 경우 broker·ledger 접촉 0.
   dry-run이 `blocked`/`market_conditions_unavailable`이면 주입을 시도하지 말고 조건을 재확인한다.
4. **주입** — `...(dry_run=false, confirm=true)`로 실 데모 라운드트립 실행. confirm 경로도 dry-run과
   동일한 server-derived 조건·리스크 프리플라이트를 거친 뒤에만 실 주문을 낸다. 결과(status/realized_pnl)를 기록.
5. **회고** — 다음 세션에서 직전 결정의 결과(net vs buy&hold, LLM vs 규칙 baseline)를 보고 전략을 조정한다.

## 안전
- 1x · notional<=10 USDT · 손실예산 게이트(Phase 1) · demo-fapi only — executor가 강제.
- spread/data-age는 **서버 관측값만** 판정에 사용한다. caller/LLM이 spread·age를 입력할 수 없고,
  관측 실패 시 0/0으로 합성하지 않고 `market_conditions_unavailable`로 fail-close 한다(ROB-841).
- 같은 날 결과는 `session_tag="llm"`로 분리 집계(규칙 baseline과 비교; surfacing은 D-PR2).

## Validated-signal gate 면제 (ROB-937)

이 대화형 LLM 경로(`binance_demo_scalping_submit_decision`)는 ROB-905 validated-signal
gate 적용을 **의도적으로 면제**한다. ROB-905 gate는 반복 스케줄러 틱
(`run_demo_scalping_tick`)에만 배선되어, gate 아티팩트가 `validated`가 아니면
`confirm=true`를 dry-run으로 다운그레이드한다. 그 gate는 이 MCP 도구에는 적용되지
않는다 — 즉 gate 아티팩트가 denied여도 이 경로의 실 데모 라운드트립은 그대로 집행된다.

이 경로의 권한(authorization)은 gate 아티팩트가 아니라 **다른 가드 스택**이다:

- (a) registry opt-in — `binance_demo_scalping_enabled` (미설정 시 도구 자체가 미등록)
- (b) 사람 per-call `confirm=true` (매 호출마다 명시해야 실 주문)
- (c) ROB-841 market-conditions fail-close (서버 관측 실패 시 `market_conditions_unavailable`)
- (d) allowlist(XRPUSDT/DOGEUSDT/SOLUSDT) · 1x · notional≤10 USDT (executor 강제)

### 응답 감사 필드
- `validated_signal_gate: {"allowed": bool, "reason": str}` — 실행 전 1회 평가한 gate 판정을
  **감사용으로만** 에코한다. `allowed=false`(예: `gate_path_unset`)여도 라운드트립은 집행된다.
  dry-run·confirm·`market_conditions_unavailable`·error 응답 모두에 실린다(관측 일관성).
- `authorization_mode` — 실 주문(confirm=true) 응답은 `"operator_interactive_exception"`,
  dry-run/미confirm 응답은 `"dry_run"`. gate가 아니라 사람 인가로 집행됐음을 명시하는 마커.

교차참조: ROB-905(스케줄러 gate)·ROB-316(OOS gross-negative 신호 posture)·ROB-937(이 면제 명시).

### ⚠ 전방 위험
`confirm=true`의 인가 전제는 **사람 per-call 승인**이다. 만약 비대화형 자동 caller
(예: ROB-840 champion/challenger 러너)가 이 경로를 구동하면 사람-인가 전제가 소멸하므로,
그 경우엔 이 경로에도 validated-signal gate 강제(차단/다운그레이드)를 별도 후속으로 배선해야 한다.

## MCP 배포 반영 절차
MCP 배포 반영 절차 = ops/native/scripts sync → mcp 재기동, BINANCE_*는 래퍼의 접두사 export에 의존. `BINANCE_*` 접두사(예: `BINANCE_FUTURES_DEMO_ENABLED` 등)를 통해 노출되는 환경 변수들은 MCP 기동 래퍼의 접두사 export 기능을 거쳐 `os.environ`에 공급되므로, 배포 시 스크립트 동기화와 MCP 재기동이 필수적이다.
