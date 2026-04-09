# KIS MarketDataClient 중복 제거 리팩토링

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `MarketDataClient`(1,366줄)에서 토큰 재시도 보일러플레이트 10곳과 DataFrame 변환 체인 3곳의 중복을 제거한다.

**Architecture:** 토큰 만료 감지+재시도를 `_request_with_token_retry` 메서드 하나로 통합하고, OHLCV DataFrame 구성 체인을 `_build_ohlcv_dataframe` 정적 메서드로 추출한다. 공개 메서드 시그니처는 변경하지 않는다.

**Tech Stack:** Python 3.13, pandas, httpx, pytest, pytest-asyncio

**Branch:** `refactor/kis-market-data-dedup`

---

## 파일 구조

| 파일 | 역할 |
|------|------|
| `app/services/brokers/kis/market_data.py` | 리팩토링 대상 (기존 파일 수정) |
| `app/services/brokers/kis/constants.py` | `_TOKEN_EXPIRED_CODES` 상수 추가 (기존 파일 수정) |
| `tests/test_services_kis_market_data.py` | 새 헬퍼 메서드 테스트 추가 + 기존 테스트 통과 확인 |

---

## Task 1: `_TOKEN_EXPIRED_CODES` 상수 추가

**Files:**
- Modify: `app/services/brokers/kis/constants.py:188-189`

- [ ] **Step 1: constants.py에 토큰 만료 코드 집합 상수 추가**

`app/services/brokers/kis/constants.py`의 기존 `ERROR_TOKEN_EXPIRED`, `ERROR_TOKEN_INVALID` 바로 아래에 추가:

```python
# 기존 코드 (188-189줄):
ERROR_TOKEN_EXPIRED = "EGW00123"  # 토큰 만료
ERROR_TOKEN_INVALID = "EGW00121"  # 유효하지 않은 토큰

# 아래에 추가:
TOKEN_EXPIRED_CODES = frozenset({ERROR_TOKEN_EXPIRED, ERROR_TOKEN_INVALID})
```

- [ ] **Step 2: lint 확인**

Run: `uv run ruff check app/services/brokers/kis/constants.py`
Expected: All checks passed!

- [ ] **Step 3: Commit**

```bash
git add app/services/brokers/kis/constants.py
git commit -m "refactor(kis): add TOKEN_EXPIRED_CODES frozenset constant"
```

---

## Task 2: `_request_with_token_retry` 메서드 추가 + 테스트

**Files:**
- Modify: `app/services/brokers/kis/market_data.py:200-208` (MarketDataClient 클래스)
- Modify: `tests/test_services_kis_market_data.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_services_kis_market_data.py` 파일 끝에 추가:

