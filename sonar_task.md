# SonarCloud 전체 이슈 수정 태스크

프로젝트: mgh3326_auto_trader | 총 2752개 이슈

---

## 이슈 현황

- **BUG**: 542개
  - BLOCKER: 2개
  - CRITICAL: 2개
  - MAJOR: 537개
  - MINOR: 1개
- **VULNERABILITY**: 24개
  - BLOCKER: 8개
  - CRITICAL: 6개
  - MINOR: 10개
- **CODE_SMELL**: 2186개
  - BLOCKER: 128개
  - CRITICAL: 384개
  - MAJOR: 440개
  - MINOR: 1233개
  - INFO: 1개

## 규칙별 빈도 (상위 20개)

- `python:S7503` (1107개): async 함수인데 await 없음 → async 제거 또는 await 추가
- `python:S1244` (522개): float 동등 비교 (==) → pytest.approx() 또는 math.isclose() 사용
- `python:S3776` (182개): Cognitive Complexity 초과 → 헬퍼 함수로 분리
- `python:S8410` (121개): FastAPI Depends() → Annotated 타입힌트 방식으로 변경
- `python:S1186` (86개): 빈 함수/메서드 → pass 또는 구현 추가
- `shelldre:S7688` (83개): Shell: 변수 미사용
- `python:S1192` (76개): 중복 문자열 리터럴 → 상수로 추출
- `python:S8415` (53개): FastAPI 관련 type hint 이슈
- `python:S6546` (42개): 비권장 패턴
- `python:S117` (31개): 변수명 컨벤션
- `shelldre:S7682` (30개): 
- `python:S3457` (29개): f-string 또는 format 이슈
- `javascript:S7781` (25개): JS 이슈
- `css:S7924` (23개): CSS 이슈
- `python:S112` (22개): 범용 예외 사용 → 구체적 예외로
- `python:S7483` (20개): 
- `python:S8396` (20개): 타입 힌트 이슈
- `python:S1481` (17개): 미사용 변수 → 제거
- `Web:S6853` (16개): 
- `shelldre:S7679` (14개): 

---

## 수정 우선순위

### 1. BUG / BLOCKER (2개)

#### `python:S5644` — 2개 — 보안 이슈

- `tests/test_services_krx.py` L909: Fix this code; "captured_cache_data" does not have a "__getitem__" method.
- `tests/test_services_krx.py` L910: Fix this code; "captured_cache_data" does not have a "__getitem__" method.

### 2. BUG / CRITICAL (2개)

#### `pythonbugs:S6466` — 2개 

- `app/mcp_server/tooling/analysis_tool_handlers.py` L466: Fix this access on a collection that may trigger an 'IndexError'.
- `app/mcp_server/tooling/analysis_tool_handlers.py` L467: Fix this access on a collection that may trigger an 'IndexError'.

### 3. BUG / MAJOR (537개)

#### `python:S1244` — 522개 — float 동등 비교 (==) → pytest.approx() 또는 math.isclose() 사용

- `tests/_mcp_screen_stocks_support.py` L1622: Do not perform equality checks with floating point values.
- `tests/_mcp_screen_stocks_support.py` L1628: Do not perform equality checks with floating point values.
- `tests/_mcp_screen_stocks_support.py` L1629: Do not perform equality checks with floating point values.
- `tests/_mcp_screen_stocks_support.py` L1632: Do not perform equality checks with floating point values.
- `tests/_mcp_screen_stocks_support.py` L1633: Do not perform equality checks with floating point values.
- `tests/_mcp_screen_stocks_support.py` L1720: Do not perform equality checks with floating point values.
- `tests/_mcp_screen_stocks_support.py` L1721: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L3195: Do not perform equality checks with floating point values.
- `tests/test_mcp_screen_stocks_kr.py` L148: Do not perform equality checks with floating point values.
- `tests/test_mcp_screen_stocks_kr.py` L154: Do not perform equality checks with floating point values.
- `tests/test_mcp_screen_stocks_kr.py` L155: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2753: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2754: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2755: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2756: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2757: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2758: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2843: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2844: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2845: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2846: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2847: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2848: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2890: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L2891: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L243: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L254: Do not perform equality checks with floating point values.
- `tests/test_mcp_fundamentals_tools.py` L294: Do not perform equality checks with floating point values.
- `tests/test_market_data_service.py` L967: Do not perform equality checks with floating point values.
- `tests/test_mcp_recommend_flow.py` L167: Do not perform equality checks with floating point values.
- ... 외 492개 더

#### `python:S1764` — 4개 — 동일 표현식 비교 (x != x) → 버그

- `app/mcp_server/tooling/fundamentals_sources_naver.py` L89: Correct one of the identical sub-expressions on both sides of operator "!=".
- `app/mcp_server/tooling/analysis_screen_core.py` L157: Correct one of the identical sub-expressions on both sides of operator "!=".
- `app/mcp_server/tooling/analysis_screen_core.py` L168: Correct one of the identical sub-expressions on both sides of operator "!=".
- `app/mcp_server/tooling/analysis_screen_core.py` L247: Correct one of the identical sub-expressions on both sides of operator "!=".

#### `pythonbugs:S2583` — 4개 — 항상 False인 조건 → dead code

- `app/mcp_server/tooling/fundamentals_sources_naver.py` L89: Fix this condition that always evaluates to false.
- `app/mcp_server/tooling/analysis_screen_core.py` L247: Fix this condition that always evaluates to false.
- `app/mcp_server/tooling/analysis_screening.py` L161: Fix this condition that always evaluates to true.
- `app/auth/admin_router.py` L62: Fix this condition that always evaluates to true.

#### `Web:TableHeaderHasIdOrScopeCheck` — 4개 

- `app/templates/orderbook_dashboard.html` L186: Add either an 'id' or a 'scope' attribute to this <th> tag.
- `app/templates/orderbook_dashboard.html` L187: Add either an 'id' or a 'scope' attribute to this <th> tag.
- `app/templates/orderbook_dashboard.html` L188: Add either an 'id' or a 'scope' attribute to this <th> tag.
- `app/templates/orderbook_dashboard.html` L206: Add either an 'id' or a 'scope' attribute to this <th> tag.

#### `python:S7497` — 1개 

- `websocket_monitor.py` L522: Ensure that the asyncio.CancelledError exception is re-raised after your cleanup code.

#### `python:S3981` — 1개 

- `tests/_mcp_screen_stocks_support.py` L623: The length of a collection is always ">=0", so update this test to either "==0" or ">0".

#### `css:S4649` — 1개 

- `blog/images/download-image4.html` L7: Unexpected missing generic font family

### 4. BUG / MINOR (1개)

#### `Web:MouseEventWithoutKeyboardEquivalentCheck` — 1개 

- `app/templates/analysis_list.html` L695: Add a 'onKeyPress|onKeyDown|onKeyUp' attribute to this <span> tag.

### 5. VULNERABILITY / BLOCKER (8개)

#### `python:S2068` — 8개 — 하드코딩된 크리덴셜 → 환경변수로

- `tests/test_services_krx.py` L1019: "password" detected here, review this potentially hard-coded credential.
- `tests/test_services_krx.py` L1049: "password" detected here, review this potentially hard-coded credential.
- `tests/test_services_krx.py` L1125: "password" detected here, review this potentially hard-coded credential.
- `tests/test_services_krx.py` L1149: "password" detected here, review this potentially hard-coded credential.
- `tests/test_services_krx.py` L1240: "password" detected here, review this potentially hard-coded credential.
- `tests/test_services_krx.py` L1080: "password" detected here, review this potentially hard-coded credential.
- `tests/test_services_krx.py` L1102: "password" detected here, review this potentially hard-coded credential.
- `tests/test_sentry_init.py` L280: "password" detected here, review this potentially hard-coded credential.

### 6. VULNERABILITY / CRITICAL (6개)

#### `python:S5542` — 2개 — 취약한 암호화

- `app/services/kis_websocket.py` L885: Use secure mode and padding scheme.
- `tests/test_kis_websocket.py` L444: Use secure mode and padding scheme.

#### `python:S5527` — 2개 — SSL/TLS 검증 비활성화

- `app/services/upbit_market_websocket.py` L62: Enable server hostname verification on this SSL/TLS connection.
- `app/services/upbit_websocket.py` L50: Enable server hostname verification on this SSL/TLS connection.

