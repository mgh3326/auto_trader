# ROB-388 — KR `screen_stocks` 개장 경로 복구 설계

- **이슈:** ROB-388 (오케스트레이션 ROB-394의 1번)
- **날짜:** 2026-06-01
- **상태:** 설계 승인됨 → 구현 계획 대상
- **관련 리포트:** `investment_report` `report_uuid=4e20c131-617e-46cb-beed-2013695918ce` (kr/kis_live/nxt)

## 목표

2026-06-01 NXT 프리마켓 개장 어드바이저리 리포트 작업 중, 신규 매수 후보 발굴의 핵심 경로인
`screen_stocks(market="kr")`가 개장 타이밍에 두 가지로 실패했다. 이 두 결함을 **좁은 수정 + 테스트**로
복구해 KR 발굴 entrypoint를 신뢰 가능하게 만든다. 큰 리팩터는 하지 않는다.

## 증상 (재확인)

1. **`trade_amount` 스키마-동작 불일치 (false affordance):**
   MCP `screen_stocks` 도구의 `sort_by` Literal enum이 `trade_amount`를 **모든 시장에 유효한 것처럼**
   노출하지만, 호출 시 `_validate_screen_filters`가 비-crypto에 대해
   `ValueError: 'trade_amount' sorting is only supported for crypto market`로 거부한다.
   → 스키마가 사실과 다르다.

2. **`KRX session expired after re-auth`:**
   `sort_by="change_rate"` 재시도 시 KRX 세션이 만료되고, 재인증 후에도 LOGOUT이면
   `app/services/krx.py`의 `fetch_data`가 raw `httpx.HTTPStatusError("KRX session expired after re-auth")`를
   raise한다. 이 예외가 KR 스크리닝 경로 전체로 전파되어 발굴 도구가 개장 직후 unhandled 에러로 죽는다.

## 코드 현황 (근거)

- `app/mcp_server/tooling/analysis_registration.py:210` — `sort_by`가 단일 `Literal[... "trade_amount" ...]`로
  모든 시장 공유. 시장별 차등 불가.
- `app/mcp_server/tooling/screening/common.py:577` — `else`(비-crypto) 블록에서 `trade_amount`를 일괄 거부.
- `app/mcp_server/tooling/screening/common.py:686` — `_sort_and_limit`의 `sort_field_map`이
  `trade_amount` → `trade_amount_24h`(crypto 전용 필드)로만 매핑.
- `app/services/krx.py:517 / 592` — KRX 종목 데이터는 이미 `value = ACC_TRDVAL`(거래대금) 필드를 보유.
  → **KR `trade_amount` 정렬은 데이터가 이미 있어 좁게 지원 가능.**
- `app/services/krx.py:234` — 재인증 후 LOGOUT 시 raw httpx 예외 raise.
- `app/mcp_server/tooling/screening/response.py:44` — `_build_screen_response`가 `warnings` +
  `meta_fields`를 지원 → 스키마 변경 없이 구조화 신호 전달 가능.
- `app/mcp_server/tooling/screening/kr.py:622` — `_screen_kr_with_fallback`(tvscreener → legacy `_screen_kr`)가
  KR 스크리닝 경계.

## 설계

### 변경 1 — KR `trade_amount` 정렬 실제 지원

방향: false affordance를 **"진짜 기능"으로 전환**(가장 정직한 해소). KR 데이터에 이미 거래대금(`value`)이
있으므로 좁게 구현한다. US는 데이터 경로가 다르므로 이번 범위에서 제외하되 actionable error로 명확화한다.

1. **`common.py::_validate_screen_filters`** — 비-crypto `else` 블록을 market-aware로 변경:
   - KR 계열(`kr`/`kospi`/`kosdaq`/`konex`/`all`): `trade_amount` **허용**.
   - 그 외(US 등): `trade_amount` 거부하되 메시지를 actionable하게
     (예: `"'trade_amount' sorting is supported for KR and crypto; for US use 'volume', 'market_cap', or 'change_rate'."`).
