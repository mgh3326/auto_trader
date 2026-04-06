"""Tests for AI Markdown Service"""

import pytest

from app.services.ai_markdown_service import AIMarkdownService


@pytest.fixture
def service():
    return AIMarkdownService()


@pytest.fixture
def sample_portfolio_data():
    return {
        "positions": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc",
                "market_type": "US",
                "evaluation": 5000000,
                "profit_loss": 500000,
                "profit_rate": 0.11,
                "quantity": 10,
            },
            {
                "symbol": "005930",
                "name": "삼성전자",
                "market_type": "KR",
                "evaluation": 10000000,
                "profit_loss": -200000,
                "profit_rate": -0.02,
                "quantity": 100,
            },
        ],
        "total_evaluation": 15000000,
    }


@pytest.fixture
def sample_stock_data():
    return {
        "summary": {
            "symbol": "AAPL",
            "name": "Apple Inc",
            "market_type": "US",
            "current_price": 185.5,
            "avg_price": 167.0,
            "quantity": 10.5,
            "profit_rate": 0.11,
            "evaluation": 5000000,
        },
        "weights": {
            "portfolio_weight_pct": 15.5,
            "market_weight_pct": 33.3,
        },
        "journal": {
            "target_price": 200.0,
            "stop_loss_price": 150.0,
            "target_distance_pct": 7.8,
            "stop_distance_pct": -19.1,
        },
    }