#### `python:S4830` — 2개 — SSL 인증서 검증 무시

- `app/services/upbit_market_websocket.py` L67: Enable server certificate validation on this SSL/TLS connection.
- `app/services/upbit_websocket.py` L55: Enable server certificate validation on this SSL/TLS connection.

### 7. VULNERABILITY / MINOR (10개)

#### `pythonsecurity:S5145` — 10개 — 정규식 DoS 취약점

- `app/services/market_data/service.py` L206: Change this code to not log user-controlled data.
- `app/services/market_data/service.py` L222: Change this code to not log user-controlled data.
- `app/services/yahoo_ohlcv_cache.py` L697: Change this code to not log user-controlled data.
- `app/jobs/kis_trading.py` L1094: Change this code to not log user-controlled data.
- `app/services/upbit_orderbook.py` L93: Change this code to not log user-controlled data.
- `app/services/upbit_orderbook.py` L74: Change this code to not log user-controlled data.
- `app/services/upbit_orderbook.py` L86: Change this code to not log user-controlled data.
- `app/services/upbit_orderbook.py` L98: Change this code to not log user-controlled data.
- `app/services/upbit_orderbook.py` L101: Change this code to not log user-controlled data.
- `app/services/kis_holdings_service.py` L42: Change this code to not log user-controlled data.

### 8. CODE_SMELL / BLOCKER (128개)

#### `python:S8410` — 121개 — FastAPI Depends() → Annotated 타입힌트 방식으로 변경

