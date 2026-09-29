"""Unit tests for Naver Finance service."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from app.services import naver_finance

# ROB-1296: this module is a provider *boundary* suite -- it drives the real
# client functions against a mocked HTTP layer, which is exactly what the
# autouse provider seams replace. Opt out so the code under test is the real
# thing. The transport backstop and the socket guard both stay in force, so
# opting out here still cannot reach a network.
pytestmark = pytest.mark.usefixtures("allow_external_providers")

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
        assert result["per"] == pytest.approx(12.5)
        assert result["pbr"] == pytest.approx(1.2)
        assert result["roe"] == pytest.approx(18.5)
        assert result["roe_controlling"] == pytest.approx(17.2)
        assert result["dividend_yield"] == pytest.approx(0.02, abs=0.001)

    def test_skips_zero_per(self) -> None:
        html = '<html><body><em id="_per">0</em></body></html>'
        soup = BeautifulSoup(html, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None

    def test_skips_na_per(self) -> None:
        soup = BeautifulSoup(SAMPLE_VALUATION_MINIMAL_MAIN_HTML, "lxml")
        result = naver_finance._parse_financial_metrics(soup)
        assert result["per"] is None
        assert result["pbr"] == pytest.approx(2.1)
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

    def test_parses_sector_from_upjong_link_with_number(self):
        """ROB-512: 현행 페이지의 동종업종비교 upjong 링크에서 한글 업종명과
        안정 식별자(업종번호 no=)를 추출한다. 구 셀렉터(div.tab_con1 em a)는
        현행 페이지에서 죽어 있다(2026-06-11 라이브 확인)."""
        html = (
            '<div class="section trade_compare"><h4 class="h_sub sub_tit7">'
            "<span>동종업종비교</span><em>(업종명 : "
            '<a href="/sise/sise_group_detail.naver?type=upjong&amp;no=278">'
            '반도체와반도체장비</a><span class="bar">｜</span>)</em></h4></div>'
        )
        soup = BeautifulSoup(html, "html.parser")
        result = naver_finance._parse_industry_info(soup)
        assert result["sector"] == "반도체와반도체장비"
        assert result["sector_no"] == "278"

    def test_sector_no_none_when_no_upjong_link(self):
        soup = BeautifulSoup("<div>업종 정보 없음</div>", "html.parser")
        result = naver_finance._parse_industry_info(soup)
        assert result["sector"] is None
        assert result["sector_no"] is None


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


# Synthesized from the desk-probed key list for the m.stock.naver.com news API
# (#904 hk comment): array of groups [{total, items:[article]}]. This is NOT a
# captured real response — it is assembled from the probed field names.
SAMPLE_NEWS_JSON_PAYLOAD: list[dict[str, Any]] = [
    {
        "total": 2,
        "items": [
            {
                "id": "0010001234",
                "officeId": "001",
                "articleId": "0001234",
                "officeName": "연합뉴스",
                "datetime": "202609281530",
                "type": "article",
                "title": "삼성전자, 신제품 발표",
                "titleFull": "삼성전자, 신제품 발표",
                "body": "삼성전자가 신제품을 발표했다.",
                "photoType": 1,
                "imageOriginLink": (
                    "https://imgnews.pstatic.net/image/001/20260928/x.jpg"
                ),
                "mobileNewsUrl": ("https://n.news.naver.com/mnews/article/001/0001234"),
            },
            {
                "id": "0090005678",
                "officeId": "009",
                "articleId": "0005678",
                "officeName": "한국경제",
                "datetime": "202609281430",
                "type": "article",
                "title": "반도체 시장 전망",
                "titleFull": "반도체 시장 전망",
                "body": "반도체 시장 전망 요약.",
                "photoType": 0,
                "imageOriginLink": None,
                "mobileNewsUrl": ("https://n.news.naver.com/mnews/article/009/0005678"),
            },
        ],
    },
    {
        "total": 1,
        "items": [
            {
                "id": "0010009999",
                "officeId": "001",
                "articleId": "0009999",
                "officeName": "연합뉴스",
                "datetime": "202609271800",
                "type": "article",
                "title": "이전 그룹의 기사",
                "titleFull": "이전 그룹의 기사",
                "body": "",
                "photoType": 0,
                "imageOriginLink": None,
                "mobileNewsUrl": ("https://n.news.naver.com/mnews/article/001/0009999"),
            },
        ],
    },
]


def _normalized_news_item() -> dict[str, Any]:
    """The normalized dict shape ``fetch_stock_news`` emits for one article."""
    return {
        "title": "삼성전자, 신제품 발표",
        "url": "https://n.news.naver.com/mnews/article/001/0001234",
        "source": "연합뉴스",
        "datetime": "2026-09-28T15:30:00+09:00",
        "id": "0010001234",
        "officeId": "001",
        "articleId": "0001234",
    }


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

# Synthesized from the desk-verified trend endpoint shape
# (m.stock.naver.com/api/stock/{code}/trend) — see tests/fixtures/investor_flow/.
_TREND_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "investor_flow"


def _load_trend_fixture(name: str = "005930_trend_synthesized.json") -> Any:
    import json

    return json.loads((_TREND_FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ROB-486: 리스트 fixture 날짜를 상대값으로 생성해 recency 윈도우 시한폭탄을 막는다.
_OPINION_LIST_DATE_RECENT_1 = date.today() - timedelta(days=30)
_OPINION_LIST_DATE_RECENT_2 = date.today() - timedelta(days=45)
_OPINION_LIST_DATE_STALE = date.today() - timedelta(days=400)


def _research_write_date(d: date) -> str:
    """writeDate arrives as ISO 'YYYY-MM-DD' on the mobile JSON API (#930)."""
    return d.isoformat()


# #930: the retired finance.naver.com company_list/company_read HTML samples are
# gone. The m.stock.naver.com JSON payloads below mirror the desk-captured
# fixtures in tests/fixtures/naver_research/ (list_005930.json /
# detail_96343.json).
_RESEARCH_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "naver_research"


def _load_research_fixture(name: str) -> Any:
    import json

    return json.loads((_RESEARCH_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _research_list_item(
    research_id: int,
    *,
    item_code: str = "005930",
    item_name: str = "삼성전자",
    title: str = "리포트",
    broker: str = "삼성증권",
    write_date: date | None = None,
) -> dict[str, Any]:
    return {
        "researchCategory": "종목분석",
        "category": "종목분석",
        "itemCode": item_code,
        "itemName": item_name,
        "researchId": research_id,
        "title": title,
        "brokerName": broker,
        "writeDate": _research_write_date(write_date or _OPINION_LIST_DATE_RECENT_1),
        "readCount": "123",
        "previewContent": "",
    }


def _research_detail_payload(
    research_id: int,
    *,
    item_code: str = "005930",
    opinion: Any = "Buy",
    goal_price: Any = "85000",
) -> dict[str, Any]:
    return {
        "researchContent": {
            "itemCode": item_code,
            "itemName": "삼성전자",
            "researchId": research_id,
            "title": "리포트",
            "brokerName": "삼성증권",
            "writeDate": _research_write_date(_OPINION_LIST_DATE_RECENT_1),
            "readCount": "1",
            "attachUrl": "https://example.com/r.pdf",
            "content": "",
            "opinion": opinion,
            "goalPrice": goal_price,
            "prevGoalPrice": None,
            "priceAtWriteDate": "75000",
        },
        "researchSummaries": [],
    }


SAMPLE_RESEARCH_LIST_005930 = [
    _research_list_item(
        12345,
        title="반도체 업황 개선 전망",
        broker="삼성증권",
        write_date=_OPINION_LIST_DATE_RECENT_1,
    ),
    _research_list_item(
        12346,
        title="실적 호조 지속",
        broker="미래에셋",
        write_date=_OPINION_LIST_DATE_RECENT_2,
    ),
]

SAMPLE_RESEARCH_DETAILS_005930 = {
    12345: _research_detail_payload(12345, opinion="Buy", goal_price="85000"),
    12346: _research_detail_payload(12346, opinion="StrongBuy", goal_price="90000"),
}

SAMPLE_RESEARCH_LIST_DUPLICATE = [
    SAMPLE_RESEARCH_LIST_005930[0],
    {**SAMPLE_RESEARCH_LIST_005930[0], "readCount": "9999"},
    SAMPLE_RESEARCH_LIST_005930[1],
]

SAMPLE_RESEARCH_LIST_005880_MIXED_STALE = [
    _research_list_item(
        22345,
        item_code="005880",
        item_name="대한해운",
        title="실적 전망",
        broker="신한투자증권",
        write_date=_OPINION_LIST_DATE_RECENT_1,
    ),
    _research_list_item(
        22346,
        item_code="005880",
        item_name="대한해운",
        title="구 리포트",
        broker="하나증권",
        write_date=_OPINION_LIST_DATE_STALE,
    ),
]


def _detail_005880(research_id: int, goal_price: Any) -> dict[str, Any]:
    return _research_detail_payload(
        research_id,
        item_code="005880",
        opinion="Buy",
        goal_price=goal_price,
    )


def _research_json_stub(
    *,
    list_payload: Any,
    details: dict[int, Any] | None = None,
    detail_errors: dict[int, BaseException] | None = None,
) -> Any:
    """A ``_fetch_research_json_with_client`` stand-in routing on the URL path.

    ``.calls`` records every requested URL so tests can prove the detail path
    is built from researchId and never from the stock code (#930 trap).
    """

    calls: list[str] = []

    async def _stub(
        client: Any, url: str, params: dict[str, Any] | None = None
    ) -> Any:
        _ = client, params
        calls.append(url)
        if "/api/research/stock/" in url:
            return list_payload
        if "/api/research/company/" in url:
            research_id = int(url.rsplit("/", 1)[-1])
            if detail_errors and research_id in detail_errors:
                raise detail_errors[research_id]
            payload = (details or {}).get(research_id)
            if payload is None:
                payload = _research_detail_payload(research_id)
            return payload
        raise AssertionError(f"unexpected research url {url}")

    _stub.calls = calls
    return _stub


def _stub_current_price(
    monkeypatch: pytest.MonkeyPatch, soup_html: str
) -> None:
    async def mock_fetch_html(
        url: str, params: dict[str, Any] | None = None
    ) -> BeautifulSoup:
        _ = url, params
        return BeautifulSoup(soup_html, "lxml")

    monkeypatch.setattr(naver_finance.investor, "_fetch_html", mock_fetch_html)

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

SAMPLE_CURRENT_PRICE_HTML_005880 = """
<html><body>
<p class="no_today">
    <span class="blind">현재가</span>
    <em><span class="blind">1,914</span></em>
</p>
</body></html>
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
    """fetch_stock_news / fetch_news against the m.stock.naver.com JSON shape.

    Fixture is synthesized from the #904 desk-probed key list; the legacy
    table.type5 HTML path was retired upstream (410 Gone).
    """

    def _patch_json(
        self, monkeypatch: pytest.MonkeyPatch, payload: Any
    ) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            seen["url"] = url
            seen["params"] = params
            return payload

        monkeypatch.setattr(naver_finance.news, "_fetch_json", mock_fetch_json)
        return seen

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._patch_json(monkeypatch, SAMPLE_NEWS_JSON_PAYLOAD)

        result = await naver_finance.fetch_stock_news("005930", limit=10)

        assert seen["url"] == "https://m.stock.naver.com/api/news/stock/005930"
        assert seen["params"]["page"] == 1
        assert result.skipped == {}
        assert [item["title"] for item in result.items] == [
            "삼성전자, 신제품 발표",
            "반도체 시장 전망",
            "이전 그룹의 기사",
        ]
        first = result.items[0]
        assert first["source"] == "연합뉴스"
        assert first["datetime"] == "2026-09-28T15:30:00+09:00"
        assert first["url"] == ("https://n.news.naver.com/mnews/article/001/0001234")
        assert first["id"] == "0010001234"
        assert first["officeId"] == "001"
        assert first["articleId"] == "0001234"

    async def test_fetch_news_compat_returns_items(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_json(monkeypatch, SAMPLE_NEWS_JSON_PAYLOAD)
        result = await naver_finance.fetch_news("005930", limit=10)
        assert [item["title"] for item in result] == [
            "삼성전자, 신제품 발표",
            "반도체 시장 전망",
            "이전 그룹의 기사",
        ]

    async def test_limit_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_json(monkeypatch, SAMPLE_NEWS_JSON_PAYLOAD)
        result = await naver_finance.fetch_stock_news("005930", limit=1)
        assert len(result.items) == 1
        assert result.items[0]["title"] == "삼성전자, 신제품 발표"

    async def test_empty_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_json(monkeypatch, [])
        result = await naver_finance.fetch_stock_news("005930")
        assert result.items == []
        assert result.skipped == {}

    async def test_malformed_items_are_skipped_with_counted_reasons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = [
            "not-a-dict-group",
            {"items": "not-a-list"},
            {
                "total": 5,
                "items": [
                    "not-a-dict-item",
                    {
                        "officeId": "001",
                        "mobileNewsUrl": "https://x/a",
                        "datetime": "202609281530",
                    },
                    {
                        "title": "URL 없는 기사",
                        "datetime": "202609281530",
                        "officeId": "001",
                    },
                    {
                        "title": "시간 깨진 기사",
                        "mobileNewsUrl": "https://x/b",
                        "datetime": "not-a-time",
                    },
                    SAMPLE_NEWS_JSON_PAYLOAD[0]["items"][0],
                    # duplicate id — same article re-served in another group
                    dict(SAMPLE_NEWS_JSON_PAYLOAD[0]["items"][0]),
                ],
            },
        ]
        self._patch_json(monkeypatch, payload)

        result = await naver_finance.fetch_stock_news("005930", limit=10)

        assert len(result.items) == 1
        assert result.items[0]["id"] == "0010001234"
        assert result.skipped == {
            "invalid_group": 2,
            "invalid_item": 1,
            "missing_title": 1,
            "missing_url": 1,
            "invalid_datetime": 1,
            "duplicate": 1,
        }

    async def test_non_list_payload_raises_contract_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_json(monkeypatch, {"error": "blocked"})
        with pytest.raises(naver_finance.NaverNewsContractError):
            await naver_finance.fetch_stock_news("005930")

    async def test_provider_http_error_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            raise httpx.HTTPStatusError(
                "410 Gone",
                request=httpx.Request("GET", url),
                response=httpx.Response(410),
            )

        monkeypatch.setattr(naver_finance.news, "_fetch_json", mock_fetch_json)

        with pytest.raises(httpx.HTTPStatusError):
            await naver_finance.fetch_stock_news("005930")


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
        assert result["per"] == pytest.approx(15.23)
        assert result["pbr"] == pytest.approx(1.45)
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
    """Tests for fetch_investor_trends (Naver mobile trend JSON, #900)."""

    async def test_success_parses_fixture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            captured["url"] = url
            captured["params"] = params
            return _load_trend_fixture()

        monkeypatch.setattr(naver_finance.investor, "_fetch_json", mock_fetch_json)

        result = await naver_finance.fetch_investor_trends("005930", days=20)

        assert captured["url"] == "https://m.stock.naver.com/api/stock/005930/trend"
        assert captured["params"] == {"pageSize": 20}
        assert result["symbol"] == "005930"
        assert result["skipped"] == {}
        assert len(result["data"]) == 4

        day1 = result["data"][0]
        assert day1["date"] == "2026-09-28"
        assert day1["close"] == 75500
        # change is derived from the next row's closePrice (newest-first).
        assert day1["change"] == 500
        assert day1["change_pct"] == pytest.approx(500 / 75000)
        assert day1["volume"] == 12345678
        # Signed comma strings keep their sign.
        assert day1["foreign_net"] == 4513767
        assert day1["institutional_net"] == 1234567
        assert day1["individual_net"] == -5748334
        # Percent string parses to a 0..100 rate (not a fraction).
        assert day1["foreign_holding_rate"] == pytest.approx(46.64)
        # Not in the payload — NULL, never fabricated.
        assert day1["foreign_holding_shares"] is None

        day2 = result["data"][1]
        assert day2["date"] == "2026-09-25"
        assert day2["foreign_net"] == -500000
        assert day2["institutional_net"] == -200000
        assert day2["individual_net"] == 700000
        assert day2["change"] == 500
        assert day2["change_pct"] == pytest.approx(500 / 74500)

        # Oldest row has no prior close in the payload -> no derived change.
        oldest = result["data"][-1]
        assert oldest["date"] == "2026-09-23"
        assert oldest["foreign_net"] == 0
        assert oldest["change"] is None
        assert oldest["change_pct"] is None

    async def test_days_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            captured["params"] = params
            return _load_trend_fixture()

        monkeypatch.setattr(naver_finance.investor, "_fetch_json", mock_fetch_json)

        result = await naver_finance.fetch_investor_trends("005930", days=1)

        assert captured["params"] == {"pageSize": 1}
        assert len(result["data"]) == 1
        assert result["data"][0]["date"] == "2026-09-28"

    async def test_malformed_rows_skipped_with_counted_reasons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            return _load_trend_fixture("005930_trend_malformed.json")

        monkeypatch.setattr(naver_finance.investor, "_fetch_json", mock_fetch_json)

        result = await naver_finance.fetch_investor_trends("005930", days=20)

        # Only the last (fully valid) row survives.
        assert [r["date"] for r in result["data"]] == ["2026-09-22"]
        assert result["skipped"] == {
            "invalid bizdate": 1,
            "invalid foreignerPureBuyQuant": 2,  # empty string + bad grouping
            "missing organPureBuyQuant": 1,
            "invalid individualPureBuyQuant": 1,
            "row is not a JSON object": 1,
        }

    async def test_missing_flow_fields_skip_row_but_keep_valid_siblings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            return [
                {
                    "bizdate": "20260928",
                    "closePrice": "75,500",
                    "foreignerPureBuyQuant": "+100",
                    # organPureBuyQuant missing entirely
                    "individualPureBuyQuant": "-50",
                },
                {
                    "bizdate": "20260925",
                    "closePrice": "75,000",
                    "foreignerPureBuyQuant": "-200",
                    "organPureBuyQuant": "-",
                    "individualPureBuyQuant": "+150",
                },
                {
                    "bizdate": "20260924",
                    "closePrice": "74,500",
                    "foreignerPureBuyQuant": "+300",
                    "organPureBuyQuant": "+40",
                    "individualPureBuyQuant": "-340",
                },
            ]

        monkeypatch.setattr(naver_finance.investor, "_fetch_json", mock_fetch_json)

        result = await naver_finance.fetch_investor_trends("005930", days=20)

        assert [r["date"] for r in result["data"]] == ["2026-09-24"]
        assert result["skipped"] == {
            "missing organPureBuyQuant": 1,
            "invalid organPureBuyQuant": 1,
        }

    async def test_optional_fields_malformed_keep_row_with_nulls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            return [
                {
                    "bizdate": "20260928",
                    "closePrice": "",
                    "accumulatedTradingVolume": "-",
                    "foreignerPureBuyQuant": "+100",
                    "organPureBuyQuant": "+40",
                    "individualPureBuyQuant": "-140",
                    "foreignerHoldRatio": "abc",
                }
            ]

        monkeypatch.setattr(naver_finance.investor, "_fetch_json", mock_fetch_json)

        result = await naver_finance.fetch_investor_trends("005930", days=20)

        assert len(result["data"]) == 1
        row = result["data"][0]
        assert row["foreign_net"] == 100
        assert row["close"] is None
        assert row["volume"] is None
        assert row["foreign_holding_rate"] is None
        assert row["change"] is None
        assert row["change_pct"] is None

    async def test_non_list_payload_returns_empty_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A maintenance/error body that decodes to a dict instead of a list
        # must yield the documented empty shape (data == []), never raise — but
        # it is counted as an invalid-response skip so it is distinguishable
        # from a legitimately empty day.
        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            return {"error": "maintenance"}

        monkeypatch.setattr(naver_finance.investor, "_fetch_json", mock_fetch_json)

        result = await naver_finance.fetch_investor_trends("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["data"] == []
        assert result["skipped"] == {"payload is not a JSON list": 1}

    async def test_empty_list_payload_returns_empty_data(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def mock_fetch_json(
            url: str, params: dict[str, Any] | None = None
        ) -> Any:
            return []

        monkeypatch.setattr(naver_finance.investor, "_fetch_json", mock_fetch_json)

        result = await naver_finance.fetch_investor_trends("005930", days=20)

        assert result["data"] == []
        assert result["skipped"] == {}


@pytest.mark.unit
class TestParseTrendHelpers:
    """Direct unit tests for the trend JSON parsers (#900)."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("+4,513,767", 4513767),
            ("-500,000", -500000),
            ("+0", 0),
            ("0", 0),
            ("45,13,767", None),  # non-canonical grouping = corruption, reject
            ("1,23", None),
            ("1234,567", None),
            ("+1,234,567", 1234567),
            (4513767, 4513767),
            (5.0, 5),
            ("-", None),
            ("+", None),
            ("", None),
            ("   ", None),
            (None, None),
            (True, None),
            (5.5, None),
            ("5.5", None),
            ("abc", None),
            ("+4,513,767원", None),
        ],
    )
    def test_parse_trend_int(self, value: Any, expected: int | None) -> None:
        assert naver_finance.investor._parse_trend_int(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("20260928", "2026-09-28"),
            ("20240101", "2024-01-01"),
            ("2026-09-28", None),
            ("2026928", None),
            ("20261301", None),
            ("20260931", None),
            ("", None),
            (None, None),
            ("abcd1234", None),
        ],
    )
    def test_parse_trend_bizdate(self, value: Any, expected: str | None) -> None:
        assert naver_finance.investor._parse_trend_bizdate(value) == expected


@pytest.mark.unit
class TestParseHoldingRate:
    """ROB-448: foreign holding-rate parser (percent 0..100). Module-level/sync so it
    does NOT inherit TestFetchInvestorTrends's class-level @pytest.mark.asyncio."""

    def test_parse_holding_rate_unit(self) -> None:
        # '%' must NOT trigger parse_korean_number's /100 (which would yield 0.4773).
        assert naver_finance.investor._parse_holding_rate("47.73%") == pytest.approx(
            47.73
        )
        assert naver_finance.investor._parse_holding_rate("12.5") == pytest.approx(12.5)
        assert naver_finance.investor._parse_holding_rate("") is None
        assert naver_finance.investor._parse_holding_rate(None) is None


@pytest.mark.unit
class TestParseTotalInfos:
    """ROB-448: directly exercise _parse_total_infos (the eps/bps/market_cap source the
    fetch_valuation overlay surfaces) — all overlay tests stub _fetch_integration, so
    without this the raw-JSON → parsed-metric loop (and a typo like .get('esp')) is
    untested."""

    def test_parses_metrics_with_unit_suffixes(self) -> None:
        result = naver_finance.valuation._parse_total_infos(
            [
                {"code": "eps", "value": "5,432원"},
                {"code": "bps", "value": "50,000원"},
                {"code": "marketValue", "value": "400조"},
                {"code": "per", "value": "12.5배"},
                {"code": "pbr", "value": "1.2배"},
                {"code": "unmapped", "value": "ignore me"},
            ]
        )
        assert result["eps"] == pytest.approx(5432)  # won/share, 원 stripped
        assert result["bps"] == pytest.approx(50000)
        assert result["market_cap"] == pytest.approx(
            400_000_000_000_000
        )  # 400조 raw KRW
        assert result["per"] == pytest.approx(12.5)
        assert result["pbr"] == pytest.approx(1.2)
        assert "unmapped" not in result


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchInvestmentOpinions:
    """Tests for fetch_investment_opinions function."""

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930,
            details=SAMPLE_RESEARCH_DETAILS_005930,
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        assert result["symbol"] == "005930"
        assert result["count"] == 2
        assert len(result["opinions"]) == 2
        assert "warnings" not in result

        # First opinion
        op1 = result["opinions"][0]
        assert op1["stock_name"] == "삼성전자"
        assert op1["title"] == "반도체 업황 개선 전망"
        assert op1["firm"] == "삼성증권"
        assert op1["rating"] == "Buy"
        assert op1["rating_bucket"] == "buy"
        assert op1["target_price"] == 85000
        assert op1["date"] == _OPINION_LIST_DATE_RECENT_1.isoformat()
        assert op1["url"] == "https://m.stock.naver.com/research/company/12345"

        # Second opinion — the compact "StrongBuy" label normalizes to Strong Buy
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
        assert consensus["upside_pct"] == pytest.approx(16.67, abs=0.01)
        assert consensus["current_price"] == 75000

    async def test_detail_urls_use_research_id_never_stock_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#930 trap: /api/research/company/{stock_code} reads researchId 5930
        of a DIFFERENT stock. Every detail URL must carry a researchId that
        came out of the list payload — never the requested stock code."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930,
            details=SAMPLE_RESEARCH_DETAILS_005930,
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        await naver_finance.fetch_investment_opinions("005930", limit=10)

        assert stub.calls[0] == (
            "https://m.stock.naver.com/api/research/stock/005930"
        )
        detail_calls = [u for u in stub.calls if "/api/research/company/" in u]
        assert detail_calls == [
            "https://m.stock.naver.com/api/research/company/12345",
            "https://m.stock.naver.com/api/research/company/12346",
        ]
        assert not any(u.endswith("/005930") for u in detail_calls)

    async def test_desk_fixture_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real desk-captured payloads (list_005930 + detail_96343) drive
        the full pipeline: 10 rows, ISO writeDate parsing, and the compact
        'StrongBuy' label -> 'Strong Buy'/'buy' bucket. window_months=60 keeps
        the fixed 2026 fixture dates inside the recency window regardless of
        the day the suite runs."""
        list_payload = _load_research_fixture("list_005930.json")
        detail_96343 = _load_research_fixture("detail_96343.json")
        stub = _research_json_stub(
            list_payload=list_payload, details={96343: detail_96343}
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions(
            "005930", limit=10, window_months=60
        )

        assert result["count"] == 10
        first = result["opinions"][0]
        assert first["firm"] == "유진투자증권"
        assert first["date"] == "2026-09-29"
        assert first["title"] == "긴 호흡으로"
        assert first["rating"] == "Strong Buy"
        assert first["rating_bucket"] == "buy"
        assert first["target_price"] == 560000
        assert first["url"] == (
            "https://m.stock.naver.com/research/company/96343"
        )
        assert result["consensus"]["total_count"] == 10
        assert result["consensus"]["buy_count"] == 10
        assert result["consensus"]["strong_buy_count"] == 1
        assert result["consensus"]["window_months"] == 60
        assert result["consensus"]["newest_opinion_date"] == "2026-09-29"

    async def test_limit_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930,
            details=SAMPLE_RESEARCH_DETAILS_005930,
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions("005930", limit=1)

        assert result["count"] == 1

    async def test_empty_list_raises_not_silent_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#930 regression lock: an empty research list — the exact signature
        of the retired redirecting endpoint — raises instead of producing a
        valid-looking zero-opinion result."""
        stub = _research_json_stub(list_payload=[], details={})
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        with pytest.raises(
            naver_finance.NaverResearchContractError, match="0 reports"
        ):
            await naver_finance.fetch_investment_opinions("005930", limit=10)

    async def test_non_list_payload_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _research_json_stub(
            list_payload={"result": "not-a-list"}, details={}
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        with pytest.raises(
            naver_finance.NaverResearchContractError, match="expected a JSON list"
        ):
            await naver_finance.fetch_investment_opinions("005930", limit=10)

    async def test_all_malformed_rows_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _research_json_stub(
            list_payload=[{"junk": True}, "not-a-dict"],
            details={},
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        with pytest.raises(
            naver_finance.NaverResearchContractError,
            match="every row was malformed",
        ):
            await naver_finance.fetch_investment_opinions("005930", limit=10)

    async def test_malformed_row_warned_and_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        list_payload = [
            SAMPLE_RESEARCH_LIST_005930[0],
            # A row for a different symbol — the filter-drift signature.
            _research_list_item(99999, item_code="000660"),
            {"researchId": None},
            SAMPLE_RESEARCH_LIST_005930[1],
        ]
        stub = _research_json_stub(
            list_payload=list_payload,
            details=SAMPLE_RESEARCH_DETAILS_005930,
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        assert result["count"] == 2
        assert any("itemCode mismatch" in w for w in result["warnings"])
        assert any("researchId" in w for w in result["warnings"])
        assert all(o["rating_bucket"] == "buy" for o in result["opinions"])

    async def test_missing_target_price(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real report without a target price stays a valid row — target_price
        is None and the row still counts."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930,
            details={
                12345: _research_detail_payload(
                    12345, opinion="Hold", goal_price=None
                ),
                12346: SAMPLE_RESEARCH_DETAILS_005930[12346],
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        # First opinion has no target price, second has 90000
        assert result["opinions"][0]["target_price"] is None
        assert result["opinions"][0]["rating"] == "Hold"
        assert result["opinions"][1]["target_price"] == 90000

        # Stats should only use the one with target price
        consensus = result["consensus"]
        assert consensus["avg_target_price"] == 90000
        assert consensus["max_target_price"] == 90000
        assert consensus["min_target_price"] == 90000

    async def test_partial_detail_failure_keeps_row_unrated_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single detail outage must NOT fabricate a Hold vote: the row is
        reported with rating=None / rating_bucket='unrated' — counted in
        total_count but in none of buy/hold/sell — plus a warnings entry."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930,
            details=SAMPLE_RESEARCH_DETAILS_005930,
            detail_errors={12345: RuntimeError("detail boom")},
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        assert result["count"] == 2
        failed = result["opinions"][0]
        assert failed["rating"] is None
        assert failed["rating_bucket"] == "unrated"
        assert failed["target_price"] is None
        assert failed["title"] == "반도체 업황 개선 전망"
        assert any(
            "researchId 12345" in w and "detail boom" in w
            for w in result["warnings"]
        )
        consensus = result["consensus"]
        assert consensus["total_count"] == 2
        assert consensus["rows_used"] == 2
        assert consensus["buy_count"] == 1
        assert consensus["hold_count"] == 0
        assert consensus["sell_count"] == 0
        assert consensus["avg_target_price"] == 90000

    async def test_all_detail_failures_raise_not_silent_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every detail failing must raise — an all-unrated result would cache
        and serve as a valid-looking zero-signal consensus (#930)."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930,
            details={},
            detail_errors={
                12345: RuntimeError("boom1"),
                12346: RuntimeError("boom2"),
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        with pytest.raises(
            naver_finance.NaverResearchContractError,
            match="detail fetch failed for all",
        ):
            await naver_finance.fetch_investment_opinions("005930", limit=10)

    async def test_detail_item_code_mismatch_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A detail payload for a different stock is contract corruption —
        the #930 stock-code trap must surface, not silently attach."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930[:1],
            details={
                12345: _research_detail_payload(12345, item_code="000660"),
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        with pytest.raises(
            naver_finance.NaverResearchContractError,
            match="detail fetch failed for all",
        ):
            await naver_finance.fetch_investment_opinions("005930", limit=10)

    async def test_detail_research_id_mismatch_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """researchContent.researchId is re-verified against the requested id —
        a detail served for researchId 5930 (the stock-code trap shape) is
        rejected rather than mapped."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005930[:1],
            details={
                12345: _research_detail_payload(5930),
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        with pytest.raises(
            naver_finance.NaverResearchContractError,
            match="detail fetch failed for all",
        ):
            await naver_finance.fetch_investment_opinions("005930", limit=10)

    async def test_opinion_label_mapping_to_buckets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#930 AC1: map Naver opinion labels to buy/hold/sell. Compact
        English labels (StrongBuy/TradingBuy) and Korean labels both resolve;
        an absent opinion maps to Hold, never fabricated as Buy."""
        list_payload = [
            _research_list_item(1),
            _research_list_item(2),
            _research_list_item(3),
            _research_list_item(4),
            _research_list_item(5),
            _research_list_item(6),
        ]
        stub = _research_json_stub(
            list_payload=list_payload,
            details={
                1: _research_detail_payload(1, opinion="StrongBuy"),
                2: _research_detail_payload(2, opinion="TradingBuy"),
                3: _research_detail_payload(3, opinion="Hold"),
                4: _research_detail_payload(4, opinion="Sell"),
                5: _research_detail_payload(5, opinion="매수"),
                6: _research_detail_payload(6, opinion=None),
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        buckets = [o["rating_bucket"] for o in result["opinions"]]
        assert buckets == ["buy", "buy", "hold", "sell", "buy", "hold"]
        labels = [o["rating"] for o in result["opinions"]]
        assert labels == [
            "Strong Buy",
            "Buy",
            "Hold",
            "Sell",
            "Buy",
            "Hold",
        ]
        consensus = result["consensus"]
        assert consensus["strong_buy_count"] == 1
        assert consensus["buy_count"] == 3
        assert consensus["hold_count"] == 2
        assert consensus["sell_count"] == 1
        assert consensus["total_count"] == 6

    async def test_deduplicates_duplicate_research_ids_before_detail_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_DUPLICATE,
            details=SAMPLE_RESEARCH_DETAILS_005930,
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML)

        result = await naver_finance.fetch_investment_opinions("005930", limit=10)

        detail_calls = [u for u in stub.calls if "/api/research/company/" in u]
        assert detail_calls == [
            "https://m.stock.naver.com/api/research/company/12345",
            "https://m.stock.naver.com/api/research/company/12346",
        ]
        assert result["count"] == 2
        assert [opinion["target_price"] for opinion in result["opinions"]] == [
            85000,
            90000,
        ]
        assert result["consensus"]["avg_target_price"] == 87500
        assert result["consensus"]["current_price"] == 75000
        assert any(
            "duplicate researchId" in w for w in result.get("warnings", [])
        )

    async def test_recency_window_excludes_stale_targets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROB-486 (005880 모양): 12개월 밖 목표가는 집계 제외 + 메타데이터 보고."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005880_MIXED_STALE,
            details={
                22345: _detail_005880(22345, "3000"),
                22346: _detail_005880(22346, "23000"),
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML_005880)

        result = await naver_finance.fetch_investment_opinions("005880", limit=10)

        # opinions 리스트에는 stale 행도 참고용으로 그대로 남는다.
        assert result["count"] == 2
        consensus = result["consensus"]
        assert consensus["avg_target_price"] is None
        assert consensus["median_target_price"] is None
        assert consensus["upside_pct"] is None
        assert consensus["buy_count"] == 1
        assert consensus["total_count"] == 1
        assert consensus["rows_total"] == 2
        assert consensus["rows_used"] == 1
        assert consensus["rows_excluded_stale"] == 1
        assert consensus["rows_undated"] == 0
        assert consensus["window_months"] == 12
        assert consensus["target_price_honest"] is False
        assert (
            consensus["newest_opinion_date"] == _OPINION_LIST_DATE_RECENT_1.isoformat()
        )

    async def test_window_months_param_threads_to_consensus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROB-486: window_months 파라미터가 build_consensus 까지 전달된다.

        400일 전 행의 목표가는 비-outlier(5,000 — 현재가 1,914 기준 +161%)로
        두어 recency 윈도우 효과만 분리 검증한다 (outlier 가드는 sibling 테스트).
        """
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005880_MIXED_STALE,
            details={
                22345: _detail_005880(22345, "3000"),
                22346: _detail_005880(22346, "5000"),
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML_005880)

        result = await naver_finance.fetch_investment_opinions(
            "005880", limit=10, window_months=24
        )

        consensus = result["consensus"]
        assert consensus["window_months"] == 24
        # 400일 전 행도 24개월 윈도우에는 생존 → (3000+5000)/2.
        assert consensus["rows_used"] == 2
        assert consensus["avg_target_price"] == 4000
        assert consensus["target_price_outlier_count"] == 0
        assert consensus["target_price_honest"] is True

    async def test_window_survivor_outlier_target_still_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ROB-486+488 레이어링: recency 윈도우 생존 행이라도 outlier 목표가
        (23,000 — 현재가 1,914 기준 +1,101% > +300%)는 집계에서 제외된다.
        카운트(rows_used)는 크레딧, 목표가 통계만 필터."""
        stub = _research_json_stub(
            list_payload=SAMPLE_RESEARCH_LIST_005880_MIXED_STALE,
            details={
                22345: _detail_005880(22345, "3000"),
                22346: _detail_005880(22346, "23000"),
            },
        )
        monkeypatch.setattr(
            naver_finance.investor, "_fetch_research_json_with_client", stub
        )
        _stub_current_price(monkeypatch, SAMPLE_CURRENT_PRICE_HTML_005880)

        result = await naver_finance.fetch_investment_opinions(
            "005880", limit=10, window_months=24
        )

        consensus = result["consensus"]
        assert consensus["window_months"] == 24
        # 두 행 모두 24개월 윈도우 생존 — 카운트는 2.
        assert consensus["rows_used"] == 2
        # 23,000 은 outlier 가드로 목표가 집계에서만 제외 → avg 는 3,000.
        assert consensus["avg_target_price"] == 3000
        assert consensus["target_price_outlier_count"] == 1
        assert consensus["target_price_honest"] is False


@pytest.mark.asyncio
@pytest.mark.unit
class TestResearchContractEdges:
    """#930: payload-contract edge cases — the fail-loud shapes that must
    never masquerade as a valid zero-consensus result."""

    async def test_non_json_body_is_a_contract_error(self) -> None:
        """An HTML/redirect body at the JSON endpoint is a contract error with
        status+content-type context, not an unlabeled decode failure."""
        import json as _json

        response = AsyncMock()
        response.status_code = 200
        response.headers = {"content-type": "text/html; charset=utf-8"}
        response.raise_for_status = lambda: None
        response.json = lambda: (_ for _ in ()).throw(
            _json.JSONDecodeError("expecting value", "<html>...", 0)
        )
        client = AsyncMock()
        client.get = AsyncMock(return_value=response)

        with pytest.raises(
            naver_finance.NaverResearchContractError, match="non-JSON"
        ):
            await naver_finance.investor._fetch_research_json_with_client(
                client,
                "https://m.stock.naver.com/api/research/stock/005930",
            )

    async def test_http_error_propagates(self) -> None:
        import httpx

        response = AsyncMock()
        response.status_code = 500
        response.raise_for_status = lambda: (
            (_ for _ in ()).throw(
                httpx.HTTPStatusError(
                    "500", request=AsyncMock(), response=response
                )
            )
        )
        client = AsyncMock()
        client.get = AsyncMock(return_value=response)

        with pytest.raises(httpx.HTTPStatusError):
            await naver_finance.investor._fetch_research_json_with_client(
                client,
                "https://m.stock.naver.com/api/research/stock/005930",
            )

    async def test_detail_payload_missing_research_content_raises(self) -> None:
        with pytest.raises(
            naver_finance.NaverResearchContractError,
            match="missing researchContent",
        ):
            naver_finance._parse_research_detail_payload("005930", 96343, {})

    async def test_detail_payload_parses_desk_fixture(self) -> None:
        detail = _load_research_fixture("detail_96343.json")
        parsed = naver_finance._parse_research_detail_payload(
            "005930", 96343, detail
        )
        assert parsed == {"target_price": 560000, "rating": "StrongBuy"}

    async def test_list_payload_parses_desk_fixture(self) -> None:
        list_payload = _load_research_fixture("list_005930.json")
        items, skipped = naver_finance._parse_research_list_payload(
            "005930", list_payload
        )
        assert skipped == {}
        assert len(items) == 10
        assert items[0]["research_id"] == 96343
        assert items[0]["date"] == "2026-09-29"
        assert items[0]["firm"] == "유진투자증권"


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchKrSnapshot:
    def _install_snapshot_stubs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        request_counts: dict[str, int],
        *,
        research_stub: Any,
        sise_fails: bool = False,
    ) -> None:
        async def mock_fetch_html_with_client(
            client: Any, url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            _ = client, params
            if "main.naver" in url:
                request_counts["main"] += 1
                return BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
            if "sise.naver" in url:
                request_counts["sise"] += 1
                if sise_fails:
                    raise RuntimeError("sise unavailable")
                return BeautifulSoup(SAMPLE_VALUATION_SISE_HTML, "lxml")
            return BeautifulSoup("<html></html>", "lxml")

        async def mock_fetch_stock_news(code: str, limit: int = 20):
            _ = code, limit
            request_counts["news"] += 1
            return naver_finance.NaverNewsFetchResult(
                items=[_normalized_news_item()]
            )

        async def routed_research(
            client: Any, url: str, params: dict[str, Any] | None = None
        ) -> Any:
            if "/api/research/stock/" in url:
                request_counts["list"] += 1
            else:
                request_counts["detail"] += 1
            return await research_stub(client, url, params=params)

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
        monkeypatch.setattr(
            naver_finance.investor, "fetch_stock_news", mock_fetch_stock_news
        )
        monkeypatch.setattr(
            naver_finance.investor,
            "_fetch_research_json_with_client",
            routed_research,
        )

    async def test_snapshot_uses_research_json_and_reuses_main_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request_counts = {
            "main": 0,
            "sise": 0,
            "news": 0,
            "list": 0,
            "detail": 0,
        }
        self._install_snapshot_stubs(
            monkeypatch,
            request_counts,
            research_stub=_research_json_stub(
                list_payload=SAMPLE_RESEARCH_LIST_005930,
                details=SAMPLE_RESEARCH_DETAILS_005930,
            ),
        )

        snapshot = await naver_finance._fetch_kr_snapshot(
            "005930", news_limit=5, opinion_limit=10
        )

        assert request_counts == {
            "main": 1,
            "sise": 1,
            "news": 1,
            "list": 1,
            "detail": 2,
        }
        assert snapshot["valuation"]["current_price"] == 75000
        assert snapshot["news"][0]["title"] == "삼성전자, 신제품 발표"
        assert snapshot["opinions"]["count"] == 2
        assert snapshot["opinions"]["consensus"]["avg_target_price"] == 87500
        assert snapshot["opinions"]["consensus"]["current_price"] == 75000
        assert snapshot["opinions"]["consensus"]["upside_pct"] == pytest.approx(
            16.67, abs=0.01
        )

    async def test_snapshot_keeps_other_sections_when_one_page_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request_counts = {"main": 0, "sise": 0, "news": 0, "list": 0, "detail": 0}
        self._install_snapshot_stubs(
            monkeypatch,
            request_counts,
            sise_fails=True,
            research_stub=_research_json_stub(
                list_payload=SAMPLE_RESEARCH_LIST_005930,
                details=SAMPLE_RESEARCH_DETAILS_005930,
            ),
        )

        snapshot = await naver_finance._fetch_kr_snapshot(
            "005930", news_limit=5, opinion_limit=10
        )

        assert snapshot["valuation"] is None
        assert snapshot["news"][0]["title"] == "삼성전자, 신제품 발표"
        assert snapshot["opinions"]["count"] == 2
        assert snapshot["opinions"]["consensus"]["current_price"] == 75000

    async def test_snapshot_opinions_fail_loud_on_research_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#930: a research-list failure must leave an explicit error block in
        the bundle — never a silently-missing or zeroed opinions section — while
        the other sections still succeed."""
        request_counts = {"main": 0, "sise": 0, "news": 0, "list": 0, "detail": 0}

        async def failing_research(
            client: Any, url: str, params: dict[str, Any] | None = None
        ) -> Any:
            _ = client, url, params
            raise RuntimeError("research api down")

        self._install_snapshot_stubs(
            monkeypatch, request_counts, research_stub=failing_research
        )

        snapshot = await naver_finance._fetch_kr_snapshot(
            "005930", news_limit=5, opinion_limit=10
        )

        assert snapshot["valuation"]["current_price"] == 75000
        assert snapshot["news"][0]["title"] == "삼성전자, 신제품 발표"
        opinions = snapshot["opinions"]
        assert opinions["count"] == 0
        assert opinions["opinions"] == []
        assert opinions["consensus"] is None
        assert "research api down" in opinions["error"]

    async def test_snapshot_opinions_fail_loud_on_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#930: the empty-list silent-zero signature becomes an error block,
        not an all-zero consensus."""
        request_counts = {"main": 0, "sise": 0, "news": 0, "list": 0, "detail": 0}
        self._install_snapshot_stubs(
            monkeypatch,
            request_counts,
            research_stub=_research_json_stub(list_payload=[], details={}),
        )

        snapshot = await naver_finance._fetch_kr_snapshot(
            "005930", news_limit=5, opinion_limit=10
        )

        assert "error" in snapshot["opinions"]
        assert "0 reports" in snapshot["opinions"]["error"]
        assert snapshot["opinions"]["consensus"] is None


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

    @pytest.fixture(autouse=True)
    def _stub_integration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ROB-448: fetch_valuation now overlays eps/bps/market_cap via the mobile
        # _fetch_integration endpoint. Stub it to {} so these HTML-only tests stay
        # hermetic (no network) — the overlay leaves the 3 keys None. The overlay
        # behaviour itself is asserted in test_overlays_eps_bps_market_cap.
        async def _empty(code: str, client: Any) -> dict[str, Any]:  # noqa: ARG001
            return {}

        monkeypatch.setattr(naver_finance.valuation, "_fetch_integration", _empty)

    async def test_overlays_eps_bps_market_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ROB-448: eps/bps/market_cap (RAW KRW) from _fetch_integration overlay the
        # HTML-scraped valuation. additive — existing keys untouched.
        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
            return BeautifulSoup(SAMPLE_VALUATION_SISE_HTML, "lxml")

        async def fake_integration(code: str, client: Any) -> dict[str, Any]:  # noqa: ARG001
            return {"eps": 5432.0, "bps": 50000.0, "market_cap": 400_000_000_000_000.0}

        monkeypatch.setattr(naver_finance.valuation, "_fetch_html", mock_fetch_html)
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_integration
        )

        result = await naver_finance.fetch_valuation("005930")

        assert result["eps"] == pytest.approx(5432.0)  # won/share
        assert result["bps"] == pytest.approx(50000.0)
        assert result["market_cap"] == pytest.approx(400_000_000_000_000.0)  # raw KRW
        # HTML-scraped keys untouched by the overlay
        assert result["per"] == pytest.approx(12.5)
        assert result["pbr"] == pytest.approx(1.2)

    async def test_overlay_fails_open_leaves_keys_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ROB-448: a mobile-API failure must not break valuation — eps/bps/market_cap
        # degrade to None, HTML keys still returned.
        async def mock_fetch_html(
            url: str, params: dict[str, Any] | None = None
        ) -> BeautifulSoup:
            if "main.naver" in url:
                return BeautifulSoup(SAMPLE_VALUATION_MAIN_HTML, "lxml")
            return BeautifulSoup(SAMPLE_VALUATION_SISE_HTML, "lxml")

        async def boom(code: str, client: Any) -> dict[str, Any]:  # noqa: ARG001
            raise RuntimeError("mobile api down")

        monkeypatch.setattr(naver_finance.valuation, "_fetch_html", mock_fetch_html)
        monkeypatch.setattr(naver_finance.valuation, "_fetch_integration", boom)

        result = await naver_finance.fetch_valuation("005930")

        assert result["eps"] is None
        assert result["bps"] is None
        assert result["market_cap"] is None
        assert result["per"] == pytest.approx(12.5)  # valuation still intact

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
        assert result["per"] == pytest.approx(12.5)
        assert result["pbr"] == pytest.approx(1.2)
        assert result["roe"] == pytest.approx(18.5)  # ROE(%)
        assert result["roe_controlling"] == pytest.approx(17.2)  # ROE(지배주주)
        assert result["dividend_yield"] == pytest.approx(
            0.02, abs=0.001
        )  # 2.00% -> 0.02
        assert result["high_52w"] == 90000
        assert result["low_52w"] == 60000
        # Position: (75000 - 60000) / (90000 - 60000) = 0.5
        assert result["current_position_52w"] == pytest.approx(0.5)

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
        assert result["pbr"] == pytest.approx(2.1)
        assert result["roe"] is None  # Not in HTML
        assert result["roe_controlling"] is None  # Not in HTML
        assert result["dividend_yield"] is None
        assert result["high_52w"] == 500000
        assert result["low_52w"] == 200000
        # Position: (450000 - 200000) / (500000 - 200000) = 0.833...
        assert result["current_position_52w"] == pytest.approx(0.83, abs=0.01)

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

        assert result["current_position_52w"] == pytest.approx(0.0)

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

        assert result["current_position_52w"] == pytest.approx(1.0)

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
            content = """
                <html>
                <head><title>반도체 : Npay 증권</title></head>
                <body>
                    <table class="type_5">
                        <tr><td><a href="/item/main.naver?code=000002">Peer</a></td></tr>
                    </table>
                </body>
                </html>
                """.encode("euc-kr")

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
            request_timeout: float | None = None,
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


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersConcurrency:
    async def test_peer_fanout_is_bounded_by_semaphore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 3)

        # Target returns 8 integration peers so, pre-trim, 8 peer fetches queue.
        peers_raw = [{"itemCode": f"00000{i}"} for i in range(1, 9)]

        in_flight = 0
        peak = 0

        async def fake_fetch_integration(
            code: str, _client: Any, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            nonlocal in_flight, peak
            if code == "000100":  # target
                return {
                    "symbol": code,
                    "name": "Target",
                    "per": 10,
                    "pbr": 1.1,
                    "market_cap": 1000,
                    "current_price": 50000,
                    "change_pct": 1.0,
                    "industry_code": "123",
                    "peers_raw": peers_raw,
                }
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)  # hold the slot so overlap is observable
            in_flight -= 1
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

        class FakeResponse:
            content = b"<html><head><title>x : Npay</title></head></html>"

        class FakeClient:
            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        result = await naver_finance.fetch_sector_peers("000100", limit=8)

        assert peak <= 3, f"peak in-flight {peak} exceeded semaphore cap 3"
        assert len(result["peers"]) == 8


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersPeerTimeout:
    async def test_peers_use_short_request_timeout_target_uses_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "naver_peer_fetch_timeout_seconds", 5.0)
        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 5)

        # Record the request_timeout each _fetch_integration call receives.
        seen: dict[str, float | None] = {}

        async def fake_fetch_integration(
            code: str, _client: Any, request_timeout: float | None = None
        ) -> dict[str, Any]:
            seen[code] = request_timeout
            base = {
                "symbol": code,
                "name": code,
                "per": 10,
                "pbr": 1.0,
                "market_cap": 100,
                "current_price": 1,
                "change_pct": 0.0,
                "industry_code": "123",
                "peers_raw": [],
            }
            if code == "000100":
                base["peers_raw"] = [{"itemCode": "000200"}]
            return base

        class FakeResponse:
            content = b"<html><head><title>x : Npay</title></head></html>"

        class FakeClient:
            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        await naver_finance.fetch_sector_peers("000100", limit=1)

        assert seen["000100"] is None, "target must keep the client-level 10s timeout"
        assert seen["000200"] == 5.0, "peer must use the short per-request timeout"


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersTrim:
    async def test_no_overfetch_when_integration_has_enough_peers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 10)

        # Integration returns 8 peers; limit=5 -> should fetch exactly 5, not 10.
        peers_raw = [{"itemCode": f"90000{i}"} for i in range(1, 9)]
        fetched_peer_codes: list[str] = []

        async def fake_fetch_integration(
            code: str, _client: Any, request_timeout: float | None = None
        ) -> dict[str, Any]:
            if code != "000100":
                fetched_peer_codes.append(code)
            base = {
                "symbol": code,
                "name": code,
                "per": 10,
                "pbr": 1.0,
                "market_cap": 100,
                "current_price": 1,
                "change_pct": 0.0,
                "industry_code": "123",
                "peers_raw": [],
            }
            if code == "000100":
                base["peers_raw"] = peers_raw
            return base

        sector_gets: list[Any] = []

        class FakeResponse:
            content = (
                "<html><head><title>반도체 : Npay 증권</title></head>"
                "<body><table class='type_5'>"
                "<tr><td><a href='/item/main.naver?code=777777'>P</a></td></tr>"
                "</table></body></html>"
            ).encode("euc-kr")

        class FakeClient:
            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                sector_gets.append((url, params))
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        result = await naver_finance.fetch_sector_peers("000100", limit=5)

        assert len(fetched_peer_codes) == 5, (
            f"expected 5 peer fetches, got {len(fetched_peer_codes)}"
        )
        # sector name still resolved from the scrape (dual-purpose page)
        assert result["sector"] == "반도체"
        # sector page still fetched exactly once (never skipped)
        assert len(sector_gets) == 1
        # scrape-derived extra (777777) must NOT appear — integration peers sufficed
        assert "777777" not in fetched_peer_codes


@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersCache:
    async def test_target_served_from_integration_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from app.core.config import settings
        from app.services.naver_finance import peer_cache

        monkeypatch.setattr(settings, "naver_peer_cache_enabled", True)
        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 5)

        class _FakeRedis:
            def __init__(self) -> None:
                self.store: dict[str, str] = {}

            async def get(self, key: str) -> str | None:
                return self.store.get(key)

            async def set(self, key: str, value: str, ex: int | None = None) -> None:
                self.store[key] = value

        fake = _FakeRedis()
        fake.store["naver_peer:integ:000100"] = json.dumps(
            {
                "symbol": "000100",
                "name": "CachedTarget",
                "per": 7,
                "pbr": 0.9,
                "market_cap": 500,
                "current_price": 12345,
                "change_pct": 2.0,
                "industry_code": "123",
                "peers_raw": [{"itemCode": "000200"}],
            }
        )

        async def fake_get_client() -> Any:
            return fake

        monkeypatch.setattr(peer_cache, "_get_redis_client", fake_get_client)

        async def fake_fetch_integration(
            code: str, _client: Any, request_timeout: float | None = None
        ) -> dict[str, Any]:
            if code == "000100":
                raise AssertionError("target must be served from cache, not fetched")
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

        class FakeResponse:
            content = b"<html><head><title>x : Npay</title></head></html>"

        class FakeClient:
            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        result = await naver_finance.fetch_sector_peers("000100", limit=1)

        assert result["name"] == "CachedTarget"
        assert result["current_price"] == 12345
        assert result["peers"][0]["symbol"] == "000200"