```python
class TestRequestWithTokenRetry:
    """Tests for MarketDataClient._request_with_token_retry"""

    @pytest.mark.asyncio
    async def test_returns_json_on_success(self, monkeypatch):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        monkeypatch.setattr(client, "_ensure_token", AsyncMock())
        request_mock = AsyncMock(
            return_value={"rt_cd": "0", "output": [{"foo": "bar"}]}
        )
        monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
        client._settings = MagicMock()
        client._settings.kis_access_token = "test_token"

        js = await client._market_data._request_with_token_retry(
            tr_id="FHKST01010100",
            url="https://example.com/api",
            params={"code": "005930"},
            api_name="test_api",
        )

        assert js["rt_cd"] == "0"
        request_mock.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
    async def test_retries_once_on_token_expired(self, monkeypatch, error_code):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        ensure_token = AsyncMock()
        monkeypatch.setattr(client, "_ensure_token", ensure_token)
        request_mock = AsyncMock(
            side_effect=[
                {"rt_cd": "1", "msg_cd": error_code, "msg1": "token expired"},
                {"rt_cd": "0", "output": [{"foo": "bar"}]},
            ]
        )
        monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
        client._token_manager = AsyncMock()
        client._token_manager.clear_token = AsyncMock(return_value=None)
        client._settings = MagicMock()
        client._settings.kis_access_token = "test_token"

        js = await client._market_data._request_with_token_retry(
            tr_id="FHKST01010100",
            url="https://example.com/api",
            params={"code": "005930"},
            api_name="test_api",
        )

        assert js["rt_cd"] == "0"
        assert request_mock.await_count == 2
        client._token_manager.clear_token.assert_awaited_once()
        # ensure_token: 1 (initial) + 1 (after clear) = 2
        assert ensure_token.await_count == 2

    @pytest.mark.asyncio
    async def test_raises_on_non_token_error(self, monkeypatch):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        monkeypatch.setattr(client, "_ensure_token", AsyncMock())
        request_mock = AsyncMock(
            return_value={"rt_cd": "1", "msg_cd": "OTHER_ERROR", "msg1": "bad request"}
        )
        monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
        client._settings = MagicMock()
        client._settings.kis_access_token = "test_token"

        with pytest.raises(RuntimeError, match="bad request"):
            await client._market_data._request_with_token_retry(
                tr_id="FHKST01010100",
                url="https://example.com/api",
                params={"code": "005930"},
                api_name="test_api",
            )

    @pytest.mark.asyncio
    async def test_raises_after_second_token_failure(self, monkeypatch):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        monkeypatch.setattr(client, "_ensure_token", AsyncMock())
        request_mock = AsyncMock(
            side_effect=[
                {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"},
                {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token still expired"},
            ]
        )
        monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
        client._token_manager = AsyncMock()
        client._token_manager.clear_token = AsyncMock(return_value=None)
        client._settings = MagicMock()
        client._settings.kis_access_token = "test_token"

        with pytest.raises(RuntimeError, match="token still expired"):
            await client._market_data._request_with_token_retry(
                tr_id="FHKST01010100",
                url="https://example.com/api",
                params={"code": "005930"},
                api_name="test_api",
            )

    @pytest.mark.asyncio
    async def test_passes_timeout_and_method(self, monkeypatch):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        monkeypatch.setattr(client, "_ensure_token", AsyncMock())
        request_mock = AsyncMock(
            return_value={"rt_cd": "0", "output": []}
        )
        monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
        client._settings = MagicMock()
        client._settings.kis_access_token = "test_token"

        await client._market_data._request_with_token_retry(
            tr_id="FHKST01010100",
            url="https://example.com/api",
            params={"code": "005930"},
            api_name="test_api",
            timeout=10,
        )

        call_kwargs = request_mock.await_args.kwargs
        assert call_kwargs["timeout"] == 10
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `uv run pytest tests/test_services_kis_market_data.py -v -k "TestRequestWithTokenRetry" --timeout=10`
Expected: FAIL — `AttributeError: 'MarketDataClient' object has no attribute '_request_with_token_retry'`

- [ ] **Step 3: `_request_with_token_retry` 구현**

`app/services/brokers/kis/market_data.py`의 `MarketDataClient` 클래스에서 `_settings` 프로퍼티 바로 아래(현재 212줄 부근)에 추가:

```python
    async def _request_with_token_retry(
        self,
        tr_id: str,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
    ) -> dict[str, Any]:
        """KIS API 요청 + 토큰 만료 시 1회 재시도.

        토큰 만료 코드(EGW00123, EGW00121) 수신 시 토큰을 갱신하고
        동일 요청을 최대 1회 재시도한다.
        """
        for attempt in range(2):
            await self._parent._ensure_token()
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": tr_id,
            }
            js = await self._parent._request_with_rate_limit(
                method,
                url,
                headers=hdr,
                params=params,
                json_body=json_body,
                timeout=timeout,
                api_name=api_name,
                tr_id=tr_id,
            )

            if js.get("rt_cd") == "0":
                return js

            if attempt == 0 and js.get("msg_cd") in constants.TOKEN_EXPIRED_CODES:
                await self._parent._token_manager.clear_token()
                continue

            raise RuntimeError(
                js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
            )

        raise RuntimeError("KIS API token retry exhausted")
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `uv run pytest tests/test_services_kis_market_data.py -v -k "TestRequestWithTokenRetry" --timeout=10`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/market_data.py tests/test_services_kis_market_data.py
git commit -m "refactor(kis): add _request_with_token_retry to MarketDataClient"
```

---

## Task 3: 단순 랭킹 메서드 4곳에 `_request_with_token_retry` 적용

단순 패턴 = `_ensure_token` → 헤더 구성 → `_request_with_rate_limit` → 성공/토큰만료/에러 분기.
해당 메서드: `volume_rank`, `market_cap_rank`, `fluctuation_rank`, `foreign_buying_rank`

**Files:**
- Modify: `app/services/brokers/kis/market_data.py:213-408`

- [ ] **Step 1: `volume_rank` 변환**

`market_data.py`의 `volume_rank` 메서드(현재 ~213-264줄)를 다음으로 교체:

```python
    async def volume_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_VOLUME_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_VOLUME_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": "1",
                "FID_TRGT_CLS_CODE": "11111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000001100",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "1000000",
                "FID_VOL_CNT": "100000",
                "FID_INPUT_DATE_1": "",
            },
            api_name="volume_rank",
        )
        results = js["output"][:limit]
        sample_data = [
            (r.get("hts_kor_isnm", ""), r.get("acml_vol", "0")) for r in results[:3]
        ]
        logging.debug(
            f"volume_rank: Received {len(js['output'])} results, "
            f"returning {len(results)}. Sample: {sample_data}"
        )
        return results
