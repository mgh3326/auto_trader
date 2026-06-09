# ROB-471 — `get_quote(US)` 가격 소스를 KIS 해외 현재가로 전환 (Yahoo fast_info → KIS overseas primary)

- **Linear**: ROB-471 (High, Improvement) · 관련 ROB-416 / ROB-365 / ROB-97
- **Date**: 2026-06-09
- **Status**: Design approved → plan → TDD implementation (subagent-driven)
- **Scope**: read-only 시세 경로. broker/order/watch/order-intent mutation 없음. migration 없음.

---

## 1. 배경 / 문제

`get_quote(market="us")`가 운영 중 **전건 실패**(`Symbol 'X' not found`)하는 사례가 반복된다(ROB-416). 근본원인은 "US 미지원"이 아니라 **Yahoo `fast_info`의 crumb/auth/rate-limit 취약성** — fast_info가 예외 없이 빈 `last_price`(close=None) dict를 반환하면 `_fetch_quote_equity_us`가 모든 US 티커를 `Symbol not found`로 표면화한다. 즉 시세 소스(Yahoo)가 systemically 흔들리면 US 시세 경로 전체가 죽는다.

운영자 방향(2026-06-09): Yahoo fast_info가 불안정하면 **KIS 해외(해외주식) 현재가 broker API로 US 가격을 가져온다.** KIS를 **primary**, Yahoo를 **fallback**으로.

## 2. 핵심 발견 (코드 grounding)

`get_quote` 외에 **이미 존재하지만 구현되지 않은** 계약이 있다:

- `KISClient.inquire_overseas_price(symbol)`는 **4개 프로덕션 호출지에서 호출**되지만 **어디에도 정의되지 않았다** → 런타임 `AttributeError` → 각 호출지가 `try/except`로 삼키고 `current_price = 0`으로 degrade:
  - `app/jobs/kis_trading.py:79` (`_fetch_overseas_new_price`)
  - `app/jobs/kis_market_adapters.py:760` (`fetch_manual_price`)
  - `app/services/merged_portfolio_service.py:294`, `:458`
- 테스트 모킹(`tests/test_merged_portfolio_service.py`, `tests/test_kis_tasks.py`)이 이미 계약을 고정한다: **`inquire_overseas_price(symbol) -> DataFrame`, `close` 컬럼, row 0 = 현재가, exchange 인자 불필요(positional 1개).**
- KIS 상수는 이미 정의됨(미사용): `app/services/brokers/kis/constants.py:99-100`
  - `OVERSEAS_PRICE_URL = "/uapi/overseas-price/v1/quotations/price"`
  - `OVERSEAS_PRICE_TR = "HHDFS00000300"  # 해외주식 현재가 조회`

→ 따라서 **이 누락된 메서드 하나를 구현**하고 `_fetch_quote_equity_us`를 그것으로 라우팅하면, get_quote가 고쳐지는 동시에 **위 4개 깨진 호출지가 부수적으로 복구**된다.

## 3. 확정된 결정 (브레인스토밍, 2026-06-09)

| # | 결정 | 선택 |
|---|------|------|
| D1 | 거래소(NASDAQ/NYSE/AMEX) 해석 | **DB 조회** `get_us_exchange_by_symbol()` (us_symbol_universe). 미등록/inactive/empty → Yahoo fallback. |
| D2 | 컷오버 공격성 | **플래그, default KIS-primary.** `us_quote_kis_primary: bool = True`. operator가 `false`로 즉시 롤백. |
| D3 | 응답 필드 / TR | **HHDFS00000300 minimal.** price(`last`) + previous_close(`base`) + volume(`tvol`). open/high/low = `None`(정직, 위조 금지). |
| D4 | 4개 깨진 호출지 범위 | **메서드 구현(부수 복구) + 라이트 회귀 테스트.** 각 호출지 로직 확장은 follow-up. |

추가 정직 메타(이슈 §4, 자명한 선택으로 채택): `source="kis_overseas"`, `delayed=True`. `as_of`는 HHDFS00000300에 타임스탬프 필드가 없어 생략(타임스탬프 TR 전환 시 follow-up).