- `app/routers/screener.py` L125: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L126: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/portfolio.py` L56: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/portfolio.py` L57: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L128: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L121: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L122: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L123: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L124: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L127: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L129: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L130: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L165: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L187: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L199: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L207: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L208: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/screener.py` L216: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/trading.py` L534: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/trading.py` L535: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/trading.py` L582: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/trading.py` L583: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L19: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L20: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L21: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L22: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L23: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L66: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L100: Use "Annotated" type hints for FastAPI dependency injection
- `app/routers/kospi200.py` L130: Use "Annotated" type hints for FastAPI dependency injection
- ... 외 91개 더

#### `python:S8409` — 5개 

- `app/auth/router.py` L52: Remove this redundant "response_model" parameter; it duplicates the return type annotation.
- `app/auth/router.py` L113: Remove this redundant "response_model" parameter; it duplicates the return type annotation.
- `app/auth/router.py` L217: Remove this redundant "response_model" parameter; it duplicates the return type annotation.
- `app/auth/router.py` L385: Remove this redundant "response_model" parameter; it duplicates the return type annotation.
- `app/routers/health.py` L16: Remove this redundant "response_model" parameter; it duplicates the return type annotation.

#### `python:S3516` — 1개 

- `tests/test_kis_tasks.py` L1723: Refactor this method to not always return the same value.

#### `python:S1845` — 1개 

- `app/middleware/auth.py` L63: Rename field "public_api_paths" to prevent any misunderstanding/clash with field "PUBLIC_API_PATHS" 

### 9. CODE_SMELL / CRITICAL (384개)

#### `python:S3776` — 182개 — Cognitive Complexity 초과 → 헬퍼 함수로 분리

- `app/mcp_server/tooling/analysis_screen_core.py` L380: Refactor this function to reduce its Cognitive Complexity from 18 to the 15 allowed.
- `app/mcp_server/tooling/analysis_screen_core.py` L641: Refactor this function to reduce its Cognitive Complexity from 25 to the 15 allowed.
- `app/services/us_intraday_candles_read_service.py` L363: Refactor this function to reduce its Cognitive Complexity from 42 to the 15 allowed.
- `app/services/us_intraday_candles_read_service.py` L610: Refactor this function to reduce its Cognitive Complexity from 18 to the 15 allowed.
- `app/mcp_server/tooling/analysis_recommend.py` L614: Refactor this function to reduce its Cognitive Complexity from 23 to the 15 allowed.
- `app/mcp_server/tooling/analysis_recommend.py` L689: Refactor this function to reduce its Cognitive Complexity from 34 to the 15 allowed.
- `app/services/brokers/kis/base.py` L170: Refactor this function to reduce its Cognitive Complexity from 16 to the 15 allowed.
- `app/services/brokers/kis/account.py` L15: Refactor this function to reduce its Cognitive Complexity from 27 to the 15 allowed.
- `app/services/naver_finance.py` L313: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
- `app/services/naver_finance.py` L412: Refactor this function to reduce its Cognitive Complexity from 55 to the 15 allowed.
- `tests/test_naver_finance.py` L718: Refactor this function to reduce its Cognitive Complexity from 17 to the 15 allowed.
- `tests/test_naver_finance.py` L790: Refactor this function to reduce its Cognitive Complexity from 17 to the 15 allowed.
- `app/services/us_candles_sync_service.py` L461: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
- `tests/test_us_candles_sync.py` L49: Refactor this function to reduce its Cognitive Complexity from 28 to the 15 allowed.
- `app/services/brokers/kis/market_data.py` L126: Refactor this function to reduce its Cognitive Complexity from 28 to the 15 allowed.
- `app/services/kis_websocket.py` L569: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
- `app/services/kis_websocket.py` L730: Refactor this function to reduce its Cognitive Complexity from 26 to the 15 allowed.
- `app/services/brokers/kis/account.py` L90: Refactor this function to reduce its Cognitive Complexity from 38 to the 15 allowed.
- `app/services/brokers/kis/account.py` L316: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
- `app/services/brokers/kis/account.py` L551: Refactor this function to reduce its Cognitive Complexity from 22 to the 15 allowed.
- `app/services/brokers/kis/base.py` L306: Refactor this function to reduce its Cognitive Complexity from 42 to the 15 allowed.
- `app/services/brokers/kis/domestic_orders.py` L45: Refactor this function to reduce its Cognitive Complexity from 18 to the 15 allowed.
- `app/services/brokers/kis/domestic_orders.py` L85: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
- `app/services/brokers/kis/domestic_orders.py` L503: Refactor this function to reduce its Cognitive Complexity from 20 to the 15 allowed.
- `app/services/brokers/kis/market_data.py` L1112: Refactor this function to reduce its Cognitive Complexity from 17 to the 15 allowed.
- `app/services/brokers/kis/overseas_orders.py` L30: Refactor this function to reduce its Cognitive Complexity from 16 to the 15 allowed.
- `app/services/brokers/kis/overseas_orders.py` L214: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
- `app/services/brokers/kis/overseas_orders.py` L456: Refactor this function to reduce its Cognitive Complexity from 19 to the 15 allowed.
- `app/services/kis_trading_service.py` L97: Refactor this function to reduce its Cognitive Complexity from 39 to the 15 allowed.
- `app/services/kis_trading_service.py` L257: Refactor this function to reduce its Cognitive Complexity from 32 to the 15 allowed.
- ... 외 152개 더

#### `python:S1186` — 86개 — 빈 함수/메서드 → pass 또는 구현 추가

- `alembic/versions/86961c84a0ce_merge_kr_intraday_and_us_candle_heads.py` L9: Add a nested comment explaining why this function is empty, or complete the implementation.
- `alembic/versions/86961c84a0ce_merge_kr_intraday_and_us_candle_heads.py` L13: Add a nested comment explaining why this function is empty, or complete the implementation.
- `alembic/versions/a9d6e4c2b1f0_merge_us_candles_and_trade_profile_heads.py` L9: Add a nested comment explaining why this function is empty, or complete the implementation.
- `alembic/versions/a9d6e4c2b1f0_merge_us_candles_and_trade_profile_heads.py` L13: Add a nested comment explaining why this function is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L978: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L988: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L994: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1118: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1125: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1131: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1223: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1229: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1259: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1266: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1272: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1318: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1325: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L1331: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2097: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2104: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2110: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2166: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2173: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2179: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2219: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2226: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2232: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2284: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2291: Add a nested comment explaining why this method is empty, or complete the implementation.
- `tests/test_mcp_fundamentals_tools.py` L2297: Add a nested comment explaining why this method is empty, or complete the implementation.
- ... 외 56개 더

#### `python:S1192` — 76개 — 중복 문자열 리터럴 → 상수로 추출

- `tests/_mcp_screen_stocks_support.py` L229: Define a constant instead of duplicating this literal "SK hynix Inc." 3 times.
- `tests/_mcp_screen_stocks_support.py` L1777: Define a constant instead of duplicating this literal "legacy KR path should not run for stock reque
- `app/services/disclosures/dart.py` L110: Define a constant instead of duplicating this literal "DART functionality not available" 3 times.
- `app/monitoring/trade_notifier.py` L1752: Define a constant instead of duplicating this literal "OpenClaw mirror result: correlation_id=%s dis
- `app/services/brokers/kis/account.py` L332: Define a constant instead of duplicating this literal "KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다." 3 times.
- `app/services/brokers/kis/domestic_orders.py` L113: Define a constant instead of duplicating this literal "KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다." 5 times.
- `app/services/brokers/kis/overseas_orders.py` L58: Define a constant instead of duplicating this literal "KIS_ACCOUNT_NO 환경변수가 설정되지 않았습니다." 5 times.
- `tests/_mcp_screen_stocks_support.py` L245: Define a constant instead of duplicating this literal "app.mcp_server.tooling.analysis_screen_core._
- `tests/_mcp_screen_stocks_support.py` L757: Define a constant instead of duplicating this literal "app.mcp_server.tooling.analysis_screen_core._
- `tests/_mcp_screen_stocks_support.py` L830: Define a constant instead of duplicating this literal "app.mcp_server.tooling.analysis_screen_core._
- `tests/_mcp_screen_stocks_support.py` L892: Define a constant instead of duplicating this literal "app.mcp_server.tooling.analysis_screen_core._
- `app/services/kis_trading_service.py` L121: Define a constant instead of duplicating this literal "분석 결과 없음" 4 times.
- `app/services/kis_trading_service.py` L469: Define a constant instead of duplicating this literal "매도 주문 실패" 4 times.
- `tests/_mcp_screen_stocks_support.py` L219: Define a constant instead of duplicating this literal "Samsung Electronics Co., Ltd." 9 times.
- `tests/_mcp_screen_stocks_support.py` L445: Define a constant instead of duplicating this literal "Apple Inc." 9 times.
- `tests/_mcp_screen_stocks_support.py` L103: Define a constant instead of duplicating this literal "SK하이닉스" 3 times.
- `verify_db_kr_candles_1m_subtask_5_4.py` L89: Define a constant instead of duplicating this literal "double precision" 6 times.
- `app/services/tvscreener_service.py` L223: Define a constant instead of duplicating this literal "Retrying after %.2fs delay..." 3 times.
- `app/monitoring/trade_notifier.py` L872: Define a constant instead of duplicating this literal "Discord send failed, falling back to Telegram
- `app/monitoring/trade_notifier.py` L487: Define a constant instead of duplicating this literal "application/json" 4 times.
- `app/mcp_server/tooling/analysis_screen_core.py` L2170: Define a constant instead of duplicating this literal "[Screen-US-TV] %s" 4 times.
- `app/mcp_server/tooling/analysis_screen_core.py` L1943: Define a constant instead of duplicating this literal "[Screen-KR-TV] %s" 4 times.
- `alembic/versions/4d9f0b2c7a11_add_trade_profile_tables.py` L53: Define a constant instead of duplicating this literal "now()" 7 times.
- `alembic/versions/4d9f0b2c7a11_add_trade_profile_tables.py` L91: Define a constant instead of duplicating this literal "users.id" 4 times.
- `app/models/trade_profile.py` L98: Define a constant instead of duplicating this literal "users.id" 4 times.
- `app/services/brokers/upbit/client.py` L62: Define a constant instead of duplicating this literal "/unknown" 3 times.
- `app/services/kr_symbol_universe_service.py` L205: Define a constant instead of duplicating this literal "KR symbol universe parse source=%s valid=%d s
- `alembic/versions/f2c1e9b7a4d0_add_research_backtest_tables.py` L62: Define a constant instead of duplicating this literal "now()" 5 times.
- `alembic/versions/f2c1e9b7a4d0_add_research_backtest_tables.py` L105: Define a constant instead of duplicating this literal "research.backtest_runs.id" 3 times.
- `app/models/research_backtest.py` L90: Define a constant instead of duplicating this literal "research.backtest_runs.id" 3 times.
- ... 외 46개 더

#### `python:S8396` — 20개 — 타입 힌트 이슈

- `app/schemas/news.py` L36: Add an explicit default value to this optional field.
- `app/schemas/news.py` L37: Add an explicit default value to this optional field.
- `app/schemas/news.py` L39: Add an explicit default value to this optional field.
- `app/schemas/news.py` L40: Add an explicit default value to this optional field.
- `app/schemas/news.py` L41: Add an explicit default value to this optional field.
- `app/schemas/news.py` L44: Add an explicit default value to this optional field.
- `app/schemas/news.py` L46: Add an explicit default value to this optional field.
- `app/schemas/news.py` L56: Add an explicit default value to this optional field.
- `app/schemas/news.py` L59: Add an explicit default value to this optional field.
- `app/schemas/news.py` L60: Add an explicit default value to this optional field.
- `app/schemas/news.py` L61: Add an explicit default value to this optional field.
- `app/schemas/news.py` L63: Add an explicit default value to this optional field.
- `app/schemas/news.py` L64: Add an explicit default value to this optional field.
- `app/schemas/news.py` L66: Add an explicit default value to this optional field.
- `app/routers/symbol_settings.py` L109: Add an explicit default value to this optional field.
- `app/routers/symbol_settings.py` L111: Add an explicit default value to this optional field.
- `app/routers/symbol_settings.py` L171: Add an explicit default value to this optional field.
- `app/routers/symbol_settings.py` L172: Add an explicit default value to this optional field.
- `app/routers/symbol_settings.py` L173: Add an explicit default value to this optional field.
- `app/schemas/manual_holdings.py` L115: Add an explicit default value to this optional field.

#### `plsql:S1192` — 13개 

- `scripts/sql/us_candles_timescale.sql` L49: Define a constant instead of duplicating this literal 4 times.
- `scripts/sql/us_candles_timescale.sql` L64: Define a constant instead of duplicating this literal 6 times.
- `scripts/sql/us_candles_timescale.sql` L84: Define a constant instead of duplicating this literal 5 times.
- `scripts/sql/us_candles_timescale.sql` L104: Define a constant instead of duplicating this literal 6 times.
- `scripts/sql/us_candles_timescale.sql` L125: Define a constant instead of duplicating this literal 4 times.
- `scripts/sql/us_candles_timescale.sql` L127: Define a constant instead of duplicating this literal 3 times.
- `scripts/sql/us_candles_timescale.sql` L146: Define a constant instead of duplicating this literal 8 times.
- `scripts/sql/us_candles_timescale.sql` L157: Define a constant instead of duplicating this literal 4 times.
- `scripts/sql/us_candles_timescale.sql` L168: Define a constant instead of duplicating this literal 8 times.
- `scripts/sql/us_candles_timescale.sql` L190: Define a constant instead of duplicating this literal 8 times.
- `scripts/sql/us_candles_timescale.sql` L212: Define a constant instead of duplicating this literal 8 times.
- `scripts/sql/us_candles_timescale.sql` L241: Define a constant instead of duplicating this literal 5 times.
- `scripts/migrate_symbols_to_dot_format.sql` L10: Define a constant instead of duplicating this literal 4 times.

#### `python:S5727` — 4개 

- `tests/test_services_kis_client.py` L17: Remove this identity check; it will always be True.
- `tests/test_kis_imports.py` L36: Remove this identity check; it will always be True.
- `tests/test_mcp_fundamentals_tools.py` L907: Remove this == comparison; it will always be False.
- `tests/test_services_krx.py` L907: Remove this identity check; it will always be False.

#### `python:S5655` — 3개 

- `tests/test_services_krx.py` L908: Change this argument; Function "len" expects a different type
- `tests/test_analyst_normalizer.py` L107: Change this argument; Function "rating_to_bucket" expects a different type
- `tests/test_analyst_normalizer.py` L129: Change this argument; Function "is_strong_buy" expects a different type

### 10. CODE_SMELL / MAJOR (440개)

#### `shelldre:S7688` — 83개 — Shell: 변수 미사용

- `quick_benchmark_subtask_5_5.sh` L24: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `quick_benchmark_subtask_5_5.sh` L60: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L40: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L54: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L126: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L126: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L131: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L151: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L159: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L167: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L175: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L186: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L198: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L206: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L214: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L222: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test_discord_webhooks.sh` L235: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `docs/verify_tvscreener_endpoints.sh` L69: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `docs/verify_tvscreener_endpoints.sh` L79: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `docs/verify_tvscreener_endpoints.sh` L92: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `docs/verify_tvscreener_endpoints.sh` L101: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `docs/verify_tvscreener_endpoints.sh` L103: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `docs/verify_tvscreener_endpoints.sh` L110: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `docs/verify_tvscreener_endpoints.sh` L117: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/healthcheck.sh` L73: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/healthcheck.sh` L91: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/healthcheck.sh` L97: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/healthcheck.sh` L81: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test-caddy-https.sh` L83: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- `scripts/test-caddy-https.sh` L91: Use '[[' instead of '[' for conditional tests. The '[[' construct is safer and more feature-rich.
- ... 외 53개 더

#### `python:S8415` — 53개 — FastAPI 관련 type hint 이슈

- `app/routers/manual_holdings.py` L218: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/manual_holdings.py` L258: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/portfolio.py` L166: Document this HTTPException with status code 500 in the "responses" parameter.
- `app/routers/trading.py` L575: Document this HTTPException with status code 500 in the "responses" parameter.
- `app/routers/trading.py` L593: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/trading.py` L620: Document this HTTPException with status code 500 in the "responses" parameter.
- `app/routers/kospi200.py` L76: Document this HTTPException with status code 404 in the "responses" parameter.
- `app/routers/manual_holdings.py` L261: Document this HTTPException with status code 500 in the "responses" parameter.
- `app/routers/upbit_trading.py` L232: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/upbit_trading.py` L252: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/upbit_trading.py` L269: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/upbit_trading.py` L284: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/upbit_trading.py` L304: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/upbit_trading.py` L328: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/upbit_trading.py` L346: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/manual_holdings.py` L102: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/manual_holdings.py` L128: Document this HTTPException with status code 404 in the "responses" parameter.
- `app/routers/manual_holdings.py` L147: Document this HTTPException with status code 404 in the "responses" parameter.
- `app/routers/manual_holdings.py` L287: Document this HTTPException with status code 404 in the "responses" parameter.
- `app/routers/manual_holdings.py` L310: Document this HTTPException with status code 404 in the "responses" parameter.
- `app/routers/manual_holdings.py` L348: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/portfolio.py` L70: Document this HTTPException with status code 500 in the "responses" parameter.
- `app/routers/portfolio.py` L122: Document this HTTPException with status code 500 in the "responses" parameter.
- `app/routers/portfolio.py` L174: Document this HTTPException with status code 404 in the "responses" parameter.
- `app/routers/trading.py` L304: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/trading.py` L324: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/trading.py` L383: Document this HTTPException with status code 500 in the "responses" parameter.
- `app/routers/trading.py` L417: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/trading.py` L426: Document this HTTPException with status code 400 in the "responses" parameter.
- `app/routers/trading.py` L446: Document this HTTPException with status code 400 in the "responses" parameter.
- ... 외 23개 더