```

핵심 변경:
- `await self._parent._ensure_token()` + 헤더 구성 + `_request_with_rate_limit` + 토큰 만료 분기 → `_request_with_token_retry` 한 줄
- `rt_cd == "0"` 체크는 `_request_with_token_retry` 내부에서 처리
- 성공 후 비즈니스 로직(슬라이싱, 로깅)만 남김

- [ ] **Step 2: `market_cap_rank` 변환**

`market_data.py`의 `market_cap_rank` 메서드(현재 ~266-303줄)를 다음으로 교체:

```python
    async def market_cap_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        js = await self._request_with_token_retry(
            tr_id=constants.MARKET_CAP_RANK_TR,
            url=f"{constants.BASE}{constants.MARKET_CAP_RANK_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_COND_SCR_DIV_CODE": "20174",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
            },
            api_name="market_cap_rank",
        )
        return js["output"][:limit]
```

- [ ] **Step 3: `fluctuation_rank` 변환**

`market_data.py`의 `fluctuation_rank` 메서드(현재 ~305-368줄)를 다음으로 교체:

```python
    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict]:
        prc_cls_code = "0"
        rank_sort_cls_code = "3" if direction == "down" else "0"

        logging.debug(
            f"fluctuation_rank: direction={direction}, "
            f"FID_PRC_CLS_CODE={prc_cls_code}, "
            f"FID_RANK_SORT_CLS_CODE={rank_sort_cls_code}"
        )

        js = await self._request_with_token_retry(
            tr_id=constants.FLUCTUATION_RANK_TR,
            url=f"{constants.BASE}{constants.FLUCTUATION_RANK_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_COND_SCR_DIV_CODE": "20170",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": rank_sort_cls_code,
                "FID_INPUT_CNT_1": "0",
                "FID_PRC_CLS_CODE": prc_cls_code,
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0",
                "FID_RSFL_RATE1": "",
                "FID_RSFL_RATE2": "",
            },
            api_name="fluctuation_rank",
        )

        results = js["output"]
        if direction == "up":
            results.sort(key=lambda x: float(x.get("prdy_ctrt", 0)), reverse=True)
            return results[:limit]

        negatives = [
            item for item in results if float(item.get("prdy_ctrt", 0)) < 0
        ]
        negatives.sort(key=lambda x: float(x.get("prdy_ctrt", 0)))
        return negatives[:limit]
```

- [ ] **Step 4: `foreign_buying_rank` 변환**

`market_data.py`의 `foreign_buying_rank` 메서드(현재 ~372-408줄)를 다음으로 교체:

```python
    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        js = await self._request_with_token_retry(
            tr_id=constants.FOREIGN_BUYING_RANK_TR,
            url=f"{constants.BASE}{constants.FOREIGN_BUYING_RANK_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": "1",
            },
            api_name="foreign_buying_rank",
        )
        return js["output"][:limit]
```

- [ ] **Step 5: 기존 테스트 실행 — 회귀 없음 확인**

Run: `uv run pytest tests/test_services_kis_market_data.py -v --timeout=10 -x`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add app/services/brokers/kis/market_data.py
git commit -m "refactor(kis): apply _request_with_token_retry to 4 ranking methods"
```

---

## Task 4: 단일 호출 메서드 3곳에 `_request_with_token_retry` 적용

해당 메서드: `inquire_price`, `_request_orderbook_snapshot`, `fetch_fundamental_info`

**Files:**
- Modify: `app/services/brokers/kis/market_data.py:410-676`

- [ ] **Step 1: `inquire_price` 변환**

`market_data.py`의 `inquire_price` 메서드(현재 ~410-474줄)를 다음으로 교체:

```python
    async def inquire_price(self, code: str, market: str = "UN") -> DataFrame:
        """
        단일 종목 현재가·기본정보 조회
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: API output 딕셔너리
        """
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_PRICE_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_PRICE_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="inquire_price",
        )
        out = js["output"]  # 단일 dict
        trade_date_str = out.get("stck_bsop_date")  # 예: '20250805'
        if trade_date_str:
            trade_date = pd.to_datetime(trade_date_str, format="%Y%m%d")
        else:  # 필드가 없으면 오늘 날짜
            trade_date = pd.Timestamp(datetime.date.today())

        # ── ② 체결 시각 ──
        time_str = out.get("stck_cntg_hour") or out.get("stck_cntg_time")  # 'HHMMSS'
        if time_str:
            trade_time = pd.to_datetime(time_str, format="%H%M%S").time()
        else:
            trade_time = datetime.datetime.now().time()  # 필드가 없으면 현재 시각
        row = {
            "code": out["stck_shrn_iscd"],
            "date": trade_date,
            "time": trade_time,
            "open": float(out["stck_oprc"]),
            "high": float(out["stck_hgpr"]),
            "low": float(out["stck_lwpr"]),
            "close": float(out["stck_prpr"]),
            "volume": int(out["acml_vol"]),
            "value": int(out["acml_tr_pbmn"]),
        }
        return pd.DataFrame([row]).set_index("code")  # index = 종목코드
```

