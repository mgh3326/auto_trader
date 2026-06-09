# ROB-471 — get_quote(US) → KIS 해외 현재가 primary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** US `get_quote`의 가격 소스를 Yahoo fast_info에서 KIS 해외 현재가(HHDFS00000300) primary로 전환하고, 누락된 `inquire_overseas_price`를 구현한다(4개 깨진 호출지 부수 복구).

**Architecture:** 새 `OverseasMarketDataMixin.inquire_overseas_price`(+ `KISClient` 위임)가 HHDFS00000300을 호출해 단일행 `close/previous_close/volume` DataFrame을 반환. `_fetch_quote_equity_us`가 `us_quote_kis_primary` 플래그(default True) 하에서 KIS-primary → Yahoo-fallback으로 동작하며, 거래소는 `get_us_exchange_by_symbol`(DB)로 해석. 정직 메타(`source`, `delayed`) + 에러 분리(symbol_not_found vs quote_unavailable).

**Tech Stack:** Python 3.13, async, pandas, pydantic-settings, pytest/pytest-asyncio, KIS REST.

**Spec:** `docs/superpowers/specs/2026-06-09-rob471-get-quote-us-kis-overseas-design.md`

---

## File Structure

| 파일 | 책임 | 변경 |
|------|------|------|
| `app/services/brokers/kis/overseas_market_data.py` | KIS 해외 마켓데이터 | `inquire_overseas_price` + `_build_overseas_price_frame` 추가 (Task 1) |
| `app/services/brokers/kis/client.py` | KISClient 파사드 | `inquire_overseas_price` 위임 (Task 1) |
| `tests/test_services_kis_market_data.py` | KIS 마켓데이터 단위 | 새 메서드 파싱/매핑/empty 테스트 (Task 1) |
| `app/core/config.py` | 런타임 설정 | `us_quote_kis_primary` 플래그 (Task 2) |
| `app/mcp_server/tooling/market_data_quotes.py` | get_quote 구현 | `_fetch_quote_equity_us` 전환 + `_fetch_us_quote_from_kis` + logger/imports/promoted helpers (Task 3) |
| `tests/test_mcp_quotes_tools.py` | get_quote 단위 | US 테스트 재작성/추가 (Task 3) |
| `tests/test_mcp_shared_utils.py` | get_quote 공유 단위 | INVALID 테스트 KIS-스킵 패치 (Task 4) |

---

## Task 1: KIS `inquire_overseas_price` (HHDFS00000300) + 위임

**Files:**
- Modify: `app/services/brokers/kis/overseas_market_data.py` (add method after `inquire_overseas_daily_price`, ~:234)
- Modify: `app/services/brokers/kis/client.py` (add delegate after the `inquire_overseas_daily_price` delegate, ~:254)
- Test: `tests/test_services_kis_market_data.py` (new class `TestKISOverseasPrice`)

- [ ] **Step 1: Write the failing tests**

`tests/test_services_kis_market_data.py` 끝에 추가 (기존 `TestKISOverseasDailyPrice`의 httpx/settings 패치 패턴을 미러):

```python
class TestKISOverseasPrice:
    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_overseas_price_parses_output(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": {"last": "150.25", "base": "148.00", "tvol": "1234567"},
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._token_manager = AsyncMock()

        result = await client.inquire_overseas_price(symbol="AAPL", exchange_code="NASD")

        assert not result.empty
        assert float(result.iloc[0]["close"]) == pytest.approx(150.25)
        assert float(result.iloc[0]["previous_close"]) == pytest.approx(148.00)
        assert int(result.iloc[0]["volume"]) == 1234567

        params = mock_client.get.call_args.kwargs["params"]
        assert params["EXCD"] == "NAS"
        assert params["SYMB"] == "AAPL"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_overseas_price_maps_exchange_and_symbol(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": {"last": "402.10", "base": "400.00", "tvol": "9000"},
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._token_manager = AsyncMock()

        await client.inquire_overseas_price(symbol="BRK.B", exchange_code="NYSE")

        params = mock_client.get.call_args.kwargs["params"]
        assert params["EXCD"] == "NYS"
        assert params["SYMB"] == "BRK/B"  # to_kis_symbol: . -> /

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_overseas_price_empty_when_no_last(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"rt_cd": "0", "output": {"base": "100.0"}}
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._token_manager = AsyncMock()

        result = await client.inquire_overseas_price(symbol="AAPL")
        assert result.empty
        assert list(result.columns) == ["close", "previous_close", "volume"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_services_kis_market_data.py::TestKISOverseasPrice -v`
