"""Tests for KRX service caching and fallback logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import krx
from app.services.krx import KRXSessionManager


def _mock_krx_settings(
    member_id: str = "testuser", credential: str = "testpass"
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.krx_member_id = member_id
    mock_settings.krx_password = credential
    return mock_settings


class TestKRXCaching:
    """Test KRX Redis caching and in-memory fallback."""

    @pytest.mark.asyncio
    async def test_redis_cache_hit(self, monkeypatch):
        """Test Redis cache hit returns cached data."""
        mock_cached_data = [
            {"code": "005930", "name": "삼성전자", "market_cap": 4800000}
        ]

        async def mock_get_cached_data(cache_key):
            return mock_cached_data

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)

        # Mock _fetch_krx_data to ensure it's not called
        fetch_called = False

        async def mock_fetch_krx_data(**kwargs):
            nonlocal fetch_called
            fetch_called = True
            return []

        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        result = await krx.fetch_stock_all(market="STK")

        assert result == mock_cached_data
        assert not fetch_called, "Should not call API when cache hit"

    @pytest.mark.asyncio
    async def test_redis_cache_miss_fetches_api(self, monkeypatch):
        """Test Redis cache miss triggers API fetch."""
        mock_api_data = [
            {
                "ISU_CD": "005930",
                "ISU_SRT_CD": "005930",
                "ISU_ABBRV": "삼성전자",
                "ISU_NM": "삼성전자",
                "MKT_NM": "KOSPI",
                "TDD_CLSPRC": "80,000",
                "MKTCAP": "480,000,000,000,000",  # 원 단위 (with money=1)
                "ACC_TRDVOL": "10,000,000",
                "ACC_TRDVAL": "800,000,000,000",
            }
        ]

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_stock_all(market="STK")

        assert len(result) == 1
        assert result[0]["code"] == "005930"
        assert result[0]["name"] == "삼성전자"
        # Verify market_cap is converted to 억원 (원 / 1억)
        assert (
            result[0]["market_cap"] == 4800000
        )  # 480,000,000,000,000 / 1억 = 4,800,000억원

    @pytest.mark.asyncio
    async def test_empty_response_fallback_to_previous_date(self, monkeypatch):
        """Test empty API response triggers fallback to previous trading date."""
        call_dates = []

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(bld, mktId, trdDd, **kwargs):
            call_dates.append(trdDd)
            # First call returns empty, second call returns data
            if len(call_dates) == 1:
                return []
            else:
                return [
                    {
                        "ISU_CD": "005930",
                        "ISU_SRT_CD": "005930",
                        "ISU_ABBRV": "삼성전자",
                        "ISU_NM": "삼성전자",
                        "MKT_NM": "KOSPI",
                        "CLSPRC": "80000",
                        "MKTCAP": "480000000",
                        "TRDVOL": "10000000",
                        "TRDVAL": "800000000000",
                    }
                ]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_stock_all(market="STK")

        assert len(call_dates) == 2, "Should try multiple dates"
        assert len(result) == 1
        assert result[0]["code"] == "005930"

    @pytest.mark.asyncio
    async def test_cache_key_format(self, monkeypatch):
        """Test cache key format includes date and market."""
        captured_keys = []

        async def mock_get_cached_data(cache_key):
            captured_keys.append(cache_key)
            return None

        async def mock_fetch_krx_data(**kwargs):
            return [
                {
                    "ISU_CD": "005930",
                    "ISU_SRT_CD": "005930",
                    "ISU_ABBRV": "삼성전자",
                    "ISU_NM": "삼성전자",
                    "MKT_NM": "KOSPI",
                    "CLSPRC": "80000",
                    "MKTCAP": "480000000",
                    "TRDVOL": "10000000",
                    "TRDVAL": "800000000000",
                }
            ]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        await krx.fetch_stock_all(market="STK")

        assert len(captured_keys) > 0
        # Key format should be "krx:stock:all:STK:{date}"
        key = captured_keys[0]
        assert key.startswith("krx:stock:all:STK:"), f"Invalid cache key format: {key}"


class TestKRXETFCaching:
    """Test KRX ETF caching."""

    @pytest.mark.asyncio
    async def test_etf_cache_key_format(self, monkeypatch):
        """Test ETF cache key format: krx:etf:all:{date}"""
        captured_keys = []

        async def mock_get_cached_data(cache_key):
            captured_keys.append(cache_key)
            return None

        async def mock_fetch_krx_data(**kwargs):
            return [
                {
                    "ISU_CD": "069500",
                    "ISU_SRT_CD": "069500",
                    "ISU_ABBRV": "KODEX 200",
                    "ISU_NM": "KODEX 200",
                    "IDX_NM": "KOSPI 200",
                    "IDX_IND_CLSS_CD": "01",
                    "IDX_IND_CLSS_NM": "주식",
                    "CLSPRC": "45000",
                    "MKTCAP": "4500000",
                    "TRDVOL": "1000000",
                    "TRDVAL": "45000000000",
                }
            ]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        await krx.fetch_etf_all()

        assert len(captured_keys) > 0
        key = captured_keys[0]
        # Key format should be "krx:etf:all:{date}"
        assert key.startswith("krx:etf:all:"), f"Invalid ETF cache key format: {key}"

    @pytest.mark.asyncio
    async def test_etf_market_cap_conversion(self, monkeypatch):
        """Test ETF market_cap is converted to 억원."""

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return [
                {
                    "ISU_CD": "069500",
                    "ISU_SRT_CD": "069500",
                    "ISU_ABBRV": "KODEX 200",
                    "ISU_NM": "KODEX 200",
                    "IDX_IND_NM": "KOSPI 200",
                    "IDX_IND_CLSS_CD": "01",
                    "IDX_IND_CLSS_NM": "주식",
                    "TDD_CLSPRC": "45,000",
                    "MKTCAP": "4,500,000,000,000",  # 원 단위 (with money=1)
                    "ACC_TRDVOL": "1,000,000",
                    "ACC_TRDVAL": "45,000,000,000",
                }
            ]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_etf_all()

        assert len(result) == 1
        # 4,500,000,000,000 / 1억 = 45,000억원
        assert result[0]["market_cap"] == 45000


class TestKRXETFCategoryClassification:
    """Test ETF category classification."""

    def test_classify_etf_category_semiconductor(self):
        """Test ETF category classification for semiconductor."""
        categories = krx.classify_etf_category("KODEX 반도체", "Wise 반도체지수")

        assert "반도체" in categories

    def test_classify_etf_category_us_stocks(self):
        """Test ETF category classification for US stocks."""
        categories = krx.classify_etf_category("KB STAR 미국S&P500", "S&P 500")

        assert "미국주식" in categories

    def test_classify_etf_category_ai(self):
        """Test ETF category classification for AI."""
        categories = krx.classify_etf_category("KODEX AI", "AI 테마지수")

        assert "AI" in categories

    def test_classify_etf_category_dividend(self):
        """Test ETF category classification for dividend."""
        categories = krx.classify_etf_category("TIGER 배당성장", "배당성장지수")

        assert "배당" in categories

    def test_classify_etf_category_bonds(self):
        """Test ETF category classification for bonds."""
        categories = krx.classify_etf_category("KODEX 국채", "국고채 지수")
        assert "채권" in categories


class TestKRXChangeRate:
    """Test KRX change rate parsing with sign handling."""

    @pytest.mark.asyncio
    async def test_parse_change_rate_positive(self, monkeypatch):
        """Test parsing positive change rate (FLUC_TP_CD=1)."""
        mock_api_data = [
            {
                "ISU_CD": "005930",
                "ISU_SRT_CD": "005930",
                "ISU_ABBRV": "삼성전자",
                "ISU_NM": "삼성전자",
                "MKT_NM": "KOSPI",
                "CLSPRC": "80000",
                "MKTCAP": "480000000000000",
                "ACC_TRDVOL": "10000000",
                "ACC_TRDVAL": "800000000000",
                "FLUC_RT": "2.5",
                "CMPPREVDD_PRC": "2000",
                "FLUC_TP_CD": "1",
            }
        ]

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_stock_all(market="STK")

        assert len(result) == 1
        assert result[0]["change_rate"] == 2.5
        assert result[0]["change_price"] == 2000

    @pytest.mark.asyncio
    async def test_parse_change_rate_negative(self, monkeypatch):
        """Test parsing negative change rate (FLUC_TP_CD=2)."""
        mock_api_data = [
            {
                "ISU_CD": "000660",
                "ISU_SRT_CD": "000660",
                "ISU_ABBRV": "SK하이닉스",
                "ISU_NM": "SK하이닉스",
                "MKT_NM": "KOSPI",
                "CLSPRC": "150000",
                "MKTCAP": "150000000000000",
                "ACC_TRDVOL": "5000000",
                "ACC_TRDVAL": "75000000000000",
                "FLUC_RT": "1.2",
                "CMPPREVDD_PRC": "1800",
                "FLUC_TP_CD": "2",
            }
        ]

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_stock_all(market="STK")

        assert len(result) == 1
        assert result[0]["change_rate"] == -1.2
        assert result[0]["change_price"] == -1800

    @pytest.mark.asyncio
    async def test_parse_change_rate_unchanged(self, monkeypatch):
        """Test parsing unchanged (FLUC_TP_CD=3)."""
        mock_api_data = [
            {
                "ISU_CD": "035420",
                "ISU_SRT_CD": "035420",
                "ISU_ABBRV": "삼성SDS",
                "ISU_NM": "삼성SDS",
                "MKT_NM": "KOSPI",
                "CLSPRC": "325000",
                "MKTCAP": "100000000000000",
                "ACC_TRDVOL": "100000",
                "ACC_TRDVAL": "32500000000000",
                "FLUC_RT": "0",
                "CMPPREVDD_PRC": "0",
                "FLUC_TP_CD": "3",
            }
        ]

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_stock_all(market="STK")

        assert len(result) == 1
        assert result[0]["change_rate"] == 0.0
        assert result[0]["change_price"] == 0.0

    @pytest.mark.asyncio
    async def test_etf_parse_change_rate_positive(self, monkeypatch):
        """Test parsing ETF positive change rate."""
        mock_api_data = [
            {
                "ISU_CD": "069500",
                "ISU_SRT_CD": "069500",
                "ISU_ABBRV": "KODEX 200",
                "ISU_NM": "KODEX 200",
                "CLSPRC": "45000",
                "MKTCAP": "45000000000",
                "ACC_TRDVOL": "1000000",
                "ACC_TRDVAL": "45000000000",
                "FLUC_RT": "1.5",
                "CMPPREVDD_PRC": "700",
                "FLUC_TP_CD": "1",
            }
        ]

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_etf_all()

        assert len(result) == 1
        assert result[0]["change_rate"] == 1.5
        assert result[0]["change_price"] == 700


class TestKRXValuation:
    """Test KRX batch valuation endpoint."""

    @pytest.mark.asyncio
    async def test_valuation_parsing(self, monkeypatch):
        """Test valuation data parsing and DVD_YLD conversion."""
        mock_api_data = [
            {
                "ISU_SRT_CD": "005930",
                "PER": "12.5",
                "PBR": "1.2",
                "EPS": "6400",
                "BPS": "66000",
                "DVD_YLD": "2.56",
            },
            {
                "ISU_SRT_CD": "000660",
                "PER": "-",
                "PBR": "-",
                "EPS": "-",
                "BPS": "-",
                "DVD_YLD": "0",
            },
            {
                "ISU_SRT_CD": "035420",
                "PER": "0",
                "PBR": "0.8",
                "EPS": "12000",
                "BPS": "80000",
                "DVD_YLD": "3.5",
            },
        ]

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_valuation_all(market="ALL")

        assert len(result) == 3
        assert result["005930"]["per"] == 12.5
        assert result["005930"]["pbr"] == 1.2
        assert result["005930"]["eps"] == 6400
        assert result["005930"]["bps"] == 66000
        assert result["005930"]["dividend_yield"] == 0.0256
        assert result["000660"]["per"] is None
        assert result["000660"]["pbr"] is None
        assert result["035420"]["per"] is None
        assert result["035420"]["pbr"] == 0.8
        assert result["035420"]["eps"] == 12000
        assert result["035420"]["bps"] == 80000
        assert result["035420"]["dividend_yield"] == 0.035

    @pytest.mark.asyncio
    async def test_valuation_cache_key_format(self, monkeypatch):
        """Test valuation cache key format."""
        captured_keys = []

        async def mock_get_cached_data(cache_key):
            captured_keys.append(cache_key)
            return None

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        async def mock_fetch_krx_data(**kwargs):
            return []

        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        await krx.fetch_valuation_all(market="STK", trd_date="20250101")

        assert len(captured_keys) == 1
        key = captured_keys[0]
        assert key == "krx:valuation:STK:20250101"

    @pytest.mark.asyncio
    async def test_valuation_graceful_failure(self, monkeypatch):
        """Test graceful failure when valuation fetch fails."""

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return []

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_valuation_all(market="STK", trd_date="20250101")
        assert result == {}

    def test_classify_etf_category_battery(self):
        """Test ETF category classification for battery."""
        categories = krx.classify_etf_category("TIGER 2차전지", "2차전지 테마")

        assert "2차전지" in categories

    def test_classify_etf_category_defense(self):
        """Test ETF category classification for defense."""
        categories = krx.classify_etf_category("KODEX 방산", "방산 테마")

        assert "방산" in categories

    def test_classify_etf_category_gold(self):
        """Test ETF category classification for gold."""
        categories = krx.classify_etf_category("KODEX 골드선물", "금 선물")

        assert "금" in categories

    def test_classify_etf_category_oil(self):
        """Test ETF category classification for oil."""
        categories = krx.classify_etf_category("TIGER 원유선물", "WTI 원유")

        assert "원유" in categories

    def test_classify_etf_category_kospi200(self):
        """Test ETF category classification for KOSPI 200."""
        categories = krx.classify_etf_category("KODEX 200", "KOSPI 200")

        assert "코스피200" in categories

    def test_classify_etf_category_kosdaq150(self):
        """Test ETF category classification for KOSDAQ 150."""
        categories = krx.classify_etf_category("KODEX KOSDAQ 150", "KOSDAQ 150")

        assert "코스닥150" in categories

    def test_classify_etf_category_india(self):
        """Test ETF category classification for India."""
        categories = krx.classify_etf_category("TIGER 인도", "Nifty 50")

        assert "인도" in categories

    def test_classify_etf_category_japan(self):
        """Test ETF category classification for Japan."""
        categories = krx.classify_etf_category("KODEX 일본", "Nikkei 225")

        assert "일본" in categories

    def test_classify_etf_category_china(self):
        """Test ETF category classification for China."""
        categories = krx.classify_etf_category("TIGER 중국", "CSI 300")

        assert "중국" in categories

    def test_classify_etf_category_unknown_returns_기타(self):
        """Test ETF category classification returns '기타' for unknown."""
        categories = krx.classify_etf_category("알수없는ETF", "알수없는지수")

        assert "기타" in categories


class TestKRXFallbackLogic:
    """Test KRX fallback logic between Redis and memory cache."""

    @pytest.mark.asyncio
    async def test_redis_get_exception_falls_back_to_memory_cache(self, monkeypatch):
        """Test Redis get exception triggers memory cache fallback."""
        trading_date = "20260309"

        # Mock Redis client to raise exception
        class MockRedisClient:
            async def get(self, key):
                raise ConnectionError("Simulated Redis connection error")

            async def setex(self, key, ttl, value):
                raise ConnectionError("Simulated Redis connection error")

        async def mock_get_redis_client():
            return MockRedisClient()

        monkeypatch.setattr(krx, "_get_redis_client", mock_get_redis_client)

        cache_key = f"krx:stock:all:STK:{trading_date}"
        krx._MEMORY_CACHE.pop(cache_key, None)

        # Prepare data for memory cache (will be set by first call)
        mock_api_data = [
            {
                "ISU_CD": "005930",
                "ISU_SRT_CD": "005930",
                "ISU_ABBRV": "삼성전자",
                "ISU_NM": "삼성전자",
                "MKT_NM": "KOSPI",
                "CLSPRC": "80000",
                "MKTCAP": "480000000",
                "TRDVOL": "10000000",
                "TRDVAL": "800000000000",
            }
        ]

        fetch_called_count = 0

        async def mock_fetch_krx_data(**kwargs):
            nonlocal fetch_called_count
            fetch_called_count += 1
            return mock_api_data

        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        try:
            result1 = await krx.fetch_stock_all(market="STK", trd_date=trading_date)
            assert len(result1) == 1
            assert result1[0]["code"] == "005930"
            assert fetch_called_count == 1

            assert cache_key in krx._MEMORY_CACHE, (
                "Memory cache should have exact stock key"
            )
            cached_data, _ = krx._MEMORY_CACHE[cache_key]
            assert cached_data[0]["code"] == "005930"

            result2 = await krx.fetch_stock_all(market="STK", trd_date=trading_date)
            assert len(result2) == 1
            assert result2[0]["code"] == "005930"
            assert fetch_called_count == 1, (
                "Should reuse memory cache, not fetch API again"
            )
        finally:
            krx._MEMORY_CACHE.pop(cache_key, None)

    @pytest.mark.asyncio
    async def test_redis_set_exception_still_saves_to_memory_cache(self, monkeypatch):
        """Test Redis set exception doesn't prevent memory cache storage."""

        # Mock Redis client to raise exception on set
        class MockRedisClient:
            async def get(self, key):
                return None  # Cache miss

            async def setex(self, key, ttl, value):
                raise ConnectionError("Simulated Redis connection error")

        async def mock_get_redis_client():
            return MockRedisClient()

        monkeypatch.setattr(krx, "_get_redis_client", mock_get_redis_client)

        mock_api_data = [
            {
                "ISU_CD": "005930",
                "ISU_SRT_CD": "005930",
                "ISU_ABBRV": "삼성전자",
                "ISU_NM": "삼성전자",
                "MKT_NM": "KOSPI",
                "CLSPRC": "80000",
                "MKTCAP": "480000000",
                "TRDVOL": "10000000",
                "TRDVAL": "800000000000",
            }
        ]

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        result = await krx.fetch_stock_all(market="STK")

        assert len(result) == 1
        assert result[0]["code"] == "005930"

        # Verify data was saved to memory cache despite Redis failure
        cache_keys = [k for k in krx._MEMORY_CACHE.keys() if "krx:stock:all:STK" in k]
        assert len(cache_keys) > 0, "Memory cache should have data even if Redis fails"

        # Cleanup
        for key in cache_keys:
            if key in krx._MEMORY_CACHE:
                del krx._MEMORY_CACHE[key]

    @pytest.mark.asyncio
    async def test_memory_cache_ttl_expired_items_ignored(self, monkeypatch):
        """Test expired memory cache items are ignored and cleaned up."""
        from datetime import UTC, datetime

        # Mock Redis client to always return None (cache miss)
        class MockRedisClient:
            async def get(self, key):
                return None

            async def setex(self, key, ttl, value):
                pass  # Success

        async def mock_get_redis_client():
            return MockRedisClient()

        monkeypatch.setattr(krx, "_get_redis_client", mock_get_redis_client)

        # Generate today's cache key
        from datetime import date

        today_str = date.today().strftime("%Y%m%d")
        cache_key = f"krx:stock:all:STK:{today_str}"

        async def mock_fetch_max_working_date():
            return today_str

        monkeypatch.setattr(krx, "_fetch_max_working_date", mock_fetch_max_working_date)

        # Inject expired data into memory cache
        expired_timestamp = datetime.now(UTC).timestamp() - (
            krx._MEMORY_CACHE_TTL + 100
        )
        krx._MEMORY_CACHE[cache_key] = (
            [{"code": "expired", "name": "Expired"}],
            expired_timestamp,
        )

        mock_api_data = [
            {
                "ISU_CD": "005930",
                "ISU_SRT_CD": "005930",
                "ISU_ABBRV": "삼성전자",
                "ISU_NM": "삼성전자",
                "MKT_NM": "KOSPI",
                "CLSPRC": "80000",
                "MKTCAP": "480000000",
                "TRDVOL": "10000000",
                "TRDVAL": "800000000000",
            }
        ]

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        result = await krx.fetch_stock_all(market="STK")

        # Should fetch fresh data, not use expired cache
        assert len(result) == 1
        assert result[0]["code"] == "005930"
        assert result[0]["code"] != "expired"

        # Verify fresh data is now in memory cache (expired one should be replaced or cleaned)
        if cache_key in krx._MEMORY_CACHE:
            cached_data, _ = krx._MEMORY_CACHE[cache_key]
            assert cached_data[0]["code"] == "005930", "Should have fresh data in cache"

        # Cleanup
        if cache_key in krx._MEMORY_CACHE:
            del krx._MEMORY_CACHE[cache_key]


