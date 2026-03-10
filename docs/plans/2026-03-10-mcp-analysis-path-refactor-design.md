# MCP Analysis Path Refactor Design

작성일: 2026-03-10  
상태: 승인안 (analyze-mode synthesized; user continued to documentation)

## 1. 배경

- 현재 MCP 분석 경로는 `app/mcp_server/tooling/analysis_tool_handlers.py`, `app/mcp_server/tooling/analysis_screening.py`, `app/mcp_server/tooling/analysis_screen_core.py`, `app/mcp_server/tooling/analysis_recommend.py`에 책임이 분산되어 있다.
- 공개 MCP 계약은 실제로 `app/mcp_server/tooling/analysis_registration.py`와 `app/mcp_server/tooling/analysis_tool_handlers.py`가 소유하지만, 내부 구현은 `analysis_screen_core.py`와 `analysis_screening.py`에 걸쳐 있고 handler가 core를 직접 import하는 경로가 남아 있다.
- `analysis_screening.py`는 이미 일부 compatibility shim 역할을 하고 있지만, `_analyze_stock_impl`과 `_recommend_stocks_impl`만 안정 경계에 가깝고 screening helper는 아직 충분히 수렴되지 않았다.
- 테스트 결합도 원안보다 넓다. `tests/test_mcp_screen_stocks.py`, `tests/test_mcp_recommend.py` 외에도 `tests/test_tvscreener_stocks.py`, `tests/test_tvscreener_crypto.py`, `tests/test_crypto_composite_score.py`, `tests/test_mcp_fundamentals_tools.py`, `tests/_mcp_tooling_support.py`가 내부 심볼과 monkeypatch surface에 직접 결합되어 있다.
- 따라서 이번 리팩토링은 "모듈 분해" 자체보다 "호환성 경계 고정 후 내부 이동"이 먼저다.

## 2. 요구사항 확정

- MCP 공개 tool 이름, 파라미터 시그니처, warning 문구, response key, registration 동작은 바꾸지 않는다.
- 공개 안정 경계는 `app/mcp_server/tooling/analysis_registration.py`와 `app/mcp_server/tooling/analysis_tool_handlers.py`다.
- migration 동안 `app/mcp_server/tooling/analysis_screening.py`는 compatibility facade로 유지한다.
- `app/mcp_server/tooling/analysis_screen_core.py:2635`의 `screen_stocks_unified(...)` 시그니처는 유지한다.
- `_analyze_stock_impl`은 새 구현 모듈로 옮길 수 있지만, `analysis_screening._analyze_stock_impl` alias는 유지한다.
- `_recommend_stocks_impl` facade는 유지하고, 실제 단계 분해는 `app/mcp_server/tooling/analysis_recommend.py` 내부에서 진행한다.
- 테스트 파일 분리는 production ownership이 안정된 뒤 마지막에 진행한다.

## 3. 대안 검토 및 선택

### 대안 A: 테스트 파일을 먼저 분리

- 장점: 최종 구조와 비슷한 테스트 레이아웃을 빨리 얻을 수 있다.
- 단점: 현재 테스트가 모듈 ownership이 아니라 import/patch 지형을 따라가고 있어서, 초반에 파일만 나누면 수정 범위가 커지고 compatibility risk는 줄지 않는다.

### 대안 B: handler import를 먼저 facade로 정리

- 장점: 표면상 import graph가 빨리 단순해진다.
- 단점: `_patch_runtime_attr`와 직접 monkeypatch하는 테스트들이 한 번에 깨질 가능성이 높다. shim이 준비되기 전에 handler binding을 바꾸는 것은 위험하다.

### 대안 C: facade-first internal extraction (채택)

- 장점: public MCP contract와 test patch surface를 먼저 고정한 뒤 내부 구현만 이동할 수 있다.
- 장점: crypto, analyze, recommend를 순서대로 분리하면서도 regression 원인을 국소화할 수 있다.
- 단점: 초기에는 shim/re-export가 잠시 늘어나서 구조가 완전히 깔끔해지기 전까지 중간 상태가 길어진다.

## 4. 아키텍처

### 4.1 공개 안정 경계

- `app/mcp_server/tooling/analysis_registration.py`는 MCP tool 이름과 handler binding의 최종 소유자다.
- `app/mcp_server/tooling/analysis_tool_handlers.py`는 MCP-facing facade다. 최종 상태에서는 screening/recommend/analyze 관련 import를 `analysis_screening.py` 하나로만 받도록 수렴한다.
- 단, migration 초반에는 handler가 기존 심볼을 계속 사용하더라도, 내부 구현 이동 전에 `analysis_screening.py`가 필요한 helper와 alias를 먼저 재노출해야 한다.

