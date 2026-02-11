"""KRX (Korea Exchange) public API service.

This module provides functions to fetch:
- All Korean stocks (KOSPI/KOSDAQ)
- All Korean ETFs
- ETF category classification

Uses Redis caching with in-memory fallback and automatic trading date fallback.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import redis.asyncio as redis

from app.core.config import settings

from app.models.kospi200 import Kospi200Constituent
if TYPE_CHECKING:
    from redis.asyncio import Redis

# Constants
KRX_API_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_CACHE_TTL = 300  # 5 minutes
KRX_MAX_RETRY_DATES = 10  # Max days to search back for trading date
_MEMORY_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_MEMORY_CACHE_TTL = 300  # Same as Redis TTL

logger = logging.getLogger(__name__)


async def _get_redis_client() -> Redis:
    """Get Redis client for caching."""
    return redis.from_url(
        settings.get_redis_url(),
        decode_responses=True,
    )


def _parse_korean_number(value_str: str | None) -> int | float | None:
    """Parse Korean number formats.

    Handles formats like:
    - "1,234" → 1234
    - "-" → None
    - "" → None
    """
    if value_str is None:
        return None

    value_str = str(value_str).strip()
    if not value_str or value_str == "-":
        return None

    # Remove commas
    value_str = value_str.replace(",", "")

    try:
        # Try to parse as float first
        value = float(value_str)
        return int(value) if value.is_integer() else value
    except ValueError:
        return None


async def _fetch_krx_data(
    bld: str,
    mktId: str | None = None,
    trdDd: str | None = None,
    idxIndClssCd: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch data from KRX public API.

    Args:
        bld: Data type code (e.g., "dbms/MDC/STAT/standard/MDCSTAT01701" for ETFs)
        mktId: Market code (STK=KOSPI, KSQ=KOSDAQ)
        trdDd: Trading date in YYYYMMDD format
        idxIndClssCd: Index classification code for ETF category filtering

    Returns:
        List of dictionaries with KRX data
    """
    # KRX API requires POST with form data
    data: dict[str, str] = {"bld": bld}
    if mktId is not None:
        data["mktId"] = mktId
    if trdDd is not None:
        data["trdDd"] = trdDd
    if idxIndClssCd is not None:
        data["idxIndClssCd"] = idxIndClssCd

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(KRX_API_URL, data=data)
        response.raise_for_status()

        # KRX returns JSON with "OutBlock_1" or "output" key containing the data
        result = response.json()
        return result.get("OutBlock_1", []) or result.get("output", [])


async def _get_cache_key(suffix: str, date_str: str | None = None) -> str:
    """Generate cache key for KRX data."""
    if date_str:
        return f"krx:{suffix}:{date_str}"
    return f"krx:{suffix}"


async def _get_cached_data(cache_key: str) -> list[dict[str, Any]] | None:
    """Try to get data from Redis cache, then memory cache."""
    # Try Redis first
    try:
        redis_client = await _get_redis_client()
        cached = await redis_client.get(cache_key)
        if cached:
            logger.debug(f"Redis cache hit for {cache_key}")
            # Deserialize JSON if cached data
            try:
                return json.loads(cached)
            except (json.JSONDecodeError, TypeError):
                # Fallback for string data
                return cached
    except Exception as e:
        logger.debug(f"Redis cache miss for {cache_key}: {e}")

    # Try memory cache as fallback
    if cache_key in _MEMORY_CACHE:
        data, timestamp = _MEMORY_CACHE[cache_key]
        if datetime.now(UTC).timestamp() - timestamp < _MEMORY_CACHE_TTL:
            logger.debug(f"Memory cache hit for {cache_key}")
            return data
        else:
            del _MEMORY_CACHE[cache_key]

    return None


async def _set_cached_data(cache_key: str, data: list[dict[str, Any]]) -> None:
    """Set data in both Redis and memory cache."""
    # Serialize data as JSON for consistent storage
    json_data = json.dumps(data)

    # Set Redis cache
    try:
        redis_client = await _get_redis_client()
        await redis_client.setex(
            cache_key,
            KRX_CACHE_TTL,
            json_data,
        )
    except Exception as e:
        logger.debug(f"Failed to set Redis cache for {cache_key}: {e}")

    # Set memory cache
    _MEMORY_CACHE[cache_key] = (data, datetime.now(UTC).timestamp())


