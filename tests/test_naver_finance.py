"""Unit tests for Naver Finance service."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from app.services import naver_finance

# ---------------------------------------------------------------------------
# Helper Function Tests
# ---------------------------------------------------------------------------


class TestParseNaverDate:
    """Tests for _parse_naver_date helper."""

    def test_full_date_dot_format(self) -> None:
        assert naver_finance._parse_naver_date("2024.01.15") == "2024-01-15"
        assert naver_finance._parse_naver_date("2024.1.5") == "2024-01-05"

    def test_full_date_dash_format(self) -> None:
        assert naver_finance._parse_naver_date("2024-01-15") == "2024-01-15"
        assert naver_finance._parse_naver_date("2024-1-5") == "2024-01-05"

    def test_full_date_slash_format(self) -> None:
        assert naver_finance._parse_naver_date("2024/01/15") == "2024-01-15"

    def test_short_date_assumes_current_year(self) -> None:
        result = naver_finance._parse_naver_date("01.15")
        assert result == f"{date.today().year}-01-15"

        result = naver_finance._parse_naver_date("1.5")
        assert result == f"{date.today().year}-01-05"

    def test_none_for_empty(self) -> None:
        assert naver_finance._parse_naver_date("") is None
        assert naver_finance._parse_naver_date(None) is None
        assert naver_finance._parse_naver_date("   ") is None

    def test_returns_original_for_unrecognized_format(self) -> None:
        assert naver_finance._parse_naver_date("invalid") == "invalid"


class TestParseKoreanNumber:
    """Tests for _parse_korean_number helper."""

    def test_simple_integer(self) -> None:
        assert naver_finance._parse_korean_number("1234") == 1234
        assert naver_finance._parse_korean_number("1,234") == 1234
        assert naver_finance._parse_korean_number("1,234,567") == 1234567

    def test_simple_float(self) -> None:
        assert naver_finance._parse_korean_number("12.34") == 12.34
        assert naver_finance._parse_korean_number("1,234.56") == 1234.56

    def test_percentage(self) -> None:
        result = naver_finance._parse_korean_number("5.67%")
        assert result is not None
        assert abs(result - 0.0567) < 0.0001

        result = naver_finance._parse_korean_number("100%")
        assert result is not None
        assert abs(result - 1.0) < 0.0001

    def test_korean_unit_jo(self) -> None:
        # 1조 = 1,000,000,000,000 (10^12)
        assert naver_finance._parse_korean_number("1조") == 1_0000_0000_0000
        assert naver_finance._parse_korean_number("2.5조") == 2_5000_0000_0000

    def test_korean_unit_eok(self) -> None:
        # 1억 = 100,000,000 (10^8)
        assert naver_finance._parse_korean_number("1억") == 1_0000_0000
        assert naver_finance._parse_korean_number("100억") == 100_0000_0000

    def test_korean_unit_man(self) -> None:
        # 1만 = 10,000 (10^4)
        assert naver_finance._parse_korean_number("1만") == 1_0000
        assert naver_finance._parse_korean_number("5만") == 5_0000

    def test_korean_units_combined(self) -> None:
        # 1조 2,345억 = 1,234,500,000,000
        result = naver_finance._parse_korean_number("1조 2,345억")
        expected = 1_0000_0000_0000 + 2345 * 1_0000_0000
        assert result == expected

        # 400조 1,234억
        result = naver_finance._parse_korean_number("400조 1,234억")
        expected = 400 * 1_0000_0000_0000 + 1234 * 1_0000_0000
        assert result == expected

    def test_negative_number_with_minus(self) -> None:
        assert naver_finance._parse_korean_number("-1,234") == -1234
        assert naver_finance._parse_korean_number("-5.67") == -5.67

    def test_negative_number_with_arrow(self) -> None:
        assert naver_finance._parse_korean_number("▼1,234") == -1234
        assert naver_finance._parse_korean_number("▼100") == -100

    def test_positive_number_with_arrow(self) -> None:
        assert naver_finance._parse_korean_number("▲1,234") == 1234

    def test_none_for_invalid(self) -> None:
        assert naver_finance._parse_korean_number("") is None
        assert naver_finance._parse_korean_number(None) is None
        assert naver_finance._parse_korean_number("N/A") is None
        assert naver_finance._parse_korean_number("--") is None

    def test_with_whitespace(self) -> None:
        assert naver_finance._parse_korean_number("  1,234  ") == 1234
        assert naver_finance._parse_korean_number("1 억") == 1_0000_0000


# ---------------------------------------------------------------------------
# HTML Fixtures
# ---------------------------------------------------------------------------


SAMPLE_NEWS_HTML = """
<html>
<body>
<table class="type5">
    <tr>
        <td class="title"><a href="/item/news_read.naver?article_id=123">삼성전자, 신제품 발표</a></td>
        <td class="info">연합뉴스</td>
        <td class="date">2024.01.15</td>
    </tr>
    <tr>
        <td class="title"><a href="/item/news_read.naver?article_id=124">반도체 시장 전망</a></td>
        <td class="info">한국경제</td>
        <td class="date">2024.01.14</td>
    </tr>