Expected: FAIL — `AttributeError: 'KISClient' object has no attribute 'inquire_overseas_price'`

- [ ] **Step 3: Implement the method + builder in the mixin**

`app/services/brokers/kis/overseas_market_data.py`, `inquire_overseas_daily_price`(~:234) 바로 뒤에 추가. (파일 상단에 이미 `from app.core.symbol import to_kis_symbol`, `from . import constants`, `import pandas as pd`, `from typing import Any` 존재.)

```python
    async def inquire_overseas_price(
        self, symbol: str, exchange_code: str = "NASD"
    ) -> pd.DataFrame:
        """해외주식 현재가 조회 (HHDFS00000300).

        Returns a single-row DataFrame with columns [close, previous_close,
        volume]. 'last'(현재가)이 없거나 <= 0이면 empty DataFrame(예외 아님).
        transport/auth 에러는 _request_with_token_retry에서 예외로 전파된다.
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

    @staticmethod
    def _build_overseas_price_frame(out: dict[str, Any]) -> pd.DataFrame:
        """HHDFS00000300 output dict → 단일행 현재가 DataFrame.

        'last'(현재가) 없거나 <= 0 → empty frame. 위조 금지.
        """
        empty_cols = ["close", "previous_close", "volume"]

        def _f(value: Any) -> float | None:
            try:
                return float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        def _i(value: Any) -> int | None:
            try:
                return int(float(value)) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        close = _f(out.get("last"))
        if close is None or close <= 0:
            return pd.DataFrame(columns=empty_cols)
        return pd.DataFrame(
            [
                {
                    "close": close,
                    "previous_close": _f(out.get("base")),
                    "volume": _i(out.get("tvol")),
                }
            ]
        )
```

- [ ] **Step 4: Add the `KISClient` delegate**

`app/services/brokers/kis/client.py`, `inquire_overseas_daily_price` 위임(~:251-254) 바로 뒤에 추가 (`DataFrame`은 이미 import됨):

```python
    async def inquire_overseas_price(
        self, symbol: str, exchange_code: str = "NASD"
    ) -> DataFrame:
        return await self._market_data.inquire_overseas_price(symbol, exchange_code)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_services_kis_market_data.py::TestKISOverseasPrice -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add app/services/brokers/kis/overseas_market_data.py app/services/brokers/kis/client.py tests/test_services_kis_market_data.py
git commit -m "feat(ROB-471): KIS inquire_overseas_price (HHDFS00000300) 해외 현재가 구현

last->close/base->previous_close/tvol->volume 단일행 frame, no-last면 empty.
누락 계약(4개 호출지 의존) 구현. read-only 조회 TR.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `us_quote_kis_primary` config 플래그

**Files:**
- Modify: `app/core/config.py` (KIS 섹션, `kis_mock_scalping_enabled` ~:181 바로 뒤)

- [ ] **Step 1: Add the field**

`app/core/config.py`의 `kis_mock_scalping_enabled: bool = False` 줄(~:181) 다음에 추가:

```python

    # ROB-471: US get_quote 가격 소스 선택. True → KIS 해외 현재가(HHDFS00000300)
    # primary + Yahoo fast_info fallback. False → Yahoo primary(레거시).
    # 라이브 파싱 이상 시 operator가 US_QUOTE_KIS_PRIMARY=false 로 즉시 롤백.
    us_quote_kis_primary: bool = True
