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
        # "01.01" is always past or today (never future)
        result = naver_finance._parse_naver_date("01.01")
        assert result == f"{date.today().year}-01-01"

        result = naver_finance._parse_naver_date("1.1")
        assert result == f"{date.today().year}-01-01"

    def test_two_digit_year_format(self) -> None:
        """Test YY.MM.DD format (e.g., "26.01.30" → "2026-01-30")."""
        assert naver_finance._parse_naver_date("26.01.30") == "2026-01-30"
        assert naver_finance._parse_naver_date("24.12.25") == "2024-12-25"
        assert naver_finance._parse_naver_date("25.1.5") == "2025-01-05"
        # Edge case: year 00 → 2000
        assert naver_finance._parse_naver_date("00.06.15") == "2000-06-15"

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
            <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
            <td><a href="company_read.naver?nid=12345&page=1">반도체 업황 개선 전망</a></td>
            <td>삼성증권</td>
            <td><a href="https://example.com/report1.pdf"></a></td>
            <td class="date">26.01.15</td>
            <td>1234</td>
        </tr>
        <tr>
            <td><a href="/item/main.naver?code=005930">삼성전자</a></td>
            <td><a href="company_read.naver?nid=12346&page=1">실적 호조 지속</a></td>
            <td>미래에셋</td>
            <td><a href="https://example.com/report2.pdf"></a></td>
            <td class="date">26.01.14</td>
            <td>5678</td>
        </tr>
    </tbody>
</table>
</body>
</html>
"""

SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_1 = """
<html>
<body>
<table class="type_1" summary="종목분석 리포트 본문내용">
    <tr>
        <th class="view_sbj">
            <span><em>삼성전자</em></span>
            반도체 업황 개선 전망
            <p class="source">삼성증권 | 2026.01.15</p>
        </th>
    </tr>
    <tr>
        <td colspan="2">
            <div class="view_info">
                <div class="view_info_1">
                    목표가 <em class="money"><strong>85,000</strong></em>
                    <span class="division">|</span>
                    투자의견 <em class="coment">매수</em>
                </div>
            </div>
        </td>
    </tr>
</table>
</body>
</html>
"""

SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_2 = """
<html>
<body>
<table class="type_1" summary="종목분석 리포트 본문내용">
    <tr>
        <th class="view_sbj">
            <span><em>삼성전자</em></span>
            실적 호조 지속
            <p class="source">미래에셋 | 2026.01.14</p>
        </th>
    </tr>
    <tr>
        <td colspan="2">
            <div class="view_info">
                <div class="view_info_1">
                    목표가 <em class="money"><strong>90,000</strong></em>
                    <span class="division">|</span>
                    투자의견 <em class="coment">Strong Buy</em>
                </div>
            </div>
        </td>
    </tr>
</table>
</body>
</html>
"""

SAMPLE_CURRENT_PRICE_HTML = """
<html>
<body>
<div class="wrap_company">
    <h2><a>삼성전자</a></h2>
</div>
<p class="no_today">
    <span class="blind">현재가</span>
    <em><span class="blind">75,000</span></em>
</p>
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
            # Return different HTML based on URL
            if "company_list.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")
            elif "company_read.naver" in url:
                nid = (params or {}).get("nid", "")
                if nid == "12345":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_1, "lxml"
                    )
                elif nid == "12346":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_2, "lxml"
                    )
            elif "main.naver" in url:
                return BeautifulSoup(SAMPLE_CURRENT_PRICE_HTML, "lxml")
            return BeautifulSoup("<html></html>", "lxml")

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
        assert op1["rating"] == "Buy"
        assert op1["rating_bucket"] == "buy"
        assert op1["target_price"] == 85000
        assert op1["date"] == "2026-01-15"

        # Second opinion
        op2 = result["opinions"][1]
        assert op2["rating"] == "Strong Buy"
        assert op2["rating_bucket"] == "buy"
        assert op2["target_price"] == 90000

        assert "consensus" in result
        consensus = result["consensus"]
        assert consensus["buy_count"] == 2
        assert consensus["hold_count"] == 0
        assert consensus["sell_count"] == 0
        assert consensus["total_count"] == 2
        assert consensus["avg_target_price"] == 87500
        assert consensus["median_target_price"] == 87500
        assert consensus["min_target_price"] == 85000
        assert consensus["max_target_price"] == 90000
        assert abs(consensus["upside_pct"] - 16.67) < 0.01
        assert consensus["current_price"] == 75000

    async def test_limit_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            if "company_list.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")
            elif "company_read.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_1, "lxml")
            elif "main.naver" in url:
                return BeautifulSoup(SAMPLE_CURRENT_PRICE_HTML, "lxml")
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investment_opinions("005930", limit=1)

        assert result["count"] == 1

    async def test_empty_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test with no opinions found."""

        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup("<html><table class='type_1'></table></html>", "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        assert result["count"] == 0
        assert result["opinions"] == []
        assert result["consensus"] is not None
        assert result["consensus"]["avg_target_price"] is None
        assert result["consensus"]["min_target_price"] is None

    async def test_missing_target_price(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test when some reports don't have target price."""
        detail_without_target = """
        <html><body>
        <div class="view_info_1">
            목표가 <em class="money"><strong></strong></em>
            투자의견 <em class="coment">없음</em>
        </div>
        </body></html>
        """

        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            if "company_list.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")
            elif "company_read.naver" in url:
                nid = (params or {}).get("nid", "")
                if nid == "12345":
                    return BeautifulSoup(detail_without_target, "lxml")
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_2, "lxml")
            elif "main.naver" in url:
                return BeautifulSoup(SAMPLE_CURRENT_PRICE_HTML, "lxml")
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        # First opinion has no target price, second has 90000
        assert result["opinions"][0]["target_price"] is None
        assert result["opinions"][1]["target_price"] == 90000

        # Stats should only use the one with target price
        assert "consensus" in result
        consensus = result["consensus"]
        assert consensus["avg_target_price"] == 90000
        assert consensus["max_target_price"] == 90000
        assert consensus["min_target_price"] == 90000