### 4.2 `analysis_screening.py`의 역할

- `analysis_screening.py`는 리팩토링 기간 동안 compatibility facade 역할을 맡는다.
- 유지 대상:
  - `_analyze_stock_impl`
  - `_recommend_stocks_impl`
  - `_error_payload`
  - ranking helper (`_get_us_rankings`, `_get_crypto_rankings`, `_calculate_pearson_correlation`)
  - handler가 필요로 하는 screening helper re-export (`screen_stocks_unified`, normalization/validation helper 등)
- 중요한 점은 "어디에 구현이 있느냐"보다 "handler와 tests가 어느 심볼을 patch/import하느냐"가 더 중요하다는 것이다.

### 4.3 Screening 분해

- 목표 모듈:
  - `app/mcp_server/tooling/analysis_screen_common.py`
  - `app/mcp_server/tooling/analysis_screen_kr.py`
  - `app/mcp_server/tooling/analysis_screen_us.py`
  - `app/mcp_server/tooling/analysis_screen_crypto.py`
- `analysis_screen_core.py`는 thin coordinator + fallback selector로 남긴다.
- `screen_stocks_unified(...)`는 계속 `analysis_screen_core.py`에 남기되, 내부에서 market별 collector/finalizer를 호출하는 형태로 얇게 만든다.
- 가장 먼저 뽑을 seam은 crypto 후처리다. 이유:
  - legacy path `_screen_crypto(...)`와 tvscreener path `_screen_crypto_via_tvscreener(...)`가 BTC crash fallback, warning-market filter, CoinGecko merge/stale warning, RSI sort coercion, `meta` 조립, final response shaping을 거의 같은 계약으로 반복한다.
  - 이 로직은 handler/import graph를 건드리지 않고도 추출할 수 있다.
- 새 crypto 공통 진입점은 `finalize_crypto_screen(...)`로 통합한다. 이 함수는 다음을 모두 책임진다.
  - BTC crash fallback
  - warning market filter
  - CoinGecko merge 및 stale/unavailable warning
  - RSI enrichment diagnostics 결합
  - `sort_by="rsi"` 강제 asc 처리
  - `filters_applied`/`meta`/response 조립

### 4.4 내부 타입

- dataclass 대신 `TypedDict`/type alias를 쓴다. 현재 구현과 테스트는 dict 중심 계약에 맞춰져 있기 때문이다.
- 도입 대상:
  - `ScreenCandidate`
  - `ScreenFilters`
  - `ScreenResponse`
- 이 타입은 public payload를 바꾸기 위한 것이 아니라 internal helper 간 계약을 문서화하는 용도다.

### 4.5 `_analyze_stock_impl` 이동

- `_analyze_stock_impl`의 최종 구현은 새 `app/mcp_server/tooling/analysis_analyze.py`로 이동한다.
- 그러나 `analysis_screening._analyze_stock_impl`는 stable alias로 유지한다.
- `analysis_tool_handlers.py`는 analyze path에 대해 새 모듈을 직접 import하지 않고 `analysis_screening` facade를 통해 계속 접근한다.
- 이 순서를 쓰는 이유는 `tests/test_mcp_fundamentals_tools.py`가 `_analyze_stock_impl` patch surface에 의존하기 때문이다.

### 4.6 Recommend 분해

- `app/mcp_server/tooling/analysis_recommend.py`는 내부 phase 함수로 분해한다.
- 권장 단계:
  - `_prepare_recommend_request(...)`
  - `_collect_kr_candidates(...)`
  - `_collect_us_candidates(...)`
  - `_collect_crypto_candidates(...)`
  - `_apply_exclusions_and_dedupe(...)`
  - `_apply_kr_relaxed_fallback(...)`
  - `_enrich_missing_rsi(...)`
  - `_score_and_allocate(...)`
  - `_empty_recommend_response(...)`
  - `_build_recommend_response(...)`
- `analysis_screening._recommend_stocks_impl` facade는 그대로 두고, handler는 그 facade를 계속 사용한다.

### 4.7 Handler import cleanup

- handler cleanup은 마지막 단계다.
- 목표 end state:
  - `analysis_tool_handlers.py`는 screening/recommend/analyze 관련 심볼을 `analysis_screening.py` 하나로만 import한다.
  - `analysis_screen_core.py`는 handler가 직접 import하지 않는다.