</table>
</body>
</html>
"""

SAMPLE_PROFILE_HTML = """
<html>
<body>
<div class="wrap_company">
    <h2><a>삼성전자</a></h2>
</div>
<div class="code">005930 코스피</div>
<em id="_market_sum">400조 1,234억</em>
<table class="no_info">
    <tr><th>PER</th><td><em>15.23</em></td></tr>
    <tr><th>PBR</th><td><em>1.45</em></td></tr>
    <tr><th>EPS</th><td><em>5,432</em></td></tr>
</table>
<div class="tab_con1">
    <em><a>전기전자</a></em>
</div>
</body>
</html>
"""

SAMPLE_INVESTOR_TRENDS_HTML = """
<html>
<body>
<!-- First table.type2 is empty (matches real Naver structure) -->
<table class="type2">
    <tbody><tr><td></td></tr></tbody>
</table>
<!-- Second table.type2 has the actual data -->
<table class="type2">
    <tr>
        <td>2024.01.15</td>
        <td>75,000</td>
        <td>▲500</td>
        <td>+0.67%</td>
        <td>10,000,000</td>
        <td>1,000,000</td>
        <td>-500,000</td>
    </tr>
    <tr>
        <td>2024.01.14</td>
        <td>74,500</td>
        <td>▼300</td>
        <td>-0.40%</td>
        <td>8,000,000</td>
        <td>-200,000</td>
        <td>300,000</td>
    </tr>
</table>
</body>
</html>
"""

SAMPLE_INVESTMENT_OPINIONS_HTML = """
<html>
<body>
<table class="type_1">
    <tbody>
        <tr>
            <td>삼성전자</td>
            <td><a href="/research/company_read.naver?id=1">반도체 업황 개선 전망</a></td>
            <td>삼성증권</td>
            <td>매수</td>
            <td>85,000</td>
            <td>2024.01.15</td>
        </tr>
        <tr>
            <td>삼성전자</td>
            <td><a href="/research/company_read.naver?id=2">실적 호조 지속</a></td>
            <td>미래에셋</td>
            <td>Strong Buy</td>
            <td>90,000</td>
            <td>2024.01.14</td>
        </tr>
    </tbody>
</table>
</body>
</html>
"""

SAMPLE_VALUATION_MAIN_HTML = """
<html>
<body>
<div class="wrap_company">
    <h2><a>삼성전자</a></h2>
</div>
<p class="no_today">
    <span class="blind">현재가</span>
    <em><span class="blind">75,000</span></em>
</p>
<em id="_per">12.50</em>
<em id="_pbr">1.20</em>
<em id="_dvr">2.00</em>
<table>
    <tr>
        <th>ROE(지배주주)</th><td>17.20</td>
    </tr>
    <tr>
        <th>ROE(%)</th><td>18.50</td>
    </tr>
