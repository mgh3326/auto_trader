import asyncio
import importlib
import logging
import random
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt
import pandas as pd

from app.core.async_rate_limiter import RateLimitExceededError, get_limiter
from app.core.config import settings
from app.services.upbit_symbol_universe_service import get_active_upbit_markets

logger = logging.getLogger(__name__)

UPBIT_REST = "https://api.upbit.com/v1"
UPBIT_CANDLES_RATE_LIMIT_KEY = "GET /v1/candles/*"
_UPBIT_TICKER_BATCH_SIZE = 50
_INTERVAL_TO_ENDPOINT = {
    "day": "days",
    "week": "weeks",
    "month": "months",
    "1m": "minutes/1",
    "3m": "minutes/3",
    "5m": "minutes/5",
    "10m": "minutes/10",
    "15m": "minutes/15",
    "30m": "minutes/30",
    "1h": "minutes/60",
    "4h": "minutes/240",
}
_unmapped_rate_limit_keys_logged: set[str] = set()
_ticker_price_cache: dict[str, tuple[float, float]] = {}
_ticker_inflight_symbol_tasks: dict[str, asyncio.Task[dict[str, float]]] = {}
_ticker_cache_lock: asyncio.Lock | None = None
_ticker_cache_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_ticker_cache_lock() -> asyncio.Lock:
    global _ticker_cache_lock, _ticker_cache_lock_loop
    loop = asyncio.get_running_loop()
    if _ticker_cache_lock is None or _ticker_cache_lock_loop is not loop:
        _ticker_cache_lock = asyncio.Lock()
        _ticker_cache_lock_loop = loop
    return _ticker_cache_lock


def _get_upbit_ohlcv_cache_service() -> Any:
    return importlib.import_module("app.services.upbit_ohlcv_cache")


def _safe_parse_retry_after(value: str | None) -> float:
    """Safely parse Retry-After header, returning 0 on failure."""
    if not value:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _get_upbit_get_api_key(api_path: str) -> str:
    normalized_path = str(api_path or "/unknown")
    if normalized_path.startswith("/v1/candles/"):
        return UPBIT_CANDLES_RATE_LIMIT_KEY
    return f"GET {normalized_path}"


def _get_upbit_rate_limit(api_key: str) -> tuple[int, float]:
    """Get rate limit for a specific Upbit API key, falling back to defaults."""
    if api_key == UPBIT_CANDLES_RATE_LIMIT_KEY:
        return 10, 1.0

    api_limits = settings.upbit_api_rate_limits
    if api_key in api_limits:
        limit_config = api_limits[api_key]
        rate = int(limit_config.get("rate", settings.upbit_rate_limit_rate))
        period = float(limit_config.get("period", settings.upbit_rate_limit_period))
        return rate, period
    if api_key not in _unmapped_rate_limit_keys_logged:
        logger.warning(
            "[upbit] Unmapped API rate limit for %s, using defaults (%s/%ss)",
            api_key,
            settings.upbit_rate_limit_rate,
            settings.upbit_rate_limit_period,
        )
        _unmapped_rate_limit_keys_logged.add(api_key)
    return settings.upbit_rate_limit_rate, settings.upbit_rate_limit_period