## 4. 설계

### 4.1 컴포넌트 A — `OverseasMarketDataMixin.inquire_overseas_price`
파일: `app/services/brokers/kis/overseas_market_data.py` (형제 메서드 `inquire_overseas_daily_price` 바로 뒤, ~:234)

```python
async def inquire_overseas_price(
    self, symbol: str, exchange_code: str = "NASD"
) -> pd.DataFrame:
    """해외주식 현재가 조회 (HHDFS00000300).

    Returns a single-row DataFrame with at least a 'close' column
    (current price). previous_close/volume도 가능하면 포함.
    빈/파싱불가 응답이면 empty DataFrame (예외 아님).
    """
    excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
    excd = excd_map.get(exchange_code, exchange_code[:3])
    js = await self._request_with_token_retry(
        tr_id=constants.OVERSEAS_PRICE_TR,
        url=self._kis_url(constants.OVERSEAS_PRICE_URL),
        params={"AUTH": "", "EXCD": excd, "SYMB": to_kis_symbol(symbol)},
        timeout=10,
        api_name="inquire_overseas_price",
    )
    out = js.get("output") or {}
    return self._build_overseas_price_frame(out)
```

`_build_overseas_price_frame(out)` (정적 헬퍼):
- `close = _to_float_or_none(out.get("last"))` — **현재가** (코드베이스의 minute 파서도 `last`를 close로 매핑: overseas_market_data.py:42). `last`만을 현재가 필드로 사용(grab-bag 금지 → 실제 KIS 필드가 다르면 잘못된 숫자가 아니라 None→fallback).
- `previous_close = _to_float_or_none(out.get("base"))` — 전일종가
- `volume = _to_int_or_none(out.get("tvol"))` — 당일거래량
- `close`가 `None` 또는 `<= 0`이면 → **empty DataFrame** 반환(컬럼만 `["close","previous_close","volume"]`).
- 아니면 1-row DataFrame: `[{close, previous_close, volume}]`.

**에러 계약**: transport/auth 에러는 `_request_with_token_retry`에서 예외로 전파(기존 4개 호출지가 try/except로 처리). "응답했으나 가격 없음" → empty frame. → infra 실패 vs no-data 분리.

> 참고: HHDFS00000300 정확한 `output` 필드명은 라이브/문서 검증 게이트(operator). `last/base/tvol`은 KIS 해외 현재가 표준 필드이며 코드베이스의 minute 파서(`last`→close)와 일치한다. 방어적 파싱 + D2 플래그가 안전망.

### 4.2 컴포넌트 B — `KISClient.inquire_overseas_price` 위임
파일: `app/services/brokers/kis/client.py` (`inquire_overseas_daily_price` 위임 ~:244-254 뒤)

```python
async def inquire_overseas_price(
    self, symbol: str, exchange_code: str = "NASD"
) -> DataFrame:
    return await self._market_data.inquire_overseas_price(symbol, exchange_code)
```

### 4.3 컴포넌트 C — `_fetch_quote_equity_us` 전환
파일: `app/mcp_server/tooling/market_data_quotes.py:490-546`

구조 (KIS-primary, Yahoo-fallback, 플래그 게이트):

```python
async def _fetch_quote_equity_us(symbol: str) -> dict[str, Any]:
    norm = str(symbol or "").strip().upper()
    not_found_message = f"Symbol '{norm}' not found"
    unavailable_message = f"Quote temporarily unavailable for '{norm}'"

    kis_infra_error = False
    if settings.us_quote_kis_primary:
        try:
            quote = await _fetch_us_quote_from_kis(norm)  # dict | None
            if quote is not None:
                return quote
            # None = KIS가 응답했으나 가격 없음 / 심볼 미등록 → no-data, fall through
        except Exception as exc:
            kis_infra_error = True
            logger.warning("KIS overseas quote failed for %s; falling back to Yahoo: %s", norm, exc)

    # FALLBACK: Yahoo (기존 본문 보존)
    try:
        fast_info = await yahoo_service.fetch_fast_info(norm)
    except Exception as exc:
        raise RuntimeError(unavailable_message) from exc   # 양쪽 infra 실패 → quote_unavailable

    close_raw = fast_info.get("close")
    if close_raw is None or _to_float_or_none(close_raw) is None or float(close_raw) <= 0:
        if kis_infra_error:
            raise RuntimeError(unavailable_message)  # KIS infra + Yahoo no-price → 심볼 확정 불가
        raise ValueError(not_found_message)          # 둘 다 정상응답·가격 없음 → symbol_not_found
    # ... 기존 Yahoo 응답 dict (+ "delayed": True)
```