#### `python:S6546` — 42개 — 비권장 패턴

- `alembic/versions/0d59098a1b34_add_news_analysis_tables.py` L16: Use a union type expression for this type hint.
- `alembic/versions/0d59098a1b34_add_news_analysis_tables.py` L17: Use a union type expression for this type hint.
- `alembic/versions/0d59098a1b34_add_news_analysis_tables.py` L18: Use a union type expression for this type hint.
- `alembic/versions/1b7e2a9a0a9d_change_reasons_to_jsonb.py` L16: Use a union type expression for this type hint.
- `alembic/versions/1b7e2a9a0a9d_change_reasons_to_jsonb.py` L17: Use a union type expression for this type hint.
- `alembic/versions/1b7e2a9a0a9d_change_reasons_to_jsonb.py` L18: Use a union type expression for this type hint.
- `alembic/versions/3c24a5cf6f5e_add_refresh_tokens_table.py` L15: Use a union type expression for this type hint.
- `alembic/versions/3c24a5cf6f5e_add_refresh_tokens_table.py` L16: Use a union type expression for this type hint.
- `alembic/versions/3c24a5cf6f5e_add_refresh_tokens_table.py` L17: Use a union type expression for this type hint.
- `alembic/versions/566682e4e76e_fix_timestamp_defaults.py` L16: Use a union type expression for this type hint.
- `alembic/versions/566682e4e76e_fix_timestamp_defaults.py` L17: Use a union type expression for this type hint.
- `alembic/versions/566682e4e76e_fix_timestamp_defaults.py` L18: Use a union type expression for this type hint.
- `alembic/versions/7cff05b5aa4d_add_manual_holdings_and_broker_accounts.py` L16: Use a union type expression for this type hint.
- `alembic/versions/7cff05b5aa4d_add_manual_holdings_and_broker_accounts.py` L17: Use a union type expression for this type hint.
- `alembic/versions/7cff05b5aa4d_add_manual_holdings_and_broker_accounts.py` L18: Use a union type expression for this type hint.
- `alembic/versions/a135dbde152e_add_user_trade_defaults_and_update_.py` L16: Use a union type expression for this type hint.
- `alembic/versions/a135dbde152e_add_user_trade_defaults_and_update_.py` L17: Use a union type expression for this type hint.
- `alembic/versions/a135dbde152e_add_user_trade_defaults_and_update_.py` L18: Use a union type expression for this type hint.
- `alembic/versions/a69eac660fba_add_symbol_trade_settings_table.py` L15: Use a union type expression for this type hint.
- `alembic/versions/a69eac660fba_add_symbol_trade_settings_table.py` L16: Use a union type expression for this type hint.
- `alembic/versions/a69eac660fba_add_symbol_trade_settings_table.py` L17: Use a union type expression for this type hint.
- `alembic/versions/add_dca_plans_and_steps.py` L16: Use a union type expression for this type hint.
- `alembic/versions/add_dca_plans_and_steps.py` L17: Use a union type expression for this type hint.
- `alembic/versions/add_dca_plans_and_steps.py` L18: Use a union type expression for this type hint.
- `alembic/versions/b3e58be9e79b_init.py` L16: Use a union type expression for this type hint.
- `alembic/versions/b3e58be9e79b_init.py` L17: Use a union type expression for this type hint.
- `alembic/versions/b3e58be9e79b_init.py` L18: Use a union type expression for this type hint.
- `alembic/versions/bb51ac8d080c_add_authentication_fields_to_user_model.py` L16: Use a union type expression for this type hint.
- `alembic/versions/bb51ac8d080c_add_authentication_fields_to_user_model.py` L17: Use a union type expression for this type hint.
- `alembic/versions/bb51ac8d080c_add_authentication_fields_to_user_model.py` L18: Use a union type expression for this type hint.
- ... 외 12개 더

#### `shelldre:S7682` — 30개 

- `scripts/test_discord_webhooks.sh` L15: Add an explicit return statement at the end of the function.
- `scripts/test_discord_webhooks.sh` L22: Add an explicit return statement at the end of the function.
- `scripts/test_discord_webhooks.sh` L26: Add an explicit return statement at the end of the function.
- `scripts/test_discord_webhooks.sh` L30: Add an explicit return statement at the end of the function.
- `scripts/test_discord_webhooks.sh` L34: Add an explicit return statement at the end of the function.
- `scripts/test_discord_webhooks.sh` L39: Add an explicit return statement at the end of the function.
- `scripts/test_discord_webhooks.sh` L139: Add an explicit return statement at the end of the function.
- `docs/verify_tvscreener_endpoints.sh` L19: Add an explicit return statement at the end of the function.
- `docs/verify_tvscreener_endpoints.sh` L23: Add an explicit return statement at the end of the function.
- `docs/verify_tvscreener_endpoints.sh` L27: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L38: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L44: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L48: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L53: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L58: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L63: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L68: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L90: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L151: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L272: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L297: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L310: Add an explicit return statement at the end of the function.
- `scripts/test-caddy-https.sh` L334: Add an explicit return statement at the end of the function.
- `scripts/deploy.sh` L24: Add an explicit return statement at the end of the function.
- `scripts/migrate.sh` L44: Add an explicit return statement at the end of the function.
- `scripts/migrate.sh` L65: Add an explicit return statement at the end of the function.
- `scripts/migrate.sh` L81: Add an explicit return statement at the end of the function.
- `scripts/migrate.sh` L114: Add an explicit return statement at the end of the function.
- `scripts/migrate.sh` L156: Add an explicit return statement at the end of the function.
- `scripts/migrate.sh` L177: Add an explicit return statement at the end of the function.