2. **KR 정렬이 `trade_amount`를 거래대금으로 처리:**
   - KR 행 정규화 시 `row["trade_amount"] = row["value"]`(ACC_TRDVAL)를 명시적으로 노출.
     legacy `_screen_kr`와 tvscreener `_screen_kr_via_tvscreener` 양쪽 모두 일관되게.
   - `_sort_and_limit`의 `trade_amount` 해석을 fallback 체인으로:
     `trade_amount_24h`(crypto) → `trade_amount`(KR). US는 validation에서 차단되므로 도달하지 않음.
3. **tool description 보완:** `analysis_registration.py`의 `screen_stocks` description에 1줄 추가 —
   `trade_amount`는 KR/crypto 지원, US 미지원 (단일 Literal enum 한계를 문서로 보완).

### 변경 2 — KRX session-expired 구조화 분류

방향: raw 예외 전파 대신 **분류 가능한 타입 + 구조화 신호** 반환. live 세션 재현은 operator-gated이므로
동작 변경(prewarm/auto-retry)은 범위 밖으로 두고 분류·표면화에 집중한다.

1. **`krx.py`** — 전용 예외 `KRXSessionExpiredError`(httpx.HTTPStatusError 하위로 정의해 기존 호출자 호환)를
   `fetch_data`의 재인증-후-LOGOUT 분기에서 raise. 호출자가 타입으로 분류 가능.
2. **KR 스크리닝 경계**(`_screen_kr_with_fallback` 또는 `_screen_kr`)에서 `KRXSessionExpiredError`를 catch →
   빈 결과 + 구조화 신호 반환 (`_build_screen_response`의 기존 인자 사용, 스키마 변경 없음):
   - `meta.data_state = "unavailable"`
   - `meta.retryable = true`
   - `meta.reason = "krx_session_expired"`
   - `warnings = ["KRX 세션이 만료되어 KR 스크리너를 일시적으로 사용할 수 없습니다. 잠시 후 재시도하세요."]`
3. **범위 한정:** 개장 전 세션 prewarm / short-backoff auto-retry는 이번 PR에 포함하지 않는다.
   live KRX 타이밍 의존이라 fake로 완전 검증 불가 → handoff/blocker로 문서화.

## 테스트 (fake / unit, read-only)

- **T1:** `_validate_screen_filters` — KR(`kr`/`kospi`/`kosdaq`)에서 `trade_amount` 허용(예외 없음),
  US에서 actionable error(메시지에 대안 정렬 키 포함) raise.
- **T2:** `_sort_and_limit` — KR fake rows(`value` 보유, `trade_amount_24h` 없음)가 거래대금 내림차순으로
  올바르게 정렬되는지. crypto fake rows(`trade_amount_24h` 보유)는 기존대로 정렬되는지(회귀 방지).
- **T3:** KR 스크리닝 경계 — fake KRX client가 `KRXSessionExpiredError`를 던질 때, raise 전파 대신
  `data_state="unavailable"` + `retryable=true` + warning을 담은 응답을 반환하는지.

## 안전 경계

- broker/order/watch/order-intent mutation 없음. 전부 read-only.
- 스키마 enum 추가/DB 마이그레이션 없음.
- `recommend_stocks` MCP 재노출 없음.
- scheduler/Prefect 등록·활성화 없음. prod env/secrets 출력·커밋 없음.
- 좁은 수정 우선; 무관한 리팩터 금지.

## 산출물 / 핸드오프

- 독립 PR (base: `origin/main`, worktree `auto_trader.rob-388` / branch `rob-388`).
- 정확한 검증 명령 + 결과를 PR 및 ROB-394 handoff 코멘트에 기록.
- 잔여 blocker(KRX prewarm/auto-retry live 검증)를 명시 후 ROB-389로 인계.

## 비목표 (Out of scope)

- US `trade_amount` 정렬 실제 구현.
- KRX 세션 prewarm / 자동 재시도의 live 검증.
- `sort_by` enum의 시장별 분리(단일 Literal 유지, description으로 보완).
- 모멘텀/`candidate_universe` freshness(= ROB-389), NXT orderbook(= ROB-390) 등 후속 이슈 범위.