@pytest.mark.unit
class TestRatingNormalization:
    """Tests for _normalize_rating function."""

    def test_case_insensitive_normalization(self) -> None:
        """Test that rating normalization is case-insensitive."""
        from app.services.naver_finance import _normalize_rating

        # Test uppercase
        assert _normalize_rating("BUY") == "buy"
        assert _normalize_rating("STRONG BUY") == "buy"
        assert _normalize_rating("HOLD") == "hold"
        assert _normalize_rating("MARKET PERFORM") == "hold"
        assert _normalize_rating("SELL") == "sell"

        # Test mixed case
        assert _normalize_rating("Strong Buy") == "buy"
        assert _normalize_rating("Market Perform") == "hold"
        assert _normalize_rating("Overweight") == "buy"

        # Test lowercase
        assert _normalize_rating("buy") == "buy"
        assert _normalize_rating("hold") == "hold"
        assert _normalize_rating("sell") == "sell"

    def test_space_handling(self) -> None:
        """Test that spaces in ratings are handled correctly."""
        from app.services.naver_finance import _normalize_rating

        # With spaces
        assert _normalize_rating("market perform") == "hold"
        assert _normalize_rating("strong buy") == "buy"
        assert _normalize_rating("strong sell") == "sell"
        assert _normalize_rating("equal weight") == "hold"

        # Without spaces (compact form)
        assert _normalize_rating("marketperform") == "hold"
        assert _normalize_rating("equalweight") == "hold"

        # Leading/trailing whitespace
        assert _normalize_rating("  buy  ") == "buy"
        assert _normalize_rating(" strong buy ") == "buy"

    def test_korean_ratings(self) -> None:
        """Test Korean rating normalization."""
        from app.services.naver_finance import _normalize_rating

        assert _normalize_rating("매수") == "buy"
        assert _normalize_rating("강력매수") == "buy"
        assert _normalize_rating("강매") == "buy"
        assert _normalize_rating("비중확대") == "buy"
        assert _normalize_rating("중립") == "hold"
        assert _normalize_rating("보유") == "hold"
        assert _normalize_rating("매도") == "sell"
        assert _normalize_rating("비중축소") == "sell"

    def test_unknown_ratings_default_to_hold(self) -> None:
        """Test that unknown ratings default to 'hold'."""
        from app.services.naver_finance import _normalize_rating

        assert _normalize_rating("unknown rating") == "hold"
        assert _normalize_rating("xyz") == "hold"
        assert _normalize_rating(None) == "hold"
        assert _normalize_rating("") == "hold"


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
        """Test successful short interest data fetch via KRX API."""

        # Mock the company name fetch
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_SHORT_INTEREST_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        # Mock KRX API response
        krx_response = {
            "OutBlock_1": [
                {
                    "TRD_DD": "2024/01/15",
                    "CVSRTSELL_TRDVOL": "100,000",
                    "CVSRTSELL_TRDVAL": "1,000,000,000",
                    "STR_CONST_VAL1": "1,234,567",
                    "STR_CONST_VAL2": "98,765,432,100",
                },
                {
                    "TRD_DD": "2024/01/14",
                    "CVSRTSELL_TRDVOL": "80,000",
                    "CVSRTSELL_TRDVAL": "800,000,000",
                    "STR_CONST_VAL1": "-",
                    "STR_CONST_VAL2": "-",
                },
            ]
        }

        class MockResponse:
            status_code = 200

            def json(self) -> dict:
                return krx_response

        async def mock_post(self, url: str, **kwargs) -> MockResponse:
            return MockResponse()

        import httpx

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        # Mock daily volumes for short_ratio calculation
        async def mock_daily_volumes(code: str, days: int) -> dict[str, int]:
            return {
                "2024-01-15": 2_000_000,  # total volume
                "2024-01-14": 1_600_000,  # total volume
            }

        monkeypatch.setattr(naver_finance, "_fetch_daily_volumes", mock_daily_volumes)

        result = await naver_finance.fetch_short_interest("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert len(result["short_data"]) == 2

        # Check first day data
        day1 = result["short_data"][0]
        assert day1["date"] == "2024-01-15"
        assert day1["short_volume"] == 100_000
        assert day1["short_amount"] == 1_000_000_000
        assert day1["total_volume"] == 2_000_000
        # short_ratio = 100,000 / 2,000,000 * 100 = 5.0%
        assert day1["short_ratio"] == 5.0

        # Check second day data
        day2 = result["short_data"][1]
        assert day2["date"] == "2024-01-14"
        assert day2["total_volume"] == 1_600_000
        # short_ratio = 80,000 / 1,600,000 * 100 = 5.0%
        assert day2["short_ratio"] == 5.0

        # Check average short ratio
        assert result["avg_short_ratio"] == 5.0  # (5.0 + 5.0) / 2

        # Check balance data (from first entry with balance)
        assert "short_balance" in result
        assert result["short_balance"]["balance_shares"] == 1_234_567

    async def test_short_ratio_rounding_with_missing_volume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_SHORT_INTEREST_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        krx_response = {
            "OutBlock_1": [
                {
                    "TRD_DD": "2024/01/15",
                    "CVSRTSELL_TRDVOL": "100,000",
                    "CVSRTSELL_TRDVAL": "1,000,000,000",
                    "STR_CONST_VAL1": "1,234,567",
                    "STR_CONST_VAL2": "98,765,432,100",
                },
                {
                    "TRD_DD": "2024/01/14",
                    "CVSRTSELL_TRDVOL": "80,000",
                    "CVSRTSELL_TRDVAL": "800,000,000",
                    "STR_CONST_VAL1": "-",
                    "STR_CONST_VAL2": "-",
                },
            ]
        }

        class MockResponse:
            status_code = 200

            def json(self) -> dict:
                return krx_response

        async def mock_post(self, url: str, **kwargs) -> MockResponse:
            return MockResponse()

        import httpx

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        async def mock_daily_volumes(code: str, days: int) -> dict[str, int]:
            return {
                "2024-01-15": 3_000_000,
            }

        monkeypatch.setattr(naver_finance, "_fetch_daily_volumes", mock_daily_volumes)

        result = await naver_finance.fetch_short_interest("005930", days=20)

        day1 = result["short_data"][0]
        assert day1["total_volume"] == 3_000_000
        assert day1["short_ratio"] == 3.33

        day2 = result["short_data"][1]
        assert day2.get("total_volume") is None
        assert day2.get("short_ratio") is None

        assert result["avg_short_ratio"] == 3.33

    async def test_empty_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test with empty short selling data from KRX."""

        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_SHORT_INTEREST_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        async def mock_daily_volumes(code: str, days: int) -> dict[str, int]:
            return {}

        monkeypatch.setattr(naver_finance, "_fetch_daily_volumes", mock_daily_volumes)

        # Mock empty KRX API response
        class MockResponse:
            status_code = 200

            def json(self) -> dict:
                return {"OutBlock_1": []}

        async def mock_post(self, url: str, **kwargs) -> MockResponse:
            return MockResponse()

        import httpx

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        # Also mock pykrx fallback to return empty
        async def mock_pykrx_fetch(code: str, days: int) -> tuple[list, dict | None]:
            return [], None

        monkeypatch.setattr(
            naver_finance, "_fetch_short_data_from_pykrx", mock_pykrx_fetch
        )

        result = await naver_finance.fetch_short_interest("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["short_data"] == []
        assert result["avg_short_ratio"] is None
        assert "short_balance" not in result

    async def test_krx_exception_handling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test handling of KRX API exceptions."""

        async def mock_fetch_html(
            url: str, params: dict | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_SHORT_INTEREST_HTML, "lxml")

        monkeypatch.setattr(naver_finance, "_fetch_html", mock_fetch_html)

        async def mock_daily_volumes(code: str, days: int) -> dict[str, int]:
            return {}

        monkeypatch.setattr(naver_finance, "_fetch_daily_volumes", mock_daily_volumes)

        # Mock KRX API to raise exception
        async def mock_post(self, url: str, **kwargs) -> None:
            raise Exception("Network error")

        import httpx

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        # Also mock pykrx fallback to return empty
        async def mock_pykrx_fetch(code: str, days: int) -> tuple[list, dict | None]:
            return [], None

        monkeypatch.setattr(
            naver_finance, "_fetch_short_data_from_pykrx", mock_pykrx_fetch
        )

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