#### `python:S3457` — 29개 — f-string 또는 format 이슈

- `benchmark_performance_subtask_5_5.py` L165: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L197: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L223: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L228: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L232: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L257: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L324: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L333: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L342: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L353: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L360: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L361: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L362: Add replacement fields or use a normal string instead of an f-string.
- `benchmark_performance_subtask_5_5.py` L363: Add replacement fields or use a normal string instead of an f-string.
- `verify_cache_warmup_subtask_5_3.py` L154: Add replacement fields or use a normal string instead of an f-string.
- `verify_cache_warmup_subtask_5_3.py` L211: Add replacement fields or use a normal string instead of an f-string.
- `verify_db_kr_candles_1m_subtask_5_4.py` L191: Add replacement fields or use a normal string instead of an f-string.
- `verify_db_kr_candles_1m_subtask_5_4.py` L198: Add replacement fields or use a normal string instead of an f-string.
- `verify_db_kr_candles_1m_subtask_5_4.py` L302: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L81: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L83: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L146: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L148: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L152: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L154: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L167: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L169: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L173: Add replacement fields or use a normal string instead of an f-string.
- `verify_e2e_implementation.py` L175: Add replacement fields or use a normal string instead of an f-string.

#### `css:S7924` — 23개 — CSS 이슈

- `app/templates/screener_dashboard.html` L209: Text does not meet the minimal contrast requirement with its background.
- `app/templates/screener_dashboard.html` L215: Text does not meet the minimal contrast requirement with its background.
- `app/templates/screener_report_detail.html` L84: Text does not meet the minimal contrast requirement with its background.
- `app/templates/screener_report_detail.html` L90: Text does not meet the minimal contrast requirement with its background.
- `app/templates/orderbook_dashboard.html` L29: Text does not meet the minimal contrast requirement with its background.
- `app/templates/orderbook_dashboard.html` L58: Text does not meet the minimal contrast requirement with its background.
- `app/templates/orderbook_dashboard.html` L63: Text does not meet the minimal contrast requirement with its background.
- `app/templates/orderbook_dashboard.html` L68: Text does not meet the minimal contrast requirement with its background.
- `app/templates/orderbook_dashboard.html` L74: Text does not meet the minimal contrast requirement with its background.
- `app/templates/manual_holdings_dashboard.html` L21: Text does not meet the minimal contrast requirement with its background.
- `app/templates/kis_domestic_trading_dashboard.html` L115: Text does not meet the minimal contrast requirement with its background.
- `app/templates/kis_overseas_trading_dashboard.html` L63: Text does not meet the minimal contrast requirement with its background.
- `app/templates/upbit_trading_dashboard.html` L151: Text does not meet the minimal contrast requirement with its background.
- `app/templates/upbit_trading_dashboard.html` L14: Text does not meet the minimal contrast requirement with its background.
- `blog/images/download-all-images.html` L52: Text does not meet the minimal contrast requirement with its background.
- `blog/images/download-image4.html` L14: Text does not meet the minimal contrast requirement with its background.
- `app/templates/stock_latest_dashboard.html` L13: Text does not meet the minimal contrast requirement with its background.
- `app/templates/analysis_json_dashboard.html` L111: Text does not meet the minimal contrast requirement with its background.
- `app/templates/analysis_list.html` L527: Text does not meet the minimal contrast requirement with its background.
- `app/templates/analysis_list.html` L128: Text does not meet the minimal contrast requirement with its background.
- `app/templates/analysis_list.html` L324: Text does not meet the minimal contrast requirement with its background.
- `app/templates/analysis_list.html` L381: Text does not meet the minimal contrast requirement with its background.
- `app/templates/dashboard.html` L79: Text does not meet the minimal contrast requirement with its background.

#### `python:S112` — 22개 — 범용 예외 사용 → 구체적 예외로

- `app/services/market_data/service.py` L413: Replace this generic exception class with a more specific one.
- `app/services/market_data/service.py` L512: Replace this generic exception class with a more specific one.
- `tests/test_mcp_fundamentals_tools.py` L766: Replace this generic exception class with a more specific one.
- `tests/test_mcp_fundamentals_tools.py` L784: Replace this generic exception class with a more specific one.
- `tests/test_mcp_fundamentals_tools.py` L1085: Replace this generic exception class with a more specific one.
- `tests/test_mcp_fundamentals_tools.py` L1233: Replace this generic exception class with a more specific one.
- `app/services/account/service.py` L133: Replace this generic exception class with a more specific one.
- `app/services/account/service.py` L219: Replace this generic exception class with a more specific one.
- `app/services/account/service.py` L265: Replace this generic exception class with a more specific one.
- `app/services/market_data/service.py` L295: Replace this generic exception class with a more specific one.
- `app/services/market_data/service.py` L376: Replace this generic exception class with a more specific one.
- `app/services/market_data/service.py` L446: Replace this generic exception class with a more specific one.
- `app/services/market_data/service.py` L616: Replace this generic exception class with a more specific one.
- `app/services/orders/service.py` L208: Replace this generic exception class with a more specific one.
- `app/services/orders/service.py` L292: Replace this generic exception class with a more specific one.
- `app/services/orders/service.py` L384: Replace this generic exception class with a more specific one.
- `app/services/kis_websocket.py` L533: Replace this generic exception class with a more specific one.
- `app/services/kis_websocket.py` L270: Replace this generic exception class with a more specific one.
- `app/services/kis_websocket.py` L529: Replace this generic exception class with a more specific one.
- `tests/test_kis_tasks.py` L308: Replace this generic exception class with a more specific one.
- `tests/test_kis_tasks.py` L400: Replace this generic exception class with a more specific one.
- `app/services/upbit_websocket.py` L125: Replace this generic exception class with a more specific one.

#### `python:S7483` — 20개 

- `app/services/brokers/kis/base.py` L147: Remove this "timeout" parameter and use a timeout context manager instead.
- `app/services/brokers/kis/base.py` L201: Remove this "timeout" parameter and use a timeout context manager instead.
- `app/services/brokers/kis/base.py` L314: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L687: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L728: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L771: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L830: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L869: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L355: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L377: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L595: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L619: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L644: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L28: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L87: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L150: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L190: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L245: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L279: Remove this "timeout" parameter and use a timeout context manager instead.
- `tests/test_kis_rankings.py` L330: Remove this "timeout" parameter and use a timeout context manager instead.

#### `Web:S6853` — 16개 

- `app/templates/manual_holdings_dashboard.html` L223: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L231: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L238: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L242: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L246: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L250: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L273: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L277: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L281: A form label must be associated with a control and have accessible text.
- `app/templates/manual_holdings_dashboard.html` L285: A form label must be associated with a control and have accessible text.
- `app/templates/kis_domestic_trading_dashboard.html` L308: A form label must be associated with a control and have accessible text.
- `app/templates/kis_overseas_trading_dashboard.html` L295: A form label must be associated with a control and have accessible text.
- `app/templates/upbit_trading_dashboard.html` L315: A form label must be associated with a control and have accessible text.
- `blog/images/download-all-images.html` L117: A form label must be associated with a control and have accessible text.
- `blog/images/download-image4.html` L29: A form label must be associated with a control and have accessible text.
- `app/templates/stock_latest_dashboard.html` L261: A form label must be associated with a control and have accessible text.

#### `shelldre:S7679` — 14개 