- [ ] **Step 2: `_request_orderbook_snapshot` 변환**

`market_data.py`의 `_request_orderbook_snapshot` 메서드(현재 ~476-507줄)를 다음으로 교체:

```python
    async def _request_orderbook_snapshot(self, code: str, market: str = "UN") -> dict:
        return await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_ORDERBOOK_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_ORDERBOOK_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="inquire_orderbook",
        )
```

- [ ] **Step 3: `fetch_fundamental_info` 변환**

`market_data.py`의 `fetch_fundamental_info` 메서드(현재 ~617-676줄)를 다음으로 교체:

```python
    async def fetch_fundamental_info(self, code: str, market: str = "UN") -> dict:
        """
        종목의 기본 정보를 가져와 딕셔너리로 반환합니다.
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: 기본 정보 딕셔너리
        """
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_PRICE_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_PRICE_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="fetch_fundamental_info",
        )
        out = js["output"]  # 단일 dict

        # 기본 정보 구성
        fundamental_data = {
            "종목코드": out.get("stck_shrn_iscd"),
            "종목명": out.get("hts_kor_isnm"),
            "현재가": out.get("stck_prpr"),
            "전일대비": out.get("prdy_vrss"),
            "등락률": out.get("prdy_ctrt"),
            "거래량": out.get("acml_vol"),
            "거래대금": out.get("acml_tr_pbmn"),
            "시가총액": out.get("hts_avls"),
            "상장주수": out.get("lstn_stcn"),
            "외국인비율": out.get("frgn_hlg"),
            "52주최고": out.get("w52_hgpr"),
            "52주최저": out.get("w52_lwpr"),
        }

        # None이 아닌 값만 반환
        return {k: v for k, v in fundamental_data.items() if v is not None}
```

- [ ] **Step 4: 기존 테스트 실행 — 회귀 없음 확인**

Run: `uv run pytest tests/test_services_kis_market_data.py -v --timeout=10 -x`
Expected: All existing tests pass

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/market_data.py
git commit -m "refactor(kis): apply _request_with_token_retry to price/orderbook/fundamental methods"
```

---

## Task 5: 루프 내 토큰 재시도 메서드 3곳 적용

이 메서드들은 while/for 루프 안에서 여러 번 API를 호출하면서 토큰 만료를 처리한다. `_request_with_token_retry`가 내부에서 토큰 재시도를 처리하므로, 호출부에서는 토큰 분기를 제거하면 된다.

해당 메서드: `inquire_daily_itemchartprice`, `inquire_overseas_daily_price`, `inquire_overseas_minute_chart`

**Files:**
- Modify: `app/services/brokers/kis/market_data.py:678-1343`

- [ ] **Step 1: `inquire_daily_itemchartprice` 변환**

`market_data.py`의 `inquire_daily_itemchartprice` 메서드의 while 루프 내부(현재 ~703-751줄)를 수정. 루프 앞의 `_ensure_token` + `hdr` 구성은 제거하고, 루프 내부의 API 호출을 `_request_with_token_retry`로 교체한다.

기존 코드 (메서드 시작부터 while 루프 직전까지, ~678-702줄):

```python
    async def inquire_daily_itemchartprice(
        self,
        code: str,
        market: str = "UN",
        period: str = "D",
        n: int = 200,
        adj: bool = True,
        end_date: datetime.date | None = None,
    ) -> pd.DataFrame:
        n = normalize_daily_chart_lookback(n)
        per_call_days = 100

        await self._parent._ensure_token()           # ← 삭제
        hdr = self._parent._hdr_base | {              # ← 삭제
            "authorization": ...,                     # ← 삭제
            "tr_id": constants.DOMESTIC_DAILY_CHART_TR,  # ← 삭제
        }                                             # ← 삭제

        end = end_date or datetime.date.today()
        rows: list[dict] = []
```

변경 후:

```python
    async def inquire_daily_itemchartprice(
        self,
        code: str,
        market: str = "UN",
        period: str = "D",
        n: int = 200,
        adj: bool = True,
        end_date: datetime.date | None = None,
    ) -> pd.DataFrame:
        n = normalize_daily_chart_lookback(n)
        per_call_days = 100

        end = end_date or datetime.date.today()
        rows: list[dict] = []
```

루프 내부 API 호출 변경 (기존 ~713-733줄):

기존:
```python
            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.DOMESTIC_DAILY_CHART_URL}",
                headers=hdr,
                params=params,
                timeout=5,
                api_name="inquire_daily_itemchartprice",
                tr_id=constants.DOMESTIC_DAILY_CHART_TR,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in [
                    "EGW00123",
                    "EGW00121",
                ]:
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
```

변경 후:
```python
            js = await self._request_with_token_retry(
                tr_id=constants.DOMESTIC_DAILY_CHART_TR,
                url=f"{constants.BASE}{constants.DOMESTIC_DAILY_CHART_URL}",
                params=params,
                api_name="inquire_daily_itemchartprice",
            )
