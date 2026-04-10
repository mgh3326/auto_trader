# Fix: KIS ETF 심볼 현재가 조회 실패 (Issue #487)

## 문제

`get_holdings(include_current_price=True)` 호출 시 KIS 국내 계좌에 포함된 ETF 8개 심볼의 현재가 조회가 실패한다.
모두 `current_price` 단계에서 `Symbol '133690' not found` 에러가 발생한다.

**영향받는 심볼:** 133690, 360750, 368590, 379780, 379810, 381170, 381180, 433330

## 근본 원인

KIS API의 `FID_COND_MRKT_DIV_CODE` 파라미터에 `"UN"` (통합) 마켓 코드를 사용하고 있는데,
이 코드는 KOSPI/KOSDAQ 일반 주식만 커버하며 **ETF/ETN을 포함하지 않는다**.

에러 발생 경로:
1. `_collect_kis_positions()` → KIS `fetch_my_stocks()` 결과를 모두 `instrument_type="equity_kr"`로 분류
2. `_fetch_price_map_for_positions()` → `equity_kr` 포지션에 대해 `_fetch_quote_equity_kr()` 호출
3. `_fetch_quote_equity_kr()` → `inquire_daily_itemchartprice(code=symbol, market="UN", n=1)` 호출
4. KIS API가 ETF 심볼에 대해 빈 결과 반환 → `ValueError("Symbol not found")` 발생

## 해결 방안

KIS 국내 시장 API 호출의 기본 마켓 코드를 `"UN"` → `"J"`로 변경한다.

- `"J"` = KRX 통합 코드 (주식 + ETF/ETN 포함)
- `"UN"` = 통합 (주식만, ETF/ETN 제외)
- `kr_hourly_candles_read_service`에서 이미 `market_code="J"`를 실운영에 사용 중이며 정상 동작 확인됨
- `"J"`는 일반 주식 조회 결과에도 영향 없음 (KOSPI/KOSDAQ 동일 커버)

## 변경 범위

### 프로덕션 코드 (7개 파일)

| 파일 | 변경 내용 | 변경 수 |
|------|-----------|---------|
| `app/services/brokers/kis/domestic_market_data.py` | 8개 메서드의 `market: str = "UN"` 기본값 → `"J"` | 8 |
| `app/services/brokers/kis/client.py` | 8개 프록시 메서드의 기본값 → `"J"` | 8 |
| `app/services/brokers/kis/constants.py` | `DOMESTIC_MARKET_CODES` 주석 명확화 | 1 |
| `app/services/market_data/service.py` | 4곳의 hardcoded `market="UN"` → `"J"` | 4 |
| `app/mcp_server/tooling/market_data_quotes.py` | 3곳의 hardcoded `market="UN"` → `"J"` | 3 |
| `app/mcp_server/tooling/market_data_indicators.py` | 2곳의 hardcoded `market="UN"` → `"J"` | 2 |
| `app/routers/trading.py` | Protocol 시그니처 1곳 + hardcoded 1곳 → `"J"` | 2 |

### 기본값 상속으로 자동 커버되는 파일 (변경 불필요)

| 파일 | 호출 방식 |
|------|-----------|
| `app/services/portfolio_overview_service.py:645` | `kis_client.inquire_price(symbol)` — market 인자 미전달 |
| `app/services/merged_portfolio_service.py:289` | `kis_client.inquire_price(ticker)` — market 인자 미전달 |

이 두 파일은 `domestic_market_data.py`의 기본값 변경으로 자동으로 `"J"`가 적용된다.

### 테스트 코드 (6개 파일)

| 파일 | 변경 내용 |
|------|-----------|
| `tests/test_services_kis_market_data.py` | `market="UN"` → `"J"` (6곳) |
| `tests/test_mcp_ohlcv_tools.py` | assert `"UN"` → `"J"` (1곳) |
| `tests/test_mcp_quotes_tools.py` | assert `"UN"` → `"J"` (1곳) |
| `tests/test_mcp_indicator_tools.py` | assert `"UN"` → `"J"` (2곳) |
| `tests/test_market_data_service.py` | mock 시그니처 + assert `"UN"` → `"J"` (다수) |
| `tests/test_trading_orderbook_router.py` | mock 시그니처 `"UN"` → `"J"` (4곳) |

### `DOMESTIC_MARKET_CODES` 주석 명확화

```python
# 변경 전
DOMESTIC_MARKET_CODES = {
    "K": "코스피",
    "Q": "코스닥",
    "UN": "통합",
    "J": "통합(랭킹 호환)",
}

# 변경 후
DOMESTIC_MARKET_CODES = {
    "K": "코스피",
    "Q": "코스닥",
    "J": "통합(주식+ETF/ETN)",   # 기본값
    "UN": "통합(주식만, ETF/ETN 제외)",
}
```

## ETF 회귀 단위 테스트

`tests/test_mcp_quotes_tools.py`에 parametrize 테스트 추가:

```python
@pytest.mark.parametrize("symbol,label", [
    ("005930", "stock"),   # 삼성전자 — 일반 주식
    ("133690", "etf"),     # TIGER 은행TOP10 — ETF
])
async def test_fetch_quote_equity_kr_uses_market_j(symbol, label):
    """market='J' 코드로 KIS API 호출, 일반 주식과 ETF 모두 정상 동작 확인."""
    # inquire_daily_itemchartprice를 mock하여:
    # 1. market="J"가 전달되는지 assert
    # 2. 정상 DataFrame 반환 시 올바른 dict 구조가 나오는지 assert
    ...
```

**검증 항목:**
- `inquire_daily_itemchartprice`에 `market="J"`가 전달되는지
- 정상 DataFrame 반환 시 `{"symbol", "instrument_type", "price", "source"}` 키가 포함되는지
- ETF 심볼과 일반 주식 심볼 모두 동일한 코드 경로로 처리되는지

## 테스트 플랜

### 자동화 테스트
1. 기존 테스트 전량 `"UN"` → `"J"` 변경 후 `make test` 통과 확인
2. ETF + 일반 주식 parametrize 단위 테스트 추가 및 통과 확인

### 수동 검증 (PR 머지 전 1회)
- KIS API에서 일반 주식 심볼(005930)을 `market="J"`로 호출하여 기존과 동일한 결과 반환 확인
- KIS API에서 ETF 심볼(133690)을 `market="J"`로 호출하여 정상 결과 반환 확인

### 별도 이슈로 분리 (선택)
- `get_holdings` 통합 테스트에 ETF 포지션 포함 시나리오 추가

## 변경하지 않는 것

- ETF를 별도 `instrument_type`(`"etf_kr"` 등)으로 분류하지 않음
- `kr_symbol_universe` 싱크 소스에 ETF 전용 소스를 추가하지 않음
- `_collect_kis_positions()`의 `instrument_type="equity_kr"` 분류 로직 변경 없음
- `portfolio_overview_service.py`, `merged_portfolio_service.py`는 기본값 상속으로 자동 커버되므로 직접 수정하지 않음
