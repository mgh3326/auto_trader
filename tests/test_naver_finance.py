"""Unit tests for Naver Finance service."""

from __future__ import annotations

from datetime import date
from typing import Any
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


class TestParseBasicInfo:
    """Tests for _parse_basic_info sub-parser."""

    def test_extracts_name_and_price(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
        result = naver_finance._parse_basic_info(soup)
        assert result["name"] == "삼성전자"
        assert result["current_price"] == 75000

    def test_missing_name(self) -> None:
        soup = BeautifulSoup("<html></html>", "lxml")
        result = naver_finance._parse_basic_info(soup)
        assert result["name"] is None
        assert result["current_price"] is None

    def test_fallback_price_parsing(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MINIMAL_MAIN_HTML, "lxml")
        result = naver_finance._parse_basic_info(soup)
        assert result["name"] == "효성중공업"
        assert result["current_price"] == 450000


class TestParseFinancialMetrics:
    """Tests for _parse_financial_metrics sub-parser."""

    def test_extracts_all_metrics(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] == 12.5
        assert result["pbr"] == 1.2
        assert result["roe"] == 18.5
        assert result["roe_controlling"] == 17.2
        assert abs(result["dividend_yield"] - 0.02) < 0.001

    def test_skips_zero_per(self) -> None:
        html = '<html><body><em id="_per">0</em></body></html>'
        soup = BeautifulSoup(html, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None

    def test_skips_na_per(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MINIMAL_MAIN_HTML, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None
        assert result["pbr"] == 2.1
        assert result["roe"] is None
        assert result["dividend_yield"] is None

    def test_empty_html(self) -> None:
        soup = BeautifulSoup("<html></html>", "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None
        assert result["pbr"] is None
        assert result["roe"] is None
        assert result["roe_controlling"] is None
        assert result["dividend_yield"] is None


class TestParseIndustryInfo:
    """Tests for _parse_industry_info sub-parser."""

    def test_extracts_exchange_and_sector(self) -> None:
        soup = BeautifulSoup(SAMPLE_PROFILE_HTML, "lxml")
        result = naver_finance._parse_industry_info(soup)
        assert result["exchange"] == "KOSPI"
        assert result["sector"] == "전기전자"

    def test_kosdaq_exchange(self) -> None:
        html = '<html><body><div class="code">123456 코스닥</div></body></html>'
        soup = BeautifulSoup(html, "lxml")
        result = naver_finance._parse_industry_info(soup)
        assert result["exchange"] == "KOSDAQ"
        assert result["sector"] is None

    def test_empty_html(self) -> None:
        soup = BeautifulSoup("<html></html>", "lxml")
        result = naver_finance._parse_industry_info(soup)
        assert result["exchange"] is None
        assert result["sector"] is None


class TestParsePeerComparison:
    """Tests for _parse_peer_comparison sub-parser."""

    def test_builds_sorted_peer_list(self) -> None:
        raw = [
            {
                "symbol": "AAA",
                "name": "Small",
                "current_price": 1000,
                "change_pct": 1.0,
                "per": 10.0,
                "pbr": 1.0,
                "market_cap": 100,
            },
            {
                "symbol": "BBB",
                "name": "Big",
                "current_price": 5000,
                "change_pct": -0.5,
                "per": 15.0,
                "pbr": 2.0,
                "market_cap": 999,
            },
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=5)
        assert len(result) == 2
        assert result[0]["symbol"] == "BBB"  # market_cap 999 first
        assert result[1]["symbol"] == "AAA"

    def test_none_entries_skipped(self) -> None:
        raw = [
            None,
            {
                "symbol": "CCC",
                "name": "Only",
                "current_price": 2000,
                "change_pct": 0.0,
                "per": 8.0,
                "pbr": 0.5,
                "market_cap": 50,
            },
            None,
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=5)
        assert len(result) == 1
        assert result[0]["symbol"] == "CCC"

    def test_limit_applied(self) -> None:
        raw = [
            {
                "symbol": f"S{i}",
                "name": f"Stock{i}",
                "current_price": 1000 * i,
                "change_pct": 0.0,
                "per": 10.0,
                "pbr": 1.0,
                "market_cap": 100 * i,
            }
            for i in range(1, 6)
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=3)
        assert len(result) == 3
        # Top 3 by market_cap: S5(500), S4(400), S3(300)
        assert [p["symbol"] for p in result] == ["S5", "S4", "S3"]

    def test_none_market_cap_sorted_last(self) -> None:
        raw = [
            {
                "symbol": "X",
                "name": "NoMcap",
                "current_price": 1000,
                "change_pct": 0.0,
                "per": None,
                "pbr": None,
                "market_cap": None,
            },
            {
                "symbol": "Y",
                "name": "HasMcap",
                "current_price": 2000,
                "change_pct": 0.0,
                "per": 5.0,
                "pbr": 1.0,
                "market_cap": 200,
            },
        ]
        result = naver_finance._parse_peer_comparison(raw, limit=5)
        assert result[0]["symbol"] == "Y"
        assert result[1]["symbol"] == "X"


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

SAMPLE_INVESTMENT_OPINIONS_DUPLICATE_HTML = """
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
            <td><a href="company_read.naver?nid=12345&page=9">반도체 업황 개선 전망</a></td>
            <td>삼성증권</td>
            <td><a href="https://example.com/report1.pdf"></a></td>
            <td class="date">26.01.15</td>
            <td>9999</td>
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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_NEWS_HTML, "lxml")

        monkeypatch.setattr(naver_finance.news, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_news("005930", limit=10)

        assert len(result) == 2
        assert result[0]["title"] == "삼성전자, 신제품 발표"
        assert result[0]["source"] == "연합뉴스"
        assert result[0]["datetime"] == "2024-01-15"
        assert "news_read.naver" in result[0]["url"]

    async def test_limit_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_NEWS_HTML, "lxml")

        monkeypatch.setattr(naver_finance.news, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_news("005930", limit=1)

        assert len(result) == 1

    async def test_empty_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance.news, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_news("005930")
        assert result == []


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchCompanyProfile:
    """Tests for fetch_company_profile function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_PROFILE_HTML, "lxml")

        monkeypatch.setattr(naver_finance.company, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            # Minimal HTML with only name
            return BeautifulSoup(
                '<div class="wrap_company"><h2><a>테스트</a></h2></div>',
                "lxml",
            )

        monkeypatch.setattr(naver_finance.company, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_INVESTOR_TRENDS_HTML, "lxml")

        monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup(SAMPLE_INVESTOR_TRENDS_HTML, "lxml")

        monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investor_trends("005930", days=1)

        assert len(result["data"]) == 1


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchInvestmentOpinions:
    """Tests for fetch_investment_opinions function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
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

        monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            if "company_list.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")
            elif "company_read.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_1, "lxml")
            elif "main.naver" in url:
                return BeautifulSoup(SAMPLE_CURRENT_PRICE_HTML, "lxml")
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investment_opinions("005930", limit=1)

        assert result["count"] == 1

    async def test_empty_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test with no opinions found."""

        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup("<html><table class='type_1'></table></html>", "lxml")

        monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
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

        monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

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

    async def test_deduplicates_duplicate_nids_before_detail_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        detail_calls: list[str] = []

        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            if "company_list.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_DUPLICATE_HTML, "lxml")
            if "company_read.naver" in url:
                nid = str((params or {}).get("nid", ""))
                detail_calls.append(nid)
                if nid == "12345":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_1, "lxml"
                    )
                if nid == "12346":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_2, "lxml"
                    )
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_CURRENT_PRICE_HTML, "lxml")
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        assert detail_calls == ["12345", "12346"]
        assert result["count"] == 2
        assert [opinion["target_price"] for opinion in result["opinions"]] == [
            85000,
            90000,
        ]
        assert result["consensus"]["avg_target_price"] == 87500
        assert result["consensus"]["current_price"] == 75000


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchKrSnapshot:
    async def test_snapshot_reuses_single_main_page_for_consensus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request_counts = {
            "main": 0,
            "sise": 0,
            "news": 0,
            "company_list": 0,
            "detail": 0,
        }

        async def mock_fetch_html_with_client(
            client, url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            _ = client
            if "main.naver" in url:
                request_counts["main"] += 1
                return BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
            if "sise.naver" in url:
                request_counts["sise"] += 1
                return BeautifulSoup(SAMPLE_VALUATION_SISE_HTML, "lxml")
            if "news_news.naver" in url:
                request_counts["news"] += 1
                return BeautifulSoup(SAMPLE_NEWS_HTML, "lxml")
            if "company_list.naver" in url:
                request_counts["company_list"] += 1
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")
            if "company_read.naver" in url:
                request_counts["detail"] += 1
                nid = str((params or {}).get("nid", ""))
                if nid == "12345":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_1, "lxml"
                    )
                if nid == "12346":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_2, "lxml"
                    )
            return BeautifulSoup("<html></html>", "lxml")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)
        monkeypatch.setattr(
            naver_finance.investor,
            "_fetch_html_with_client",
            mock_fetch_html_with_client,
            raising=False,
        )

        snapshot = await naver_finance._fetch_kr_snapshot(
            "005930", news_limit=5, opinion_limit=10
        )

        assert request_counts == {
            "main": 1,
            "sise": 1,
            "news": 1,
            "company_list": 1,
            "detail": 2,
        }
        assert snapshot["valuation"]["current_price"] == 75000
        assert snapshot["news"][0]["title"] == "삼성전자, 신제품 발표"
        assert snapshot["opinions"]["count"] == 2
        assert snapshot["opinions"]["consensus"]["avg_target_price"] == 87500
        assert snapshot["opinions"]["consensus"]["current_price"] == 75000
        assert abs(snapshot["opinions"]["consensus"]["upside_pct"] - 16.67) < 0.01

    async def test_snapshot_keeps_other_sections_when_one_page_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def mock_fetch_html_with_client(
            client: Any,
            url: str,
            params: dict[str, Any] | None = None,
        ) -> BeautifulSoup:
            _ = client, params
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
            if "sise.naver" in url:
                raise RuntimeError("sise unavailable")
            if "news_news.naver" in url:
                return BeautifulSoup(SAMPLE_NEWS_HTML, "lxml")
            if "company_list.naver" in url:
                return BeautifulSoup(SAMPLE_INVESTMENT_OPINIONS_HTML, "lxml")
            if "company_read.naver" in url:
                nid = str((params or {}).get("nid", ""))
                if nid == "12345":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_1, "lxml"
                    )
                if nid == "12346":
                    return BeautifulSoup(
                        SAMPLE_INVESTMENT_OPINIONS_DETAIL_HTML_2, "lxml"
                    )
            return BeautifulSoup("<html></html>", "lxml")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)
        monkeypatch.setattr(
            naver_finance.investor,
            "_fetch_html_with_client",
            mock_fetch_html_with_client,
            raising=False,
        )

        snapshot = await naver_finance._fetch_kr_snapshot(
            "005930", news_limit=5, opinion_limit=10
        )

        assert snapshot["valuation"] is None
        assert snapshot["news"][0]["title"] == "삼성전자, 신제품 발표"
        assert snapshot["opinions"]["count"] == 2
        assert snapshot["opinions"]["consensus"]["current_price"] == 75000


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


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchValuation:
    """Tests for fetch_valuation function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            # Return different HTML based on URL
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
            else:  # sise.naver
                return BeautifulSoup(SAMPLE_VALUATION_SISE_HTML, "lxml")

        monkeypatch.setattr(naver_finance.valuation, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_VALUATION_MINIMAL_MAIN_HTML, "lxml")
            else:
                return BeautifulSoup(SAMPLE_VALUATION_MINIMAL_SISE_HTML, "lxml")

        monkeypatch.setattr(naver_finance.valuation, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(main_html, "lxml")
            return BeautifulSoup(sise_html, "lxml")

        monkeypatch.setattr(naver_finance.valuation, "_fetch_html", mock_fetch_html)

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
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(main_html, "lxml")
            return BeautifulSoup(sise_html, "lxml")

        monkeypatch.setattr(naver_finance.valuation, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_valuation("000000")

        assert result["current_position_52w"] == 1.0

    async def test_empty_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test with empty HTML."""

        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            return BeautifulSoup("<html></html>", "lxml")

        monkeypatch.setattr(naver_finance.valuation, "_fetch_html", mock_fetch_html)

        result = await naver_finance.fetch_valuation("000000")

        assert result["symbol"] == "000000"
        assert result["name"] is None
        assert result["current_price"] is None
        assert result["current_position_52w"] is None


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeers:
    async def test_fetches_sector_page_once_for_codes_and_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sector_gets: list[tuple[str, dict[str, Any] | None]] = []

        class FakeResponse:
            content = (
                """
                <html>
                <head><title>반도체 : Npay 증권</title></head>
                <body>
                    <table class="type_5">
                        <tr><td><a href="/item/main.naver?code=000002">Peer</a></td></tr>
                    </table>
                </body>
                </html>
                """.encode("euc-kr")
            )

            @property
            def text(self) -> str:
                return self.content.decode("euc-kr")

        class FakeClient:
            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def get(
                self,
                url: str,
                params: dict[str, Any] | None = None,
            ) -> FakeResponse:
                sector_gets.append((url, params))
                return FakeResponse()

        async def fake_fetch_integration(
            code: str,
            _client: Any,
        ) -> dict[str, Any]:
            if code == "000001":
                return {
                    "symbol": code,
                    "name": "Target",
                    "per": 10,
                    "pbr": 1.1,
                    "market_cap": 1000,
                    "current_price": 50000,
                    "change_pct": 1.0,
                    "industry_code": "123",
                    "peers_raw": [],
                }
            return {
                "symbol": code,
                "name": "Peer",
                "per": 11,
                "pbr": 1.2,
                "market_cap": 900,
                "current_price": 40000,
                "change_pct": 0.5,
                "industry_code": "123",
                "peers_raw": [],
            }

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation,
            "_fetch_integration",
            fake_fetch_integration,
        )

        result = await naver_finance.fetch_sector_peers("000001", limit=1)

        assert result["sector"] == "반도체"
        assert result["peers"][0]["symbol"] == "000002"
        assert len(sector_gets) == 1