class TestAIMarkdownService:
    def test_init(self, service):
        assert service.investment_profile is not None

    def test_generate_portfolio_stance_markdown(self, service, sample_portfolio_data):
        result = service.generate_portfolio_stance_markdown(sample_portfolio_data)

        assert result["title"].startswith("포트폴리오 전체 스탠스")
        assert "portfolio-stance-" in result["filename"]
        assert "# " in result["content"]
        assert "투자 성향" in result["content"]
        assert "Apple Inc" in result["content"]
        assert "삼성전자" in result["content"]
        assert "metadata" in result
        assert result["metadata"]["position_count"] == 2

    def test_generate_stock_stance_markdown(self, service, sample_stock_data):
        result = service.generate_stock_stance_markdown(sample_stock_data)

        assert "AAPL" in result["title"]
        assert "Apple Inc" in result["content"]
        assert "stock-AAPL-stance.md" == result["filename"]
        assert "현재 포지션 정보" in result["content"]
        assert "$185.50" in result["content"]

    def test_generate_stock_add_or_hold_markdown(self, service, sample_stock_data):
        result = service.generate_stock_add_or_hold_markdown(sample_stock_data)

        assert "추가매수 vs 유지" in result["title"]
        assert "stock-AAPL-add-or-hold.md" == result["filename"]
        assert "추가매수가 가능한가요" in result["content"]
        assert "목표가" in result["content"]

    def test_format_price(self, service):
        assert service._format_price(100.5, "US") == "$100.50"
        assert service._format_price(100500, "KR") == "₩100,500.00"
        assert service._format_price(None, "US") == "N/A"

    def test_empty_positions(self, service):
        empty_data = {"positions": []}
        result = service.generate_portfolio_stance_markdown(empty_data)
        assert "보유 종목 수: 0개" in result["content"]
        assert "보유 종목 없음" in result["content"]

    def test_missing_optional_fields(self, service):
        minimal_data = {
            "summary": {
                "symbol": "TEST",
                "name": "Test Corp",
                "market_type": "US",
            },
            "weights": {},
            "journal": {},
        }
        result = service.generate_stock_stance_markdown(minimal_data)
        assert "TEST" in result["content"]
        assert "N/A" in result["content"]  # Missing price fields

    def test_extract_portfolio_summary_prefers_evaluation_krw_for_totals_and_allocation(
        self, service
    ):
        portfolio_data = {
            "positions": [
                {
                    "symbol": "005930",
                    "market_type": "KR",
                    "evaluation": 10_000_000,
                    "evaluation_krw": 10_000_000,
                    "profit_loss": 500_000,
                    "profit_loss_krw": 500_000,
                },
                {
                    "symbol": "AAPL",
                    "market_type": "US",
                    "evaluation": 2_000,  # USD raw
                    "evaluation_krw": 2_600_000,  # normalized (rate 1300)
                    "profit_loss": 300,
                    "profit_loss_krw": 390_000,
                },
                {
                    "symbol": "KRW-BTC",
                    "market_type": "CRYPTO",
                    "evaluation": 4_000_000,
                    "evaluation_krw": 4_000_000,
                    "profit_loss": -100_000,
                    "profit_loss_krw": -100_000,
                },
            ]
        }

        summary = service._extract_portfolio_summary(portfolio_data)

        # 10M + 2.6M + 4M = 16.6M
        assert summary["total_evaluation"] == 16_600_000
        # US weight: 2.6M / 16.6M * 100 = 15.66... -> 15.7
        assert round(summary["allocation"]["US"], 1) == 15.7
        assert round(sum(summary["allocation"].values()), 1) == 100.0

    def test_format_top_holdings_uses_normalized_krw_for_sorting_and_display(
        self, service
    ):
        positions = [
            {
                "symbol": "SMALL_KR",
                "name": "작은국내",
                "market_type": "KR",
                "evaluation": 1_000_000,
                "evaluation_krw": 1_000_000,
            },
            {
                "symbol": "BIG_US",
                "name": "큰미국",
                "market_type": "US",
                "evaluation": 2_000,  # Raw USD 2000 < KRW 1,000,000
                "evaluation_krw": 2_600_000,  # Normalized KRW 2,600,000 > KRW 1,000,000
            },
        ]

        formatted = service._format_top_holdings(positions, limit=10)

        # BIG_US should come first because 2.6M > 1M
        lines = formatted.split("\n")
        assert "큰미국" in lines[0]
        assert "2,600,000원" in lines[0]
        assert "작은국내" in lines[1]
        assert "1,000,000원" in lines[1]

    def test_generate_portfolio_stance_markdown_surfaces_warning_when_us_krw_value_missing(
        self, service
    ):
        portfolio_data = {
            "positions": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "market_type": "KR",
                    "evaluation": 1_000_000,
                    "evaluation_krw": 1_000_000,
                },
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc",
                    "market_type": "US",
                    "evaluation": 2_000,
                    "evaluation_krw": None,  # FX missing
                },
            ],
            "warnings": [
                "환율 정보를 가져올 수 없어 일부 해외 자산의 원화 환산이 정확하지 않을 수 있습니다."
            ],
        }

        result = service.generate_portfolio_stance_markdown(portfolio_data)

        # Result should contain the warning from the data
        assert "환율 정보" in result["content"]
        # KR totals/allocation must not be polluted by raw USD fallback
        assert "- 총 평가금액: 1,000,000원" in result["content"]
        assert "해외주식: 0.2%" not in result["content"]
        # US holding should be shown without pretending the raw USD amount is KRW
        assert "2,000원" not in result["content"]
        assert "원화 환산 불가" in result["content"]

    def test_generate_stock_stance_markdown_shows_evaluation_in_krw_for_us(
        self, service
    ):
        stock_data = {
            "summary": {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "market_type": "US",
                "current_price": 170.0,
                "avg_price": 150.0,
                "quantity": 10.0,
                "profit_rate": 0.1333,
                "evaluation": 1700.0,
                "evaluation_krw": 2_295_000.0,
            },
            "weights": {
                "portfolio_weight_pct": 15.0,
                "market_weight_pct": 30.0,
            },
        }

        result = service.generate_stock_stance_markdown(stock_data)

        # evaluation should show KRW, not USD
        assert "₩2,295,000" in result["content"]
        # current_price and avg_price should still show USD
        assert "$170.00" in result["content"]
        assert "$150.00" in result["content"]