async def _retry_with_backoff(
    limiter: Any,
    send_fn: Any,
    *,
    url: str,
    max_retries: int | None = None,
    base_delay: float | None = None,
) -> Any:
    """Common retry-with-backoff loop for Upbit API requests.

    Parameters
    ----------
    limiter
        Rate limiter with an ``acquire`` coroutine.
    send_fn
        Zero-arg async callable returning ``httpx.Response``.
    url
        For logging only.
    max_retries / base_delay
        Override ``settings.api_rate_limit_retry_429_max`` /
        ``settings.api_rate_limit_retry_429_base_delay``.
    """
    if max_retries is None:
        max_retries = settings.api_rate_limit_retry_429_max
    if base_delay is None:
        base_delay = settings.api_rate_limit_retry_429_base_delay

    for attempt in range(max_retries + 1):
        await limiter.acquire(
            blocking_callback=lambda w: logger.warning(
                "[upbit] Rate limit wait: %.3fs (url=%s)", w, url
            )
        )

        try:
            response = await send_fn()

            if response.status_code == 429:
                retry_after = _safe_parse_retry_after(
                    response.headers.get("Retry-After")
                )
                wait_time = (
                    retry_after
                    if retry_after > 0
                    else base_delay * (2**attempt) + random.uniform(0, 0.1)
                )
                logger.warning(
                    "[upbit] 429 received, attempt %d/%d, waiting %.3fs (url=%s)",
                    attempt + 1,
                    max_retries + 1,
                    wait_time,
                    url,
                )
                await asyncio.sleep(wait_time)
                continue

            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < max_retries:
                wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                logger.warning(
                    "[upbit] HTTP 429, attempt %d/%d, waiting %.3fs (url=%s)",
                    attempt + 1,
                    max_retries + 1,
                    wait_time,
                    url,
                )
                await asyncio.sleep(wait_time)
                continue
            raise
        except httpx.RequestError as e:
            if attempt < max_retries:
                wait_time = base_delay * (2**attempt) + random.uniform(0, 0.1)
                logger.warning(
                    "[upbit] Request error: %s, attempt %d/%d, retrying in %.3fs (url=%s)",
                    e,
                    attempt + 1,
                    max_retries + 1,
                    wait_time,
                    url,
                )
                await asyncio.sleep(wait_time)
                continue
            raise

    raise RateLimitExceededError(f"Upbit rate limit retries exhausted for {url}")