</table>
</body>
</html>
"""

SAMPLE_VALUATION_SISE_HTML = """
<html>
<body>
<table>
    <tr>
        <th>52주 최고</th><td>90,000</td>
        <th>52주 최저</th><td>60,000</td>
    </tr>
</table>
</body>
</html>
"""

SAMPLE_VALUATION_MINIMAL_MAIN_HTML = """
<html>
<body>
<div class="wrap_company">
    <h2><a>효성중공업</a></h2>
</div>
<p class="no_today">450,000</p>
<em id="_per">N/A</em>
<em id="_pbr">2.10</em>
</body>
</html>
"""

SAMPLE_VALUATION_MINIMAL_SISE_HTML = """
<html>
<body>
<table>
    <tr>
        <th>52주 최고</th><td>500,000</td>
        <th>52주 최저</th><td>200,000</td>
    </tr>
</table>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Service Function Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchNews:
    """Tests for fetch_news function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_NEWS_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_news("005930", limit=10)

        assert len(result) == 2
        assert result[0]["title"] == "삼성전자, 신제품 발표"
        assert result[0]["source"] == "연합뉴스"
        assert result[0]["datetime"] == "2024-01-15"
        assert "news_read.naver" in result[0]["url"]

    async def test_limit_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_NEWS_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_news("005930", limit=1)

        assert len(result) == 1

    async def test_empty_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_news("005930")
        assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchCompanyProfile:
    """Tests for fetch_company_profile function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_PROFILE_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_company_profile("005930")

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert result["exchange"] == "KOSPI"
        assert result["sector"] == "전기전자"
        # Market cap: 400조 1,234억
        assert result["market_cap"] == 400 * 1_0000_0000_0000 + 1234 * 1_0000_0000
        assert result["per"] == 15.23
        assert result["pbr"] == 1.45
        assert result["eps"] == 5432

    async def test_filters_none_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            # Minimal HTML with only name
            return BeautifulSoup(
                '<div class="wrap_company"><h2><a>테스트</a></h2></div>',
                "lxml",
            )

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_company_profile("000000")

        # Only symbol and name should be present
        assert result["symbol"] == "000000"
        assert result["name"] == "테스트"
        assert "per" not in result  # None values filtered


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchInvestorTrends:
    """Tests for fetch_investor_trends function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_INVESTOR_TRENDS_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investor_trends("005930", days=20)

        assert result["symbol"] == "005930"
        assert len(result["data"]) == 2

        # First day
        day1 = result["data"][0]
        assert day1["date"] == "2024-01-15"
        assert day1["close"] == 75000
        assert day1["change"] == 500  # ▲500
        assert day1["institutional_net"] == 1000000
        assert day1["foreign_net"] == -500000

        # Second day
        day2 = result["data"][1]
        assert day2["date"] == "2024-01-14"
        assert day2["change"] == -300  # ▼300

    async def test_days_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_INVESTOR_TRENDS_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investor_trends("005930", days=1)

        assert len(result["data"]) == 1


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchInvestmentOpinions:
    """Tests for fetch_investment_opinions function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        assert result["symbol"] == "005930"
        assert result["count"] == 2
        assert len(result["opinions"]) == 2

        # First opinion
        op1 = result["opinions"][0]
        assert op1["stock_name"] == "삼성전자"
        assert op1["title"] == "반도체 업황 개선 전망"
        assert op1["firm"] == "삼성증권"
        assert op1["rating"] == "매수"
        assert op1["target_price"] == 85000
        assert op1["date"] == "2024-01-15"

    async def test_limit_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investment_opinions("005930", limit=1)

        assert result["count"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchHtml:
    """Tests for _fetch_html function."""

    async def test_euc_kr_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mock httpx.AsyncClient
        mock_response = AsyncMock()
        mock_response.content = "한글 테스트".encode("euc-kr")
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)

        soup = await naver_finance._fetch_html("https://example.com")
        assert "한글 테스트" in soup.get_text()

    async def test_utf8_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mock httpx.AsyncClient with UTF-8 content that fails EUC-KR
        mock_response = AsyncMock()
        # UTF-8 content with characters invalid in EUC-KR
        mock_response.content = "한글 UTF-8 😀".encode()
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)

        soup = await naver_finance._fetch_html("https://example.com")
        # Should fall back to UTF-8 and contain the text
        text = soup.get_text()
        assert "한글" in text


