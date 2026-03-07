import asyncio
import datetime
import logging
import random
from typing import Any, cast

import httpx
import pandas as pd
from pandas import DataFrame

from app.core.async_rate_limiter import RateLimitExceededError, get_limiter
from app.core.config import settings
from app.core.symbol import to_kis_symbol
from app.services.brokers.kis.holdings_api import HoldingsAPI
from app.services.brokers.kis.market_data_api import MarketDataAPI
from app.services.brokers.kis.orders_api import OrdersAPI
from app.services.brokers.kis.transport import KISTransport
from app.services.redis_token_manager import redis_token_manager

BASE = "https://openapi.koreainvestment.com:9443"
VOL_URL = "/uapi/domestic-stock/v1/quotations/volume-rank"
PRICE_TR = "FHKST01010100"
PRICE_URL = "/uapi/domestic-stock/v1/quotations/inquire-price"
DAILY_ITEMCHARTPRICE_URL = (
    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
)
VOL_TR = "FHPST01710000"  # 실전 전용
DAILY_ITEMCHARTPRICE_TR = "FHKST03010100"  # (일봉·주식·실전/모의 공통)

MARKET_CAP_RANK_URL = "/uapi/domestic-stock/v1/ranking/market-cap"
MARKET_CAP_RANK_TR = "FHPST01740000"
FLUCTUATION_RANK_URL = "/uapi/domestic-stock/v1/ranking/fluctuation"
FLUCTUATION_RANK_TR = "FHPST01700000"
FOREIGN_BUYING_RANK_URL = "/uapi/domestic-stock/v1/quotations/foreign-institution-total"
FOREIGN_BUYING_RANK_TR = "FHPTJ04400000"

# 호가 조회 관련 URL 및 TR ID
ORDERBOOK_URL = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
ORDERBOOK_TR = "FHKST01010200"  # 주식현재가호가상체결

# 분봉 데이터 관련 URL 및 TR ID 추가
MINUTE_CHART_URL = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
MINUTE_CHART_TR = "FHKST03010200"  # 분봉 조회 TR ID
TIME_DAILY_CHART_URL = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
TIME_DAILY_CHART_TR = "FHKST03010230"  # 당일분봉 조회 TR ID

# 주식잔고 조회 관련 URL 및 TR ID 추가
BALANCE_URL = "/uapi/domestic-stock/v1/trading/inquire-balance"
BALANCE_TR = "TTTC8434R"  # 실전투자 주식잔고조회
BALANCE_TR_MOCK = "VTTC8434R"  # 모의투자 주식잔고조회

# 해외주식 잔고조회 관련 URL 및 TR ID 추가
OVERSEAS_BALANCE_URL = "/uapi/overseas-stock/v1/trading/inquire-balance"
OVERSEAS_BALANCE_TR = "TTTS3012R"  # 실전투자 해외주식 잔고조회
OVERSEAS_BALANCE_TR_MOCK = "VTTS3012R"  # 모의투자 해외주식 잔고조회

# 해외주식 일봉/분봉 조회 관련 URL 및 TR ID
OVERSEAS_DAILY_CHART_URL = "/uapi/overseas-price/v1/quotations/dailyprice"
OVERSEAS_DAILY_CHART_TR = "HHDFS76240000"  # 해외주식 기간별시세 (v1_해외주식-010)
OVERSEAS_PERIOD_CHART_URL = (
    "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice"
)
OVERSEAS_PERIOD_CHART_TR = (
    "FHKST03030100"  # 해외주식 종목/지수/환율 기간별시세 (v1_해외주식-012)
)
OVERSEAS_MINUTE_CHART_URL = (
    "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
)
OVERSEAS_MINUTE_CHART_TR = "FHKST03010200"  # 해외주식 분봉조회 (v1_해외주식-030)
OVERSEAS_PRICE_URL = "/uapi/overseas-price/v1/quotations/price"
OVERSEAS_PRICE_TR = "HHDFS00000300"  # 해외주식 현재가 조회

# 해외주식 거래 관련 URL 및 TR ID
OVERSEAS_MARGIN_URL = "/uapi/overseas-stock/v1/trading/foreign-margin"
OVERSEAS_MARGIN_TR = "TTTC2101R"  # 실전투자 해외증거금 통화별조회
OVERSEAS_MARGIN_TR_MOCK = "VTTS2101R"  # 모의투자 해외증거금 통화별조회 (추정)

# 통합증거금 조회 (원화 + 외화 예수금)
INTEGRATED_MARGIN_URL = "/uapi/domestic-stock/v1/trading/intgr-margin"
INTEGRATED_MARGIN_TR = "TTTC0869R"  # 실전투자 통합증거금 조회
INTEGRATED_MARGIN_TR_MOCK = "VTTC0869R"  # 모의투자 통합증거금 조회

OVERSEAS_BUYABLE_AMOUNT_URL = "/uapi/overseas-stock/v1/trading/inquire-psamount"
OVERSEAS_BUYABLE_AMOUNT_TR = "TTTS3007R"  # 실전투자 해외주식 매수가능금액조회
OVERSEAS_BUYABLE_AMOUNT_TR_MOCK = "VTTS3007R"  # 모의투자 해외주식 매수가능금액조회

OVERSEAS_ORDER_URL = "/uapi/overseas-stock/v1/trading/order"
OVERSEAS_ORDER_BUY_TR = "TTTT1002U"  # 실전투자 해외주식 매수주문
OVERSEAS_ORDER_BUY_TR_MOCK = "VTTT1002U"  # 모의투자 해외주식 매수주문
OVERSEAS_ORDER_SELL_TR = "TTTT1006U"  # 실전투자 해외주식 매도주문
OVERSEAS_ORDER_SELL_TR_MOCK = "VTTT1006U"  # 모의투자 해외주식 매도주문

# 해외주식 주문 조회 및 취소
OVERSEAS_ORDER_INQUIRY_URL = "/uapi/overseas-stock/v1/trading/inquire-nccs"
OVERSEAS_ORDER_INQUIRY_TR = "TTTS3018R"  # 해외주식 미체결내역 조회 (실전/모의 공통)