class TestKRXValuationCacheRecovery:
    """Test KRX valuation cache schema migration tolerance."""

    @pytest.mark.asyncio
    async def test_valuation_cache_old_format_recovery(self, monkeypatch):
        """Old cache entries without ISU_SRT_CD are handled gracefully."""
        old_cache_format = [
            {"per": 12.5, "pbr": 1.2, "dividend_yield": 0.0256},
            {"per": 15.0, "pbr": 1.5, "dividend_yield": 0.03},
        ]

        async def mock_get_cached_data(cache_key):
            return old_cache_format

        async def mock_fetch_krx_data(**kwargs):
            return [
                {
                    "ISU_SRT_CD": "005930",
                    "PER": "12.5",
                    "PBR": "1.2",
                    "DVD_YLD": "2.56",
                }
            ]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_valuation_all(market="ALL")

        assert "005930" in result
        assert result["005930"]["per"] == 12.5

    @pytest.mark.asyncio
    async def test_valuation_cache_new_format_with_isu_srt_cd(self, monkeypatch):
        """New cache entries with ISU_SRT_CD are processed correctly."""
        new_cache_format = [
            {
                "ISU_SRT_CD": "005930",
                "per": 12.5,
                "pbr": 1.2,
                "dividend_yield": 0.0256,
            },
            {
                "ISU_SRT_CD": "000660",
                "per": 15.0,
                "pbr": 1.5,
                "dividend_yield": 0.03,
            },
        ]

        async def mock_get_cached_data(cache_key):
            return new_cache_format

        fetch_called = False

        async def mock_fetch_krx_data(**kwargs):
            nonlocal fetch_called
            fetch_called = True
            return []

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx.fetch_valuation_all(market="ALL")

        assert len(result) == 2
        assert "005930" in result
        assert "000660" in result
        assert result["005930"]["per"] == 12.5
        assert not fetch_called, "Should use cache, not call API"

    @pytest.mark.asyncio
    async def test_valuation_cache_storage_includes_isu_srt_cd(self, monkeypatch):
        """Stored cache entries include ISU_SRT_CD field."""
        captured_cache_data = None

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return [
                {
                    "ISU_SRT_CD": "005930",
                    "PER": "12.5",
                    "PBR": "1.2",
                    "EPS": "6400",
                    "BPS": "66000",
                    "DVD_YLD": "2.56",
                }
            ]

        async def mock_set_cached_data(cache_key, data):
            nonlocal captured_cache_data
            captured_cache_data = data

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        await krx.fetch_valuation_all(market="ALL")

        assert isinstance(captured_cache_data, list)
        assert len(captured_cache_data) == 1
        cached_entry = captured_cache_data[0]
        assert isinstance(cached_entry, dict)
        assert cached_entry["ISU_SRT_CD"] == "005930"
        assert cached_entry["per"] == 12.5