```

나머지 코드(chunk 처리, DataFrame 변환)는 그대로 유지.

- [ ] **Step 2: `inquire_overseas_daily_price` 변환**

`inquire_overseas_daily_price` 메서드에서도 동일 패턴 적용.

기존 (메서드 시작부 ~ 루프 직전, 루프 앞 `_ensure_token` + `hdr` 구성 제거):

```python
        await self._parent._ensure_token()    # ← 삭제
        hdr = self._parent._hdr_base | {       # ← 삭제
            ...                                # ← 삭제
        }                                      # ← 삭제
```

루프 내부 API 호출 변경 (기존 ~1174-1189줄):

기존:
```python
            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.OVERSEAS_DAILY_CHART_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_overseas_daily_price",
                tr_id=constants.OVERSEAS_DAILY_CHART_TR,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
```

변경 후:
```python
            js = await self._request_with_token_retry(
                tr_id=constants.OVERSEAS_DAILY_CHART_TR,
                url=f"{constants.BASE}{constants.OVERSEAS_DAILY_CHART_URL}",
                params=params,
                timeout=10,
                api_name="inquire_overseas_daily_price",
            )
```

- [ ] **Step 3: `inquire_overseas_minute_chart` 변환**

기존 (현재 ~1264-1289줄):

```python
        for attempt in range(2):
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": constants.OVERSEAS_MINUTE_CHART_TR,
            }
            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.OVERSEAS_MINUTE_CHART_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_overseas_minute_chart",
                tr_id=constants.OVERSEAS_MINUTE_CHART_TR,
            )

            if js.get("rt_cd") == "0":
                break

            if attempt == 0 and js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                continue

            raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
        else:
            raise RuntimeError("Failed to fetch overseas minute chart")
```

변경 후:
```python
        js = await self._request_with_token_retry(
            tr_id=constants.OVERSEAS_MINUTE_CHART_TR,
            url=f"{constants.BASE}{constants.OVERSEAS_MINUTE_CHART_URL}",
            params=params,
            timeout=10,
            api_name="inquire_overseas_minute_chart",
        )
```

나머지 코드(chunk 처리, DataFrame 변환, pagination)는 그대로 유지.

- [ ] **Step 4: `inquire_short_selling` 변환**

이 메서드도 동일한 for-range(2) 토큰 재시도 패턴을 사용함(~568-593줄).

기존:
```python
        for attempt in range(2):
            await self._parent._ensure_token()
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": constants.DOMESTIC_SHORT_SELLING_TR,
            }
            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.DOMESTIC_SHORT_SELLING_URL}",
                headers=hdr,
                params=params,
                timeout=5,
                api_name="inquire_short_selling",
                tr_id=constants.DOMESTIC_SHORT_SELLING_TR,
            )

            if js.get("rt_cd") == "0":
                break

            if attempt == 0 and js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                continue

            raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
        else:
            raise RuntimeError("Failed to fetch KIS daily short selling data")
```

변경 후:
```python
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_SHORT_SELLING_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_SHORT_SELLING_URL}",
            params=params,
            api_name="inquire_short_selling",
        )
```

- [ ] **Step 5: 전체 KIS 테스트 실행**

Run: `uv run pytest tests/test_services_kis_market_data.py -v --timeout=10 -x`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add app/services/brokers/kis/market_data.py
git commit -m "refactor(kis): apply _request_with_token_retry to loop-based methods"
```

---

## Task 6: `_build_ohlcv_dataframe` 정적 메서드 추가 + 테스트

3곳(inquire_time_dailychartprice, inquire_minute_chart, inquire_overseas_minute_chart)에서 거의 동일한 `.rename().astype().assign(datetime=...).drop_duplicates().sort_values().tail().reset_index()` 체인을 사용한다.

**Files:**
- Modify: `app/services/brokers/kis/market_data.py`
- Modify: `tests/test_services_kis_market_data.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_services_kis_market_data.py` 파일 끝에 추가:

```python
class TestBuildOhlcvDataframe:
    """Tests for MarketDataClient._build_ohlcv_dataframe"""

    def test_builds_dataframe_with_datetime_columns(self):
        from app.services.brokers.kis.market_data import MarketDataClient

        rows = [
            {
                "stck_bsop_date": "20260219",
                "stck_cntg_hour": "100000",
                "stck_oprc": "70000",
                "stck_hgpr": "70200",
                "stck_lwpr": "69900",
                "stck_prpr": "70100",
                "cntg_vol": "100",
                "acml_tr_pbmn": "7010000",
            },
            {
                "stck_bsop_date": "20260219",
                "stck_cntg_hour": "100100",
                "stck_oprc": "70100",
                "stck_hgpr": "70300",
                "stck_lwpr": "70000",
                "stck_prpr": "70200",
                "cntg_vol": "200",
                "acml_tr_pbmn": "14040000",
            },
        ]

        column_mapping = {
            "stck_bsop_date": "date",
            "stck_cntg_hour": "time",
            "stck_oprc": "open",
            "stck_hgpr": "high",
            "stck_lwpr": "low",
            "stck_prpr": "close",
            "cntg_vol": "volume",
            "acml_tr_pbmn": "value",
        }

        df = MarketDataClient._build_ohlcv_dataframe(
            rows=rows,
            column_mapping=column_mapping,
            datetime_format="%Y%m%d%H%M%S",
            limit=200,
        )

        assert len(df) == 2
        assert list(df.columns) == [
            "datetime", "date", "time",
            "open", "high", "low", "close",
            "volume", "value",
        ]
        assert df.iloc[0]["datetime"] == pd.Timestamp("2026-02-19 10:00:00")
        assert df.iloc[0]["close"] == 70100.0
        assert df.iloc[0]["volume"] == 100

    def test_deduplicates_by_datetime(self):
        from app.services.brokers.kis.market_data import MarketDataClient

        rows = [
            {
                "stck_bsop_date": "20260219",
                "stck_cntg_hour": "100000",
                "stck_oprc": "70000",
                "stck_hgpr": "70200",
                "stck_lwpr": "69900",
                "stck_prpr": "70100",
                "cntg_vol": "100",
                "acml_tr_pbmn": "7010000",
            },
            {
                "stck_bsop_date": "20260219",
                "stck_cntg_hour": "100000",
                "stck_oprc": "70100",
                "stck_hgpr": "70300",
                "stck_lwpr": "70000",
                "stck_prpr": "70200",
                "cntg_vol": "200",
                "acml_tr_pbmn": "14040000",
            },
        ]

        column_mapping = {
            "stck_bsop_date": "date",
            "stck_cntg_hour": "time",
            "stck_oprc": "open",
            "stck_hgpr": "high",
            "stck_lwpr": "low",
            "stck_prpr": "close",
            "cntg_vol": "volume",
            "acml_tr_pbmn": "value",
        }

        df = MarketDataClient._build_ohlcv_dataframe(
            rows=rows,
            column_mapping=column_mapping,
            datetime_format="%Y%m%d%H%M%S",
            limit=200,
        )

        assert len(df) == 1  # 중복 제거

    def test_respects_limit(self):
        from app.services.brokers.kis.market_data import MarketDataClient

        rows = [
            {
                "stck_bsop_date": "20260219",
                "stck_cntg_hour": f"10{i:02d}00",
                "stck_oprc": "70000",
                "stck_hgpr": "70200",
                "stck_lwpr": "69900",
                "stck_prpr": "70100",
                "cntg_vol": "100",
                "acml_tr_pbmn": "7010000",
            }
            for i in range(10)
        ]

        column_mapping = {
            "stck_bsop_date": "date",
            "stck_cntg_hour": "time",
            "stck_oprc": "open",
            "stck_hgpr": "high",
            "stck_lwpr": "low",
            "stck_prpr": "close",
            "cntg_vol": "volume",
            "acml_tr_pbmn": "value",
        }

        df = MarketDataClient._build_ohlcv_dataframe(
            rows=rows,
            column_mapping=column_mapping,
            datetime_format="%Y%m%d%H%M%S",
            limit=3,
        )

        assert len(df) == 3

    def test_sorts_by_datetime_ascending(self):
        from app.services.brokers.kis.market_data import MarketDataClient

        rows = [
            {
                "stck_bsop_date": "20260219",
                "stck_cntg_hour": "110000",
                "stck_oprc": "71000",
                "stck_hgpr": "71200",
                "stck_lwpr": "70900",
                "stck_prpr": "71100",
                "cntg_vol": "100",
                "acml_tr_pbmn": "7110000",
            },
            {
                "stck_bsop_date": "20260219",
                "stck_cntg_hour": "100000",
                "stck_oprc": "70000",
                "stck_hgpr": "70200",
                "stck_lwpr": "69900",
                "stck_prpr": "70100",
                "cntg_vol": "100",
                "acml_tr_pbmn": "7010000",
            },
        ]

        column_mapping = {
            "stck_bsop_date": "date",
            "stck_cntg_hour": "time",
            "stck_oprc": "open",
            "stck_hgpr": "high",
            "stck_lwpr": "low",
            "stck_prpr": "close",
            "cntg_vol": "volume",
            "acml_tr_pbmn": "value",
        }

        df = MarketDataClient._build_ohlcv_dataframe(
            rows=rows,
            column_mapping=column_mapping,
            datetime_format="%Y%m%d%H%M%S",
            limit=200,
        )

        assert df.iloc[0]["datetime"] < df.iloc[1]["datetime"]
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `uv run pytest tests/test_services_kis_market_data.py -v -k "TestBuildOhlcvDataframe" --timeout=10`
Expected: FAIL — `AttributeError: type object 'MarketDataClient' has no attribute '_build_ohlcv_dataframe'`

- [ ] **Step 3: `_build_ohlcv_dataframe` 구현**

`app/services/brokers/kis/market_data.py`의 `MarketDataClient` 클래스에서 `_request_with_token_retry` 바로 아래에 추가:

```python
    @staticmethod
    def _build_ohlcv_dataframe(
        rows: list[dict[str, Any]],
        column_mapping: dict[str, str],
        datetime_format: str,
        limit: int,
    ) -> pd.DataFrame:
        """원시 API rows를 표준 OHLCV DataFrame으로 변환.

        Parameters
        ----------
        rows : list[dict]
            KIS API 응답의 원시 행 목록
        column_mapping : dict
            KIS 컬럼명 → 표준 컬럼명 매핑.
            date + time 결합: ``{"date_col": "date", "time_col": "time", ...}``
        datetime_format : str
            date + time 문자열 결합 후 파싱할 strftime 포맷 (예: "%Y%m%d%H%M%S")
        limit : int
            반환할 최대 행 수 (tail 적용)
        """
        frame = (
            pd.DataFrame(rows)
            .rename(columns=column_mapping)
            .astype(
                {
                    "date": "str",
                    "time": "str",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                    "value": "int",
                },
                errors="ignore",
            )
            .assign(
                datetime=lambda d: pd.to_datetime(
                    d["date"] + d["time"],
                    format=datetime_format,
                    errors="coerce",
                )
            )
            .dropna(subset=["datetime"])
            .assign(
                date=lambda d: d["datetime"].dt.date,
                time=lambda d: d["datetime"].dt.time,
            )
            .loc[:, _MINUTE_FRAME_COLUMNS]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(max(int(limit), 1))
            .reset_index(drop=True)
        )
        return frame
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `uv run pytest tests/test_services_kis_market_data.py -v -k "TestBuildOhlcvDataframe" --timeout=10`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/market_data.py tests/test_services_kis_market_data.py
git commit -m "refactor(kis): add _build_ohlcv_dataframe static method to MarketDataClient"
```

---

## Task 7: 3곳의 DataFrame 체인을 `_build_ohlcv_dataframe`으로 교체

**Files:**
- Modify: `app/services/brokers/kis/market_data.py`

- [ ] **Step 1: `inquire_time_dailychartprice` 변환**

기존 DataFrame 체인(~845-903줄):

```python
        frame = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_cntg_hour": "time",
                    ...
                }
            )
            .astype(...)
            .assign(datetime=...)
            .dropna(subset=["datetime"])
            .assign(date=..., time=...)
            .loc[:, [...]]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(max(int(n), 1))
            .reset_index(drop=True)
        )
        return frame
