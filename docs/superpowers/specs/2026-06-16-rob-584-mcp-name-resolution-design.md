# ROB-584 — MCP 도구 응답에 종목명(name) 및 해석 상태(name_resolved) 추가 설계

## 1. 개요 (Goal)
`analyze_stock_batch`, `get_quote`, `get_orderbook`, `get_execution_strength` 등 심볼 기반 MCP 도구가 응답 페이로드에 종목명(`name`)을 포함하지 않아, 다운스트림 에이전트가 심볼(예: 034220)만 보고 이름을 잘못 추측(예: "두산" vs 실제 "LG디스플레이")하는 오류를 방지한다.

이를 위해 시장별(KR, US, Crypto) 유니버스 DB를 조회하여 이름을 반환하는 공용 헬퍼를 구축하고, 대상 도구들에 배선한다.

## 2. 주요 변경 사항

### 2.1 공용 헬퍼: `resolve_names(symbols, market_type)`
*   **위치:** `app/mcp_server/tooling/name_resolution.py` (신규)
*   **기능:**
    *   입력받은 심볼 리스트에 대해 시장별 유니버스 서비스(`get_kr_names_by_symbols` 등)를 호출하여 이름을 조회한다.
    *   **Fallback:** DB에 이름이 없는 경우 심볼을 그대로 `name`으로 사용한다.
    *   **Flag:** 이름이 성공적으로 조회되었는지 여부를 `name_resolved` (bool) 필드로 명시한다.
*   **응답 구조:** `dict[str, dict[str, Any]]` (예: `{"005930": {"name": "삼성전자", "name_resolved": true}}`)

### 2.2 `analyze_stock_batch` 업데이트
*   `_run_batch_analysis` (`analysis_tool_handlers.py`)에서 분석 결과 수집 후, 심볼들을 시장별로 그룹화하여 `resolve_names`를 배치 호출한다.
*   조회된 이름 정보를 각 종목의 분석 결과(`analysis` dict)에 주입한다.
*   `_summarize_analysis_result` 요약 계약에 `name`과 `name_resolved` 필드를 추가한다.

### 2.3 기타 시장 데이터 도구 업데이트
*   `get_quote`, `get_orderbook`, `get_execution_strength` (`market_data_quotes.py`)가 응답 반환 직전에 `resolve_names`를 호출하여 이름 정보를 보강한다.

## 3. 상세 설계

### 3.1 `resolve_names` 로직
1.  `market_type`에 따라 적절한 유니버스 서비스 호출:
    *   `equity_kr`: `app.services.kr_symbol_universe_service.get_kr_names_by_symbols`
    *   `equity_us`: `app.services.us_symbol_universe_service.get_us_names_by_symbols`
    *   `crypto`: `app.services.upbit_symbol_universe_service.get_upbit_market_display_names` (korean_name 우선)
2.  결과 맵핑:
    *   Found: `{"name": resolved_name, "name_resolved": true}`
    *   Not Found: `{"name": original_symbol, "name_resolved": false}`

### 3.2 배선 지점 (Wiring)

#### `app/mcp_server/tooling/analysis_tool_handlers.py`
*   `_run_batch_analysis`: `asyncio.gather`로 분석 완료 후 이름 해석 수행.
*   `_summarize_analysis_result`: 요약 딕셔너리에 필드 추가.

#### `app/mcp_server/tooling/market_data_quotes.py`
*   `_get_quote_impl`: 결과 반환 전 `resolve_names` 적용.
*   `_get_orderbook_impl`: 결과 반환 전 `resolve_names` 적용.
*   `_get_execution_strength_impl`: 결과 반환 전 `resolve_names` 적용.

## 4. 테스트 전략
1.  **Unit Test:** `resolve_names` 헬퍼가 각 시장별로 올바르게 이름을 해석하고 fallback을 수행하는지 확인.
2.  **Integration Test:** `analyze_stock_batch` 호출 시 `results[symbol]` 내에 `name`과 `name_resolved`가 포함되는지 확인.
3.  **Live API Test (Option):** 실제 시장 심볼을 사용하여 DB 연동 확인.
