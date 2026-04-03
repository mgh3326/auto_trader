# tests/test_trade_notifier_formatters_telegram.py
"""Tests for Telegram message formatters."""

import pytest

from app.monitoring.trade_notifier.formatters_telegram import (
    format_analysis_notification_telegram,
    format_automation_summary_telegram,
    format_buy_notification_telegram,
    format_cancel_notification_telegram,
    format_failure_notification_telegram,
    format_sell_notification_telegram,
    format_toss_price_recommendation_html,
)


@pytest.mark.unit
class TestFormatBuyNotificationTelegram:
    def test_basic(self):
        msg = format_buy_notification_telegram(
            symbol="BTC",
            korean_name="비트코인",
            order_count=1,
            total_amount=100000.0,
            prices=[100000.0],
            volumes=[0.001],
            market_type="암호화폐",
        )
        assert "*💰 매수 주문 접수*" in msg
        assert "비트코인" in msg
        assert "BTC" in msg

    def test_multiple_orders(self):
        msg = format_buy_notification_telegram(
            symbol="ETH",
            korean_name="이더리움",
            order_count=3,
            total_amount=300000.0,
            prices=[100000.0, 100000.0, 100000.0],
            volumes=[0.01, 0.01, 0.01],
            market_type="암호화폐",
        )
        assert "*주문 상세:*" in msg
        assert "3건" in msg

    def test_prices_only(self):
        msg = format_buy_notification_telegram(
            symbol="BTC",
            korean_name="비트코인",
            order_count=1,
            total_amount=100000.0,
            prices=[100000.0],
            volumes=[],
            market_type="암호화폐",
        )
        assert "*매수 가격대:*" in msg


@pytest.mark.unit
class TestFormatSellNotificationTelegram:
    def test_basic(self):
        msg = format_sell_notification_telegram(
            symbol="ETH",
            korean_name="이더리움",
            order_count=2,
            total_volume=0.5,
            prices=[2000000.0, 2100000.0],
            volumes=[0.25, 0.25],
            expected_amount=1025000.0,
            market_type="암호화폐",
        )
        assert "*💸 매도 주문 접수*" in msg
        assert "이더리움" in msg

    def test_prices_only(self):
        msg = format_sell_notification_telegram(
            symbol="ETH",
            korean_name="이더리움",
            order_count=1,
            total_volume=0.5,
            prices=[2000000.0],
            volumes=[],
            expected_amount=1000000.0,
            market_type="암호화폐",
        )
        assert "*매도 가격대:*" in msg


@pytest.mark.unit
class TestFormatCancelNotificationTelegram:
    def test_basic(self):
        msg = format_cancel_notification_telegram(
            symbol="XRP",
            korean_name="리플",
            cancel_count=5,
            order_type="매수",
            market_type="암호화폐",
        )
        assert "*🚫 주문 취소*" in msg
        assert "리플" in msg
        assert "5건" in msg


@pytest.mark.unit
class TestFormatAnalysisNotificationTelegram:
    def test_buy(self):
        msg = format_analysis_notification_telegram(
            symbol="BTC",
            korean_name="비트코인",
            decision="buy",
            confidence=85.5,
            reasons=["상승 추세"],
            market_type="암호화폐",
        )
        assert "*📊 AI 분석 완료*" in msg
        assert "🟢" in msg
        assert "매수" in msg

    def test_sell(self):
        msg = format_analysis_notification_telegram(
            symbol="ETH",
            korean_name="이더리움",
            decision="sell",
            confidence=90.0,
            reasons=["하락 추세", "거래량 감소"],
            market_type="암호화폐",
        )
        assert "🔴" in msg
        assert "매도" in msg

    def test_hold(self):
        msg = format_analysis_notification_telegram(
            symbol="XRP",
            korean_name="리플",
            decision="hold",
            confidence=50.0,
            reasons=[],
            market_type="암호화폐",
        )
        assert "🟡" in msg
        assert "보유" in msg

    def test_unknown_decision(self):
        msg = format_analysis_notification_telegram(
            symbol="BTC",
            korean_name="비트코인",
            decision="unknown",
            confidence=10.0,
            reasons=[],
            market_type="암호화폐",
        )
        assert "⚪" in msg
        assert "unknown" in msg