```

- [ ] **Step 2: Verify the field loads with the correct default**

Run: `uv run python -c "from app.core.config import settings; print(settings.us_quote_kis_primary)"`
Expected: `True`

(이 플래그의 동작은 Task 3의 `test_get_quote_us_flag_off_uses_yahoo`에서 기능적으로 검증된다.)

- [ ] **Step 3: Commit**

```bash
git add app/core/config.py
git commit -m "feat(ROB-471): us_quote_kis_primary 플래그 (default True)

US get_quote KIS-primary 컷오버의 operator 롤백 레버.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `_fetch_quote_equity_us` KIS-primary 전환

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py` (imports 상단; `_fetch_quote_equity_us` :490-546 재작성; 새 헬퍼)
- Test: `tests/test_mcp_quotes_tools.py` (US 섹션 :360-406 재작성/추가)

- [ ] **Step 1: Write/replace the failing tests**

`tests/test_mcp_quotes_tools.py` 상단 import에 다음이 없으면 추가:
```python
import pandas as pd
from app.services.us_symbol_universe_service import USSymbolNotRegisteredError
```
(`AsyncMock`, `_patch_runtime_attr`, `yahoo_service`, `build_tools`, `pytest`는 이미 import됨.)

기존 `test_get_quote_us_equity`(:366-392)를 아래 KIS-primary 버전으로 **교체**하고, 기존 `test_get_quote_us_equity_propagates_upstream_exception`(:396-406)을 아래 버전으로 **교체**한 뒤, 나머지 4개 테스트를 **추가**:

```python
@pytest.mark.asyncio
async def test_get_quote_us_equity(monkeypatch):
    """KIS-primary happy path: source=kis_overseas, Yahoo 미호출."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    price_df = pd.DataFrame(
        [{"close": 205.0, "previous_close": 201.5, "volume": 123456789}]
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return price_df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=AssertionError("Yahoo should not be called")),
    )

    result = await tools["get_quote"]("AAPL")

    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "kis_overseas"
    assert result["price"] == pytest.approx(205.0)
    assert result["previous_close"] == pytest.approx(201.5)
    assert result["volume"] == 123456789
    assert result["open"] is None
    assert result["high"] is None
    assert result["low"] is None
    assert result["delayed"] is True


@pytest.mark.asyncio
async def test_get_quote_us_falls_back_to_yahoo(monkeypatch):
    """KIS empty → Yahoo fallback, source=yahoo."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return pd.DataFrame(columns=["close", "previous_close", "volume"])

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    mock_fast_info = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "close": 205.0,
            "previous_close": 201.5,
            "open": 202.0,
            "high": 206.2,
            "low": 200.8,
            "volume": 123456789,
        }
    )
    monkeypatch.setattr(yahoo_service, "fetch_fast_info", mock_fast_info)

    result = await tools["get_quote"]("AAPL")

    assert result["source"] == "yahoo"
    assert result["price"] == pytest.approx(205.0)
    assert result["open"] == pytest.approx(202.0)
    assert result["delayed"] is True
    mock_fast_info.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_symbol_not_found(monkeypatch):
    """KIS no-route(clean) + Yahoo close=None → ValueError symbol_not_found."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=USSymbolNotRegisteredError("not registered")),
    )
    monkeypatch.setattr(
        yahoo_service, "fetch_fast_info", AsyncMock(return_value={"close": None})
    )

    with pytest.raises(ValueError, match="Symbol 'AAPL' not found"):
        await tools["get_quote"]("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_quote_unavailable(monkeypatch):
    """KIS infra error + Yahoo close=None → RuntimeError quote_unavailable (not 'not found')."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            raise RuntimeError("kis http 500")

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service, "fetch_fast_info", AsyncMock(return_value={"close": None})
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await tools["get_quote"]("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_equity_propagates_upstream_exception(monkeypatch):
    """KIS no-route + Yahoo transport 실패 → RuntimeError (원인 메시지 보존)."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=USSymbolNotRegisteredError("not registered")),
    )
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=RuntimeError("yahoo down")),
    )

    with pytest.raises(RuntimeError, match="yahoo down"):
        await tools["get_quote"]("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_flag_off_uses_yahoo(monkeypatch):
    """us_quote_kis_primary=False → KIS 경로 스킵, Yahoo primary."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "us_quote_kis_primary", False)
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=AssertionError("KIS path should be skipped")),
    )
    mock_fast_info = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "close": 205.0,
            "previous_close": 201.5,
            "open": 202.0,
            "high": 206.2,
            "low": 200.8,
            "volume": 123456789,
        }
    )
    monkeypatch.setattr(yahoo_service, "fetch_fast_info", mock_fast_info)

    result = await tools["get_quote"]("AAPL")

    assert result["source"] == "yahoo"
    assert result["price"] == pytest.approx(205.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_quotes_tools.py -k "us_" -v`
Expected: FAIL — `_patch_runtime_attr` raises `AttributeError: No runtime module exposes attribute 'get_us_exchange_by_symbol'` (아직 import 안 됨) / source 불일치.

- [ ] **Step 3: Add imports + module logger + promote helpers**

`app/mcp_server/tooling/market_data_quotes.py`:

(a) `import datetime`(:10) 옆에 `import logging` 추가.

(b) 기존 import 블록(`from app.services.brokers.kis.client import KISClient` :47 부근)에 추가:
```python
from app.core.symbol import to_db_symbol
from app.services.us_symbol_universe_service import (
    USSymbolInactiveError,
    USSymbolNotRegisteredError,
    USSymbolUniverseEmptyError,
    get_us_exchange_by_symbol,
)
```

(c) import 블록 종료 후(모듈 최상단 코드 시작 전) 모듈 로거 + promoted 헬퍼 추가:
```python
logger = logging.getLogger(__name__)


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Replace `_fetch_quote_equity_us` and add the KIS helper**

`_fetch_quote_equity_us`(:490-546) 전체를 아래로 **교체** (내부 중첩 `_to_float_or_none`/`_to_int_or_none`는 제거 — 모듈 헬퍼 사용):

```python
async def _fetch_us_quote_from_kis(normalized_symbol: str) -> dict[str, Any] | None:
    """KIS 해외 현재가(HHDFS00000300) primary arm.

    dict → 성공. None → Yahoo fallback 신호(KIS가 응답했으나 무가격, 또는
    거래소 미해석). KIS HTTP/transport 에러는 호출자가 infra로 처리하도록 전파.
    """
    try:
        exchange_code = await get_us_exchange_by_symbol(to_db_symbol(normalized_symbol))
    except (
        USSymbolNotRegisteredError,
        USSymbolInactiveError,
        USSymbolUniverseEmptyError,
    ):
        return None

    df = await KISClient().inquire_overseas_price(normalized_symbol, exchange_code)
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    price = _to_float_or_none(row.get("close"))
    if price is None or price <= 0:
        return None
    return {
        "symbol": normalized_symbol,
        "instrument_type": "equity_us",
        "price": price,
        "previous_close": _to_float_or_none(row.get("previous_close")),
        "open": None,
        "high": None,
        "low": None,
        "volume": _to_int_or_none(row.get("volume")),
        "source": "kis_overseas",
        "delayed": True,
    }


async def _fetch_quote_equity_us(symbol: str) -> dict[str, Any]:
    """Fetch US equity quote.

    ROB-471: KIS 해외 현재가 primary(settings.us_quote_kis_primary), Yahoo
    fast_info fallback. 정직 에러 분리:
      - 둘 다 정상응답·무가격 → symbol_not_found (ValueError)
      - 한쪽이라도 infra 실패 + 무가격 → quote_unavailable (RuntimeError)
    """
    normalized_symbol = str(symbol or "").strip().upper()
    not_found_message = f"Symbol '{normalized_symbol}' not found"
    unavailable_message = (
        f"US quote temporarily unavailable for '{normalized_symbol}'"
    )

    kis_infra_error = False
    if settings.us_quote_kis_primary:
        try:
            kis_quote = await _fetch_us_quote_from_kis(normalized_symbol)
        except Exception as exc:  # noqa: BLE001 — KIS infra 실패 시 Yahoo로 degrade
            kis_infra_error = True
            logger.warning(
                "KIS overseas quote failed for '%s'; falling back to Yahoo: %s",
                normalized_symbol,
                exc,
            )
        else:
            if kis_quote is not None:
                return kis_quote

    # FALLBACK: Yahoo fast_info
    try:
        fast_info = await yahoo_service.fetch_fast_info(normalized_symbol)
    except Exception as exc:
        raise RuntimeError(
            f"{unavailable_message} (yahoo fallback failed): {exc}"
        ) from exc

    price = _to_float_or_none(fast_info.get("close"))
    if price is None or price <= 0:
        if kis_infra_error:
            raise RuntimeError(
                f"{unavailable_message} (kis errored, yahoo returned no price)"
            )
        raise ValueError(not_found_message)

    return {
        "symbol": normalized_symbol,
        "instrument_type": "equity_us",
        "price": price,
        "previous_close": _to_float_or_none(fast_info.get("previous_close")),
        "open": _to_float_or_none(fast_info.get("open")),
        "high": _to_float_or_none(fast_info.get("high")),
        "low": _to_float_or_none(fast_info.get("low")),
        "volume": _to_int_or_none(fast_info.get("volume")),
        "source": "yahoo",
        "delayed": True,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_quotes_tools.py -v`
Expected: PASS (전체 파일 green — US 6개 포함)

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/market_data_quotes.py tests/test_mcp_quotes_tools.py
git commit -m "feat(ROB-471): _fetch_quote_equity_us KIS 해외 현재가 primary 전환

KIS-primary + Yahoo fallback (us_quote_kis_primary 게이트), DB 거래소 해석,
source=kis_overseas/delayed 정직 메타, quote_unavailable vs symbol_not_found 분리.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 교차 테스트 정합 + 회귀 검증

KIS-primary 디폴트 전환으로 US `get_quote`를 호출하는 기존 테스트가 실 DB(`get_us_exchange_by_symbol`)에 닿지 않도록 패치한다. 영향 테스트는 `tests/test_mcp_shared_utils.py`의 INVALID 테스트 1건뿐(나머지 US 호출지: `test_mcp_quotes_tools.py`는 Task 3에서 처리, `test_mcp_place_order.py`는 `_fetch_quote_equity_us` 자체를 패치하므로 무영향).

**Files:**
- Modify: `tests/test_mcp_shared_utils.py` (INVALID get_quote 테스트, :380-398 부근)

- [ ] **Step 1: Run the broader suite to see the fallout**

Run: `uv run pytest tests/test_mcp_shared_utils.py -k "get_quote or quote or INVALID" -v`
Expected: INVALID 테스트가 FAIL 또는 실 DB 접근(`get_us_exchange_by_symbol` 미패치). 정확한 테스트 이름을 확인.

- [ ] **Step 2: Patch the INVALID test to skip the KIS path cleanly**

해당 테스트 함수에서, `yahoo_service.fetch_fast_info`를 close=None으로 패치하는 줄 **직전**에 KIS no-route 패치를 추가. 파일 상단 import에 없으면 추가:
```python
from app.services.us_symbol_universe_service import USSymbolNotRegisteredError
```
(`_patch_runtime_attr`은 이 파일에서 이미 사용 중이면 그대로; 없으면 `from tests._mcp_tooling_support import _patch_runtime_attr` 확인 후 사용.)

테스트 본문에 추가:
```python
        _patch_runtime_attr(
            monkeypatch,
            "get_us_exchange_by_symbol",
            AsyncMock(side_effect=USSymbolNotRegisteredError("not registered")),
        )
```
(이로써 KIS 경로는 clean no-route → None → Yahoo fallback → close=None → `kis_infra_error=False` → 기존 기대대로 `ValueError "Symbol 'INVALID' not found"`.)

- [ ] **Step 3: Run the affected files + the 4-caller regression files**

Run:
```bash
uv run pytest tests/test_mcp_shared_utils.py tests/test_mcp_quotes_tools.py tests/test_services_kis_market_data.py tests/test_merged_portfolio_service.py tests/test_kis_tasks.py tests/test_mcp_place_order.py -q
```
Expected: 전부 PASS. (merged_portfolio/kis_tasks는 `inquire_overseas_price`를 모킹하므로 계약 정합 유지 — D4 라이트 회귀.)

- [ ] **Step 4: Sweep for any other US get_quote callers**

Run: `grep -rn 'get_quote' tests/ | grep -iE '"[A-Z]{1,5}"' | grep -ivE 'KRW|btc|005930|999999|12450|0117V0|0123G0|market="kr"'`
Expected: Task 3/4에서 다룬 호출지 외에 새로운 US 심볼 get_quote 호출이 없음. 있으면 동일 패턴(KIS 경로 패치)으로 처리.

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_shared_utils.py
git commit -m "test(ROB-471): KIS-primary 디폴트 하에서 INVALID get_quote 테스트 정합

US get_quote 경로가 실 DB 거래소 해석에 닿지 않도록 KIS no-route 패치.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 최종 검증 (lint + 전체 영향 테스트)

- [ ] **Step 1: Lint (ruff — app/ + tests/ 둘 다)**

Run: `uv run ruff check app/services/brokers/kis/ app/mcp_server/tooling/market_data_quotes.py app/core/config.py tests/test_mcp_quotes_tools.py tests/test_mcp_shared_utils.py tests/test_services_kis_market_data.py`
Expected: clean (또는 `uv run ruff check --fix` 후 재확인 — 미사용 import 제거 등). **CI lint는 app/와 tests/ 둘 다 본다.**

- [ ] **Step 2: Format**

Run: `uv run ruff format app/services/brokers/kis/overseas_market_data.py app/services/brokers/kis/client.py app/mcp_server/tooling/market_data_quotes.py app/core/config.py tests/test_mcp_quotes_tools.py tests/test_mcp_shared_utils.py tests/test_services_kis_market_data.py`
Expected: 변경 없음 또는 포맷 적용.

- [ ] **Step 3: Run the full quote/KIS/portfolio test surface**

Run:
```bash
uv run pytest tests/test_mcp_quotes_tools.py tests/test_mcp_shared_utils.py tests/test_services_kis_market_data.py tests/test_merged_portfolio_service.py tests/test_kis_tasks.py tests/test_mcp_place_order.py tests/test_invest_quote_service.py tests/test_mcp_portfolio_tools.py -q
```
Expected: 전부 PASS.

- [ ] **Step 4: Final commit (if format/lint changed anything)**

```bash
git add -A
git commit -m "chore(ROB-471): ruff lint/format 정리

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" || echo "nothing to commit"
```

---

## 비-goal / operator 게이트 (구현 범위 밖)
- 4개 호출지의 per-caller 거래소 라우팅 정교화(현재 default NASD) → follow-up.
- HHDFS76200200(현재가상세) 풀 OHLCV / `as_of` 타임스탬프 → follow-up.
- 실 HHDFS00000300 필드명·15분 지연·프리/애프터마켓 동작 라이브 검증 → operator. 롤백 레버: `US_QUOTE_KIS_PRIMARY=false`.
- 키움 US 현재가 → 별도 통합.

## Self-Review 결과
- **Spec coverage**: §4.1→T1, §4.2→T1, §4.3/§4.4/§4.5→T3, §4.6→T2, §6.1→T1, §6.2→T3, §6.3(라이트 회귀)→T4(모킹 호출지 green 확인), §교차정합→T4. 전 항목 커버.
- **Placeholder scan**: 모든 step에 실 코드/명령/기대출력 포함. 없음.
- **Type/name consistency**: `inquire_overseas_price(symbol, exchange_code="NASD")`, `_build_overseas_price_frame(out)`, `_fetch_us_quote_from_kis(normalized_symbol)`, `_to_float_or_none/_to_int_or_none`(모듈), `us_quote_kis_primary`, source 값 `"kis_overseas"`/`"yahoo"` — 태스크 간 일치.