- `scripts/test_discord_webhooks.sh` L18: Assign this positional parameter to a local variable.
- `scripts/test_discord_webhooks.sh` L23: Assign this positional parameter to a local variable.
- `scripts/test_discord_webhooks.sh` L27: Assign this positional parameter to a local variable.
- `scripts/test_discord_webhooks.sh` L31: Assign this positional parameter to a local variable.
- `scripts/test_discord_webhooks.sh` L35: Assign this positional parameter to a local variable.
- `docs/verify_tvscreener_endpoints.sh` L20: Assign this positional parameter to a local variable.
- `docs/verify_tvscreener_endpoints.sh` L24: Assign this positional parameter to a local variable.
- `docs/verify_tvscreener_endpoints.sh` L28: Assign this positional parameter to a local variable.
- `scripts/test-caddy-https.sh` L40: Assign this positional parameter to a local variable.
- `scripts/test-caddy-https.sh` L45: Assign this positional parameter to a local variable.
- `scripts/test-caddy-https.sh` L49: Assign this positional parameter to a local variable.
- `scripts/test-caddy-https.sh` L54: Assign this positional parameter to a local variable.
- `scripts/test-caddy-https.sh` L59: Assign this positional parameter to a local variable.
- `scripts/test-caddy-https.sh` L64: Assign this positional parameter to a local variable.

#### `python:S125` — 13개 — 주석 처리된 코드 → 제거

- `app/services/brokers/kis/market_data.py` L474: Remove this commented out code.
- `app/services/brokers/kis/market_data.py` L709: Remove this commented out code.
- `tests/test_mcp_quotes_tools.py` L195: Remove this commented out code.
- `tests/test_kis_trading_service.py` L1160: Remove this commented out code.
- `tests/test_kis_trading_service.py` L736: Remove this commented out code.
- `tests/test_merged_portfolio_service.py` L442: Remove this commented out code.
- `tests/test_kis_trading_service.py` L543: Remove this commented out code.
- `tests/test_trading_integration.py` L71: Remove this commented out code.
- `tests/test_kis_trading_service.py` L474: Remove this commented out code.
- `tests/test_symbol_trade_settings.py` L255: Remove this commented out code.
- `tests/test_symbol_trade_settings.py` L330: Remove this commented out code.
- `app/analysis/analyzer.py` L17: Remove this commented out code.
- `app/models/__init__.py` L74: Remove this commented out code.

#### `shelldre:S7677` — 12개 

- `quick_benchmark_subtask_5_5.sh` L15: Redirect this error message to stderr (>&2).
- `quick_benchmark_subtask_5_5.sh` L25: Redirect this error message to stderr (>&2).
- `docs/verify_tvscreener_endpoints.sh` L172: Redirect this error message to stderr (>&2).
- `scripts/healthcheck.sh` L74: Redirect this error message to stderr (>&2).
- `scripts/healthcheck.sh` L82: Redirect this error message to stderr (>&2).
- `scripts/test-caddy-https.sh` L137: Redirect this error message to stderr (>&2).
- `scripts/test-caddy-https.sh` L161: Redirect this error message to stderr (>&2).
- `scripts/test-caddy-https.sh` L188: Redirect this error message to stderr (>&2).
- `scripts/test-caddy-https.sh` L247: Redirect this error message to stderr (>&2).
- `scripts/test-caddy-https.sh` L281: Redirect this error message to stderr (>&2).
- `scripts/setup-test-env.sh` L13: Redirect this error message to stderr (>&2).
- `scripts/healthcheck.sh` L66: Redirect this error message to stderr (>&2).

#### `plsql:S1739` — 12개 

- `scripts/migrate_symbols_to_dot_format.sql` L11: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L11: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L17: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L17: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L23: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L23: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L29: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L29: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L34: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L38: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L42: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition
- `scripts/migrate_symbols_to_dot_format.sql` L46: Refactor this SQL query to prevent doing a full table scan due to the value of the "LIKE" condition

#### `python:S107` — 10개 — 파라미터 과다 (>13개) → dataclass로 묶기

- `app/mcp_server/tooling/analysis_registration.py` L161: Function "screen_stocks" has 15 parameters, which is greater than the 13 authorized.
- `app/mcp_server/tooling/analysis_screen_core.py` L642: Function "normalize_screen_request" has 15 parameters, which is greater than the 13 authorized.
- `app/mcp_server/tooling/analysis_screen_core.py` L2837: Function "screen_stocks_unified" has 15 parameters, which is greater than the 13 authorized.
- `app/mcp_server/tooling/analysis_tool_handlers.py` L519: Function "screen_stocks_impl" has 15 parameters, which is greater than the 13 authorized.
- `app/services/screener_service.py` L305: Method "list_screening" has 16 parameters, which is greater than the 13 authorized.
- `app/services/screener_service.py` L443: Method "refresh_screening" has 16 parameters, which is greater than the 13 authorized.
- `app/monitoring/trade_notifier.py` L1552: Method "_format_toss_price_recommendation_discord_embed" has 18 parameters, which is greater than th
- `app/routers/screener.py` L114: Function "screener_list" has 17 parameters, which is greater than the 13 authorized.
- `app/monitoring/trade_notifier.py` L1441: Method "_format_toss_price_recommendation_html" has 18 parameters, which is greater than the 13 auth
- `app/monitoring/trade_notifier.py` L1661: Method "notify_toss_price_recommendation" has 18 parameters, which is greater than the 13 authorized

#### `python:S1172` — 9개 

- `app/services/brokers/kis/domestic_orders.py` L87: Remove the unused function parameter "is_mock".
- `app/services/brokers/kis/overseas_orders.py` L217: Remove the unused function parameter "is_mock".
- `app/services/brokers/kis/overseas_orders.py` L463: Remove the unused function parameter "order_number".
- `app/mcp_server/tooling/orders_modify_cancel.py` L24: Remove the unused function parameter "remaining".
- `app/mcp_server/tooling/fundamentals_sources_naver.py` L893: Remove the unused function parameter "manual_peers".
- `app/jobs/kis_trading.py` L75: Remove the unused function parameter "kis_quantity".
- `app/jobs/kis_trading.py` L76: Remove the unused function parameter "kis_avg_price".
- `app/auth/token_repository.py` L56: Remove the unused function parameter "db".
- `app/analysis/service_analyzers.py` L392: Remove the unused function parameter "stock_name".

#### `python:S1871` — 9개 

- `app/services/brokers/kis/market_data.py` L259: Either merge this branch with the identical one on line "255" or change one of the implementations.
- `app/services/brokers/kis/market_data.py` L298: Either merge this branch with the identical one on line "294" or change one of the implementations.
- `app/services/brokers/kis/market_data.py` L403: Either merge this branch with the identical one on line "399" or change one of the implementations.
- `app/mcp_server/tooling/market_data_indicators.py` L808: Either merge this branch with the identical one on line "801" or change one of the implementations.
- `app/mcp_server/tooling/analysis_screen_core.py` L888: Either merge this branch with the identical one on line "886" or change one of the implementations.
- `app/mcp_server/tooling/analysis_screen_core.py` L894: Either merge this branch with the identical one on line "892" or change one of the implementations.
- `app/mcp_server/tooling/analysis_screen_core.py` L900: Either merge this branch with the identical one on line "898" or change one of the implementations.
- `app/mcp_server/tooling/analysis_screen_core.py` L906: Either merge this branch with the identical one on line "904" or change one of the implementations.
- `app/mcp_server/tooling/analysis_screen_core.py` L912: Either merge this branch with the identical one on line "910" or change one of the implementations.

#### `Web:S6819` — 8개 

- `app/templates/manual_holdings_dashboard.html` L116: Use <output> instead of the status role to ensure accessibility across all devices.
- `app/templates/manual_holdings_dashboard.html` L163: Use <output> instead of the status role to ensure accessibility across all devices.
- `app/templates/kis_domestic_trading_dashboard.html` L253: Use <output> instead of the status role to ensure accessibility across all devices.
- `app/templates/kis_overseas_trading_dashboard.html` L240: Use <output> instead of the status role to ensure accessibility across all devices.
- `app/templates/kis_domestic_trading_dashboard.html` L263: Use <output> instead of the status role to ensure accessibility across all devices.
- `app/templates/kis_overseas_trading_dashboard.html` L250: Use <output> instead of the status role to ensure accessibility across all devices.
- `app/templates/upbit_trading_dashboard.html` L287: Use <output> instead of the status role to ensure accessibility across all devices.
- `app/templates/stock_latest_dashboard.html` L310: Use <output> instead of the status role to ensure accessibility across all devices.

#### `python:S1066` — 6개 