OVERSEAS_ORDER_CANCEL_URL = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
OVERSEAS_ORDER_CANCEL_TR = "TTTT1004U"  # 실전투자 해외주식 정정취소주문
OVERSEAS_ORDER_CANCEL_TR_MOCK = "VTTT1004U"  # 모의투자 해외주식 정정취소주문

# 해외주식 체결조회 (일별 주문 히스토리)
OVERSEAS_DAILY_ORDER_URL = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
OVERSEAS_DAILY_ORDER_TR = "TTTS3035R"  # 실전투자 해외주식 체결조회
OVERSEAS_DAILY_ORDER_TR_MOCK = "VTTS3035R"  # 모의투자 해외주식 체결조회

# 국내주식 주문 관련 URL 및 TR ID
KOREA_ORDER_URL = "/uapi/domestic-stock/v1/trading/order-cash"
KOREA_ORDER_BUY_TR = "TTTC0012U"  # 실전투자 국내주식 매수주문
KOREA_ORDER_BUY_TR_MOCK = "VTTC0012U"  # 모의투자 국내주식 매수주문
KOREA_ORDER_SELL_TR = "TTTC0011U"  # 실전투자 국내주식 매도주문
KOREA_ORDER_SELL_TR_MOCK = "VTTC0011U"  # 모의투자 국내주식 매도주문

# 국내주식 주문 조회 및 취소
KOREA_ORDER_INQUIRY_URL = "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
KOREA_ORDER_INQUIRY_TR = "TTTC8036R"  # 국내주식 정정취소가능주문조회 (실전/모의 공통)

KOREA_ORDER_CANCEL_URL = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
KOREA_ORDER_CANCEL_TR = "TTTC0013U"  # 실전투자 국내주식 정정취소주문
KOREA_ORDER_CANCEL_TR_MOCK = "VTTC0013U"  # 모의투자 국내주식 정정취소주문

# 국내주식 체결조회 (일별 주문 히스토리)
DOMESTIC_DAILY_ORDER_URL = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
DOMESTIC_DAILY_ORDER_TR = "TTTC8001R"  # 실전투자 국내주식 체결조회
DOMESTIC_DAILY_ORDER_TR_MOCK = "VTTC8001R"  # 모의투자 국내주식 체결조회

_DAY_FRAME_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]
_DAILY_ITEMCHARTPRICE_REQUIRED_FIELDS = {
    "stck_bsop_date",
    "stck_oprc",
    "stck_hgpr",
    "stck_lwpr",
    "stck_clpr",
    "acml_vol",
    "acml_tr_pbmn",
}


def _safe_parse_retry_after(value: str | None) -> float:
    """Safely parse Retry-After header, returning 0 on failure."""
    if not value:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _safe_status_code(response: object, *, default: int = 200) -> int:
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else default


def _empty_day_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_DAY_FRAME_COLUMNS)


def _validate_daily_itemchartprice_chunk(chunk: list[dict[str, Any]]) -> None:
    if not isinstance(chunk, list):
        raise RuntimeError(
            "Malformed KIS daily chart payload: expected list in output2/output"
        )

    for index, row in enumerate(chunk):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Malformed KIS daily chart payload at row {index}: expected object"
            )

        missing = sorted(
            field
            for field in _DAILY_ITEMCHARTPRICE_REQUIRED_FIELDS
            if row.get(field) is None or row.get(field) == ""
        )
        if missing:
            missing_fields = ", ".join(missing)
            raise RuntimeError(
                f"Malformed KIS daily chart payload at row {index}: missing {missing_fields}"
            )


def _log_kis_api_failure(
    api_name: str,
    endpoint: str,
    tr_id: str,
    request_keys: list[str],
    msg_cd: str,
    msg1: str,
) -> None:
    # Log all key names for debugging (OPSQ2001 diagnosis requires visibility)
    # Values are never logged - only key names
    logging.error(
        "KIS API 실패: api_name=%s, endpoint=%s, tr_id=%s, request_keys=%s, msg_cd=%s, msg1=%s",
        api_name,
        endpoint,
        tr_id,
        sorted(request_keys),
        msg_cd,
        msg1,
    )
    if msg_cd == "OPSQ2001" or "CMA_EVLU_AMT_ICLD_YN" in str(msg1):
        logging.warning(
            "OPSQ2001/CMA_EVLU_AMT_ICLD_YN 감지: api_name=%s, endpoint=%s, tr_id=%s",
            api_name,
            endpoint,
            tr_id,
        )


def extract_domestic_cash_summary_from_integrated_margin(
    margin_data: dict[str, Any],
) -> dict[str, Any]:
    def safe_float(val: object, default: float = 0.0) -> float:
        if val in ("", None):
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    raw = margin_data.get("raw")
    raw_payload = raw if isinstance(raw, dict) else margin_data

    return {
        "balance": safe_float(margin_data.get("stck_cash_objt_amt")),
        "orderable": safe_float(margin_data.get("stck_itgr_cash100_ord_psbl_amt")),
        "raw": raw_payload,
    }