- 단, 이 cleanup은 다음 두 조건이 만족된 뒤에만 한다.
  1. `analysis_screening.py`가 필요한 helper를 모두 재노출한다.
  2. `tests/_mcp_tooling_support.py`의 `_PATCH_MODULES`와 direct monkeypatch tests가 새 shim surface를 기준으로 정리된다.

### 4.8 테스트 구조

- split 대상은 원안대로 유지한다.
  - `tests/test_mcp_screen_stocks_kr.py`
  - `tests/test_mcp_screen_stocks_tvscreener_contract.py`
  - `tests/test_mcp_screen_stocks_crypto.py`
  - `tests/test_mcp_screen_stocks_filters_and_rsi.py`
  - `tests/test_mcp_recommend_scoring.py`
  - `tests/test_mcp_recommend_flow.py`
- 다만 split은 마지막이다.
- 먼저 할 일은 공통 helper를 `tests/_mcp_tooling_support.py` 또는 신규 `tests/_mcp_recommend_support.py`에 추출해, production move와 test move를 분리하는 것이다.

## 5. 구현 순서

1. `analysis_screening.py`에 필요한 re-export/shim surface를 추가하고, monkeypatch-sensitive 경계를 고정한다.
2. crypto 공통 후처리를 새 모듈로 추출하고, legacy/tvscreener 두 경로를 같은 finalizer로 수렴시킨다.
3. `_analyze_stock_impl`을 `analysis_analyze.py`로 이동하되 `analysis_screening._analyze_stock_impl` alias를 유지한다.
4. `analysis_recommend.py`를 phase 함수와 payload builder로 분해하고, facade는 그대로 둔다.
5. `analysis_tool_handlers.py` import를 screening facade 기준으로 정리한다.
6. `tests/_mcp_tooling_support.py` patch surface와 direct-core tests를 새 shim ownership에 맞게 정리한다.
7. 마지막에 테스트 파일을 split하고 helper를 재배치한다.

## 6. 테스트 전략

### 계약 고정

- `tests/test_mcp_screen_stocks.py`
  - response key set
  - warning 문자열
  - fallback applied 여부
  - sort/limit ordering
- `tests/test_mcp_recommend.py`
  - `recommendations`, `warnings`, `strategy_description`, `fallback_applied`, `diagnostics`
  - exclusion/dedupe
  - KR relaxed fallback

### 직접 내부 경계 회귀

- `tests/test_tvscreener_stocks.py`
- `tests/test_tvscreener_crypto.py`
- `tests/test_crypto_composite_score.py`
- `tests/test_mcp_fundamentals_tools.py`

### helper/patch surface

- `tests/_mcp_tooling_support.py`
  - `_PATCH_MODULES`
  - `_patch_runtime_attr()` 경유 patch 가능성

### 권장 검증 명령

- `uv run pytest --no-cov tests/test_tvscreener_stocks.py tests/test_tvscreener_crypto.py tests/test_crypto_composite_score.py tests/test_mcp_fundamentals_tools.py -q`
- `uv run pytest --no-cov tests/test_mcp_screen_stocks.py tests/test_mcp_recommend.py -q`
- `uv run pytest --no-cov tests/test_mcp_screen_stocks_*.py tests/test_mcp_recommend_*.py -q` (split 이후)
- `make lint`

## 7. 비목표

- MCP README나 외부 user-facing contract를 이번 단계에서 바꾸지 않는다.
- public tool 이름, argument default, warning 문구, response shape를 정리 명목으로 rename하지 않는다.
- tvscreener/Upbit/KIS business rule 자체를 새로 정의하지 않는다. 이번 작업은 ownership 정리와 duplication 제거가 목적이다.

## 8. 주의할 점

- FastMCP 관점에서는 module path보다 callable name/signature/description이 계약이지만, 이 리포에서는 테스트가 import/patch surface까지 계약처럼 취급한다. 따라서 runtime contract만 맞아도 tests는 깨질 수 있다.
- handler가 `from module import symbol` 형태로 바인딩한 심볼은 monkeypatch에 취약하다. facade-first 접근은 이 문제를 줄이기 위한 것이다.
- crypto extraction은 가장 안전한 first seam이지만, warning 문자열과 `meta.filtered_by_warning`, `meta.filtered_by_crash`, `filters_applied.sort_order`, `rsi_bucket`/`market_cap_rank` semantics가 조금만 달라도 회귀가 난다.
- 테스트 split을 production move보다 먼저 하면 diff는 커지는데 rollback point는 불명확해진다. ownership이 굳을 때까지는 기존 테스트 파일에서 assertion을 유지하는 것이 낫다.
