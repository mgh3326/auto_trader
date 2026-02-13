"""Tests for KRX service caching and fallback logic."""

import pytest

from app.services import krx


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

        # Mock Redis client to raise exception
        class MockRedisClient:
            async def get(self, key):
                raise ConnectionError("Simulated Redis connection error")

            async def setex(self, key, ttl, value):
                raise ConnectionError("Simulated Redis connection error")

        async def mock_get_redis_client():
            return MockRedisClient()

        monkeypatch.setattr(krx, "_get_redis_client", mock_get_redis_client)

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

        async def mock_fetch_krx_data(**kwargs):
            return mock_api_data

        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data)

        # First call: Redis fails, fetch from API, save to memory cache
        result1 = await krx.fetch_stock_all(market="STK")
        assert len(result1) == 1
        assert result1[0]["code"] == "005930"

        # Verify data was saved to memory cache
        cache_keys = [k for k in krx._MEMORY_CACHE.keys() if "krx:stock:all:STK" in k]
        assert len(cache_keys) > 0, "Memory cache should have data"

        # Second call: Redis fails again, but should use memory cache
        fetch_called_count = 0

        async def mock_fetch_krx_data_count(**kwargs):
            nonlocal fetch_called_count
            fetch_called_count += 1
            return mock_api_data

        monkeypatch.setattr(krx, "_fetch_krx_data", mock_fetch_krx_data_count)

        result2 = await krx.fetch_stock_all(market="STK")
        assert len(result2) == 1
        assert result2[0]["code"] == "005930"
        assert fetch_called_count == 0, "Should use memory cache, not fetch API again"

        # Cleanup
        for key in cache_keys:
            if key in krx._MEMORY_CACHE:
                del krx._MEMORY_CACHE[key]

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
    """Test KRX valuation cache schema backward compatibility."""

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

        assert captured_cache_data is not None
        assert len(captured_cache_data) == 1
        assert captured_cache_data[0]["ISU_SRT_CD"] == "005930"
        assert captured_cache_data[0]["per"] == 12.5