SAMPLE_SHORT_INTEREST_HTML = """
<html>
<body>
<div class="wrap_company">
    <h2><a>삼성전자</a></h2>
</div>
</body>
</html>
"""


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchShortInterest:
    """Tests for fetch_short_interest function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test successful short interest data fetch."""
        import pandas as pd

        # Mock the company name fetch
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_SHORT_INTEREST_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        # Mock pykrx functions
        def mock_get_shorting_status(fromdate: str, todate: str, ticker: str) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "공매도거래대금": [1_000_000_000, 800_000_000],
                    "총거래대금": [20_000_000_000, 15_000_000_000],
                    "비중": [5.0, 5.33],
                },
                index=pd.to_datetime(["2024-01-15", "2024-01-14"]),
            )

        def mock_get_shorting_balance(fromdate: str, todate: str, ticker: str) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "공매도잔고": [1_234_567],
                    "공매도금액": [98_765_432_100],
                    "비중": [0.5],
                },
                index=pd.to_datetime(["2024-01-15"]),
            )

        # Mock the pykrx.stock module
        class MockPykrxStock:
            get_shorting_status_by_date = staticmethod(mock_get_shorting_status)
            get_shorting_balance_by_date = staticmethod(mock_get_shorting_balance)

        import sys
        mock_pykrx = type(sys)("pykrx")
        mock_pykrx.stock = MockPykrxStock
        monkeypatch.setitem(sys.modules, "pykrx", mock_pykrx)
        monkeypatch.setitem(sys.modules, "pykrx.stock", MockPykrxStock)

        result = await naver_finance.fetch_short_interest("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert len(result["short_data"]) == 2

        # Check first day data
        day1 = result["short_data"][0]
        assert day1["date"] == "2024-01-15"
        assert day1["short_amount"] == 1_000_000_000
        assert day1["total_amount"] == 20_000_000_000
        assert day1["short_ratio"] == 5.0

        # Check average ratio
        assert result["avg_short_ratio"] == 5.17  # (5.0 + 5.33) / 2 rounded

        # Check balance data
        assert "short_balance" in result
        assert result["short_balance"]["balance_shares"] == 1_234_567

    async def test_empty_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test with empty short selling data."""
        import pandas as pd

        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_SHORT_INTEREST_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        def mock_get_shorting_status(fromdate: str, todate: str, ticker: str) -> pd.DataFrame:
            return pd.DataFrame()

        def mock_get_shorting_balance(fromdate: str, todate: str, ticker: str) -> pd.DataFrame:
            return pd.DataFrame()

        class MockPykrxStock:
            get_shorting_status_by_date = staticmethod(mock_get_shorting_status)
            get_shorting_balance_by_date = staticmethod(mock_get_shorting_balance)

        import sys
        mock_pykrx = type(sys)("pykrx")
        mock_pykrx.stock = MockPykrxStock
        monkeypatch.setitem(sys.modules, "pykrx", mock_pykrx)
        monkeypatch.setitem(sys.modules, "pykrx.stock", MockPykrxStock)

        result = await naver_finance.fetch_short_interest("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["short_data"] == []
        assert result["avg_short_ratio"] is None
        assert "short_balance" not in result

    async def test_pykrx_exception_handling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test handling of pykrx exceptions."""
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_SHORT_INTEREST_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        def mock_get_shorting_status(fromdate: str, todate: str, ticker: str) -> None:
            raise Exception("Network error")

        def mock_get_shorting_balance(fromdate: str, todate: str, ticker: str) -> None:
            raise Exception("Network error")

        class MockPykrxStock:
            get_shorting_status_by_date = staticmethod(mock_get_shorting_status)
            get_shorting_balance_by_date = staticmethod(mock_get_shorting_balance)

        import sys
        mock_pykrx = type(sys)("pykrx")
        mock_pykrx.stock = MockPykrxStock
        monkeypatch.setitem(sys.modules, "pykrx", mock_pykrx)
        monkeypatch.setitem(sys.modules, "pykrx.stock", MockPykrxStock)

        # Should not raise, but return empty data
        result = await naver_finance.fetch_short_interest("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["short_data"] == []


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchValuation:
    """Tests for fetch_valuation function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            # Return different HTML based on URL
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
            else:  # sise.naver
                return BeautifulSoup(SAMPLE_VALUATION_SISE_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_valuation("005930")

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert result["current_price"] == 75000
        assert result["per"] == 12.5
        assert result["pbr"] == 1.2
        assert result["roe"] == 18.5  # ROE(%)
        assert result["roe_controlling"] == 17.2  # ROE(지배주주)
        assert abs(result["dividend_yield"] - 0.02) < 0.001  # 2.00% -> 0.02
        assert result["high_52w"] == 90000
        assert result["low_52w"] == 60000
        # Position: (75000 - 60000) / (90000 - 60000) = 0.5
        assert result["current_position_52w"] == 0.5

    async def test_minimal_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test with minimal HTML data (some values missing)."""
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_VALUATION_MINIMAL_MAIN_HTML, "lxml")
            else:
                return BeautifulSoup(SAMPLE_VALUATION_MINIMAL_SISE_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_valuation("298040")

        assert result["symbol"] == "298040"
        assert result["name"] == "효성중공업"
        assert result["current_price"] == 450000
        assert result["per"] is None  # N/A parsed as None
        assert result["pbr"] == 2.1
        assert result["roe"] is None  # Not in HTML
        assert result["roe_controlling"] is None  # Not in HTML
        assert result["dividend_yield"] is None
        assert result["high_52w"] == 500000
        assert result["low_52w"] == 200000
        # Position: (450000 - 200000) / (500000 - 200000) = 0.833...
        assert abs(result["current_position_52w"] - 0.83) < 0.01

    async def test_position_calculation_at_low(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test position calculation when price is at 52-week low."""
        main_html = """
        <html><body>
        <div class="wrap_company"><h2><a>테스트</a></h2></div>
        <p class="no_today"><em><span class="blind">100,000</span></em></p>
        </body></html>
        """
        sise_html = """
        <html><body>
        <table>
            <tr><th>52주 최고</th><td>200,000</td><th>52주 최저</th><td>100,000</td></tr>
        </table>
        </body></html>
        """

        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(main_html, "lxml")
            return BeautifulSoup(sise_html, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_valuation("000000")

        assert result["current_position_52w"] == 0.0

    async def test_position_calculation_at_high(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test position calculation when price is at 52-week high."""
        main_html = """
        <html><body>
        <div class="wrap_company"><h2><a>테스트</a></h2></div>
        <p class="no_today"><em><span class="blind">200,000</span></em></p>
        </body></html>
        """
        sise_html = """
        <html><body>
        <table>
            <tr><th>52주 최고</th><td>200,000</td><th>52주 최저</th><td>100,000</td></tr>
        </table>
        </body></html>
        """

        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(main_html, "lxml")
            return BeautifulSoup(sise_html, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_valuation("000000")

        assert result["current_position_52w"] == 1.0

    async def test_empty_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test with empty HTML."""
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_valuation("000000")

        assert result["symbol"] == "000000"
        assert result["name"] is None
        assert result["current_price"] is None
        assert result["current_position_52w"] is None