class TestGetStockNameByCode:
    """Test get_stock_name_by_code utility."""

    @pytest.mark.asyncio
    async def test_get_stock_name_by_code_success(self, monkeypatch):
        """Test successful code to name resolution."""
        mock_stk_data = [
            {"short_code": "005930", "name": "삼성전자"},
            {"short_code": "000660", "name": "SK하이닉스"},
        ]
        mock_ksq_data = [
            {"short_code": "035720", "name": "카카오"},
        ]

        async def mock_fetch_stock_all_cached(market, trd_date=None):
            if market == "STK":
                return mock_stk_data
            elif market == "KSQ":
                return mock_ksq_data
            return []

        monkeypatch.setattr(krx, "fetch_stock_all_cached", mock_fetch_stock_all_cached)
        krx._CODE_TO_NAME_CACHE.clear()

        result = await krx.get_stock_name_by_code("005930")
        assert result == "삼성전자"

        result2 = await krx.get_stock_name_by_code("035720")
        assert result2 == "카카오"

    @pytest.mark.asyncio
    async def test_get_stock_name_by_code_not_found(self, monkeypatch):
        """Test code not found returns None."""
        mock_stk_data = [{"short_code": "005930", "name": "삼성전자"}]
        mock_ksq_data = []

        async def mock_fetch_stock_all_cached(market, trd_date=None):
            return mock_stk_data if market == "STK" else mock_ksq_data

        monkeypatch.setattr(krx, "fetch_stock_all_cached", mock_fetch_stock_all_cached)
        krx._CODE_TO_NAME_CACHE.clear()

        result = await krx.get_stock_name_by_code("999999")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_stock_name_by_code_cache_reuse(self, monkeypatch):
        """Test second call uses cache without re-fetching."""
        fetch_call_count = 0

        async def mock_fetch_stock_all_cached(market, trd_date=None):
            nonlocal fetch_call_count
            fetch_call_count += 1
            if market == "STK":
                return [{"short_code": "005930", "name": "삼성전자"}]
            return []

        monkeypatch.setattr(krx, "fetch_stock_all_cached", mock_fetch_stock_all_cached)
        krx._CODE_TO_NAME_CACHE.clear()

        await krx.get_stock_name_by_code("005930")
        assert fetch_call_count == 2

        fetch_call_count = 0
        result = await krx.get_stock_name_by_code("005930")
        assert result == "삼성전자"
        assert fetch_call_count == 0

    @pytest.mark.asyncio
    async def test_get_stock_name_by_code_exception_propagation(self, monkeypatch):
        """Test exceptions from fetch propagate to caller."""

        async def mock_fetch_stock_all_cached(market, trd_date=None):
            raise RuntimeError("KRX API error")

        monkeypatch.setattr(krx, "fetch_stock_all_cached", mock_fetch_stock_all_cached)
        krx._CODE_TO_NAME_CACHE.clear()

        with pytest.raises(RuntimeError, match="KRX API error"):
            await krx.get_stock_name_by_code("005930")

    @pytest.mark.asyncio
    async def test_get_stock_name_by_code_whitespace_handling(self, monkeypatch):
        """Test whitespace in code is stripped."""

        async def mock_fetch_stock_all_cached(market, trd_date=None):
            if market == "STK":
                return [{"short_code": "005930", "name": "삼성전자"}]
            return []

        monkeypatch.setattr(krx, "fetch_stock_all_cached", mock_fetch_stock_all_cached)
        krx._CODE_TO_NAME_CACHE.clear()

        result = await krx.get_stock_name_by_code("  005930  ")
        assert result == "삼성전자"