def _generate_date_candidates(
    trd_date: str | None = None, max_days: int = 10
) -> list[str]:
    """Generate date candidates for KRX API queries.

    Args:
        trd_date: Specific trading date in YYYYMMDD format (None for auto-detect)
        max_days: Maximum number of days to search back

    Returns:
        List of date strings in YYYYMMDD format, ordered by preference
    """
    # If specific date provided, return only that date
    if trd_date:
        return [trd_date]

    # Generate date candidates from today (KST) going back up to max_days
    today_kst = datetime.now(UTC) + timedelta(hours=9)
    candidates = []

    for day_offset in range(max_days):
        test_date = today_kst - timedelta(days=day_offset)
        date_str = test_date.strftime("%Y%m%d")

        # Skip weekends (Saturday=5, Sunday=6)
        weekday = test_date.weekday()
        if weekday >= 5:
            continue

        candidates.append(date_str)

    # Return candidates in reverse order (most recent first)
    return candidates


async def _get_recent_trading_date(trd_date: str | None = None) -> str:
    """Get recent trading date, falling back to previous days if needed.

    Args:
        trd_date: Preferred trading date in YYYYMMDD format

    Returns:
        Trading date string in YYYYMMDD format
    """
    candidates = _generate_date_candidates(trd_date, KRX_MAX_RETRY_DATES)
    return (
        candidates[0]
        if candidates
        else (datetime.now(UTC) + timedelta(hours=9)).strftime("%Y%m%d")
    )