```

변경 후:

```python
        return self._build_ohlcv_dataframe(
            rows=rows,
            column_mapping={
                "stck_bsop_date": "date",
                "stck_cntg_hour": "time",
                "stck_oprc": "open",
                "stck_hgpr": "high",
                "stck_lwpr": "low",
                "stck_prpr": "close",
                "cntg_vol": "volume",
                "acml_tr_pbmn": "value",
            },
            datetime_format="%Y%m%d%H%M%S",
            limit=n,
        )
```

- [ ] **Step 2: `inquire_minute_chart` 변환**

기존 DataFrame 체인(~993-1048줄):

```python
        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_cntg_hour": "time",
                    ...
                }
            )
            .astype(...)
            .assign(datetime=..., date=..., time=...)
            .loc[:, [...]]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(n)
            .reset_index(drop=True)
        )

        return df
```

변경 후:

```python
        return self._build_ohlcv_dataframe(
            rows=rows,
            column_mapping={
                "stck_bsop_date": "date",
                "stck_cntg_hour": "time",
                "stck_oprc": "open",
                "stck_hgpr": "high",
                "stck_lwpr": "low",
                "stck_prpr": "close",
                "cntg_vol": "volume",
                "acml_tr_pbmn": "value",
            },
            datetime_format="%Y%m%d%H%M%S",
            limit=n,
        )