- `tests/test_us_candles_sync.py` L68: Merge this if statement with the enclosing one.
- `app/services/redis_token_manager.py` L133: Merge this if statement with the enclosing one.
- `tests/test_import_contracts.py` L53: Merge this if statement with the enclosing one.
- `app/mcp_server/tooling/orders_history.py` L240: Merge this if statement with the enclosing one.
- `app/mcp_server/tooling/shared.py` L84: Merge this if statement with the enclosing one.
- `app/services/naver_finance.py` L483: Merge this if statement with the enclosing one.

#### `python:S6711` — 5개 

- `tests/test_mcp_indicator_math.py` L24: Use a "numpy.random.Generator" here instead of this legacy function.
- `tests/test_mcp_indicator_math.py` L28: Use a "numpy.random.Generator" here instead of this legacy function.
- `tests/test_mcp_indicator_math.py` L29: Use a "numpy.random.Generator" here instead of this legacy function.
- `tests/test_mcp_indicator_math.py` L30: Use a "numpy.random.Generator" here instead of this legacy function.
- `tests/test_mcp_indicator_math.py` L32: Use a "numpy.random.Generator" here instead of this legacy function.

#### `python:S6742` — 3개 

- `app/services/brokers/kis/market_data.py` L758: Refactor this long chain of instructions with "pandas.pipe"
- `app/services/brokers/kis/market_data.py` L846: Refactor this long chain of instructions with "pandas.pipe"
- `app/services/brokers/kis/market_data.py` L1208: Refactor this long chain of instructions with "pandas.pipe"

#### `python:S5603` — 2개 

- `app/services/brokers/kis/account.py` L18: Remove this unused function declaration.
- `tests/test_symbol_trade_settings.py` L367: Remove this unused function declaration.

#### `python:S5886` — 2개 

- `scripts/test_discord_webhook_e2e.py` L231: Return a value of type "NoneType" instead of "bool" or update function "display_configuration" type 
- `scripts/test_discord_webhook_e2e.py` L233: Return a value of type "NoneType" instead of "bool" or update function "display_configuration" type 

#### `javascript:S7761` — 2개 

- `app/templates/portfolio_dashboard.html` L546: Prefer `.dataset` over `getAttribute(…)`.
- `app/templates/upbit_trading_dashboard.html` L1501: Prefer `.dataset` over `setAttribute(…)`.

#### `javascript:S7785` — 2개 

- `app/templates/screener_dashboard.html` L1083: Prefer top-level await over an async function `fetchScreening` call.
- `app/templates/screener_report_detail.html` L525: Prefer top-level await over an async function `pollReport` call.

#### `python:S3358` — 2개 

- `app/services/kis_websocket.py` L790: Extract this nested conditional expression into an independent statement.
- `app/routers/stock_latest.py` L330: Extract this nested conditional expression into an independent statement.

#### `javascript:S3358` — 2개 

- `app/templates/kis_domestic_trading_dashboard.html` L422: Extract this nested ternary operation into an independent statement.
- `app/templates/kis_overseas_trading_dashboard.html` L409: Extract this nested ternary operation into an independent statement.

#### `javascript:S6660` — 2개 

- `app/templates/kis_domestic_trading_dashboard.html` L751: 'If' statement should not be the only statement in 'else' block
- `app/templates/kis_overseas_trading_dashboard.html` L744: 'If' statement should not be the only statement in 'else' block

#### `javascript:S4624` — 2개 

- `app/templates/stock_latest_dashboard.html` L600: Refactor this code to not use nested template literals.
- `app/templates/stock_latest_dashboard.html` L617: Refactor this code to not use nested template literals.

#### `python:S7512` — 1개 

- `app/mcp_server/tooling/analysis_screen_core.py` L1767: Modify this loop to iterate over the dictionary's values.

#### `python:S1854` — 1개 

- `app/services/stock_info_service.py` L297: Remove this assignment to local variable 'results'; the value is never used.

#### `javascript:S1854` — 1개 

- `app/templates/stock_latest_dashboard.html` L914: Remove this useless assignment to variable "data".

#### `javascript:S7762` — 1개 

- `app/templates/analysis_json_dashboard.html` L329: Prefer `childNode.remove()` over `parentNode.removeChild(childNode)`.

#### `Web:S6848` — 1개 

- `app/templates/analysis_list.html` L695: Avoid non-native interactive elements. If using native HTML is not possible, add an appropriate role

### 11. CODE_SMELL / MINOR (1233개)

#### `python:S7503` — 1107개 — async 함수인데 await 없음 → async 제거 또는 await 추가

- `tests/_mcp_screen_stocks_support.py` L1580: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1642: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1682: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1729: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1814: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1853: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1888: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1954: Use asynchronous features in this function or remove the `async` keyword.
- `tests/_mcp_screen_stocks_support.py` L1979: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L2899: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L2903: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L2945: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L2960: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L2991: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L2994: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3024: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3028: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3072: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3076: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3110: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3114: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3140: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L3152: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_screen_stocks_kr.py` L104: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L63: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L83: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L422: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L174: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L203: Use asynchronous features in this function or remove the `async` keyword.
- `tests/test_mcp_fundamentals_tools.py` L280: Use asynchronous features in this function or remove the `async` keyword.
- ... 외 1077개 더

#### `python:S117` — 31개 — 변수명 컨벤션

- `tests/test_mcp_screen_stocks_filters_and_rsi.py` L610: Rename this parameter "sortAsc" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/test_mcp_screen_stocks_filters_and_rsi.py` L610: Rename this parameter "sortField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/test_mcp_screen_stocks_filters_and_rsi.py` L982: Rename this parameter "sortAsc" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/test_mcp_screen_stocks_filters_and_rsi.py` L982: Rename this parameter "sortField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_tooling_support.py` L247: Rename this parameter "sortAsc" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_tooling_support.py` L247: Rename this parameter "sortField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/mcp_server/tooling/analysis_screen_core.py` L1656: Rename this local variable "CryptoField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/mcp_server/tooling/analysis_screen_core.py` L1939: Rename this local variable "StockField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/mcp_server/tooling/analysis_screen_core.py` L1940: Rename this local variable "Market" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/mcp_server/tooling/analysis_screen_core.py` L2166: Rename this local variable "StockField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/mcp_server/tooling/analysis_screen_core.py` L2167: Rename this local variable "Market" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/mcp_server/tooling/analysis_screen_core.py` L2584: Rename this local variable "CryptoField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/tvscreener_service.py` L424: Rename this local variable "CryptoScreener" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/tvscreener_service.py` L501: Rename this local variable "StockField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/tvscreener_service.py` L502: Rename this local variable "StockScreener" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/krx.py` L188: Rename this parameter "mktId" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/krx.py` L189: Rename this parameter "trdDd" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/krx.py` L190: Rename this parameter "idxIndClssCd" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L439: Rename this parameter "sortAsc" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L439: Rename this parameter "sortField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L2160: Rename this parameter "sortAsc" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L2160: Rename this parameter "sortField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L3446: Rename this parameter "sortAsc" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L3446: Rename this parameter "sortField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L3861: Rename this parameter "sortAsc" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/_mcp_screen_stocks_support.py` L3861: Rename this parameter "sortField" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/test_services_krx.py` L91: Rename this parameter "mktId" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `tests/test_services_krx.py` L91: Rename this parameter "trdDd" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/krx.py` L325: Rename this parameter "mktId" to match the regular expression ^[_a-z][a-z0-9_]*$.
- `app/services/krx.py` L326: Rename this parameter "trdDd" to match the regular expression ^[_a-z][a-z0-9_]*$.
- ... 외 1개 더

#### `javascript:S7781` — 25개 — JS 이슈

- `app/templates/kis_domestic_trading_dashboard.html` L834: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_domestic_trading_dashboard.html` L835: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_domestic_trading_dashboard.html` L836: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_domestic_trading_dashboard.html` L837: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_domestic_trading_dashboard.html` L838: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_domestic_trading_dashboard.html` L1001: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_overseas_trading_dashboard.html` L829: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_overseas_trading_dashboard.html` L830: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_overseas_trading_dashboard.html` L831: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_overseas_trading_dashboard.html` L832: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_overseas_trading_dashboard.html` L833: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/kis_overseas_trading_dashboard.html` L995: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/upbit_trading_dashboard.html` L1300: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/upbit_trading_dashboard.html` L1070: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/upbit_trading_dashboard.html` L1071: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/upbit_trading_dashboard.html` L1072: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/upbit_trading_dashboard.html` L1073: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/upbit_trading_dashboard.html` L1074: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/upbit_trading_dashboard.html` L1088: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/stock_latest_dashboard.html` L386: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/stock_latest_dashboard.html` L386: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/analysis_list.html` L883: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/analysis_list.html` L920: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/analysis_list.html` L1001: Prefer `String#replaceAll()` over `String#replace()`.
- `app/templates/analysis_list.html` L1005: Prefer `String#replaceAll()` over `String#replace()`.