async def _request_json(
    url: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    from urllib.parse import urlparse

    parsed_url = urlparse(url)
    api_path = parsed_url.path or "/unknown"
    api_key = _get_upbit_get_api_key(api_path)
    rate, period = _get_upbit_rate_limit(api_key)
    limiter = await get_limiter("upbit", api_key, rate=rate, period=period)

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(timeout=5) as cli:
            return await cli.get(url, params=params)

    return await _retry_with_backoff(limiter, send, url=url)


async def fetch_my_coins() -> list[dict[str, Any]]:
    return await _request_with_auth("GET", f"{UPBIT_REST}/accounts")


def parse_upbit_account_row(account: dict[str, Any]) -> dict[str, float]:
    """Parse a single ``/v1/accounts`` row → ``balance, locked, total_quantity, orderable_quantity, avg_buy_price``."""
    balance = float(account.get("balance", 0) or 0)
    locked = float(account.get("locked", 0) or 0)
    avg_buy_price = float(account.get("avg_buy_price", 0) or 0)
    return {
        "balance": balance,
        "locked": locked,
        "total_quantity": balance + locked,
        "orderable_quantity": balance,
        "avg_buy_price": avg_buy_price,
    }


async def fetch_krw_cash_summary() -> dict[str, float]:
    accounts = await fetch_my_coins()

    for account in accounts:
        if account.get("currency") == "KRW":
            parsed = parse_upbit_account_row(account)
            return {
                "balance": parsed["total_quantity"],
                "orderable": parsed["orderable_quantity"],
            }

    return {"balance": 0.0, "orderable": 0.0}


async def fetch_krw_orderable_balance() -> float:
    summary = await fetch_krw_cash_summary()
    return float(summary["orderable"])


async def check_krw_balance_sufficient(required_amount: float) -> tuple[bool, float]:
    """KRW 잔고가 충분한지 확인

    Parameters
    ----------
    required_amount : float
        필요한 금액

    Returns
    -------
    tuple[bool, float]
        (충분 여부, 현재 KRW 잔고)
    """
    current_balance = await fetch_krw_orderable_balance()
    is_sufficient = current_balance >= required_amount

    return is_sufficient, current_balance


def _normalize_upbit_interval(period: str) -> str:
    normalized = str(period or "").strip().lower()
    aliases = {
        "hour": "1h",
        "60m": "1h",
        "240m": "4h",
    }
    return aliases.get(normalized, normalized)


async def fetch_ohlcv(
    market: str = "KRW-BTC",
    days: int = 100,
    period: str = "day",
    end_date: datetime | None = None,
) -> pd.DataFrame:
    """최근 *days*개 OHLCV DataFrame 반환 (Upbit)."""
    normalized_period = _normalize_upbit_interval(period)
    request_count = min(max(int(days), 1), 200)

    if (
        normalized_period in {"day", "week", "month"}
        and settings.upbit_ohlcv_cache_enabled
    ):
        upbit_ohlcv_cache_service = _get_upbit_ohlcv_cache_service()
        cached = await upbit_ohlcv_cache_service.get_closed_candles(
            market,
            count=request_count,
            period=normalized_period,
            raw_fetcher=_fetch_ohlcv_raw,
        )
        if cached is not None:
            return cached

    raw = await _fetch_ohlcv_raw(
        market=market,
        days=request_count,
        period=normalized_period,
        end_date=end_date,
    )
    if normalized_period in {"day", "week", "month"}:
        return _filter_closed_buckets(raw, normalized_period)
    return raw


async def _fetch_candles_raw(
    market: str,
    count: int,
    interval: str,
    end_date: datetime | None = None,
) -> pd.DataFrame:
    normalized_interval = _normalize_upbit_interval(interval)
    endpoint = _INTERVAL_TO_ENDPOINT.get(normalized_interval)
    if endpoint is None:
        raise ValueError(f"period must be one of {list(_INTERVAL_TO_ENDPOINT.keys())}")

    request_count = min(max(int(count), 1), 200)
    url = f"{UPBIT_REST}/candles/{endpoint}"
    params: dict[str, Any] = {
        "market": market,
        "count": request_count,
    }
    if end_date is not None:
        params["to"] = end_date.strftime("%Y-%m-%dT%H:%M:%S")

    rows = await _request_json(url, params)
    is_minute_interval = endpoint.startswith("minutes/")

    if not rows:
        if is_minute_interval:
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
        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume", "value"]
        )

    frame = (
        pd.DataFrame(rows)
        .rename(
            columns={
                "candle_date_time_kst": "datetime",
                "opening_price": "open",
                "high_price": "high",
                "low_price": "low",
                "trade_price": "close",
                "candle_acc_trade_volume": "volume",
                "candle_acc_trade_price": "value",
            }
        )
        .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
    )

    if is_minute_interval:
        return (
            frame.assign(
                date=lambda d: d["datetime"].dt.date,
                time=lambda d: d["datetime"].dt.time,
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
            .sort_values("datetime")
            .reset_index(drop=True)
        )

    return (
        frame.assign(date=lambda d: d["datetime"].dt.date)
        .loc[:, ["date", "open", "high", "low", "close", "volume", "value"]]
        .sort_values("date")
        .reset_index(drop=True)
    )


async def _fetch_ohlcv_raw(
    market: str = "KRW-BTC",
    days: int = 100,
    period: str = "day",
    end_date: datetime | None = None,
) -> pd.DataFrame:
    if days > 200:
        raise ValueError("Upbit API는 최대 200개까지 요청 가능합니다.")
    normalized_period = _normalize_upbit_interval(period)
    return await _fetch_candles_raw(
        market=market,
        count=days,
        interval=normalized_period,
        end_date=end_date,
    )


def _filter_closed_buckets(
    df: pd.DataFrame,
    period: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df

    normalized_period = str(period or "").strip().lower()
    if normalized_period not in {"day", "week", "month"}:
        return df

    upbit_ohlcv_cache_service = _get_upbit_ohlcv_cache_service()
    last_closed_bucket = upbit_ohlcv_cache_service.get_last_closed_bucket_kst(
        normalized_period,
        now,
    )
    bucket_dates = pd.to_datetime(df["date"], errors="coerce").dt.date
    return (
        df.loc[bucket_dates <= last_closed_bucket]
        .sort_values("date")
        .reset_index(drop=True)
    )


async def fetch_price(market: str = "KRW-BTC") -> pd.DataFrame:
    """실시간 현재가 1행 DataFrame 반환 (Upbit)

    반환 컬럼: date · time · open · high · low · close · volume · value
    (open/high/low 는 24시간 기준, value=24h 누적 거래대금)
    """
    url = f"{UPBIT_REST}/ticker"
    params = {"markets": market}
    rows = await _request_json(url, params)

    if not rows:
        raise ValueError(f"마켓 {market}에 대한 데이터를 찾을 수 없습니다.")

    row = rows[0]

    # 현재 시간을 KST로 설정
    now = datetime.now(UTC).replace(tzinfo=UTC)
    kst_time = now.astimezone(timezone(timedelta(hours=9)))

    df = pd.DataFrame(
        [
            {
                "date": kst_time.date(),
                "time": kst_time.time(),
                "open": row["opening_price"],
                "high": row["high_price"],
                "low": row["low_price"],
                "close": row["trade_price"],
                "volume": row["acc_trade_volume_24h"],
                "value": row["acc_trade_price_24h"],
            }
        ]
    )

    return df


async def fetch_minute_candles(
    market: str = "KRW-BTC", unit: int = 1, count: int = 200
) -> pd.DataFrame:
    """분봉 캔들 데이터를 가져오는 메서드 (Upbit)

    Parameters
    ----------
    market : str, default "KRW-BTC"
        업비트 마켓코드 (예: "KRW-BTC", "USDT-ETH")
    unit : int, default 1
        분봉 단위 (1, 3, 5, 10, 15, 30, 60, 240)
    count : int, default 200
        가져올 캔들 수 (최대 200)

    Returns
    -------
    pd.DataFrame
        컬럼: datetime, open, high, low, close, volume, value
    """
    if unit not in [1, 3, 5, 10, 15, 30, 60, 240]:
        raise ValueError("unit은 1, 3, 5, 10, 15, 30, 60, 240 중 하나여야 합니다.")

    if count > 200:
        raise ValueError("Upbit 분봉 API는 최대 200개까지 요청 가능합니다.")

    return await _fetch_candles_raw(
        market=market,
        count=count,
        interval=f"{unit}m",
    )


async def fetch_hourly_candles(
    market: str = "KRW-BTC", count: int = 24
) -> pd.DataFrame:
    """60분 캔들 데이터를 가져오는 편의 메서드"""
    return await fetch_minute_candles(market, unit=60, count=count)


async def fetch_5min_candles(market: str = "KRW-BTC", count: int = 24) -> pd.DataFrame:
    """5분 캔들 데이터를 가져오는 편의 메서드"""
    return await fetch_minute_candles(market, unit=5, count=count)


async def fetch_1min_candles(market: str = "KRW-BTC", count: int = 20) -> pd.DataFrame:
    """1분 캔들 데이터를 가져오는 편의 메서드"""
    return await fetch_minute_candles(market, unit=1, count=count)


async def fetch_fundamental_info(market: str = "KRW-BTC") -> dict[str, Any]:
    """
    암호화폐의 기본 정보를 가져와 딕셔너리로 반환합니다.
    :param market: 업비트 마켓코드 (예: "KRW-BTC", "USDT-ETH")
    :return: 기본 정보 딕셔너리
    """
    url = f"{UPBIT_REST}/ticker"
    rows = await _request_json(url, {"markets": market})
    if not rows:
        raise ValueError(f"시장 {market} 반환 데이터 없음")

    t = rows[0]

    # 기본 정보 구성
    fundamental_data = {
        "마켓코드": t.get("market"),
        "현재가": t.get("trade_price"),
        "24시간변동": t.get("signed_change_price"),
        "24시간변동률": t.get("signed_change_rate"),
        "24시간고가": t.get("high_price"),
        "24시간저가": t.get("low_price"),
        "24시간거래량": t.get("acc_trade_volume_24h"),
        "24시간거래대금": t.get("acc_trade_price_24h"),
        "최고가": t.get("highest_52_week_price"),
        "최저가": t.get("lowest_52_week_price"),
        "최고가대비": t.get("highest_52_week_ratio"),
        "최저가대비": t.get("lowest_52_week_ratio"),
    }

    # None이 아닌 값만 반환
    return {k: v for k, v in fundamental_data.items() if v is not None}


async def fetch_all_market_codes(
    fiat: str | None = "KRW",
    include_details: bool = False,
) -> list[str] | list[dict[str, Any]]:
    """
    업비트에서 거래 가능한 모든 마켓 코드를 조회합니다.
    :param fiat: 조회할 화폐 시장 (예: "KRW", "USDT"). None이면 전체 시장 반환
    :param include_details: True면 상세 정보(dict) 리스트를 반환
    :return: 마켓 코드 리스트 또는 상세 정보 리스트
    """
    url = f"{UPBIT_REST}/market/all"
    params = {"isDetails": "true" if include_details else "false"}
    all_markets = await _request_json(url, params)

    if fiat is None:
        if include_details:
            return all_markets
        return [m["market"] for m in all_markets]

    fiat_prefix = str(fiat).upper()
    filtered = [m for m in all_markets if m["market"].startswith(fiat_prefix)]
    if include_details:
        return filtered
    # 지정된 fiat 시장의 마켓 코드만 필터링하여 반환
    return [m["market"] for m in filtered]


async def fetch_all_market_details(fiat: str | None = "KRW") -> list[dict[str, Any]]:
    markets = await fetch_all_market_codes(fiat=fiat, include_details=True)
    return [m for m in markets if isinstance(m, dict)]


async def fetch_top_traded_coins(fiat: str = "KRW") -> list[dict[str, Any]]:
    """
    지정된 fiat 시장의 모든 코인을 24시간 거래대금 순으로 정렬하여 반환합니다.
    """
    normalized_fiat = str(fiat or "KRW").strip().upper()
    market_codes = sorted(
        await get_active_upbit_markets(quote_currency=normalized_fiat)
    )
    if not market_codes:
        raise ValueError(
            "upbit_symbol_universe has no active markets for "
            f"fiat={normalized_fiat}. "
            "Sync required: uv run python scripts/sync_upbit_symbol_universe.py"
        )

    # 2. 모든 마켓 코드의 현재가 정보를 한 번의 API 호출로 가져옵니다.
    all_tickers_info = await fetch_multiple_tickers(market_codes)

    # 3. 24시간 누적 거래대금(acc_trade_price_24h) 기준으로 내림차순 정렬합니다.
    sorted_coins = sorted(
        all_tickers_info, key=lambda x: x.get("acc_trade_price_24h", 0), reverse=True
    )

    return sorted_coins


async def fetch_multiple_tickers(market_codes: list[str]) -> list[dict[str, Any]]:
    """
    여러 마켓의 현재가 정보를 한 번에 조회합니다.

    Parameters
    ----------
    market_codes : list[str]
        조회할 마켓 코드 리스트 (예: ["KRW-BTC", "KRW-ETH"])

    Returns
    -------
    list[dict]
        각 마켓의 현재가 정보 리스트
        각 항목은 다음과 같은 정보를 포함:
        - market: 마켓명 (예: "KRW-BTC")
        - trade_price: 현재가
        - signed_change_price: 24시간 변동 금액
        - signed_change_rate: 24시간 변동률
        - high_price: 24시간 최고가
        - low_price: 24시간 최저가
        - acc_trade_volume_24h: 24시간 누적 거래량
        - acc_trade_price_24h: 24시간 누적 거래대금
    """
    normalized_codes = _normalize_market_codes(market_codes)
    if not normalized_codes:
        return []

    tickers: list[dict[str, Any]] = []
    for offset in range(0, len(normalized_codes), _UPBIT_TICKER_BATCH_SIZE):
        batch_codes = normalized_codes[offset : offset + _UPBIT_TICKER_BATCH_SIZE]
        query = urlencode(
            {"markets": ",".join(batch_codes)},
            quote_via=quote,
            safe=",",
        )
        url = f"{UPBIT_REST}/ticker?{query}"
        try:
            batch_tickers = await _request_json(url)
            tickers.extend(batch_tickers)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Some market codes may be invalid/delisted
                logger.warning(
                    "[upbit] Batch ticker request returned 404 for markets: %s. "
                    "Skipping batch and continuing with others.",
                    ", ".join(batch_codes[:5])
                    + ("..." if len(batch_codes) > 5 else ""),
                )
                continue
            raise

    return tickers


async def fetch_multiple_current_prices(
    market_codes: list[str],
    use_cache: bool = True,
) -> dict[str, float]:
    """
    여러 마켓의 현재가만 간단히 조회하여 딕셔너리로 반환합니다.

    Parameters
    ----------
    market_codes : list[str]
        조회할 마켓 코드 리스트 (예: ["KRW-BTC", "KRW-ETH"])
    use_cache : bool, default=True
        True이면 짧은 TTL의 프로세스 로컬 캐시를 사용하고,
        False이면 항상 Upbit에서 현재가를 새로 조회합니다.

    Returns
    -------
    dict[str, float]
        마켓별 현재가 딕셔너리 (예: {"KRW-BTC": 95000000, "KRW-ETH": 4400000})
    """
    return await fetch_multiple_current_prices_cached(
        market_codes,
        use_cache=use_cache,
    )


def _normalize_market_codes(market_codes: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []

    for market_code in market_codes:
        code = str(market_code)
        if code in seen:
            continue
        seen.add(code)
        normalized.append(code)

    return normalized


async def _fetch_multiple_current_prices_raw(
    market_codes: list[str],
) -> dict[str, float]:
    tickers_data = await fetch_multiple_tickers(market_codes)

    prices: dict[str, float] = {}
    for item in tickers_data:
        market = item.get("market")
        trade_price = item.get("trade_price")
        if not isinstance(market, str) or trade_price is None:
            continue
        prices[market] = float(trade_price)

    return prices


async def _fetch_and_cache_missing_tickers(
    market_codes: list[str],
    ttl_seconds: float,
) -> dict[str, float]:
    try:
        fresh_prices = await _fetch_multiple_current_prices_raw(market_codes)
        expires_at = time.monotonic() + max(float(ttl_seconds), 0.0)
        if fresh_prices:
            lock = _get_ticker_cache_lock()
            async with lock:
                for market_code, price in fresh_prices.items():
                    _ticker_price_cache[market_code] = (price, expires_at)
        return fresh_prices
    finally:
        lock = _get_ticker_cache_lock()
        async with lock:
            current_task = asyncio.current_task()
            for market_code in market_codes:
                if _ticker_inflight_symbol_tasks.get(market_code) is current_task:
                    _ticker_inflight_symbol_tasks.pop(market_code, None)


async def fetch_multiple_current_prices_cached(
    market_codes: list[str],
    ttl_seconds: float = 2.0,
    use_cache: bool = True,
) -> dict[str, float]:
    normalized_codes = _normalize_market_codes(market_codes)
    if not normalized_codes:
        return {}

    if not use_cache:
        return await _fetch_multiple_current_prices_raw(normalized_codes)

    lock = _get_ticker_cache_lock()
    now = time.monotonic()
    cached_prices: dict[str, float] = {}
    missing_codes: list[str] = []
    inflight_tasks_by_code: dict[str, asyncio.Task[dict[str, float]]] = {}
    tasks_to_await: list[asyncio.Task[dict[str, float]]] = []
    seen_tasks: set[asyncio.Task[dict[str, float]]] = set()

    async with lock:
        for market_code in normalized_codes:
            cached_entry = _ticker_price_cache.get(market_code)
            if cached_entry is None:
                missing_codes.append(market_code)
                continue

            price, expires_at = cached_entry
            if expires_at > now:
                cached_prices[market_code] = price
            else:
                _ticker_price_cache.pop(market_code, None)
                missing_codes.append(market_code)

        if missing_codes:
            codes_to_fetch: list[str] = []
            for market_code in missing_codes:
                inflight_task = _ticker_inflight_symbol_tasks.get(market_code)
                if inflight_task is not None:
                    inflight_tasks_by_code[market_code] = inflight_task
                    if inflight_task not in seen_tasks:
                        seen_tasks.add(inflight_task)
                        tasks_to_await.append(inflight_task)
                    continue
                codes_to_fetch.append(market_code)

            if codes_to_fetch:
                new_task = asyncio.create_task(
                    _fetch_and_cache_missing_tickers(
                        codes_to_fetch,
                        ttl_seconds,
                    )
                )
                seen_tasks.add(new_task)
                tasks_to_await.append(new_task)
                for market_code in codes_to_fetch:
                    _ticker_inflight_symbol_tasks[market_code] = new_task
                    inflight_tasks_by_code[market_code] = new_task

    fresh_prices: dict[str, float] = {}
    if missing_codes and tasks_to_await:
        task_results = await asyncio.gather(*tasks_to_await, return_exceptions=False)
        merged_results: dict[asyncio.Task[dict[str, float]], dict[str, float]] = dict(
            zip(tasks_to_await, task_results, strict=False)
        )
        for market_code in missing_codes:
            task = inflight_tasks_by_code.get(market_code)
            if task is None:
                continue
            result_for_task = merged_results.get(task, {})
            if market_code in result_for_task:
                fresh_prices[market_code] = result_for_task[market_code]

    result: dict[str, float] = {}
    for market_code in normalized_codes:
        if market_code in fresh_prices:
            result[market_code] = fresh_prices[market_code]
        elif market_code in cached_prices:
            result[market_code] = cached_prices[market_code]

    return result


async def _request_with_auth(
    method: str,
    url: str,
    query_params: dict[str, Any] | None = None,
    body_params: dict[str, Any] | None = None,
) -> Any:
    import hashlib
    from urllib.parse import unquote, urlencode, urlparse

    parsed_url = urlparse(url)
    api_path = parsed_url.path or "/unknown"
    api_key = f"{method.upper()} {api_path}"

    rate, period = _get_upbit_rate_limit(api_key)
    limiter = await get_limiter("upbit", api_key, rate=rate, period=period)

    payload: dict[str, Any] = {
        "access_key": settings.upbit_access_key,
        "nonce": str(uuid.uuid4()),
    }

    if method.upper() in ["GET", "DELETE"] and query_params:
        query_string = unquote(urlencode(query_params, doseq=True))
        payload["query_hash"] = hashlib.sha512(query_string.encode()).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    elif method.upper() == "POST" and body_params:
        query_string = unquote(urlencode(body_params, doseq=True))
        payload["query_hash"] = hashlib.sha512(query_string.encode()).hexdigest()
        payload["query_hash_alg"] = "SHA512"

    jwt_token = jwt.encode(payload, settings.upbit_secret_key)
    authorize_token = f"Bearer {jwt_token}"
    headers: dict[str, str] = {"Authorization": authorize_token}

    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(timeout=10) as cli:
            if method.upper() == "GET":
                return await cli.get(url, headers=headers, params=query_params)
            elif method.upper() == "POST":
                return await cli.post(url, headers=headers, json=body_params)
            elif method.upper() == "DELETE":
                return await cli.delete(url, headers=headers, params=query_params)
            else:
                raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

    return await _retry_with_backoff(limiter, send, url=url)


# Re-export order functions for backward compatibility using lazy loading to avoid circular imports.
def __getattr__(name: str) -> Any:
    reexported = {
        "adjust_price_to_upbit_unit",
        "cancel_and_reorder",
        "cancel_orders",
        "fetch_closed_orders",
        "fetch_open_orders",
        "fetch_order_detail",
        "place_buy_order",
        "place_market_buy_order",
        "place_market_sell_order",
        "place_sell_order",
    }
    if name in reexported:
        from app.services.brokers.upbit import orders

        return getattr(orders, name)
    raise AttributeError(f"module {__name__} has no attribute {name}")


# --- 작은 데모 스크립트 (직접 실행 시) ----------------------------------------
if __name__ == "__main__":
    import asyncio
    import pprint

    async def demo():
        # # 기존 데모 코드
        # df = await fetch_ohlcv("KRW-BTC", 5)
        # pprint.pp(df)
        # now = await fetch_price("KRW-BTC")
        # print(now.T)
        try:
            print("--- 내 보유 자산 ---")
            my_coins = await fetch_my_coins()
            pprint.pp(my_coins)
        except httpx.HTTPStatusError as e:
            print(f"API 호출에 실패했습니다: {e.response.status_code}")
            print(f"응답 내용: {e.response.text}")
        except Exception as e:
            print(f"오류 발생: {e}")

    asyncio.run(demo())