`_fetch_us_quote_from_kis(norm)` (헬퍼, KIS arm 격리):
```python
async def _fetch_us_quote_from_kis(norm: str) -> dict[str, Any] | None:
    # 거래소 해석 (D1). 미등록/inactive/empty → None (Yahoo fallback)
    try:
        exchange = await get_us_exchange_by_symbol(to_db_symbol(norm))
    except (USSymbolNotRegisteredError, USSymbolInactiveError, USSymbolUniverseEmptyError):
        return None
    df = await KISClient().inquire_overseas_price(norm, exchange)  # transport 에러는 전파
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    close = _to_float_or_none(row.get("close"))
    if close is None or close <= 0:
        return None
    return {
        "symbol": norm,
        "instrument_type": "equity_us",
        "price": close,
        "previous_close": _to_float_or_none(row.get("previous_close")),
        "open": None, "high": None, "low": None,
        "volume": _to_int_or_none(row.get("volume")),
        "source": "kis_overseas",
        "delayed": True,
    }
```

- KIS 거래소 해석 실패(미등록 등)는 **infra 아님** → `None` → Yahoo fallback(정상 분기).
- KIS HTTP transport 에러만 예외 전파 → 상위에서 `kis_infra_error=True`.

### 4.4 컴포넌트 D — 정직 에러 분리 (이슈 §3)
- **symbol_not_found** (`ValueError`): KIS·Yahoo 둘 다 정상 응답했으나 가격 없음.
- **quote_unavailable** (`RuntimeError`): 한쪽이라도 infra 실패 + 가격 미확보 → throttle/outage가 "Symbol not found"로 오분류되지 않음(ROB-416 핵심 불만 해소).

`_get_quote_impl` 디스패치(:987-988)는 현재 equity_us를 try/except 없이 호출(예외 전파). 이 동작은 유지하되, `test_get_quote_us_equity_propagates_upstream_exception`과 정합. (US를 `_error_payload_from_exception`으로 감쌀지는 비-goal — 현 계약 보존.)

### 4.5 컴포넌트 E — 응답 형태 / 정직 메타
US KIS-primary 응답:
```
{symbol, instrument_type:"equity_us", price, previous_close, open:None, high:None, low:None,
 volume, source:"kis_overseas", delayed:True}
```
Yahoo fallback 응답: 기존 형태 + `delayed:True`(무료 fast_info도 ~15분 지연 → 정직·일관). `source:"yahoo"` 유지. `open/high/low`는 KIS 경로에서 정직하게 `None`(HHDFS00000300 미제공, 위조 금지).

### 4.6 컴포넌트 F — config 플래그
파일: `app/core/config.py` (KIS 섹션, ~:181 부근)
```python
# ROB-471: US get_quote 가격 소스. True면 KIS 해외 현재가 primary + Yahoo fallback.
# False면 Yahoo primary(레거시). 라이브 파싱 이상 시 operator 즉시 롤백 레버.
us_quote_kis_primary: bool = True
```
env: `US_QUOTE_KIS_PRIMARY`.

## 5. 새 import (market_data_quotes.py)
- `from app.core.symbol import to_db_symbol`
- `from app.services.us_symbol_universe_service import (get_us_exchange_by_symbol, USSymbolNotRegisteredError, USSymbolInactiveError, USSymbolUniverseEmptyError)` — 실제 export 명 확인 후 사용.
- `import logging` / module logger (기존 패턴 확인).