class KISClient:
    """KIS (Korea Investment & Securities) API client.

    This is the main facade class that provides access to all KIS API operations
    through composition of specialized internal modules:
    - _transport: HTTP communication, rate limiting, token management
    - _holdings: Balance and holdings operations
    - _orders: Order placement, cancellation, and inquiry
    - _market_data: Price, chart, and ranking data

    The internal modules are instantiated in __init__ and delegate actual
    operations to the appropriate specialized classes.
    """

    def __init__(self) -> None:
        """Initialize the KIS client with internal modules."""
        # Internal modules - composed from specialized classes
        self._transport = KISTransport()
        self._holdings = HoldingsAPI(self._transport)
        self._orders = OrdersAPI(self._transport)
        self._market_data = MarketDataAPI(self._transport)

        # Legacy attributes kept for backward compatibility
        self._hdr_base = {
            "appkey": settings.kis_app_key,
            "appsecret": settings.kis_app_secret,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }
        # Redis 기반 토큰 관리 사용
        self._token_manager = redis_token_manager
        self._unmapped_rate_limit_keys_logged: set[str] = set()

    async def _fetch_token(self) -> tuple[str, int]:
        """KIS API에서 새 토큰 발급"""
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{BASE}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "appkey": settings.kis_app_key,
                    "appsecret": settings.kis_app_secret,
                },
                timeout=5,
            )
        response = r.json()
        access_token = response["access_token"]
        expires_in = response.get("expires_in", 3600)  # 기본 1시간

        logging.info("KIS 새 토큰 발급 완료")
        return access_token, expires_in

    async def _ensure_token(self):
        token = await self._token_manager.get_token()
        if token:
            settings.kis_access_token = token
            logging.debug("KIS access token ready for request")
            return

        # 토큰이 없거나 만료된 경우 새로 발급 (분산 락 사용)
        async def token_fetcher():
            access_token, expires_in = await self._fetch_token()
            return access_token, expires_in

        settings.kis_access_token = await self._token_manager.refresh_token_with_lock(
            token_fetcher
        )
        logging.info("KIS access token refreshed and applied")

    @staticmethod
    def _extract_korea_order_orgno(order: dict[str, Any]) -> str | None:
        for key in (
            "KRX_FWDG_ORD_ORGNO",
            "krx_fwdg_ord_orgno",
            "ORD_GNO_BRNO",
            "ord_gno_brno",
        ):
            value = order.get(key)
            if value is None:
                continue
            orgno = str(value).strip()
            if orgno:
                return orgno
        return None

    async def _resolve_korea_order_orgno(
        self,
        order_number: str,
        stock_code: str | None,
        is_mock: bool,
    ) -> str:
        target_order_number = order_number.strip()
        target_stock_code = stock_code.strip() if stock_code is not None else None

        open_orders = await self.inquire_korea_orders(is_mock=is_mock)

        for order in open_orders:
            listed_order_number = (
                order.get("odno")
                or order.get("ODNO")
                or order.get("ord_no")
                or order.get("ORD_NO")
            )
            if str(listed_order_number).strip() != target_order_number:
                continue

            if target_stock_code:
                listed_stock_code = order.get("pdno") or order.get("PDNO")
                if str(listed_stock_code).strip() != target_stock_code:
                    continue

            orgno = self._extract_korea_order_orgno(order)
            if orgno:
                return orgno

        raise ValueError(f"KRX_FWDG_ORD_ORGNO not found for order {order_number}")

    async def _request_with_rate_limit(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
        tr_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Make an HTTP request with rate limiting and 429 retry logic.

        This method wraps httpx requests with:
        1. Sliding-window rate limiting (acquired before request)
        2. 429 response handling with exponential backoff
        3. KIS-specific rate limit heuristics (msg_cd/msg1)

        Args:
            method: HTTP method ("GET" or "POST")
            url: Full URL to request
            headers: Request headers (including authorization)
            params: Query parameters for GET requests
            json_body: JSON body for POST requests
            timeout: Request timeout in seconds
            api_name: Human-readable API name for logging
            tr_id: KIS TR_ID for per-API rate limiting

        Returns:
            Parsed JSON response

        Raises:
            RuntimeError: On KIS API errors after retries exhausted
            httpx.HTTPStatusError: On HTTP errors after retries exhausted
        """
        # Extract path from URL for per-API rate limiting
        from urllib.parse import urlparse

        parsed_url = urlparse(url)
        api_path = parsed_url.path or "/unknown"

        # Build API key for per-API rate limiting: "TR_ID|/path"
        api_key = f"{tr_id or 'unknown'}|{api_path}"

        # Get rate limit for this specific API
        rate, period = self._get_rate_limit_for_api(api_key)

        limiter = await get_limiter("kis", api_key, rate=rate, period=period)
        max_retries = settings.api_rate_limit_retry_429_max
        base_delay = settings.api_rate_limit_retry_429_base_delay

        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            await limiter.acquire(
                blocking_callback=lambda w: logging.warning(
                    "[%s] Rate limit wait: %.3fs (api=%s)",
                    "kis",
                    w,
                    api_name,
                )
            )

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if method.upper() == "GET":
                        response = await client.get(
                            url,
                            headers=headers,
                            params=params,
                            timeout=timeout,
                        )
                    else:
                        response = await client.post(
                            url,
                            headers=headers,
                            json=json_body,
                            timeout=timeout,
                        )

                status_code = _safe_status_code(response)

                if status_code == 429:
                    retry_after = _safe_parse_retry_after(
                        response.headers.get("Retry-After")
                    )
                    wait_time = (
                        retry_after
                        if retry_after > 0
                        else base_delay * (2**attempt) + random.uniform(0, 0.1)
                    )
                    logging.warning(
                        "[%s] 429 received for %s, attempt %d/%d, waiting %.3fs",
                        "kis",
                        api_name,
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                try:
                    data = response.json()
                except ValueError as exc:
                    if status_code >= 400:
                        response.raise_for_status()
                    raise RuntimeError(
                        f"KIS API non-JSON response: {api_name}"
                    ) from exc

                if not isinstance(data, dict):
                    if status_code >= 400:
                        response.raise_for_status()
                    raise RuntimeError(f"KIS API non-JSON response: {api_name}")

                if status_code >= 400 and status_code != 500:
                    response.raise_for_status()

                rt_cd = data.get("rt_cd")
                msg_cd = str(data.get("msg_cd", ""))
                msg1 = str(data.get("msg1", ""))

                if rt_cd != "0":
                    rate_limit_heuristics = [
                        "RATE",
                        "LIMIT",
                        "요청제한",
                        "초과",
                    ]
                    is_rate_limit = any(
                        h in msg_cd.upper() or h in msg1.upper()
                        for h in rate_limit_heuristics
                    )

                    if is_rate_limit and attempt < max_retries:
                        wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                        logging.warning(
                            "[%s] Rate limit heuristic triggered for %s: %s %s, attempt %d/%d, waiting %.3fs",
                            "kis",
                            api_name,
                            msg_cd,
                            msg1,
                            attempt + 1,
                            max_retries + 1,
                            wait_time,
                        )
                        await asyncio.sleep(wait_time)
                        continue

                return data

            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 429 and attempt < max_retries:
                    wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                    logging.warning(
                        "[%s] HTTP 429 for %s, attempt %d/%d, waiting %.3fs",
                        "kis",
                        api_name,
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise
            except httpx.RequestError as e:
                last_error = e
                if attempt < max_retries:
                    wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                    logging.warning(
                        "[%s] Request error for %s: %s, attempt %d/%d, retrying in %.3fs",
                        "kis",
                        api_name,
                        e,
                        attempt + 1,
                        max_retries + 1,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise

        raise RateLimitExceededError(
            f"KIS rate limit retries exhausted for {api_name}: {last_error}"
        )

    def _get_rate_limit_for_api(self, api_key: str) -> tuple[int, float]:
        """Get rate limit for a specific API key, falling back to defaults."""

        def _safe_rate(value: object, default: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

        def _safe_period(value: object, default: float) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

        default_rate = _safe_rate(getattr(settings, "kis_rate_limit_rate", 19), 19)
        default_period = _safe_period(
            getattr(settings, "kis_rate_limit_period", 1.0), 1.0
        )

        api_limits = getattr(settings, "kis_api_rate_limits", {})
        if isinstance(api_limits, dict) and api_key in api_limits:
            limit_config = api_limits[api_key]
            if isinstance(limit_config, dict):
                rate = _safe_rate(limit_config.get("rate"), default_rate)
                period = _safe_period(limit_config.get("period"), default_period)
                return rate, period

        if api_key not in self._unmapped_rate_limit_keys_logged:
            logging.warning(
                "[kis] Unmapped API rate limit for %s, using defaults (%s/%ss)",
                api_key,
                default_rate,
                default_period,
            )
            self._unmapped_rate_limit_keys_logged.add(api_key)
        return default_rate, default_period

    async def volume_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": VOL_TR,
        }

        params = {
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
        }

        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{VOL_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="volume_rank",
            tr_id=VOL_TR,
        )
        if js["rt_cd"] == "0":
            results = js["output"][:limit]
            # Safe debug sample without float conversion that could fail
            sample_data = [
                (r.get("hts_kor_isnm", ""), r.get("acml_vol", "0")) for r in results[:3]
            ]
            logging.debug(
                f"volume_rank: Received {len(js['output'])} results, "
                f"returning {len(results)}. Sample: {sample_data}"
            )
            return results
        if js["msg_cd"] == "EGW00123":
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.volume_rank(market, limit)
        elif js["msg_cd"] == "EGW00121":
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.volume_rank(market, limit)
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def market_cap_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": MARKET_CAP_RANK_TR,
        }
        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{MARKET_CAP_RANK_URL}",
            headers=hdr,
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
            timeout=5,
            api_name="market_cap_rank",
            tr_id=MARKET_CAP_RANK_TR,
        )
        if js["rt_cd"] == "0":
            return js["output"][:limit]
        if js["msg_cd"] == "EGW00123":
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.market_cap_rank(market, limit)
        elif js["msg_cd"] == "EGW00121":
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.market_cap_rank(market, limit)
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict]:
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": FLUCTUATION_RANK_TR,
        }

        # FID_PRC_CLS_CODE: "0"=전체 (공식 API 문서 기준)
        prc_cls_code = "0"
        # FID_RANK_SORT_CLS_CODE: "0"=상승률, "3"=하락율 (공식 API 문서 기준)
        rank_sort_cls_code = "3" if direction == "down" else "0"

        logging.debug(
            f"fluctuation_rank: direction={direction}, "
            f"FID_PRC_CLS_CODE={prc_cls_code}, "
            f"FID_RANK_SORT_CLS_CODE={rank_sort_cls_code}"
        )

        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{FLUCTUATION_RANK_URL}",
            headers=hdr,
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
            timeout=5,
            api_name="fluctuation_rank",
            tr_id=FLUCTUATION_RANK_TR,
        )

        if js["rt_cd"] == "0":
            results = js["output"]
            # Sort: up → descending (highest first), down → ascending (lowest first).
            if direction == "up":
                results.sort(key=lambda x: float(x.get("prdy_ctrt", 0)), reverse=True)
                return results[:limit]

            negatives = [
                item for item in results if float(item.get("prdy_ctrt", 0)) < 0
            ]
            negatives.sort(key=lambda x: float(x.get("prdy_ctrt", 0)))
            return negatives[:limit]

        if js["msg_cd"] in ("EGW00123", "EGW00121"):
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.fluctuation_rank(market, direction, limit)

        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": FOREIGN_BUYING_RANK_TR,
        }
        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{FOREIGN_BUYING_RANK_URL}",
            headers=hdr,
            params={
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": "1",
            },
            timeout=5,
            api_name="foreign_buying_rank",
            tr_id=FOREIGN_BUYING_RANK_TR,
        )
        if js["rt_cd"] == "0":
            return js["output"][:limit]
        if js["msg_cd"] == "EGW00123":
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.foreign_buying_rank(market, limit)
        elif js["msg_cd"] == "EGW00121":
            await self._token_manager.clear_token()
            await self._ensure_token()
            return await self.foreign_buying_rank(market, limit)
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def inquire_price(self, code: str, market: str = "UN") -> DataFrame:
        """
        단일 종목 현재가·기본정보 조회
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: API output 딕셔너리
        """
        await self._ensure_token()

        # 요청 헤더
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": PRICE_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),  # 000000 형태도 OK
        }

        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{PRICE_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_price",
            tr_id=PRICE_TR,
        )
        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                "EGW00123",
                "EGW00121",
            ]:  # 토큰 만료 또는 유효하지 않은 토큰
                # Redis에서 토큰 삭제 후 새로 발급
                await self._token_manager.clear_token()
                await self._ensure_token()
                # 재시도 1회
                return await self.inquire_price(code, market)
            raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
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

    async def inquire_orderbook(self, code: str, market: str = "UN") -> dict:
        """
        주식 호가(orderbook) 조회 - 10단계 매수/매도 호가
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: API output 딕셔너리
        """
        await self._ensure_token()

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": ORDERBOOK_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
        }

        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{ORDERBOOK_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_orderbook",
            tr_id=ORDERBOOK_TR,
        )
        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                "EGW00123",
                "EGW00121",
            ]:
                await self._token_manager.clear_token()
                await self._ensure_token()
                return await self.inquire_orderbook(code, market)
            raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
        output = js.get("output1")
        if output is None:
            output = js.get("output")
        if not isinstance(output, dict):
            raise RuntimeError("inquire_orderbook: missing valid output1/output dict")
        return output

    async def fetch_fundamental_info(self, code: str, market: str = "UN") -> dict:
        """
        종목의 기본 정보를 가져와 딕셔너리로 반환합니다.
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: 기본 정보 딕셔너리
        """
        await self._ensure_token()

        # 요청 헤더
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": PRICE_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),  # 000000 형태도 OK
        }

        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{PRICE_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="fetch_fundamental_info",
            tr_id=PRICE_TR,
        )
        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                "EGW00123",
                "EGW00121",
            ]:  # 토큰 만료 또는 유효하지 않은 토큰
                # Redis에서 토큰 삭제 후 새로 발급
                await self._token_manager.clear_token()
                await self._ensure_token()
                # 재시도 1회
                return await self.fetch_fundamental_info(code, market)
            raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
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

    async def inquire_daily_itemchartprice(
        self,
        code: str,
        market: str = "UN",
        n: int = 200,  # 최종 확보하고 싶은 캔들 수
        adj: bool = True,
        period: str = "D",  # D/W/M (일/주/월봉)
        end_date: datetime.date | None = None,  # None이면 오늘까지
        per_call_days: int = 150,  # 한 번 호출 시 조회 날짜 폭
    ) -> pd.DataFrame:
        """
        ✅ KIS 일봉/주봉/월봉을 여러 번 호출해 최근 n개 OHLCV만 반환
           (이평 같은 지표 계산은 외부에서!)
        컬럼: date • open • high • low • close • volume • value
        """
        await self._ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": DAILY_ITEMCHARTPRICE_TR,
        }

        end = end_date or datetime.date.today()
        rows: list[dict] = []

        while len(rows) < n:
            start = end - datetime.timedelta(days=per_call_days)
            params = {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
                "FID_PERIOD_DIV_CODE": period,  # 'D'|'W'|'M'
                "FID_ORG_ADJ_PRC": "0" if adj else "1",  # 0=수정, 1=원본
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            }

            js = await self._request_with_rate_limit(
                "GET",
                f"{BASE}{DAILY_ITEMCHARTPRICE_URL}",
                headers=hdr,
                params=params,
                timeout=5,
                api_name="inquire_daily_itemchartprice",
                tr_id=DAILY_ITEMCHARTPRICE_TR,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in [
                    "EGW00123",
                    "EGW00121",
                ]:  # 토큰 만료 또는 유효하지 않은 토큰
                    # Redis에서 토큰 삭제 후 새로 발급
                    await self._token_manager.clear_token()
                    await self._ensure_token()
                    continue
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")

            chunk = js.get("output2") or js.get("output") or []
            if not chunk:
                break  # 더 과거 없음

            _validate_daily_itemchartprice_chunk(chunk)

            rows.extend(chunk)

            # 다음 루프에서 더 과거로
            oldest_str = min(str(c["stck_bsop_date"]) for c in chunk)
            try:
                oldest = datetime.datetime.strptime(oldest_str, "%Y%m%d").date()
            except ValueError as exc:
                raise RuntimeError(
                    "Malformed KIS daily chart payload: invalid stck_bsop_date format"
                ) from exc
            end = oldest - datetime.timedelta(days=1)

        # ---- DataFrame 변환 (지표 계산 없음) ----
        if not rows:
            return _empty_day_frame()

        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_clpr": "close",
                    "acml_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
            .astype(
                {
                    "date": "int",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                    "value": "int",
                },
                errors="ignore",
            )
            .assign(date=lambda d: pd.to_datetime(d["date"], format="%Y%m%d"))
            .drop_duplicates(subset=["date"], keep="first")
            .sort_values("date")
            .tail(n)  # 요청한 개수만
            .reset_index(drop=True)
        )
        return df

    async def inquire_time_dailychartprice(
        self,
        code: str,
        market: str = "UN",
        n: int = 200,
        end_date: datetime.date | None = None,
        end_time: str | None = None,
    ) -> pd.DataFrame:
        await self._ensure_token()

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": TIME_DAILY_CHART_TR,
        }

        base_date = end_date or datetime.date.today()
        current_time = end_time or datetime.datetime.now().strftime("%H%M%S")
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_INPUT_HOUR_1": current_time,
            "FID_INPUT_DATE_1": base_date.strftime("%Y%m%d"),
            "FID_PW_DATA_INCU_YN": "N",
            "FID_FAKE_TICK_INCU_YN": "",
            "FID_ETC_CLS_CODE": "",
        }

        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{TIME_DAILY_CHART_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_time_dailychartprice",
            tr_id=TIME_DAILY_CHART_TR,
        )

        rows = js.get("output2") or js.get("output") or []
        if not rows:
            if js.get("rt_cd") != "0":
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

        frame = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_cntg_hour": "time",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_prpr": "close",
                    "cntg_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
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
                    format="%Y%m%d%H%M%S",
                    errors="coerce",
                )
            )
            .dropna(subset=["datetime"])
            .assign(
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[
                :,
                [
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ],
            ]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(max(int(n), 1))
            .reset_index(drop=True)
        )
        return frame

    @staticmethod
    def _aggregate_intraday_to_hour(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or "datetime" not in df.columns:
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

        frame = df.copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        frame = frame.dropna(subset=["datetime"]).sort_values("datetime")
        if frame.empty:
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

        grouped = (
            frame.assign(hour_bucket=lambda d: d["datetime"].dt.floor("60min"))
            .groupby("hour_bucket", as_index=False)
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                value=("value", "sum"),
            )
            .rename(columns={"hour_bucket": "datetime"})
        )
        grouped = grouped.assign(
            date=lambda d: d["datetime"].dt.date,
            time=lambda d: d["datetime"].dt.time,
        )
        return cast(
            DataFrame,
            grouped.loc[
                :,
                [
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ],
            ]
            .reset_index(drop=True)
            .copy(),
        )

    async def inquire_minute_chart(
        self,
        code: str,
        market: str = "UN",
        time_unit: int = 1,  # 1: 1분, 3: 3분, 5: 5분, 10: 10분, 15: 15분, 30: 30분, 45: 45분, 60: 60분
        n: int = 200,  # 최종 확보하고 싶은 캔들 수
        end_date: datetime.date | None = None,  # None이면 오늘까지
    ) -> pd.DataFrame:
        """
        KIS 분봉 데이터 조회
        컬럼: datetime, date, time, open, high, low, close, volume, value

        Parameters
        ----------
        code : str
            6자리 종목코드 (예: "005930")
        market : str, default "UN"
            시장 구분 (UN: 통합, K: 코스피, Q: 코스닥)
        time_unit : int, default 1
            분봉 단위 (1, 3, 5, 10, 15, 30, 45, 60)
        n : int, default 200
            가져올 캔들 수 (최대 200)
        end_date : datetime.date, optional
            종료 날짜 (None이면 오늘까지)
        """
        await self._ensure_token()

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": MINUTE_CHART_TR,
        }

        # KIS 분봉 API는 time_unit 파라미터를 제대로 인식하지 못하는 문제가 있음
        # 현재로서는 모든 시간대에서 동일한 데이터가 반환됨
        # 향후 API 문서 업데이트나 기술지원을 통해 해결 필요

        # 현재 시간을 시분초로 설정 (장 시간 내에만 작동)
        current_time = datetime.datetime.now().strftime("%H%M%S")

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_INPUT_HOUR_1": current_time,  # 현재 시분초
            "FID_INPUT_DATE_1": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_TIME_1": "01",  # 1분봉으로 고정 (API가 time_unit을 지원하지 않음)
            "FID_INPUT_TIME_2": "01",  # 1분봉으로 고정
            "FID_PW_DATA_INCU_YN": "N",
            "FID_ETC_CLS_CODE": "",
        }

        js = await self._request_with_rate_limit(
            "GET",
            f"{BASE}{MINUTE_CHART_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_minute_chart",
            tr_id=MINUTE_CHART_TR,
        )

        # 디버깅을 위한 로깅 추가
        logging.info(f"KIS 분봉 API 응답: {js}")

        # rt_cd가 비어있어도 output2에 데이터가 있을 수 있음
        rows = js.get("output2") or js.get("output") or []

        if not rows:
            # 데이터가 없는 경우 빈 DataFrame 반환
            logging.warning(f"KIS 분봉 API에서 데이터를 찾을 수 없음: {js}")
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

        # 데이터가 있으면 성공으로 처리 (rt_cd가 비어있어도)
        logging.info(f"KIS 분봉 API에서 {len(rows)}개 데이터 수집 성공")

        # DataFrame 변환
        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_cntg_hour": "time",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_prpr": "close",
                    "cntg_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
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
                # 날짜와 시간을 결합하여 datetime 생성
                datetime=lambda d: pd.to_datetime(
                    d["date"] + d["time"], format="%Y%m%d%H%M%S"
                ),
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[
                :,
                [
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ],
            ]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(n)  # 요청한 개수만
            .reset_index(drop=True)
        )

        return df

    def _aggregate_minute_candles(
        self, df_1min: pd.DataFrame, time_unit: int
    ) -> pd.DataFrame:
        """
        1분봉 데이터를 지정된 시간 단위로 집계

        Args:
            df_1min: 1분봉 DataFrame
            time_unit: 집계할 시간 단위 (분)

        Returns:
            집계된 DataFrame (완전한 시간대만)
        """
        if df_1min.empty:
            return df_1min

        # 시간을 time_unit 단위로 그룹화
        df_1min = df_1min.copy()
        df_1min["time_group"] = df_1min["datetime"].dt.floor(f"{time_unit}min")

        # 그룹별로 OHLCV 집계
        aggregated = (
            df_1min.groupby("time_group")
            .agg(
                {
                    "open": "first",  # 첫 번째 시가
                    "high": "max",  # 최고가
                    "low": "min",  # 최저가
                    "close": "last",  # 마지막 종가
                    "volume": "sum",  # 거래량 합계
                    "value": "sum",  # 거래대금 합계
                }
            )
            .reset_index()
        )

        # 완전한 시간대만 필터링 (time_unit만큼의 분 데이터가 있는 그룹만)
        complete_periods = []
        for _, row in aggregated.iterrows():
            group_start = row["time_group"]
            group_end = group_start + pd.Timedelta(minutes=time_unit)

            # 해당 그룹에 속하는 1분봉 개수 확인
            period_data = df_1min[
                (df_1min["datetime"] >= group_start) & (df_1min["datetime"] < group_end)
            ]

            # 완전한 시간대인지 확인 (time_unit만큼의 분 데이터가 있어야 함)
            if len(period_data) >= time_unit:
                complete_periods.append(row)

        if not complete_periods:
            # 완전한 시간대가 없으면 빈 DataFrame 반환
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

        # 완전한 시간대만으로 DataFrame 재구성
        df_complete = pd.DataFrame(complete_periods)

        # 컬럼명 변경 및 시간 정보 추가
        df_complete = df_complete.rename(columns={"time_group": "datetime"})
        df_complete["date"] = df_complete["datetime"].dt.date
        df_complete["time"] = df_complete["datetime"].dt.time

        # 원본 컬럼 순서로 재정렬
        columns = [
            "datetime",
            "date",
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "value",
        ]
        return cast(DataFrame, df_complete.loc[:, columns].copy())

    # =================================================================
    # Holdings/Balance API - Delegates to HoldingsAPI
    # =================================================================

    async def fetch_my_stocks(
        self,
        is_mock: bool = False,
        is_overseas: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
    ) -> list[dict]:
        """보유 주식 목록 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.fetch_my_stocks(
            is_mock=is_mock,
            is_overseas=is_overseas,
            exchange_code=exchange_code,
            currency_code=currency_code,
        )

    async def inquire_domestic_cash_balance(self, is_mock: bool = False) -> dict:
        """국내주식 현금 잔고 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.inquire_domestic_cash_balance(is_mock=is_mock)

    async def inquire_overseas_margin(self, is_mock: bool = False) -> list[dict]:
        """해외증거금 통화별 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.inquire_overseas_margin(is_mock=is_mock)

    async def inquire_integrated_margin(
        self,
        is_mock: bool = False,
        cma_evlu_amt_icld_yn: str = "N",
        wcrc_frcr_dvsn_cd: str = "01",
        fwex_ctrt_frcr_dvsn_cd: str = "01",
    ) -> dict:
        """통합증거금 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.inquire_integrated_margin(
            is_mock=is_mock,
            cma_evlu_amt_icld_yn=cma_evlu_amt_icld_yn,
            wcrc_frcr_dvsn_cd=wcrc_frcr_dvsn_cd,
            fwex_ctrt_frcr_dvsn_cd=fwex_ctrt_frcr_dvsn_cd,
        )

    async def fetch_my_overseas_stocks(
        self,
        is_mock: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
    ) -> list[dict]:
        """해외 보유 주식 목록 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.fetch_my_overseas_stocks(
            is_mock=is_mock,
            exchange_code=exchange_code,
            currency_code=currency_code,
        )

    async def fetch_my_us_stocks(
        self, is_mock: bool = False, exchange: str = "NASD"
    ) -> list[dict]:
        """미국 보유 주식 목록 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.fetch_my_us_stocks(
            is_mock=is_mock, exchange=exchange
        )

    async def fetch_minute_candles(
        self,
        code: str,
        market: str = "UN",
        end_date: datetime.date | None = None,
    ) -> dict:
        """
        분봉 데이터를 가져와서 60분, 5분, 1분 캔들로 반환

        Args:
            code: 종목코드
            market: 시장 구분 (UN: 통합)
            end_date: 종료 날짜 (None이면 오늘)

        Returns:
            분봉 캔들 데이터 딕셔너리
        """
        minute_candles = {}

        try:
            logging.info(f"분봉 데이터 수집 시작: {code}")

            # 단일 요청으로 200개 1분봉 수집
            df_1min = await self.inquire_minute_chart(
                code, market, time_unit=1, n=200, end_date=end_date
            )

            if not df_1min.empty:
                logging.info(f"1분봉 {len(df_1min)}개 수집 완료")

                minute_candles["1min"] = df_1min

                # 1분봉 데이터를 5분봉으로 가공
                df_5min = self._aggregate_minute_candles(df_1min, 5)
                minute_candles["5min"] = df_5min

                # 1분봉 데이터를 60분봉으로 가공
                df_60min = self._aggregate_minute_candles(df_1min, 60)
                minute_candles["60min"] = df_60min

                logging.info(
                    f"집계 완료 - 1분봉: {len(df_1min)}개, 5분봉: {len(df_5min)}개, 60분봉: {len(df_60min)}개"
                )

            else:
                # 데이터가 없는 경우 빈 DataFrame으로 설정
                empty_df = pd.DataFrame(
                    columns=[
                        "datetime",
                        "date",
                        "time",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "value",
                    ]
                )
                minute_candles = {"60min": empty_df, "5min": empty_df, "1min": empty_df}
                logging.warning("수집된 데이터가 없습니다")

        except Exception as e:
            logging.warning(f"분봉 데이터 수집 실패 ({code}): {e}")
            # 실패한 경우 빈 DataFrame으로 설정
            empty_df = pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )
            minute_candles = {"60min": empty_df, "5min": empty_df, "1min": empty_df}

        return minute_candles

    async def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
        period: str = "D",  # D/W/M
    ) -> pd.DataFrame:
        """
        해외주식 일봉/주봉/월봉 조회 (국내주식처럼 충분한 데이터 확보)

        Args:
            symbol: 종목 심볼 (예: "AAPL")
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            n: 조회할 캔들 수 (최소 200개 권장, 이동평균선 계산용)
            period: D(일봉)/W(주봉)/M(월봉)

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        await self._ensure_token()

        # KIS API는 거래소 코드를 3자리로 사용: NASD -> NAS, NYSE -> NYS, AMEX -> AMS
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange_code, exchange_code[:3])

        hdr = self._hdr_base | {
            "authorization": f"Bearer {settings.kis_access_token}",
            "tr_id": OVERSEAS_DAILY_CHART_TR,
        }

        rows: list[dict] = []
        max_iterations = 5  # 최대 5번 반복 (충분한 데이터 확보)
        iteration = 0

        # 국내주식처럼 충분한 데이터를 확보할 때까지 반복 조회
        while len(rows) < n and iteration < max_iterations:
            # BYMD 파라미터: 빈 값이면 최근, 날짜를 지정하면 해당 날짜부터 과거로
            if rows:
                # 이전에 가져온 데이터의 가장 오래된 날짜 찾기
                oldest_date = min(r.get("xymd", "99999999") for r in rows)
                # 하루 전으로 설정
                try:
                    oldest_dt = datetime.datetime.strptime(oldest_date, "%Y%m%d")
                    bymd = (oldest_dt - datetime.timedelta(days=1)).strftime("%Y%m%d")
                except Exception:
                    bymd = ""
            else:
                bymd = ""  # 첫 요청은 최신 데이터부터

            params = {
                "AUTH": "",
                "EXCD": excd,  # 거래소코드 (3자리)
                "SYMB": to_kis_symbol(symbol),  # 심볼 (DB형식 . -> KIS형식 /)
                "GUBN": {"D": "0", "W": "1", "M": "2"}.get(period.upper(), "0"),
                "BYMD": bymd,  # 조회기준일자
                "MODP": "1",  # 0:수정주가 미반영, 1:수정주가 반영
            }

            logging.info(
                f"해외주식 일봉 조회 요청 (반복 {iteration + 1}/{max_iterations}) - symbol: {symbol}, exchange: {excd}, bymd: {bymd}"
            )

            js = await self._request_with_rate_limit(
                "GET",
                f"{BASE}{OVERSEAS_DAILY_CHART_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_overseas_daily_price",
                tr_id=OVERSEAS_DAILY_CHART_TR,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                    await self._token_manager.clear_token()
                    await self._ensure_token()
                    continue
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")

            chunk = js.get("output2") or js.get("output") or []
            if not chunk:
                logging.info(
                    f"더 이상 과거 데이터가 없음. 현재까지 수집: {len(rows)}개"
                )
                break

            rows.extend(chunk)
            iteration += 1
            logging.info(f"누적 데이터: {len(rows)}개 / 목표: {n}개")

        if not rows:
            return pd.DataFrame(
                columns=["date", "open", "high", "low", "close", "volume"]
            )

        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "xymd": "date",
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "clos": "close",
                    "tvol": "volume",
                }
            )
            .astype(
                {
                    "date": "str",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                },
                errors="ignore",
            )
            .assign(date=lambda d: pd.to_datetime(d["date"], format="%Y%m%d"))
            .drop_duplicates(subset=["date"], keep="first")
            .sort_values("date")
            .tail(n)
            .reset_index(drop=True)
        )
        logging.info(f"해외주식 일봉 조회 완료: {len(df)}개 데이터 반환")
        return df

    # =================================================================
    # Orders API - Delegates to OrdersAPI
    # =================================================================

    async def order_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        order_type: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 (매수/매도) - Delegates to OrdersAPI."""
        return await self._orders.order_overseas_stock(
            symbol, exchange_code, order_type, quantity, price, is_mock
        )

    async def buy_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 매수 주문 편의 메서드 - Delegates to OrdersAPI."""
        return await self._orders.buy_overseas_stock(
            symbol, exchange_code, quantity, price, is_mock
        )

    async def sell_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 매도 주문 편의 메서드 - Delegates to OrdersAPI."""
        return await self._orders.sell_overseas_stock(
            symbol, exchange_code, quantity, price, is_mock
        )

    async def inquire_overseas_orders(
        self,
        exchange_code: str = "NASD",
        is_mock: bool = False,
    ) -> list[dict]:
        """해외주식 미체결 주문 조회 - Delegates to OrdersAPI."""
        return await self._orders.inquire_overseas_orders(
            exchange_code=exchange_code,
            is_mock=is_mock,
        )

    async def cancel_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 취소 - Delegates to OrdersAPI."""
        return await self._orders.cancel_overseas_order(
            order_number, symbol, exchange_code, quantity, is_mock
        )

    async def inquire_korea_orders(
        self,
        is_mock: bool = False,
    ) -> list[dict]:
        """국내주식 정정취소가능주문 조회 - Delegates to OrdersAPI."""
        return await self._orders.inquire_korea_orders(is_mock=is_mock)

    async def order_korea_stock(
        self,
        stock_code: str,
        order_type: str,
        quantity: int,
        price: int = 0,
        is_mock: bool = False,
    ) -> dict:
        """국내주식 주문 (매수/매도) - Delegates to OrdersAPI."""
        return await self._orders.order_korea_stock(
            stock_code, order_type, quantity, price, is_mock
        )

    async def sell_korea_stock(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        is_mock: bool = False,
    ) -> dict:
        """국내주식 매도 주문 편의 메서드 - Delegates to OrdersAPI."""
        return await self._orders.sell_korea_stock(stock_code, quantity, price, is_mock)

    async def cancel_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        price: int,
        order_type: str,
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict:
        """국내주식 주문 취소 - Delegates to OrdersAPI."""
        return await self._orders.cancel_korea_order(
            order_number,
            stock_code,
            quantity,
            price,
            order_type,
            is_mock,
            krx_fwdg_ord_orgno,
        )

    async def inquire_daily_order_domestic(
        self,
        start_date: str,
        end_date: str,
        stock_code: str = "",
        side: str = "00",
        order_number: str = "",
        is_mock: bool = False,
    ) -> list[dict]:
        """국내주식 일별 체결조회 (주문 히스토리) - Delegates to OrdersAPI."""
        return await self._orders.inquire_daily_order_domestic(
            start_date, end_date, stock_code, side, order_number, is_mock
        )

    async def inquire_daily_order_overseas(
        self,
        start_date: str,
        end_date: str,
        symbol: str = "%",
        exchange_code: str = "NASD",
        side: str = "00",
        order_number: str = "",
        is_mock: bool = False,
    ) -> list[dict]:
        """해외주식 일별 체결조회 (주문 히스토리) - Delegates to OrdersAPI."""
        return await self._orders.inquire_daily_order_overseas(
            start_date, end_date, symbol, exchange_code, side, order_number, is_mock
        )

    async def modify_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        new_price: int,
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict:
        """국내주식 주문 정정 (가격/수량 변경) - Delegates to OrdersAPI."""
        return await self._orders.modify_korea_order(
            order_number, stock_code, quantity, new_price, is_mock, krx_fwdg_ord_orgno
        )

    async def modify_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        new_price: float,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 정정 (가격/수량 변경) - Delegates to OrdersAPI."""
        return await self._orders.modify_overseas_order(
            order_number, symbol, exchange_code, quantity, new_price, is_mock
        )


kis = KISClient()  # 싱글턴
