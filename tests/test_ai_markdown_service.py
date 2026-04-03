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