async def fetch_stock_all(
    market: str = "STK",
    trd_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all stocks from KRX.

    Args:
        market: Market code - "STK" for KOSPI, "KSQ" for KOSDAQ
        trd_date: Trading date in YYYYMMDD format (None for auto-detect)

    Returns:
        List of stock dictionaries with keys:
        - code: Stock code (6-digit)
        - short_code: Short code
        - abbreviation: Abbreviation
        - name: Stock name
        - market: Market name
        - date: Trading date
        - close: Closing price
        - market_cap: Market cap
        - volume: Trading volume
        - value: Trading value
    """
    # Generate date candidates and try sequentially until first non-empty result
    date_candidates = _generate_date_candidates(trd_date, KRX_MAX_RETRY_DATES)

    for actual_date in date_candidates:
        # Build cache key
        cache_key = await _get_cache_key(f"stock:all:{market}", actual_date)

        # Try cache
        cached = await _get_cached_data(cache_key)
        if cached:
            logger.info(f"Cache hit for {market} on {actual_date}")
            return cached

        # Fetch from KRX API
        # Use STK/KSQ market codes for stock data
        logger.info(f"Fetching KRX stock data for market={market}, date={actual_date}")
        raw_data = await _fetch_krx_data(
            bld="dbms/MDC/STAT/standard/MDCSTAT01501",
            mktId=market,
            trdDd=actual_date,
        )

        if raw_data:
            # Normalize data
            stocks = []
            for item in raw_data:
                stock = {
                    "code": item.get("ISU_CD", "").strip(),
                    "short_code": item.get("ISU_SRT_CD", "").strip(),
                    "abbreviation": item.get("ISU_ABBRV", "").strip(),
                    "name": item.get("ISU_NM", "").strip(),
                    "market": item.get("MKT_NM", "").strip(),
                    "date": actual_date,
                    "close": _parse_korean_number(item.get("CLSPRC")),
                    "market_cap": _parse_korean_number(item.get("MKTCAP")),
                    "volume": _parse_korean_number(item.get("TRDVOL")),
                    "value": _parse_korean_number(item.get("TRDVAL")),
                }
                if stock["code"] and stock["name"]:
                    stocks.append(stock)

            # Cache result
            await _set_cached_data(cache_key, stocks)

            return stocks
        else:
            # Empty response, try next date
            logger.warning(
                f"Empty KRX response for {market} on {actual_date}, trying previous day"
            )
            continue

    # All dates exhausted
    logger.error(
        f"Failed to fetch {market} data after trying {len(date_candidates)} dates"
    )
    return []


async def fetch_etf_all(
    trd_date: str | None = None,
    idx_ind_clss_cd: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all ETFs from KRX.

    Args:
        trd_date: Trading date in YYYYMMDD format (None for auto-detect)
        idx_ind_clss_cd: Index classification code for category filtering

    Returns:
        List of ETF dictionaries with keys:
        - ISU_CD: ETF code
        - ISU_SRT_CD: Short code
        - ISU_ABBRV: Abbreviation
        - ISU_NM: ETF name
        - IDX_NM: Index name (tracking index)
        - IDX_IND_CLSS_CD: Index classification code
        - IDX_IND_CLSS_NM: Index classification name
        - TRD_DD: Trading date
        - CLSPRC: Closing price
        - MKTCAP: Market cap
        - TRDVOL: Trading volume
        - TRDVAL: Trading value
    """
    # Generate date candidates and try sequentially until first non-empty result
    date_candidates = _generate_date_candidates(trd_date, KRX_MAX_RETRY_DATES)

    for actual_date in date_candidates:
        # Build cache key
        cache_suffix = "etf:all"
        if idx_ind_clss_cd:
            cache_suffix += f":{idx_ind_clss_cd}"
        cache_key = await _get_cache_key(cache_suffix, actual_date)

        # Try cache
        cached = await _get_cached_data(cache_key)
        if cached:
            logger.info(f"Cache hit for ETFs on {actual_date}")
            return cached

        # Fetch from KRX API
        logger.info(
            f"Fetching KRX ETF data for date={actual_date}, idx_ind_clss_cd={idx_ind_clss_cd}"
        )
        raw_data = await _fetch_krx_data(
            bld="dbms/MDC/STAT/standard/MDCSTAT01701",
            trdDd=actual_date,
            idxIndClssCd=idx_ind_clss_cd,
        )

        if raw_data:
            # Normalize and data
            etfs = []
            for item in raw_data:
                etf = {
                    "code": item.get("ISU_CD", "").strip(),
                    "short_code": item.get("ISU_SRT_CD", "").strip(),
                    "abbreviation": item.get("ISU_ABBRV", "").strip(),
                    "name": item.get("ISU_NM", "").strip(),
                    "index_name": item.get("IDX_NM", "").strip(),
                    "index_class_code": item.get("IDX_IND_CLSS_CD", "").strip(),
                    "index_class_name": item.get("IDX_IND_CLSS_NM", "").strip(),
                    "date": actual_date,
                    "close": _parse_korean_number(item.get("CLSPRC")),
                    "market_cap": _parse_korean_number(item.get("MKTCAP")),
                    "volume": _parse_korean_number(item.get("TRDVOL")),
                    "value": _parse_korean_number(item.get("TRDVAL")),
                }
                if etf["code"] and etf["name"]:
                    etfs.append(etf)

            # Cache result
            await _set_cached_data(cache_key, etfs)

            return etfs
        else:
            # Empty response, try next date
            logger.warning(
                f"Empty KRX response for ETFs on {actual_date}, trying previous day"
            )
            continue

    # All dates exhausted
    logger.error(f"Failed to fetch ETF data after trying {len(date_candidates)} dates")
    return []


async def fetch_stock_all_cached(
    market: str = "STK",
    trd_date: str | None = None,
) -> list[dict[str, Any]]:
    """Wrapper for fetch_stock_all with automatic caching."""
    return await fetch_stock_all(market, trd_date)


async def fetch_etf_all_cached(
    trd_date: str | None = None,
    idx_ind_clss_cd: str | None = None,
) -> list[dict[str, Any]]:
    """Wrapper for fetch_etf_all with automatic caching."""
    return await fetch_etf_all(trd_date, idx_ind_clss_cd)


def classify_etf_category(
    etf_name: str,
    tracking_index: str,
) -> list[str]:
    """Classify ETF category based on name and tracking index.

    Args:
        etf_name: ETF name (e.g., "KB STAR 미국S&P500")
        tracking_index: Tracking index name (e.g., "S&P 500")

    Returns:
        List of category tags (e.g., ["미국주식", "S&P500", "인덱스"])

    Categories include:
        - 시장별: "국내주식", "미국주식", "글로벌", "선진국", "신흥국"
        - 테마별: "IT", "바이오", "자동차", "반도체", "금융", "배당성장"
        - 인덱스별: "S&P500", "나스닥100", "다우존스", "KOSPI200", "MSCI"
        - 전략별: "배당성장", "모멘텀", "밸류", "저변동성"
    """
    categories = []

    # Market/Region classification
    etf_name_lower = etf_name.lower()
    index_lower = tracking_index.lower()

    if any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["미국", "usa", "us", "s&p", "나스닥", "다우"]
    ):
        categories.append("미국주식")
    elif any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["글로벌", "world", "global", "all country"]
    ):
        categories.append("글로벌")
    elif any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["선진국", "msci world", "developed"]
    ):
        categories.append("선진국")
    elif any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["신흥국", "emerging", "brics"]
    ):
        categories.append("신흥국")
    else:
        categories.append("국내주식")

    # Theme/Industry classification
    if any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["it", "기술", "tech", "소프트웨어", "반도체", "반도"]
    ):
        categories.append("IT")
    elif any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["바이오", "bio", "헬스케어", "healthcare"]
    ):
        categories.append("바이오")
    elif any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["자동차", "car", "auto", "ev", "전기차"]
    ):
        categories.append("자동차")
    elif any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["금융", "finance", "bank", "은행", "증권"]
    ):
        categories.append("금융")
    elif any(
        keyword in etf_name_lower or keyword in index_lower
        for keyword in ["배당", "dividend", "income"]
    ):
        categories.append("배당성장")

    # Index type classification
    if "s&p" in index_lower:
        categories.append("S&P500")
    elif "nasdaq" in index_lower or "나스닥" in index_lower:
        categories.append("나스닥100")
    elif "dow" in index_lower or "다우" in index_lower:
        categories.append("다우존스")
    elif "kospi" in index_lower or "코스피" in index_lower:
        categories.append("KOSPI200")
    elif "msci" in index_lower:
        categories.append("MSCI")

    # Strategy classification
    if "밸류" in etf_name_lower or "value" in etf_name_lower:
        categories.append("밸류")
    elif "모멘텀" in etf_name_lower or "momentum" in etf_name_lower:
        categories.append("모멘텀")
    elif "저변동" in etf_name_lower or "low volatility" in index_lower:
        categories.append("저변동성")

    # If no categories found, add general tag
    if not categories:
        categories.append("기타")

    return categories


