# Upbit Symbol Lookup 전역 맵 제거 및 Strict DB 조회 전환 설계

## Summary

- 목표: Upbit 심볼 해석 경로에서 전역 맵/캐시(`_upbit_maps` + lazy proxy)를 제거하고 DB 직접 조회 API로 일원화한다.
- 범위: 런타임(`app/`), MCP tooling(`app/mcp_server/tooling/`), screenshot holdings 서비스, 관련 테스트/문서.
- 정책: fallback 없이 strict 예외 전파. 미등록/비활성/빈 테이블은 즉시 실패한다.

## Problem Statement

현재 Upbit 심볼 해석은 `upbit_symbol_universe_service`의 전역 맵과 lazy proxy(`COIN_TO_PAIR`, `COIN_TO_NAME_KR`, `NAME_TO_PAIR_KR` 등)에 의존한다. 이 구조는 다음 문제를 만든다.

1. 조회 타이밍과 초기화 순서(`prime_upbit_constants`)가 결합되어 런타임 에러 포인트가 분산된다.
2. 일부 경로에서 broad exception과 결합되며 오류가 silent fallback으로 숨겨진다.
3. MCP/screenshot 경로에서 map iterate 기반 해석이 남아 DB 계약(등록/활성 상태 기반)과 분리된다.

## Locked Decisions

- DB 스키마 변경 없음(마이그레이션 없음)
- 전역 캐시/TTL 캐시 도입 금지
- 루프 처리 중 미등록/비활성 발견 시 항목 skip 금지, 즉시 실패
- `db: AsyncSession | None` 시그니처 유지, `db is None`이면 내부 `AsyncSessionLocal` one-shot 조회
- 공개 계약 유지/변경은 아래 명시값만 허용

## Public Interface Changes

### Remove

- `prime_upbit_constants`
- `get_upbit_maps`
- `get_or_refresh_maps`
- `NAME_TO_PAIR_KR`
- `PAIR_TO_NAME_KR`
- `COIN_TO_PAIR`
- `COIN_TO_NAME_KR`
- `COIN_TO_NAME_EN`
- `KRW_TRADABLE_COINS`

### Keep

- `get_upbit_symbol_by_name`
- `get_active_upbit_markets`
- `get_upbit_warning_markets`
- `UpbitSymbolUniverseEmptyError`
- `UpbitSymbolNotRegisteredError`
- `UpbitSymbolInactiveError`
- `UpbitSymbolNameAmbiguousError`

### Add

- `get_upbit_market_by_coin(currency: str, quote_currency: str = "KRW", db: AsyncSession | None = None) -> str`
- `get_upbit_korean_name_by_coin(currency: str, quote_currency: str = "KRW", db: AsyncSession | None = None) -> str`
- `get_upbit_korean_name_by_market(market: str, db: AsyncSession | None = None) -> str`
- `get_upbit_coin_by_market(market: str, db: AsyncSession | None = None) -> str`
- `get_active_upbit_base_currencies(quote_currency: str = "KRW", db: AsyncSession | None = None) -> set[str]`

## Exception Contract

- 빈 테이블/조회 가능한 universe 부재: `UpbitSymbolUniverseEmptyError`
- 대상이 등록되지 않음: `UpbitSymbolNotRegisteredError`
- 대상이 등록되어 있으나 비활성: `UpbitSymbolInactiveError`
- 이름 기반 다중 활성 심볼 충돌: `UpbitSymbolNameAmbiguousError` (기존 계약 유지)
- 기본값 반환 금지(`dict.get(..., default)` 대체)

## Approaches Considered

1. **현행 유지 + 예외만 강화**
   - 장점: 변경량 작음
   - 단점: 전역 상태/초기화 순서 결합 지속, map 경로와 DB 계약 이원화
2. **전역 맵 유지 + 내부 캐시 무효화 개선**
   - 장점: 성능 유지
   - 단점: strict 계약과 충돌 가능성 높고 fallback 관성 유지
3. **선택안: 전역 맵 완전 제거 + DB strict lookup 단일화**
   - 장점: 계약 명확성, 오류 전파 일관성, 테스트 가능성 향상
   - 단점: 호출부 async lookup 전환 비용 증가

선택: 3번.

## Design Details

### 1) 서비스 코어 리팩터링

- 제거 대상: `_upbit_maps`, map builder(`_build_maps`), map loader(`_load_maps_from_db`), lazy proxy class/instance, map API.
- 신규 lookup helper는 모두 다음 공통 규칙을 따른다.
  - 입력 정규화 후 active row 우선 조회
  - 미조회 시 inactive 여부 확인 쿼리로 예외 유형 결정
  - `db is None`이면 내부 session 생성 후 one-shot 조회

