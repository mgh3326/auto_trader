# tests/test_trade_notifier_formatters_discord.py
"""Tests for Discord embed formatters — extracted from test_trade_notifier.py originals."""

import pytest

from app.monitoring.trade_notifier.formatters_discord import (
    format_analysis_notification,
    format_automation_summary,
    format_buy_notification,
    format_cancel_notification,
    format_failure_notification,
    format_sell_notification,
    format_toss_buy_recommendation,
    format_toss_sell_recommendation,
)


@pytest.mark.unit
class TestFormatBuyNotification:
    def test_basic(self):
        embed = format_buy_notification(
            symbol="BTC",
            korean_name="비트코인",
            order_count=3,
            total_amount=300000.0,
            prices=[100000.0, 101000.0, 102000.0],
            volumes=[0.001, 0.001, 0.001],
            market_type="암호화폐",
        )
        assert embed["title"] == "💰 매수 주문 접수"
        assert embed["color"] == 0x00FF00
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["종목"] == "비트코인 (BTC)"
        assert fields["시장"] == "암호화폐"
        assert fields["주문 수"] == "3건"
        assert fields["총 금액"] == "300,000원"
        assert "100,000.00원 × 0.001" in fields["주문 상세"]

    def test_without_details(self):
        embed = format_buy_notification(
            symbol="BTC",
            korean_name="비트코인",
            order_count=2,
            total_amount=200000.0,
            prices=[],
            volumes=[],
            market_type="암호화폐",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "주문 상세" not in fields


@pytest.mark.unit
class TestFormatSellNotification:
    def test_basic(self):
        embed = format_sell_notification(
            symbol="ETH",
            korean_name="이더리움",
            order_count=2,
            total_volume=0.5,
            prices=[2000000.0, 2100000.0],
            volumes=[0.25, 0.25],
            expected_amount=1025000.0,
            market_type="암호화폐",
        )
        assert embed["title"] == "💸 매도 주문 접수"
        assert embed["color"] == 0xFF0000
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["종목"] == "이더리움 (ETH)"
        assert "2,000,000.00원 × 0.25" in fields["주문 상세"]


@pytest.mark.unit
class TestFormatCancelNotification:
    def test_basic(self):
        embed = format_cancel_notification(
            symbol="XRP",
            korean_name="리플",
            cancel_count=5,
            order_type="매수",
            market_type="암호화폐",
        )
        assert embed["title"] == "🚫 주문 취소"
        assert embed["color"] == 0xFFFF00
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["취소 유형"] == "매수"
        assert fields["취소 건수"] == "5건"


@pytest.mark.unit
class TestFormatAnalysisNotification:
    def test_buy(self):
        embed = format_analysis_notification(
            symbol="BTC",
            korean_name="비트코인",
            decision="buy",
            confidence=85.5,
            reasons=["상승 추세 지속", "거래량 증가", "기술적 지표 긍정적"],
            market_type="암호화폐",
        )
        assert embed["title"] == "📊 AI 분석 완료"
        assert embed["color"] == 0x0000FF
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["판단"] == "🟢 매수"
        assert "1. 상승 추세 지속" in fields["주요 근거"]

    def test_hold(self):
        embed = format_analysis_notification(
            symbol="ETH",
            korean_name="이더리움",
            decision="hold",
            confidence=70.0,
            reasons=["시장 관망"],
            market_type="암호화폐",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["판단"] == "🟡 보유"

    def test_sell(self):
        embed = format_analysis_notification(
            symbol="XRP",
            korean_name="리플",
            decision="sell",
            confidence=90.0,
            reasons=["하락 전망"],
            market_type="암호화폐",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["판단"] == "🔴 매도"


@pytest.mark.unit
class TestFormatAutomationSummary:
    def test_without_errors(self):
        embed = format_automation_summary(
            total_coins=10,
            analyzed=10,
            bought=3,
            sold=2,
            errors=0,
            duration_seconds=45.5,
        )
        assert embed["title"] == "🤖 자동 거래 실행 완료"
        assert embed["color"] == 0x00FFFF
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["매수 주문"] == "3건"
        assert "오류 발생" not in fields

    def test_with_errors(self):
        embed = format_automation_summary(
            total_coins=5,
            analyzed=5,
            bought=1,
            sold=1,
            errors=2,
            duration_seconds=30.0,
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["오류 발생"] == "2건"


@pytest.mark.unit
class TestFormatFailureNotification:
    def test_basic(self):
        embed = format_failure_notification(
            symbol="AAPL",
            korean_name="애플",
            reason="APBK0656 해당종목정보가 없습니다.",
            market_type="해외주식",
        )
        assert embed["title"] == "⚠️ 거래 실패"
        assert embed["color"] == 0xFF6600
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["사유"] == "APBK0656 해당종목정보가 없습니다."


@pytest.mark.unit
class TestFormatTossBuyRecommendation:
    def test_toss_only(self):
        embed = format_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=68000,
            recommended_quantity=5,
            currency="원",
            market_type="국내주식",
        )
        assert embed["title"] == "📈 [토스 수동매수]"
        assert embed["color"] == 0x00FF00
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["종목"] == "삼성전자 (005930)"
        assert "한투 보유" not in fields

    def test_with_detail_url(self):
        embed = format_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=68000,
            recommended_quantity=5,
            currency="원",
            market_type="국내주식",
            detail_url="https://mgh3326.duckdns.org/portfolio/positions/kr/005930",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert (
            fields["상세"]
            == "https://mgh3326.duckdns.org/portfolio/positions/kr/005930"
        )

    def test_with_kis(self):
        embed = format_toss_buy_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=5,
            kis_avg_price=63000,
            recommended_price=68000,
            recommended_quantity=5,
            currency="원",
            market_type="국내주식",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "한투 보유" in fields


@pytest.mark.unit
class TestFormatTossSellRecommendation:
    def test_toss_only(self):
        embed = format_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=72000,
            recommended_quantity=5,
            expected_profit=35000,
            profit_percent=10.77,
            currency="원",
            market_type="국내주식",
        )
        assert embed["title"] == "📉 [토스 수동매도]"
        assert embed["color"] == 0xFF0000
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "+10.8%" in fields["💡 추천 매도가"]

    def test_negative_profit(self):
        embed = format_toss_sell_recommendation(
            symbol="005930",
            korean_name="삼성전자",
            current_price=60000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            recommended_price=62000,
            recommended_quantity=5,
            expected_profit=-15000,
            profit_percent=-4.62,
            currency="원",
            market_type="국내주식",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "-4.6%" in fields["💡 추천 매도가"]
