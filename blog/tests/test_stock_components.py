"""Tests for stock-specific SVG components."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


SAMPLE_INDICATORS = {
    "rsi14": 57.16,
    "macd_histogram": -527,
    "macd_signal": "매도 신호",
    "adx": 16.37,
    "plus_di": 22.5,
    "minus_di": 18.3,
    "stoch_rsi_k": 0.78,
    "stoch_rsi_d": 0.65,
}

SAMPLE_OHLCV = [
    {"date": "2024-01-02", "close": 73400},
    {"date": "2024-01-03", "close": 72800},
    {"date": "2024-01-04", "close": 74100},
    {"date": "2024-01-05", "close": 73600},
    {"date": "2024-01-08", "close": 72200},
]


class TestIndicatorDashboard:
    def test_create_dashboard(self) -> None:
        from blog.tools.stock.indicator_dashboard import IndicatorDashboard

        svg = IndicatorDashboard.create(
            x=60,
            y=95,
            width=600,
            height=350,
            indicators=SAMPLE_INDICATORS,
        )
        assert "RSI" in svg
        assert "MACD" in svg
        assert "ADX" in svg
        assert "StochRSI" in svg

    def test_rsi_zone_label(self) -> None:
        from blog.tools.stock.indicator_dashboard import IndicatorDashboard

        svg = IndicatorDashboard.create(
            x=0,
            y=0,
            width=600,
            height=350,
            indicators={**SAMPLE_INDICATORS, "rsi14": 75.0},
        )
        assert "과매수" in svg

    def test_missing_indicators_handled(self) -> None:
        from blog.tools.stock.indicator_dashboard import IndicatorDashboard

        svg = IndicatorDashboard.create(
            x=0,
            y=0,
            width=600,
            height=350,
            indicators={"rsi14": 50.0},  # Minimal data
        )
        assert "RSI" in svg
        assert isinstance(svg, str)


class TestPriceChart:
    def test_basic_price_line(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=60,
            y=95,
            width=800,
            height=350,
            ohlcv=SAMPLE_OHLCV,
        )
        assert "<polyline" in svg or "<path" in svg  # Line chart
        assert isinstance(svg, str)

    def test_with_bollinger_bands(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=60,
            y=95,
            width=800,
            height=350,
            ohlcv=SAMPLE_OHLCV,
            bollinger={
                "upper": [75000, 74500, 75200, 74800, 73500],
                "lower": [71000, 71200, 72800, 72400, 70900],
            },
        )
        assert "polygon" in svg or "polyline" in svg  # Band area

    def test_with_ema_lines(self) -> None:
        from blog.tools.stock.price_chart import PriceChart

        svg = PriceChart.create(
            x=60,
            y=95,
            width=800,
            height=350,
            ohlcv=SAMPLE_OHLCV,
            ema_values={"ema20": [73000, 72900, 73500, 73200, 72800]},
        )
        assert isinstance(svg, str)


class TestSupportResistance:
    def test_basic_levels(self) -> None:
        from blog.tools.stock.support_resistance import SupportResistance

        svg = SupportResistance.create(
            x=60,
            y=480,
            width=1300,
            height=280,
            supports=[62000, 58000, 55000],
            resistances=[68000, 72000, 75000],
            current_price=65800,
        )
        assert "지지" in svg or "Support" in svg
        assert "저항" in svg or "Resistance" in svg
        assert "62,000" in svg or "62000" in svg


class TestValuationCards:
    def test_valuation_grid(self) -> None:
        from blog.tools.stock.valuation_cards import ValuationCards

        svg = ValuationCards.create(
            x=60,
            y=95,
            width=1300,
            height=180,
            valuation={
                "per": 30.38,
                "pbr": 1.82,
                "roe": 6.01,
                "consensus_target": 85000,
                "current_price": 65800,
            },
        )
        assert "PER" in svg
        assert "PBR" in svg
        assert "ROE" in svg
        assert "30.38" in svg


class TestEarningsChart:
    def test_earnings_bars(self) -> None:
        from blog.tools.stock.earnings_chart import EarningsChart

        svg = EarningsChart.create(
            x=60,
            y=310,
            width=1300,
            height=300,
            financials={
                "annual_earnings": [
                    {"year": "2021", "operating_income": 51_633_000_000_000},
                    {"year": "2022", "operating_income": 43_376_000_000_000},
                ],
                "quarterly_margins": [
                    {"quarter": "Q1", "margin": 0.05},
                    {"quarter": "Q2", "margin": 0.08},
                ],
            },
        )
        assert "2021" in svg
        assert "2022" in svg


class TestInvestorFlow:
    def test_flow_bars(self) -> None:
        from blog.tools.stock.investor_flow import InvestorFlow

        svg = InvestorFlow.create(
            x=60,
            y=95,
            width=1300,
            height=250,
            investor_trends={
                "foreign_net": -15234,
                "institution_net": 8721,
                "individual_net": 6513,
                "foreign_consecutive_sell_days": 5,
            },
        )
        assert "외국인" in svg
        assert "기관" in svg
        assert isinstance(svg, str)


class TestOpinionTable:
    def test_opinion_table(self) -> None:
        from blog.tools.stock.opinion_table import OpinionTable

        svg = OpinionTable.create(
            x=60,
            y=380,
            width=1300,
            height=380,
            opinions={
                "opinions": [
                    {"firm": "삼성증권", "rating": "매수", "target": 90000},
                    {"firm": "NH투자", "rating": "매수", "target": 85000},
                ],
                "consensus": {"rating": "매수", "avg_target": 82000},
            },
        )
        assert "삼성증권" in svg
        assert "매수" in svg
        assert isinstance(svg, str)


class TestConclusionCard:
    def test_conclusion_summary(self) -> None:
        from blog.tools.stock.conclusion_card import ConclusionCard

        svg = ConclusionCard.create(
            x=60,
            y=95,
            width=1300,
            height=660,
            data={
                "indicators": {"rsi14": 57.16, "macd_signal": "중립"},
                "valuation": {"per": 30.38, "consensus_target": 85000},
                "investor_trends": {"foreign_net": -15234},
            },
            company_name="삼성전자",
        )
        assert "삼성전자" in svg
        assert isinstance(svg, str)