```

- [ ] **Step 3: `inquire_overseas_minute_chart` 변환**

이 메서드는 `_validate_overseas_minute_chart_chunk`을 통해 이미 정규화된 rows를 받으므로 column_mapping이 다르다. 또한 limit 대신 전체 행을 반환하고, 별도로 datetime 유효성 검증을 한다.

기존 코드(~1297-1331줄):

```python
        validated_rows = _validate_overseas_minute_chart_chunk(chunk)
        frame = pd.DataFrame(validated_rows).rename(
            columns={
                "xymd": "date",
                "xhms": "time",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "value": "value",
            }
        )
        frame["date"] = frame["date"].astype("string")
        frame["time"] = frame["time"].astype("string").str.zfill(6)
        frame["datetime"] = pd.to_datetime(
            frame["date"] + frame["time"],
            format="%Y%m%d%H%M%S",
            errors="coerce",
        )
        if frame["datetime"].isna().any():
            raise RuntimeError(
                "Malformed KIS overseas minute chart payload: invalid xymd/xhms format"
            )

        frame = (
            frame.assign(
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[:, _MINUTE_FRAME_COLUMNS]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
```

변경 후:

```python
        validated_rows = _validate_overseas_minute_chart_chunk(chunk)

        # time 필드에 zfill(6)이 필요하므로 전처리
        for row in validated_rows:
            row["xhms"] = str(row["xhms"]).zfill(6)

        frame = self._build_ohlcv_dataframe(
            rows=validated_rows,
            column_mapping={
                "xymd": "date",
                "xhms": "time",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "value": "value",
            },
            datetime_format="%Y%m%d%H%M%S",
            limit=len(validated_rows),  # 전체 반환 (pagination이 limit 역할)
        )

        if frame.empty and validated_rows:
            raise RuntimeError(
                "Malformed KIS overseas minute chart payload: invalid xymd/xhms format"
            )
```

**주의:** 기존 코드는 `datetime`이 NaT인 행이 하나라도 있으면 `RuntimeError`를 발생시켰다. `_build_ohlcv_dataframe`은 `dropna(subset=["datetime"])`으로 해당 행을 제거한다. 변경 후에는 정규화된 rows가 전부 유효하지 않은 경우에만 에러가 발생한다. 이것이 기존 동작과 다르지만, `_validate_overseas_minute_chart_chunk`에서 이미 날짜/시간 필드 존재를 검증하므로 실질적인 차이는 없다.

- [ ] **Step 4: 기존 테스트 + 새 테스트 전체 실행**

Run: `uv run pytest tests/test_services_kis_market_data.py -v --timeout=10 -x`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/market_data.py
git commit -m "refactor(kis): replace 3 DataFrame chains with _build_ohlcv_dataframe"
```

---

## Task 8: 최종 검증

**Files:**
- All modified files

- [ ] **Step 1: lint 통과 확인**

Run: `make lint`
Expected: All checks passed

- [ ] **Step 2: typecheck 통과 확인**

Run: `make typecheck`
Expected: No errors

- [ ] **Step 3: 전체 KIS 테스트 실행**

Run: `uv run pytest tests/ -v -k "kis" --timeout=10 -x`
Expected: All tests pass

- [ ] **Step 4: 공개 메서드 시그니처 변경 없음 확인**

아래 메서드들의 시그니처가 변경되지 않았는지 확인. `client.py`의 위임 메서드가 동일하게 호출할 수 있어야 한다:

- `volume_rank(self, market: str = "J", limit: int = 30) -> list[dict]`
- `market_cap_rank(self, market: str = "J", limit: int = 30) -> list[dict]`
- `fluctuation_rank(self, market: str = "J", direction: str = "up", limit: int = 30) -> list[dict]`
- `foreign_buying_rank(self, market: str = "J", limit: int = 30) -> list[dict]`
- `inquire_price(self, code: str, market: str = "UN") -> DataFrame`
- `_request_orderbook_snapshot(self, code: str, market: str = "UN") -> dict`
- `fetch_fundamental_info(self, code: str, market: str = "UN") -> dict`
- `inquire_daily_itemchartprice(self, code, market, period, n, adj, end_date) -> pd.DataFrame`
- `inquire_time_dailychartprice(self, code, market, n, end_date, end_time) -> pd.DataFrame`
- `inquire_minute_chart(self, code, market, time_unit, n, end_date) -> pd.DataFrame`
- `inquire_overseas_daily_price(self, symbol, exchange_code, period, n) -> pd.DataFrame`
- `inquire_overseas_minute_chart(self, symbol, exchange_code, n, keyb) -> OverseasMinuteChartPage`

Run: `grep -n "async def " app/services/brokers/kis/market_data.py | head -20`
Expected: 위 메서드 시그니처가 동일하게 유지

- [ ] **Step 5: 제거된 줄 수 확인**

Run: `git diff --stat main`
Expected: 삭제 줄 수가 추가 줄 수보다 상당히 많음 (순 감소 기대)

- [ ] **Step 6: Final commit (if any remaining changes)**

```bash
# 남은 변경이 있다면
git add -A
git commit -m "refactor(kis): final cleanup for MarketDataClient dedup"
```