### 2) 런타임 호출부 치환

- `upbit_websocket.py`: market code -> korean name은 `get_upbit_korean_name_by_market`로 치환
- `upbit_orderbook.py`: coin code 정규화는 `get_upbit_market_by_coin` 사용
- `routers/orderbook.py`: 전체 KRW 마켓 목록은 `get_active_upbit_markets(quote_currency="KRW")`
- `routers/upbit_trading.py`/`jobs/analyze.py`/`analysis/service_analyzers.py`/`routers/symbol_settings.py`
  - tradable membership 체크(`KRW_TRADABLE_COINS`) 제거
  - coin 검증은 `get_upbit_market_by_coin` 호출로 대체
  - coin 이름 해석은 `get_upbit_korean_name_by_coin`
- `jobs/daily_scan.py`: `_coin_name`을 async 조회로 전환하고 실패 시 전파

### 3) MCP/tooling 및 screenshot 경로

- `screenshot_holdings_service.py`: `get_or_refresh_maps` 제거, crypto name -> symbol 해석은 strict DB lookup(`get_upbit_symbol_by_name` 등) 사용
- `market_data_quotes.py`: crypto search map iterate 제거, `search_upbit_symbols` 사용
- `fundamentals_sources_coingecko.py`: 보유 코인 필터링 시 coin별 strict DB 검증 사용
- `portfolio_holdings.py`: `COIN_TO_NAME_KR` 제거, coin별 이름 조회 함수 사용

### 4) broad exception 정리

- symbol lookup 관련 `except Exception: pass` 구간은 제거 또는 domain-aware 재throw로 축소
- 예외를 payload로 변환해야 하는 MCP 경로는 silent suppress 대신 명시적 에러 메시지/오류 목록 반환

## Callsite Migration Matrix

- Runtime: `app/services/upbit_websocket.py`, `app/services/upbit_orderbook.py`, `app/routers/orderbook.py`, `app/routers/upbit_trading.py`, `app/jobs/analyze.py`, `app/jobs/daily_scan.py`, `app/analysis/service_analyzers.py`, `app/routers/symbol_settings.py`
- MCP/Tooling: `app/mcp_server/tooling/market_data_quotes.py`, `app/mcp_server/tooling/fundamentals_sources_coingecko.py`, `app/mcp_server/tooling/portfolio_holdings.py`
- Screenshot: `app/services/screenshot_holdings_service.py`
- 추가 동기화 대상(전역 API 삭제 영향): `websocket_monitor.py`, `upbit_websocket_monitor.py`, 관련 테스트

## Testing Strategy

### Unit (Service API)

- 신규 API 성공/미등록/비활성/빈 테이블 케이스 모두 검증
- 예외 타입별 메시지에 sync hint 포함 여부 검증

### Runtime Integration

- websocket 체결 이벤트의 code -> korean_name strict 조회
- orderbook coin 정규화(strict coin->market)
- analyze/upbit_trading에서 미등록 코인 즉시 실패

### MCP/Screenshot

- map 없이 crypto symbol/name 해석 동작
- 해석 실패 시 silent fallback 제거(명시적 에러)

### Regression

- map API 제거 후 import/monkeypatch 경로 정리
- 기존 `get_active_upbit_markets`, `get_upbit_warning_markets`, `search_upbit_symbols` 동작 유지

## Verification Commands

```bash
uv run pytest tests/test_upbit_symbol_universe_sync.py
uv run pytest tests/test_upbit_trading.py tests/test_tasks.py tests/test_daily_scan.py tests/test_upbit_orderbook_service.py tests/test_screenshot_holdings_service_resolution.py
uv run pytest tests/test_mcp_server_tools.py -k "get_or_refresh_maps or COIN_TO_NAME_KR or NAME_TO_PAIR_KR"
make lint
```

## Risks and Mitigations

- 리스크: async lookup 증가로 루프 경로 I/O 빈도 증가
  - 대응: 동작 정확성 우선 적용 후 병목이 확인될 때 명시적 배치 API 별도 설계
- 리스크: broad catch 제거로 기존 "조용한 성공" 경로가 실패로 전환
  - 대응: MCP README/테스트를 함께 업데이트하여 계약을 명문화
- 리스크: 제거 API를 참조하는 주변 스크립트 누락
  - 대응: `rg` 기반 전역 스캔과 관련 테스트 동시 수정

## Out of Scope

- DB schema 변경
- 캐시 재도입/TTL 정책 변경
- Upbit sync 스케줄/잡 정책 변경