## 6. 테스트 (TDD)

### 6.1 단위 — `inquire_overseas_price` 파싱
파일: 기존 KIS 해외 테스트 위치(예: `tests/` KIS overseas) 또는 신규.
- 정상 `output`(`{last,base,tvol}`) → 1-row frame, `close==last`, `previous_close==base`, `volume==tvol`.
- `last` 누락/0/"" → empty frame.
- `_request_with_token_retry` 예외 → 전파.
- exchange_code 매핑(NASD→NAS, NYSE→NYS, AMEX→AMS) — params의 `EXCD` 검증.
- `SYMB == to_kis_symbol(symbol)` 검증(BRK.B → BRK/B).

### 6.2 `get_quote` US — `tests/test_mcp_quotes_tools.py`
- **rewrite** `test_get_quote_us_equity`: KIS-primary happy path. `KISClient.inquire_overseas_price` + `get_us_exchange_by_symbol` 모킹 → `source=="kis_overseas"`, `price`, `previous_close`, `delayed is True`, KIS가 해석된 exchange로 호출됨.
- **new** `test_get_quote_us_falls_back_to_yahoo`: KIS empty/raise → Yahoo 호출, `source=="yahoo"`, `delayed is True`.
- **new** `test_get_quote_us_symbol_not_found`: KIS no-data + Yahoo close=None → `ValueError` "not found".
- **new** `test_get_quote_us_quote_unavailable`: KIS infra raise + Yahoo raise → `RuntimeError` "temporarily unavailable".
- **new** `test_get_quote_us_flag_off_uses_yahoo`: `settings.us_quote_kis_primary=False` → KIS 미호출, `source=="yahoo"`.
- **reconcile** `test_get_quote_us_equity_propagates_upstream_exception`.

### 6.3 라이트 회귀 (D4)
- `KISClient().inquire_overseas_price(...)`가 실제로 존재하고 `close` 컬럼 frame을 반환함을 단언(4개 호출지가 의존하는 계약이 실재함). 기존 merged_portfolio/kis_tasks 모킹 테스트는 그대로 green이어야 함.

## 7. 변경 파일 요약
| 파일 | 변경 |
|------|------|
| `app/services/brokers/kis/overseas_market_data.py` | `inquire_overseas_price` + `_build_overseas_price_frame` 추가 |
| `app/services/brokers/kis/client.py` | `inquire_overseas_price` 위임 추가 |
| `app/core/config.py` | `us_quote_kis_primary` 플래그 추가 |
| `app/mcp_server/tooling/market_data_quotes.py` | `_fetch_quote_equity_us` 전환 + `_fetch_us_quote_from_kis` 헬퍼 + import |
| `tests/test_mcp_quotes_tools.py` | US quote 테스트 재작성/추가 |
| (신규/기존) KIS overseas 단위 테스트 | `inquire_overseas_price` 파싱 테스트 |

## 8. 안전 경계 / 비-goal / operator 게이트
- **read-only.** broker mutation·order·watch·migration 없음. `inquire_overseas_price`는 GET TR.
- get_quote US는 **live KIS client**(`KISClient()`, `kis_base_url`)만 사용 — mock 분기 없음. 따라서 실제 HHDFS00000300 필드명·15분 지연·프리/애프터마켓 동작은 **라이브 검증(operator)**이 게이트. 단위 테스트는 모킹 frame으로 로직 커버.
- **비-goal**: 4개 깨진 호출지의 per-caller 거래소 라우팅 정교화(default NASD로만 동작) → follow-up. HHDFS76200200(현재가상세) 풀 OHLCV·`as_of` 타임스탬프 → follow-up. 키움 US 현재가 → 별도 통합(ROB-97 계열).
- **ROB-416 처분**: 코드 내 stopgap 없음(grep 0 hits) — 본 이슈가 상위 해법. 별도 코드 액션 불필요.
- **롤백 레버**: `US_QUOTE_KIS_PRIMARY=false`.