class KRXMarketDataService:
    """KRX 마켓 데이터 서비스"""

    KRX_DOWNLOAD_URL = "http://data.krx.co.kr/comm/fileDn/DownloadOfFileService"

    async def fetch_kospi200_constituents(self) -> list[dict]:
        """KRX에서 KOSPI200 구성종목 데이터를 가져옵니다.

        Returns:
            List[Dict]: 종목 정보 목록
            {
                "종목코드": "005930",
                "종목명": "삼성전자",
                "시가총액": 1234567890,
                "지수비중": 1.23,
                "섹터": "전기전자"
            }
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {
                "mktId": "STK",
                "trdDd": datetime.now().strftime("%Y%m%d"),
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            }

            try:
                response = await client.post(
                    self.KRX_DOWNLOAD_URL,
                    data=params,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0",
                    },
                )

                if response.status_code == 200:
                    content = response.text
                    return self._parse_krx_csv_content(content)
                else:
                    logger.error(
                        "KRX API 호출 실패: status_code=%d", response.status_code
                    )
                    return []

            except Exception as e:
                logger.error("KRX 데이터 수집 중 오류 발생: %s", e)
                return []

    def _parse_krx_csv_content(self, content: str) -> list[dict]:
        """KRX에서 반환된 CSV 형식의 데이터를 파싱합니다."""
        if not content or len(content) < 100:
            logger.warning("KRX 응답 데이터가 비어있거나 너무 짧습니다")
            return []

        lines = content.split("\n")
        if len(lines) < 2:
            return []

        headers = lines[0].split("\t")
        constituents = []

        for line in lines[1:]:
            if not line.strip():
                continue

            values = line.split("\t")
            if len(values) < len(headers):
                continue

            row = dict(zip(headers, values, strict=False))

            # 종목코드 형식: 'KR70684000' 또는 '005930'
            stock_code = row.get("종목코드", "")
            if stock_code.startswith("KR7"):
                stock_code = stock_code[4:]

            # 시가총액 파싱 (숫자로 변환)
            market_cap_str = row.get("시가총액", "0").replace(",", "")
            try:
                market_cap = float(market_cap_str) if market_cap_str else 0.0
            except ValueError:
                market_cap = 0.0

            # 지수비중 파싱
            weight_str = row.get("지수비중", "0").replace(",", "")
            try:
                weight = float(weight_str) if weight_str else 0.0
            except ValueError:
                weight = 0.0

            constituents.append(
                {
                    "stock_code": stock_code,
                    "stock_name": row.get("종목명", ""),
                    "market_cap": market_cap,
                    "weight": weight,
                    "sector": row.get("섹터", ""),
                }
            )

        return constituents


class Kospi200Service:
    """KOSPI200 구성종목 관리 서비스"""

    def __init__(self, db_session):
        self.db = db_session

    async def get_all_constituents(
        self, active_only: bool = True
    ) -> list[Kospi200Constituent]:
        """KOSPI200 구성종목 목록 조회"""
        from sqlalchemy import select

        query = select(Kospi200Constituent)

        if active_only:
            query = query.where(Kospi200Constituent.is_active == True)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_constituent_by_code(
        self, stock_code: str
    ) -> Kospi200Constituent | None:
        """종목코드로 구성종목 조회"""
        from sqlalchemy import select

        query = select(Kospi200Constituent).where(
            Kospi200Constituent.stock_code == stock_code
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def update_constituents(
        self, constituents_data: list[dict]
    ) -> dict[str, int]:
        """KOSPI200 구성종목 정보를 업데이트합니다.

        Args:
            constituents_data: KRX에서 가져온 종목 데이터 목록

        Returns:
            Dict: 업데이트 결과 통계
            {
                "added": 10,
                "updated": 180,
                "removed": 5
            }
        """
        from datetime import datetime as dt

        from sqlalchemy import select, update

        added = 0
        updated = 0
        removed = 0

        now = dt.now()

        # 현재 DB에 있는 모든 구성종목의 코드를 가져옴
        existing_codes_query = select(Kospi200Constituent.stock_code).where(
            Kospi200Constituent.is_active == True
        )
        existing_codes_result = await self.db.execute(existing_codes_query)
        existing_codes = {row[0] for row in existing_codes_result.fetchall()}

        new_codes = set()

        for data in constituents_data:
            stock_code = data["stock_code"]
            new_codes.add(stock_code)

            existing = await self.get_constituent_by_code(stock_code)

            if existing:
                # 기존 종목 업데이트
                if existing.is_active:
                    await self.db.execute(
                        update(Kospi200Constituent)
                        .where(Kospi200Constituent.id == existing.id)
                        .values(
                            stock_name=data["stock_name"],
                            market_cap=data["market_cap"],
                            weight=data["weight"],
                            sector=data["sector"],
                            updated_at=now,
                        )
                    )
                    updated += 1
                else:
                    # 이전에 제외되었던 종목이 다시 포함됨
                    await self.db.execute(
                        update(Kospi200Constituent)
                        .where(Kospi200Constituent.id == existing.id)
                        .values(
                            stock_name=data["stock_name"],
                            market_cap=data["market_cap"],
                            weight=data["weight"],
                            sector=data["sector"],
                            is_active=True,
                            removed_at=None,
                            added_at=now,
                            updated_at=now,
                        )
                    )
                    added += 1
            else:
                # 새로운 종목 추가
                new_constituent = Kospi200Constituent(
                    stock_code=stock_code,
                    stock_name=data["stock_name"],
                    market_cap=data["market_cap"],
                    weight=data["weight"],
                    sector=data["sector"],
                    is_active=True,
                    added_at=now,
                )
                self.db.add(new_constituent)
                added += 1

        # KRX 목록에 없는 종목들은 구성종목에서 제외 처리
        removed_codes = existing_codes - new_codes
        if removed_codes:
            await self.db.execute(
                update(Kospi200Constituent)
                .where(Kospi200Constituent.stock_code.in_(removed_codes))
                .values(is_active=False, removed_at=now, updated_at=now)
            )
            removed = len(removed_codes)

        await self.db.commit()

        logger.info(
            "KOSPI200 구성종목 업데이트 완료: 추가=%d, 업데이트=%d, 제외=%d",
            added,
            updated,
            removed,
        )

        return {"added": added, "updated": updated, "removed": removed}