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
            detail_url="https://mgh3326.duckdns.org/invest/stocks/kr/005930",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["상세"] == "https://mgh3326.duckdns.org/invest/stocks/kr/005930"


@pytest.mark.unit
class TestFormatFillNotification:
    def test_buy_basic_with_link_and_slippage(self):
        from app.monitoring.trade_notifier.formatters_discord import (
            format_fill_notification,
        )
        from app.services.fill_notification import FillOrder

        order = FillOrder(
            symbol="005930",
            side="bid",
            filled_price=68500.0,
            filled_qty=10.0,
            filled_amount=685000.0,
            filled_at="2026-06-14T09:31:02",
            account="kis",
            order_price=68300.0,
            order_id="0001234567",
            market_type="kr",
            currency="KRW",
        )
        embed = format_fill_notification(
            order,
            display_name="삼성전자",
            detail_url="https://x.test/invest/stocks/kr/005930",
            enrichment=None,
        )
        assert embed["title"] == "🟢 체결 · 삼성전자 (005930)"
        assert embed["color"] == 0x00FF00
        assert embed["url"] == "https://x.test/invest/stocks/kr/005930"
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["구분"] == "매수 체결"
        assert "68,500원" in fields["체결가"]
        assert "+0.29%" in fields["체결가"] or "+0.30%" in fields["체결가"]  # vs 68,300
        assert fields["수량"] == "10"
        assert fields["금액"] == "685,000원"

    def test_sell_shows_realized_pnl(self):
        from app.monitoring.trade_notifier.formatters_discord import (
            format_fill_notification,
        )
        from app.services.fill_notification import FillEnrichment, FillOrder

        order = FillOrder(
            symbol="005930",
            side="ask",
            filled_price=68500.0,
            filled_qty=10.0,
            filled_amount=685000.0,
            filled_at="2026-06-14T09:31:02",
            account="kis",
            order_price=68300.0,
            order_id="0001234567",
            market_type="kr",
            currency="KRW",
        )
        enr = FillEnrichment(
            realized_pnl_amount=12000.0, realized_pnl_rate=1.8, is_approximate=True
        )
        embed = format_fill_notification(
            order, display_name="삼성전자", detail_url=None, enrichment=enr
        )
        assert embed["color"] == 0xFF0000
        assert "url" not in embed
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["구분"] == "매도 체결"
        assert "실현손익" in fields
        assert "+12,000원" in fields["실현손익"]
        assert "~추정" in fields["실현손익"]

    def test_buy_shows_position_when_enriched(self):
        from app.monitoring.trade_notifier.formatters_discord import (
            format_fill_notification,
        )
        from app.services.fill_notification import FillEnrichment, FillOrder

        order = FillOrder(
            symbol="005930",
            side="bid",
            filled_price=68500.0,
            filled_qty=10.0,
            filled_amount=685000.0,
            filled_at="2026-06-14T09:31:02",
            account="kis",
            order_price=68300.0,
            order_id="0001234567",
            market_type="kr",
            currency="KRW",
        )
        enr = FillEnrichment(
            position_qty=30.0, position_avg_price=68100.0, is_approximate=True
        )
        embed = format_fill_notification(
            order, display_name="삼성전자", detail_url=None, enrichment=enr
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "보유" in fields
        assert "30" in fields["보유"] and "68,100원" in fields["보유"]

    def test_partial_label(self):
        from app.monitoring.trade_notifier.formatters_discord import (
            format_fill_notification,
        )
        from app.services.fill_notification import FillOrder

        order = FillOrder(
            symbol="005930",
            side="bid",
            filled_price=68500.0,
            filled_qty=10.0,
            filled_amount=685000.0,
            filled_at="2026-06-14T09:31:02",
            account="kis",
            order_price=68300.0,
            order_id="0001234567",
            market_type="kr",
            currency="KRW",
            fill_status="partial",
        )
        embed = format_fill_notification(
            order, display_name="삼성전자", detail_url=None, enrichment=None
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["구분"] == "매수 부분체결"

    def test_no_slippage_when_no_order_price(self):
        from app.monitoring.trade_notifier.formatters_discord import (
            format_fill_notification,
        )
        from app.services.fill_notification import FillOrder

        order = FillOrder(
            symbol="005930",
            side="bid",
            filled_price=68500.0,
            filled_qty=10.0,
            filled_amount=685000.0,
            filled_at="2026-06-14T09:31:02",
            account="kis",
            order_price=None,
            order_id="0001234567",
            market_type="kr",
            currency="KRW",
        )
        embed = format_fill_notification(
            order, display_name="삼성전자", detail_url=None, enrichment=None
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert "vs 주문가" not in fields["체결가"]

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


def _watch_payload(**kw):
    from decimal import Decimal
    from uuid import uuid4

    from app.services.hermes_client import (
        InvestLinks,
        OperatorActionGuidance,
        ReviewTriggerPayload,
    )

    base = {
        "event_uuid": uuid4(),
        "alert_uuid": uuid4(),
        "source_report_uuid": uuid4(),
        "source_item_uuid": uuid4(),
        "correlation_id": "c1",
        "kst_date": "2026-06-15",
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "metric": "price",
        "operator": "below",
        "threshold": Decimal("68000"),
        "threshold_key": "k",
        "intent": "buy_review",
        "action_mode": "notify_only",
        "current_value": Decimal("67500"),
        "scanner_snapshot": {},
        "outcome": "notified",
        "invest_links": InvestLinks(
            report_path="/invest/reports/r1", stock_path="/invest/stocks/kr/005930"
        ),
        "operator_action_guidance": OperatorActionGuidance(
            headline="알림 전용", requires_operator_review=False, order_behavior="none"
        ),
        "price_guidance": None,
        "planned_action": None,
        "trigger_checklist": None,
    }
    base.update(kw)
    return ReviewTriggerPayload(**base)


@pytest.mark.unit
class TestFormatWatchTrigger:
    def test_basic_with_link_and_fields(self):
        from app.monitoring.trade_notifier.formatters_discord import (
            format_investment_watch_trigger,
        )

        emb = format_investment_watch_trigger(
            _watch_payload(), display_name="삼성전자", base_url="https://x.test"
        )
        assert "삼성전자" in emb["title"] and "005930" in emb["title"]
        assert emb["url"] == "https://x.test/invest/stocks/kr/005930"
        fields = {f["name"]: f["value"] for f in emb["fields"]}
        assert (
            "price" in fields["조건"]
            and "below" in fields["조건"]
            and "68000" in fields["조건"]
        )
        assert "67500" in fields["현재값"]

    def test_price_guidance_and_checklist_rendered(self):
        from decimal import Decimal

        from app.monitoring.trade_notifier.formatters_discord import (
            format_investment_watch_trigger,
        )
        from app.services.hermes_client import PriceGuidance

        pg = PriceGuidance(
            entry_review_below_price=Decimal("66000"),
            max_chase_price=Decimal("69000"),
            suggested_limit_price_range=None,
            invalidation=None,
        )
        emb = format_investment_watch_trigger(
            _watch_payload(price_guidance=pg, trigger_checklist=["수급 확인"]),
            display_name="삼성전자",
            base_url="https://x.test",
        )
        names = {f["name"] for f in emb["fields"]}
        assert "가격 가이드" in names and "체크리스트" in names

    def test_no_link_when_invest_links_none(self):
        from app.monitoring.trade_notifier.formatters_discord import (
            format_investment_watch_trigger,
        )

        emb = format_investment_watch_trigger(
            _watch_payload(invest_links=None),
            display_name="삼성전자",
            base_url="https://x.test",
        )
        assert "url" not in emb