@pytest.mark.unit
class TestFormatAutomationSummaryTelegram:
    def test_basic(self):
        msg = format_automation_summary_telegram(
            total_coins=10,
            analyzed=10,
            bought=3,
            sold=2,
            errors=0,
            duration_seconds=45.5,
        )
        assert "*🤖 자동 거래 실행 완료*" in msg
        assert "45.5" in msg
        assert "오류 발생" not in msg

    def test_with_errors(self):
        msg = format_automation_summary_telegram(
            total_coins=10,
            analyzed=8,
            bought=1,
            sold=0,
            errors=2,
            duration_seconds=30.0,
        )
        assert "*오류 발생:* 2건" in msg


@pytest.mark.unit
class TestFormatFailureNotificationTelegram:
    def test_basic(self):
        msg = format_failure_notification_telegram(
            symbol="AAPL",
            korean_name="애플",
            reason="주문 실패",
            market_type="해외주식",
        )
        assert "*⚠️ 거래 실패*" in msg
        assert "주문 실패" in msg
        assert "해외주식" in msg


@pytest.mark.unit
class TestFormatTossPriceRecommendationHtml:
    def test_basic(self):
        html_msg = format_toss_price_recommendation_html(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=None,
            kis_avg_price=None,
            decision="buy",
            confidence=85.0,
            reasons=["상승 추세"],
            appropriate_buy_min=68000,
            appropriate_buy_max=70000,
            appropriate_sell_min=None,
            appropriate_sell_max=None,
            buy_hope_min=65000,
            buy_hope_max=67000,
            sell_target_min=None,
            sell_target_max=None,
            currency="원",
            market_type="국내주식",
        )
        assert "<b>" in html_msg
        assert "삼성전자" in html_msg
        assert "70,000원" in html_msg

    def test_with_detail_url(self):
        html_msg = format_toss_price_recommendation_html(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=5,
            kis_avg_price=63000,
            decision="buy",
            confidence=85.0,
            currency="원",
            market_type="국내주식",
            detail_url="https://mgh3326.duckdns.org/portfolio/positions/kr/005930",
        )
        assert "<b>상세:</b> https://mgh3326.duckdns.org/portfolio/positions/kr/005930" in html_msg

    def test_usd_currency(self):
        html_msg = format_toss_price_recommendation_html(
            symbol="AAPL",
            korean_name="Apple",
            current_price=150.0,
            toss_quantity=5,
            toss_avg_price=140.0,
            decision="hold",
            confidence=60.0,
            reasons=["횡보 구간"],
            currency="$",
            market_type="해외주식",
        )
        assert "$150.00" in html_msg

    def test_with_kis_holdings(self):
        html_msg = format_toss_price_recommendation_html(
            symbol="005930",
            korean_name="삼성전자",
            current_price=70000,
            toss_quantity=10,
            toss_avg_price=65000,
            kis_quantity=20,
            kis_avg_price=68000,
            decision="buy",
            confidence=80.0,
            reasons=[],
            currency="원",
            market_type="국내주식",
        )
        assert "KIS 보유" in html_msg
        assert "20주" in html_msg

    def test_html_escaping(self):
        html_msg = format_toss_price_recommendation_html(
            symbol="TEST",
            korean_name="<script>alert('xss')</script>",
            current_price=100,
            toss_quantity=1,
            toss_avg_price=100,
            decision="hold",
            confidence=50.0,
            reasons=["reason with <html> tags"],
            currency="원",
            market_type="국내주식",
        )
        assert "<script>" not in html_msg
        assert "&lt;script&gt;" in html_msg