class TestKRXSessionManager:
    """Test KRXSessionManager login, session reuse, and error handling."""

    @pytest.mark.asyncio
    async def test_session_manager_login_success(self, monkeypatch):
        """Mock httpx responses for GET / and POST login. Verify CD001 success."""
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        # GET base page
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
        # POST login - CD001 success
        login_resp = MagicMock()
        login_resp.text = '{"code":"CD001","message":"success"}'
        login_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=login_resp)

        manager._client = mock_client

        await manager._login()

        assert manager._authenticated is True
        assert manager._auth_failed is False
        mock_client.get.assert_called_once_with(krx.KRX_LOGIN_PAGE_URL)
        mock_client.post.assert_called_once_with(
            krx.KRX_LOGIN_URL,
            data={"mbrId": "testuser", "pw": "testpass"},
        )

    @pytest.mark.asyncio
    async def test_session_manager_login_duplicate_cd011(self, monkeypatch):
        """Mock CD011 response, verify retry with skipDup flag."""
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))

        # First POST returns CD011, second returns CD001
        resp_cd011 = MagicMock()
        resp_cd011.text = '{"code":"CD011","message":"duplicate"}'
        resp_cd011.raise_for_status = MagicMock()

        resp_cd001 = MagicMock()
        resp_cd001.text = '{"code":"CD001","message":"success"}'
        resp_cd001.raise_for_status = MagicMock()

        mock_client.post = AsyncMock(side_effect=[resp_cd011, resp_cd001])

        manager._client = mock_client

        await manager._login()

        assert manager._authenticated is True
        # Verify second call included skipDup
        second_call = mock_client.post.call_args_list[1]
        assert second_call[1]["data"]["skipDup"] == "Y"

    @pytest.mark.asyncio
    async def test_session_manager_login_uses_login_endpoint(self, monkeypatch):
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))

        login_resp = MagicMock()
        login_resp.text = '{"code":"CD001","message":"success"}'
        login_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=login_resp)

        manager._client = mock_client

        await manager._login()

        assert mock_client.post.call_args[0][0] == krx.KRX_LOGIN_URL

    @pytest.mark.asyncio
    async def test_session_manager_login_preflight_uses_login_page(self, monkeypatch):
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))

        login_resp = MagicMock()
        login_resp.text = '{"code":"CD001","message":"success"}'
        login_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=login_resp)

        manager._client = mock_client

        await manager._login()

        assert mock_client.get.call_args[0][0] == krx.KRX_LOGIN_PAGE_URL

    @pytest.mark.asyncio
    async def test_session_manager_login_failure(self, monkeypatch):
        """Mock failed login, verify falls back to unauthenticated mode."""
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))

        resp_fail = MagicMock()
        resp_fail.text = '{"code":"CD999","message":"unknown error"}'
        resp_fail.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=resp_fail)

        manager._client = mock_client

        await manager._login()

        assert manager._authenticated is False
        assert manager._auth_failed is True

    @pytest.mark.asyncio
    async def test_session_manager_reauth_on_logout(self, monkeypatch):
        """Mock 400 response with 'LOGOUT' body, verify automatic re-auth and retry."""
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)

        # First POST to API returns 400/LOGOUT
        logout_resp = MagicMock()
        logout_resp.status_code = 400
        logout_resp.text = "LOGOUT"
        logout_resp.request = MagicMock()

        # After re-auth, second POST returns success
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.text = '{"OutBlock_1": [{"key": "value"}]}'
        success_resp.raise_for_status = MagicMock()
        success_resp.json = MagicMock(return_value={"OutBlock_1": [{"key": "value"}]})

        # Login responses
        login_get_resp = MagicMock(status_code=200)
        login_post_resp = MagicMock()
        login_post_resp.text = '{"code":"CD001"}'
        login_post_resp.raise_for_status = MagicMock()

        mock_client.get = AsyncMock(return_value=login_get_resp)
        # post calls: 1) API->LOGOUT, 2) login->CD001, 3) API->success
        mock_client.post = AsyncMock(
            side_effect=[logout_resp, login_post_resp, success_resp]
        )

        manager._client = mock_client
        manager._authenticated = True  # Was previously authenticated

        result = await manager.fetch_data(bld="test_bld")

        assert result == [{"key": "value"}]
        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_session_manager_no_credentials_fallback(self, monkeypatch):
        """When settings have no KRX credentials, requests are unauthenticated."""
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(member_id="", credential=""),
        )

        # Mock the client creation
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.raise_for_status = MagicMock()
        success_resp.json = MagicMock(return_value={"OutBlock_1": [{"data": "test"}]})
        mock_client.post = AsyncMock(return_value=success_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await manager.fetch_data(bld="test_bld")

        assert manager._authenticated is False
        assert result == [{"data": "test"}]

    @pytest.mark.asyncio
    async def test_session_reuse_across_calls(self, monkeypatch):
        """Verify multiple fetch_data calls reuse the same httpx.AsyncClient."""
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(member_id="", credential=""),
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.raise_for_status = MagicMock()
        success_resp.json = MagicMock(return_value={"OutBlock_1": []})
        mock_client.post = AsyncMock(return_value=success_resp)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            await manager.fetch_data(bld="test1")
            await manager.fetch_data(bld="test2")
            await manager.fetch_data(bld="test3")

        # AsyncClient should be created only once
        assert mock_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_login_lock(self, monkeypatch):
        """Verify asyncio.Lock prevents concurrent login race conditions."""
        manager = KRXSessionManager()
        monkeypatch.setattr(
            "app.services.krx.settings",
            _mock_krx_settings(),
        )

        login_call_count = 0

        async def counting_login():
            nonlocal login_call_count
            login_call_count += 1
            # Simulate slow login
            await asyncio.sleep(0.01)
            manager._authenticated = True

        manager._login = counting_login

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.raise_for_status = MagicMock()
        success_resp.json = MagicMock(return_value={"OutBlock_1": []})
        mock_client.post = AsyncMock(return_value=success_resp)
        manager._client = mock_client

        # Run multiple concurrent ensure_session calls
        await asyncio.gather(
            manager._ensure_session(),
            manager._ensure_session(),
            manager._ensure_session(),
        )

        # Login should only be called once due to the lock
        assert login_call_count == 1

    @pytest.mark.asyncio
    async def test_fetch_krx_data_uses_session(self, monkeypatch):
        """Verify _fetch_krx_data delegates to session manager."""
        mock_result = [{"ISU_CD": "005930", "ISU_ABBRV": "삼성전자"}]

        async def mock_fetch_data(**kwargs):
            return mock_result

        monkeypatch.setattr(krx._krx_session, "fetch_data", mock_fetch_data)

        result = await krx._fetch_krx_data(
            bld="test_bld", mktId="STK", trdDd="20250101"
        )

        assert result == mock_result

    @pytest.mark.asyncio
    async def test_session_close(self):
        """Verify close() properly closes the httpx.AsyncClient."""
        manager = KRXSessionManager()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        manager._client = mock_client
        manager._authenticated = True

        await manager.close()

        mock_client.aclose.assert_called_once()
        assert manager._client is None
        assert manager._authenticated is False
        assert manager._auth_failed is False

class TestFetchWithDateFallback:
    """Test the _fetch_with_date_fallback common helper."""

    @pytest.mark.asyncio
    async def test_returns_normalized_data_on_success(self, monkeypatch):
        """First date succeeds → returns normalized data."""
        from app.services import krx

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return [{"RAW_FIELD": "value1"}]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        def normalize(raw_data, actual_date):
            return [{"normalized": item["RAW_FIELD"], "date": actual_date} for item in raw_data]

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=normalize,
            trd_date="20250401",
        )

        assert len(result) == 1
        assert result[0]["normalized"] == "value1"
        assert result[0]["date"] == "20250401"

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self, monkeypatch):
        """Cache hit → returns cached data without calling API."""
        from app.services import krx
        cached = [{"from": "cache"}]

        async def mock_get_cached_data(cache_key):
            return cached

        fetch_called = False

        async def mock_fetch_krx_data(**kwargs):
            nonlocal fetch_called
            fetch_called = True
            return []

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=lambda raw, dt: raw,
            trd_date="20250401",
        )

        assert result == cached
        assert not fetch_called

    @pytest.mark.asyncio
    async def test_fallback_to_next_date_on_empty(self, monkeypatch):
        """Empty response → tries next date candidate."""
        from app.services import krx
        call_dates = []

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            call_dates.append(kwargs.get("trdDd"))
            if len(call_dates) == 1:
                return []  # first date empty
            return [{"RAW": "ok"}]

        async def mock_set_cached_data(cache_key, data):
            pass

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)
        monkeypatch.setattr(krx, "_set_cached_data", mock_set_cached_data)

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=lambda raw, dt: [{"done": True}],
            trd_date=None,  # auto-detect → multiple date candidates
        )

        assert len(call_dates) >= 2
        assert result == [{"done": True}]

    @pytest.mark.asyncio
    async def test_all_dates_exhausted_returns_empty(self, monkeypatch):
        """All dates return empty → returns []."""
        from app.services import krx

        async def mock_get_cached_data(cache_key):
            return None

        async def mock_fetch_krx_data(**kwargs):
            return []

        monkeypatch.setattr(krx, "_get_cached_data", mock_get_cached_data)
        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        result = await krx._fetch_with_date_fallback(
            cache_prefix="test:prefix",
            bld="dbms/TEST/bld",
            extra_params=None,
            normalize_fn=lambda raw, dt: raw,
            trd_date="20250101",
        )

        assert result == []