#### `python:S1481` — 17개 — 미사용 변수 → 제거

- `tests/test_crypto_composite_score.py` L377: Replace the unused local variable "lo" with "_".
- `tests/test_crypto_composite_score.py` L377: Replace the unused local variable "c" with "_".
- `tests/test_crypto_composite_score.py` L383: Replace the unused local variable "lo" with "_".
- `tests/test_crypto_composite_score.py` L383: Replace the unused local variable "h" with "_".
- `tests/test_crypto_composite_score.py` L383: Replace the unused local variable "c" with "_".
- `app/mcp_server/tooling/shared.py` L613: Remove the unused local variable "max_score".
- `tests/test_kis_rankings.py` L661: Replace the unused loop index "i" with "_".
- `blog/tools/image_generator.py` L107: Remove the unused local variable "svg_paths".
- `app/jobs/analyze.py` L69: Replace the unused local variable "model" with "_".
- `upbit_websocket_monitor.py` L31: Replace the unused local variable "model" with "_".
- `app/analysis/service_analyzers.py` L581: Replace the unused local variable "model_name" with "_".
- `app/analysis/service_analyzers.py` L616: Replace the unused local variable "model_name" with "_".
- `app/analysis/service_analyzers.py` L198: Replace the unused local variable "model_name" with "_".
- `app/analysis/service_analyzers.py` L347: Replace the unused local variable "model_name" with "_".
- `app/analysis/service_analyzers.py` L163: Replace the unused local variable "model_name" with "_".
- `app/analysis/service_analyzers.py` L324: Replace the unused local variable "model_name" with "_".
- `app/analysis/service_analyzers.py` L508: Replace the unused local variable "model_name" with "_".

#### `javascript:S7773` — 13개 

- `app/templates/orderbook_dashboard.html` L346: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/orderbook_dashboard.html` L347: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/orderbook_dashboard.html` L352: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/orderbook_dashboard.html` L353: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/manual_holdings_dashboard.html` L547: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/manual_holdings_dashboard.html` L548: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/kis_domestic_trading_dashboard.html` L1020: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/kis_domestic_trading_dashboard.html` L1021: Prefer `Number.parseInt` over `parseInt`.
- `app/templates/kis_overseas_trading_dashboard.html` L1014: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/kis_overseas_trading_dashboard.html` L1015: Prefer `Number.parseInt` over `parseInt`.
- `app/templates/upbit_trading_dashboard.html` L1319: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/upbit_trading_dashboard.html` L1432: Prefer `Number.parseFloat` over `parseFloat`.
- `app/templates/upbit_trading_dashboard.html` L1433: Prefer `Number.parseFloat` over `parseFloat`.

#### `javascript:S7764` — 10개 

- `app/templates/portfolio_dashboard.html` L698: Prefer `globalThis` over `window`.
- `app/templates/screener_dashboard.html` L855: Prefer `globalThis` over `window`.
- `app/templates/screener_dashboard.html` L903: Prefer `globalThis` over `window`.
- `app/templates/screener_report_detail.html` L396: Prefer `globalThis` over `window`.
- `app/templates/screener_report_detail.html` L442: Prefer `globalThis` over `window`.
- `app/templates/orderbook_dashboard.html` L292: Prefer `globalThis` over `window`.
- `app/templates/orderbook_dashboard.html` L293: Prefer `globalThis` over `window`.
- `app/templates/upbit_trading_dashboard.html` L1077: Prefer `globalThis` over `window`.
- `app/templates/upbit_trading_dashboard.html` L1078: Prefer `globalThis` over `window`.
- `app/templates/analysis_list.html` L1062: Prefer `globalThis` over `window`.

#### `python:S7504` — 4개 

- `app/services/upbit_symbol_universe_service.py` L135: Remove this unnecessary `list()` call on an already iterable object.
- `app/services/us_symbol_universe_service.py` L157: Remove this unnecessary `list()` call on an already iterable object.
- `app/services/kr_symbol_universe_service.py` L252: Remove this unnecessary `list()` call on an already iterable object.
- `app/monitoring/sentry.py` L116: Remove this unnecessary `list()` call on an already iterable object.

#### `python:S5713` — 4개 

- `app/mcp_server/tooling/fundamentals_sources_coingecko.py` L158: Remove this redundant Exception class; it derives from another which is already caught.
- `app/mcp_server/tooling/fundamentals_handlers.py` L431: Remove this redundant Exception class; it derives from another which is already caught.
- `app/auth/web_router.py` L72: Remove this redundant Exception class; it derives from another which is already caught.
- `app/analysis/analyzer.py` L236: Remove this redundant Exception class; it derives from another which is already caught.

#### `python:S3626` — 4개 

- `app/services/krx.py` L871: Remove this redundant continue.
- `app/services/krx.py` L570: Remove this redundant continue.
- `app/services/krx.py` L702: Remove this redundant continue.
- `app/analysis/analyzer.py` L350: Remove this redundant continue.

#### `shelldre:S1481` — 3개 

- `docs/verify_tvscreener_endpoints.sh` L55: Remove the unused local variable 'start_time'.
- `docs/verify_tvscreener_endpoints.sh` L57: Remove the unused local variable 'end_time'.
- `scripts/healthcheck.sh` L17: Remove the unused local variable 'service'.

#### `shelldre:S1192` — 3개 

- `docs/verify_tvscreener_endpoints.sh` L162: Define a constant instead of using the literal 'tvscreener' 4 times.
- `docs/verify_tvscreener_endpoints.sh` L167: Define a constant instead of using the literal '==========================================' 4 times.
- `scripts/test-caddy-https.sh` L275: Define a constant instead of using the literal 'localhost' 6 times.

#### `python:S2737` — 2개 

- `app/services/us_intraday_candles_read_service.py` L625: Add logic to this except clause or eliminate it and rethrow the exception automatically.
- `app/services/krx.py` L749: Add logic to this except clause or eliminate it and rethrow the exception automatically.

#### `javascript:S2486` — 2개 

- `app/templates/admin_users.html` L278: Handle this exception or don't catch it at all.
- `app/templates/admin_users.html` L342: Handle this exception or don't catch it at all.

#### `docker:S7031` — 2개 

- `Dockerfile.api` L53: Merge this RUN instruction with the consecutive ones.
- `Dockerfile.ws` L60: Merge this RUN instruction with the consecutive ones.

#### `python:S7500` — 1개 

- `tests/test_tvscreener_integration.py` L195: Replace this comprehension with passing the iterable to the collection constructor call

#### `python:S7519` — 1개 

- `verify_e2e_implementation.py` L63: Replace with dict fromkeys method call

#### `javascript:S7735` — 1개 

- `app/templates/screener_report_detail.html` L428: Unexpected negated condition.

#### `python:S100` — 1개 

- `tests/test_services_krx.py` L598: Rename method "test_classify_etf_category_unknown_returns_기타" to match the regular expression ^[a-z_

#### `javascript:S7780` — 1개 

- `blog/images/download-image4.html` L134: `String.raw` should be used to avoid escaping `\`.

#### `javascript:S1481` — 1개 

- `app/templates/stock_latest_dashboard.html` L914: Remove the declaration of the unused 'data' variable.

### 12. CODE_SMELL / INFO (1개)

#### `python:S1135` — 1개 

- `app/routers/openclaw_callback.py` L51: Complete the task associated to this "TODO" comment.

---

## 수정 지침


1. 각 그룹을 위에서 아래로 순서대로 수정
2. 수정 후 `make test-unit` 실행해서 검증
3. 테스트 실패 시 해당 파일만 다시 수정 후 재실행
4. 한 rule 그룹 완료 시 다음으로 이동
5. VULNERABILITY는 보안 이슈라 특히 신중하게 처리
6. python:S2068 (하드코딩 크리덴셜)는 실제 시크릿인지 확인 후 처리
