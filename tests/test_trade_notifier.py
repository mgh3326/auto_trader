"""Tests for TradeNotifier."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.monitoring.trade_notifier import TradeNotifier, get_trade_notifier


@pytest.fixture
def trade_notifier():
    """Create a fresh TradeNotifier instance for testing."""
    # Reset singleton
    TradeNotifier._instance = None
    TradeNotifier._initialized = False
    notifier = TradeNotifier()
    yield notifier
    # Cleanup
    TradeNotifier._instance = None
    TradeNotifier._initialized = False


@pytest.mark.unit
def test_singleton_pattern():
    """Test that TradeNotifier follows singleton pattern."""
    notifier1 = get_trade_notifier()
    notifier2 = get_trade_notifier()
    assert notifier1 is notifier2


@pytest.mark.unit
def test_configure(trade_notifier):
    """Test TradeNotifier configuration with Telegram and Discord webhooks."""
    bot_token = "test_token"
    chat_ids = ["123456", "789012"]
    discord_webhook_us = "https://discord.com/api/webhooks/us"
    discord_webhook_kr = "https://discord.com/api/webhooks/kr"
    discord_webhook_crypto = "https://discord.com/api/webhooks/crypto"
    discord_webhook_alerts = "https://discord.com/api/webhooks/alerts"

    trade_notifier.configure(
        bot_token=bot_token,
        chat_ids=chat_ids,
        enabled=True,
        discord_webhook_us=discord_webhook_us,
        discord_webhook_kr=discord_webhook_kr,
        discord_webhook_crypto=discord_webhook_crypto,
        discord_webhook_alerts=discord_webhook_alerts,
    )

    # Verify Telegram configuration
    assert trade_notifier._bot_token == bot_token
    assert trade_notifier._chat_ids == chat_ids

    # Verify Discord webhook configuration
    assert trade_notifier._discord_webhook_us == discord_webhook_us
    assert trade_notifier._discord_webhook_kr == discord_webhook_kr
    assert trade_notifier._discord_webhook_crypto == discord_webhook_crypto
    assert trade_notifier._discord_webhook_alerts == discord_webhook_alerts

    # Verify enabled state and HTTP client
    assert trade_notifier._enabled is True
    assert trade_notifier._http_client is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown(trade_notifier):
    """Test TradeNotifier shutdown."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
    )

    with patch.object(
        trade_notifier._http_client, "aclose", new_callable=AsyncMock
    ) as mock_close:
        await trade_notifier.shutdown()
        mock_close.assert_called_once()
        assert trade_notifier._http_client is None


@pytest.mark.unit
def test_format_buy_notification(trade_notifier):
    """Test buy notification formatting as Discord embed."""
    embed = trade_notifier._format_buy_notification(
        symbol="BTC",
        korean_name="비트코인",
        order_count=3,
        total_amount=300000.0,
        prices=[100000.0, 101000.0, 102000.0],
        volumes=[0.001, 0.001, 0.001],
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "💰 매수 주문 접수"
    assert embed["color"] == 0x00FF00  # Green for buy
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["종목"] == "비트코인 (BTC)"
    assert fields["시장"] == "암호화폐"
    assert fields["주문 수"] == "3건"
    assert fields["총 금액"] == "300,000원"

    # Verify order details
    assert "주문 상세" in fields
    assert "100,000.00원 × 0.001" in fields["주문 상세"]
    assert "101,000.00원 × 0.001" in fields["주문 상세"]
    assert "102,000.00원 × 0.001" in fields["주문 상세"]


@pytest.mark.unit
def test_format_buy_notification_without_details(trade_notifier):
    """Test buy notification formatting without price/volume details."""
    embed = trade_notifier._format_buy_notification(
        symbol="BTC",
        korean_name="비트코인",
        order_count=2,
        total_amount=200000.0,
        prices=[],
        volumes=[],
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "💰 매수 주문 접수"
    assert embed["color"] == 0x00FF00  # Green for buy
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["종목"] == "비트코인 (BTC)"
    assert fields["시장"] == "암호화폐"
    assert fields["주문 수"] == "2건"
    assert fields["총 금액"] == "200,000원"


@pytest.mark.unit
def test_format_sell_notification(trade_notifier):
    """Test sell notification formatting as Discord embed."""
    embed = trade_notifier._format_sell_notification(
        symbol="ETH",
        korean_name="이더리움",
        order_count=2,
        total_volume=0.5,
        prices=[2000000.0, 2100000.0],
        volumes=[0.25, 0.25],
        expected_amount=1025000.0,
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "💸 매도 주문 접수"
    assert embed["color"] == 0xFF0000  # Red for sell
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["종목"] == "이더리움 (ETH)"
    assert fields["시장"] == "암호화폐"
    assert fields["주문 수"] == "2건"
    assert fields["총 수량"] == "0.5"
    assert fields["예상 금액"] == "1,025,000원"

    # Verify order details
    assert "주문 상세" in fields
    assert "2,000,000.00원 × 0.25" in fields["주문 상세"]
    assert "2,100,000.00원 × 0.25" in fields["주문 상세"]


@pytest.mark.unit
def test_format_sell_notification_without_volumes(trade_notifier):
    """Test sell notification formatting as Discord embed with prices but no volumes."""
    embed = trade_notifier._format_sell_notification(
        symbol="ETH",
        korean_name="이더리움",
        order_count=2,
        total_volume=0.5,
        prices=[2000000.0, 2100000.0],
        volumes=[],
        expected_amount=1025000.0,
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "💸 매도 주문 접수"
    assert embed["color"] == 0xFF0000  # Red for sell
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["종목"] == "이더리움 (ETH)"
    assert fields["시장"] == "암호화폐"
    assert fields["주문 수"] == "2건"
    assert fields["총 수량"] == "0.5"
    assert fields["예상 금액"] == "1,025,000원"

    # Verify price range (no volumes, so shows price range)
    assert "매도 가격대" in fields
    assert "2,000,000.00원" in fields["매도 가격대"]
    assert "2,100,000.00원" in fields["매도 가격대"]


@pytest.mark.unit
def test_format_cancel_notification(trade_notifier):
    """Test cancel notification formatting as Discord embed."""
    embed = trade_notifier._format_cancel_notification(
        symbol="XRP",
        korean_name="리플",
        cancel_count=5,
        order_type="매수",
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "🚫 주문 취소"
    assert embed["color"] == 0xFFFF00  # Yellow for cancel
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["종목"] == "리플 (XRP)"
    assert fields["시장"] == "암호화폐"
    assert fields["취소 유형"] == "매수"
    assert fields["취소 건수"] == "5건"


@pytest.mark.unit
def test_format_analysis_notification(trade_notifier):
    """Test analysis notification formatting as Discord embed."""
    embed = trade_notifier._format_analysis_notification(
        symbol="BTC",
        korean_name="비트코인",
        decision="buy",
        confidence=85.5,
        reasons=[
            "상승 추세 지속",
            "거래량 증가",
            "기술적 지표 긍정적",
        ],
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "📊 AI 분석 완료"
    assert embed["color"] == 0x0000FF  # Blue for analysis
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["종목"] == "비트코인 (BTC)"
    assert fields["시장"] == "암호화폐"
    assert fields["판단"] == "🟢 매수"
    assert fields["신뢰도"] == "85.5%"

    # Verify reasons (numbered list)
    assert "주요 근거" in fields
    assert "1. 상승 추세 지속" in fields["주요 근거"]
    assert "2. 거래량 증가" in fields["주요 근거"]
    assert "3. 기술적 지표 긍정적" in fields["주요 근거"]


@pytest.mark.unit
def test_format_analysis_notification_hold(trade_notifier):
    """Test analysis notification formatting for hold decision as Discord embed."""
    embed = trade_notifier._format_analysis_notification(
        symbol="ETH",
        korean_name="이더리움",
        decision="hold",
        confidence=70.0,
        reasons=["시장 관망"],
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "📊 AI 분석 완료"
    assert embed["color"] == 0x0000FF  # Blue for analysis
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["종목"] == "이더리움 (ETH)"
    assert fields["판단"] == "🟡 보유"
    assert fields["신뢰도"] == "70.0%"


@pytest.mark.unit
def test_format_analysis_notification_sell(trade_notifier):
    """Test analysis notification formatting for sell decision as Discord embed."""
    embed = trade_notifier._format_analysis_notification(
        symbol="XRP",
        korean_name="리플",
        decision="sell",
        confidence=90.0,
        reasons=["하락 전망"],
        market_type="암호화폐",
    )

    # Verify embed structure
    assert embed["title"] == "📊 AI 분석 완료"
    assert embed["color"] == 0x0000FF  # Blue for analysis
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert fields["종목"] == "리플 (XRP)"
    assert fields["판단"] == "🔴 매도"
    assert fields["신뢰도"] == "90.0%"


@pytest.mark.unit
def test_format_automation_summary(trade_notifier):
    """Test automation summary notification formatting as Discord embed."""
    embed = trade_notifier._format_automation_summary(
        total_coins=10,
        analyzed=10,
        bought=3,
        sold=2,
        errors=0,
        duration_seconds=45.5,
    )

    # Verify embed structure
    assert embed["title"] == "🤖 자동 거래 실행 완료"
    assert embed["color"] == 0x00FFFF  # Cyan for automation
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["처리 종목"] == "10개"
    assert fields["분석 완료"] == "10개"
    assert fields["매수 주문"] == "3건"
    assert fields["매도 주문"] == "2건"
    assert fields["실행 시간"] == "45.5초"


@pytest.mark.unit
def test_format_automation_summary_with_errors(trade_notifier):
    """Test automation summary notification with errors as Discord embed."""
    embed = trade_notifier._format_automation_summary(
        total_coins=5,
        analyzed=5,
        bought=1,
        sold=1,
        errors=2,
        duration_seconds=30.0,
    )

    # Verify embed structure
    assert embed["title"] == "🤖 자동 거래 실행 완료"
    assert embed["color"] == 0x00FFFF  # Cyan for automation
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["처리 종목"] == "5개"
    assert fields["분석 완료"] == "5개"
    assert fields["매수 주문"] == "1건"
    assert fields["매도 주문"] == "1건"
    assert fields["실행 시간"] == "30.0초"
    assert fields["오류 발생"] == "2건"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_buy_order_disabled(trade_notifier):
    """Test that notifications are not sent when disabled."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=False,
    )

    result = await trade_notifier.notify_buy_order(
        symbol="BTC",
        korean_name="비트코인",
        order_count=1,
        total_amount=100000.0,
        prices=[],
        volumes=[],
    )

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_buy_order_success(trade_notifier):
    """Test successful buy order notification."""
    webhook_url = "https://discord.com/api/webhooks/crypto"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_crypto=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_buy_order(
            symbol="BTC",
            korean_name="비트코인",
            order_count=1,
            total_amount=100000.0,
            prices=[100000.0],
            volumes=[0.001],
        )

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == webhook_url
        assert "embeds" in call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_test_connection_success(trade_notifier):
    """Test successful connection test."""
    webhook_url = "https://discord.com/api/webhooks/crypto"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_crypto=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.test_connection()

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == webhook_url
        assert "embeds" in call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_test_connection_disabled(trade_notifier):
    """Test connection test when disabled."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=False,
    )

    result = await trade_notifier.test_connection()
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_to_correct_webhook_by_market_type(trade_notifier):
    """Test that notifications are sent to the correct Discord webhook based on market type."""
    webhook_us = "https://discord.com/api/webhooks/us"
    webhook_kr = "https://discord.com/api/webhooks/kr"
    webhook_crypto = "https://discord.com/api/webhooks/crypto"

    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_us=webhook_us,
        discord_webhook_kr=webhook_kr,
        discord_webhook_crypto=webhook_crypto,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        # Test crypto market
        await trade_notifier.notify_buy_order(
            symbol="BTC",
            korean_name="비트코인",
            order_count=1,
            total_amount=100000.0,
            prices=[100000.0],
            volumes=[0.001],
            market_type="암호화폐",
        )
        assert mock_post.call_args.args[0] == webhook_crypto

        # Test US market
        await trade_notifier.notify_buy_order(
            symbol="AAPL",
            korean_name="애플",
            order_count=1,
            total_amount=1000.0,
            prices=[180.0],
            volumes=[5],
            market_type="해외주식",
        )
        assert mock_post.call_args.args[0] == webhook_us

        # Test KR market
        await trade_notifier.notify_buy_order(
            symbol="005930",
            korean_name="삼성전자",
            order_count=1,
            total_amount=1000000.0,
            prices=[80000.0],
            volumes=[10],
            market_type="국내주식",
        )
        assert mock_post.call_args.args[0] == webhook_kr


@pytest.mark.unit
def test_market_type_routing(trade_notifier):
    """Test that market_type is correctly routed to appropriate Discord webhook."""
    webhook_us = "https://discord.com/api/webhooks/us"
    webhook_kr = "https://discord.com/api/webhooks/kr"
    webhook_crypto = "https://discord.com/api/webhooks/crypto"

    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_us=webhook_us,
        discord_webhook_kr=webhook_kr,
        discord_webhook_crypto=webhook_crypto,
    )

    # Test US market (English)
    assert trade_notifier._get_webhook_for_market_type("US") == webhook_us

    # Test US market (Korean)
    assert trade_notifier._get_webhook_for_market_type("해외주식") == webhook_us

    # Test KR market
    assert trade_notifier._get_webhook_for_market_type("국내주식") == webhook_kr

    # Test crypto market
    assert trade_notifier._get_webhook_for_market_type("암호화폐") == webhook_crypto

    # Test unknown market type
    assert trade_notifier._get_webhook_for_market_type("unknown") is None

    # Test with whitespace
    assert trade_notifier._get_webhook_for_market_type("  해외주식  ") == webhook_us


@pytest.mark.unit
def test_format_failure_notification(trade_notifier):
    """Test failure notification formatting as Discord embed."""
    embed = trade_notifier._format_failure_notification(
        symbol="AAPL",
        korean_name="애플",
        reason="APBK0656 해당종목정보가 없습니다.",
        market_type="해외주식",
    )

    # Verify embed structure
    assert embed["title"] == "⚠️ 거래 실패"
    assert embed["color"] == 0xFF6600  # Orange for failure
    assert "🕒" in embed["description"]

    # Verify fields
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["종목"] == "애플 (AAPL)"
    assert fields["시장"] == "해외주식"
    assert fields["사유"] == "APBK0656 해당종목정보가 없습니다."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_trade_failure_success(trade_notifier):
    """Test successful trade failure notification."""
    webhook_url = "https://discord.com/api/webhooks/us"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_us=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_trade_failure(
            symbol="VOO",
            korean_name="VOO",
            reason="매도 주문 실패: APBK0656 해당종목정보가 없습니다.",
            market_type="해외주식",
        )

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == webhook_url
        assert "embeds" in call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_trade_failure_disabled(trade_notifier):
    """Test that trade failure notifications are not sent when disabled."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=False,
    )

    result = await trade_notifier.notify_trade_failure(
        symbol="VOO",
        korean_name="VOO",
        reason="매도 주문 실패",
        market_type="해외주식",
    )

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_sell_order_success(trade_notifier):
    """Test successful sell order notification."""
    webhook_url = "https://discord.com/api/webhooks/us"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_us=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_sell_order(
            symbol="AAPL",
            korean_name="애플",
            order_count=2,
            total_volume=5,
            prices=[180.0, 185.0],
            volumes=[2, 3],
            expected_amount=920.0,
            market_type="해외주식",
        )

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == webhook_url
        assert "embeds" in call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_sell_order_disabled(trade_notifier):
    """Test that sell order notifications are not sent when disabled."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=False,
    )

    result = await trade_notifier.notify_sell_order(
        symbol="AAPL",
        korean_name="애플",
        order_count=1,
        total_volume=5,
        prices=[180.0],
        volumes=[5],
        expected_amount=900.0,
        market_type="해외주식",
    )

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_openclaw_message_success(trade_notifier):
    """Test successful OpenClaw message forwarding."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_openclaw_message("scan message")

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.kwargs["json"]["text"] == "scan message"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_openclaw_message_disabled(trade_notifier):
    """Test OpenClaw forwarding when notifications are disabled."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=False,
    )

    result = await trade_notifier.notify_openclaw_message("scan message")

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_to_discord_success(trade_notifier):
    """Test successful Discord webhook HTTP POST behavior."""
    webhook_url = "https://discord.com/api/webhooks/test"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_crypto=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    # Create a sample embed
    embed = {
        "title": "Test Notification",
        "description": "Test message",
        "color": 0x00FF00,
        "fields": [],
    }

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier._send_to_discord_embed_single(embed, webhook_url)

        # Verify success
        assert result is True

        # Verify HTTP POST was called
        mock_post.assert_called_once()
        call_args = mock_post.call_args

        # Verify URL
        assert call_args.args[0] == webhook_url

        # Verify JSON payload
        assert "json" in call_args.kwargs
        json_data = call_args.kwargs["json"]
        assert "embeds" in json_data
        assert json_data["embeds"] == [embed]

        # Verify headers
        assert "headers" in call_args.kwargs
        headers = call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/json"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_to_discord_failure(trade_notifier):
    """Test Discord webhook HTTP POST failure."""
    webhook_url = "https://discord.com/api/webhooks/test"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_crypto=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(side_effect=Exception("Network error"))

    # Create a sample embed
    embed = {
        "title": "Test Notification",
        "description": "Test message",
        "color": 0x00FF00,
        "fields": [],
    }

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier._send_to_discord_embed_single(embed, webhook_url)

        # Verify failure returns False
        assert result is False

        # Verify HTTP POST was attempted
        mock_post.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_to_discord_disabled(trade_notifier):
    """Test that Discord webhook is not sent when disabled."""
    webhook_url = "https://discord.com/api/webhooks/test"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=False,
    )

    # Create a sample embed
    embed = {
        "title": "Test Notification",
        "description": "Test message",
        "color": 0x00FF00,
        "fields": [],
    }

    result = await trade_notifier._send_to_discord_embed_single(embed, webhook_url)

    # Verify returns False when disabled
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_to_discord_no_webhook(trade_notifier):
    """Test that Discord webhook is not sent when no webhook URL provided."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
    )

    # Create a sample embed
    embed = {
        "title": "Test Notification",
        "description": "Test message",
        "color": 0x00FF00,
        "fields": [],
    }

    result = await trade_notifier._send_to_discord_embed_single(embed, "")

    # Verify returns False when no webhook URL provided
    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_fallback(trade_notifier):
    """Test Telegram fallback when Discord is not configured."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    # Mock the httpx.AsyncClient to avoid proxy issues
    with patch("app.monitoring.trade_notifier.httpx.AsyncClient") as mock_client_init:
        # Create a mock client instance
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()
        mock_client_init.return_value = mock_client

        # Configure with Telegram only (no Discord webhooks)
        trade_notifier.configure(
            bot_token="test_token",
            chat_ids=["123456"],
            enabled=True,
        )

    # Call notify_buy_order without Discord configured
    result = await trade_notifier.notify_buy_order(
        symbol="BTC",
        korean_name="비트코인",
        order_count=2,
        total_amount=200000.0,
        prices=[100000.0, 100000.0],
        volumes=[0.001, 0.001],
        market_type="암호화폐",
    )

    # Should succeed via Telegram fallback
    assert result is True
    mock_client.post.assert_called_once()

    # Verify Telegram API was called (not Discord webhook)
    call_args = mock_client.post.call_args
    url = call_args.args[0]
    assert url == "https://api.telegram.org/bottest_token/sendMessage"

    # Verify the message contains Telegram markdown formatting
    json_data = call_args.kwargs["json"]
    assert "text" in json_data
    assert json_data["parse_mode"] == "Markdown"
    assert "💰 매수 주문 접수" in json_data["text"]
    assert "비트코인 \\(BTC\\)" in json_data["text"]


# =============================================================================
# Discord-first tests for notify_openclaw_message and notify_automation_summary
# =============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_openclaw_message_discord_first(trade_notifier):
    """Test OpenClaw message sends to Discord alerts webhook first."""
    webhook_url = "https://discord.com/api/webhooks/alerts"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_alerts=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_openclaw_message("OpenClaw message")

        assert result is True
        mock_post.assert_called_once()

        # Verify Discord webhook was called with content (not embeds)
        call_args = mock_post.call_args
        assert call_args.args[0] == webhook_url
        assert call_args.kwargs["json"]["content"] == "OpenClaw message"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_openclaw_message_discord_fallback_to_telegram(trade_notifier):
    """Test OpenClaw message falls back to Telegram when Discord fails."""
    webhook_url = "https://discord.com/api/webhooks/alerts"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_alerts=webhook_url,
    )

    # Discord failure, Telegram success
    mock_discord_response = MagicMock()
    mock_discord_response.raise_for_status = MagicMock(
        side_effect=Exception("Discord error")
    )
    mock_telegram_response = MagicMock()
    mock_telegram_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        side_effect=[mock_discord_response, mock_telegram_response],
    ) as mock_post:
        result = await trade_notifier.notify_openclaw_message(
            "OpenClaw message", parse_mode="HTML"
        )

        assert result is True
        assert mock_post.call_count == 2

        # First call was to Discord
        discord_call = mock_post.call_args_list[0]
        assert discord_call.args[0] == webhook_url
        assert "content" in discord_call.kwargs["json"]

        # Second call was to Telegram
        telegram_call = mock_post.call_args_list[1]
        assert "api.telegram.org" in telegram_call.args[0]
        assert telegram_call.kwargs["json"]["text"] == "OpenClaw message"
        assert telegram_call.kwargs["json"]["parse_mode"] == "HTML"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_openclaw_message_telegram_only(trade_notifier):
    """Test OpenClaw message uses Telegram when no Discord webhook configured."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        # No discord_webhook_alerts configured
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_openclaw_message("OpenClaw message")

        assert result is True
        mock_post.assert_called_once()

        # Verify Telegram was called directly
        call_args = mock_post.call_args
        assert "api.telegram.org" in call_args.args[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_automation_summary_discord_first(trade_notifier):
    """Test automation summary sends to Discord alerts webhook first."""
    webhook_url = "https://discord.com/api/webhooks/alerts"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_alerts=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_automation_summary(
            total_coins=10,
            analyzed=10,
            bought=3,
            sold=2,
            errors=0,
            duration_seconds=45.5,
        )

        assert result is True
        mock_post.assert_called_once()

        # Verify Discord webhook was called with embeds
        call_args = mock_post.call_args
        assert call_args.args[0] == webhook_url
        json_data = call_args.kwargs["json"]
        assert "embeds" in json_data
        embed = json_data["embeds"][0]
        assert embed["title"] == "🤖 자동 거래 실행 완료"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_automation_summary_discord_fallback_to_telegram(trade_notifier):
    """Test automation summary falls back to Telegram when Discord fails."""
    webhook_url = "https://discord.com/api/webhooks/alerts"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_alerts=webhook_url,
    )

    # Discord failure, Telegram success
    mock_discord_response = MagicMock()
    mock_discord_response.raise_for_status = MagicMock(
        side_effect=Exception("Discord error")
    )
    mock_telegram_response = MagicMock()
    mock_telegram_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        side_effect=[mock_discord_response, mock_telegram_response],
    ) as mock_post:
        result = await trade_notifier.notify_automation_summary(
            total_coins=10,
            analyzed=10,
            bought=3,
            sold=2,
            errors=1,
            duration_seconds=45.5,
        )

        assert result is True
        assert mock_post.call_count == 2

        # First call was to Discord with embeds
        discord_call = mock_post.call_args_list[0]
        assert discord_call.args[0] == webhook_url
        assert "embeds" in discord_call.kwargs["json"]

        # Second call was to Telegram with markdown text
        telegram_call = mock_post.call_args_list[1]
        assert "api.telegram.org" in telegram_call.args[0]
        text = telegram_call.kwargs["json"]["text"]
        assert "*🤖 자동 거래 실행 완료*" in text
        assert "*오류 발생:* 1건" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notify_automation_summary_telegram_only(trade_notifier):
    """Test automation summary uses Telegram when no Discord webhook configured."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        # No discord_webhook_alerts configured
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier.notify_automation_summary(
            total_coins=10,
            analyzed=10,
            bought=3,
            sold=2,
            errors=0,
            duration_seconds=45.5,
        )

        assert result is True
        mock_post.assert_called_once()

        # Verify Telegram was called directly
        call_args = mock_post.call_args
        assert "api.telegram.org" in call_args.args[0]
        text = call_args.kwargs["json"]["text"]
        assert "*🤖 자동 거래 실행 완료*" in text


@pytest.mark.unit
def test_format_automation_summary_telegram(trade_notifier):
    """Test automation summary formatting for Telegram."""
    message = trade_notifier._format_automation_summary_telegram(
        total_coins=10,
        analyzed=10,
        bought=3,
        sold=2,
        errors=0,
        duration_seconds=45.5,
    )

    # Verify markdown formatting
    assert "*🤖 자동 거래 실행 완료*" in message
    assert "*처리 종목:* 10개" in message
    assert "*분석 완료:* 10개" in message
    assert "*매수 주문:* 3건" in message
    assert "*매도 주문:* 2건" in message
    assert "*실행 시간:* 45.5초" in message
    assert "오류" not in message  # No errors section when errors=0


@pytest.mark.unit
def test_format_automation_summary_telegram_with_errors(trade_notifier):
    """Test automation summary formatting for Telegram with errors."""
    message = trade_notifier._format_automation_summary_telegram(
        total_coins=5,
        analyzed=5,
        bought=1,
        sold=1,
        errors=2,
        duration_seconds=30.0,
    )

    # Verify markdown formatting including errors
    assert "*🤖 자동 거래 실행 완료*" in message
    assert "*오류 발생:* 2건" in message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_to_discord_content_single_success(trade_notifier):
    """Test sending plain text content to a specific Discord webhook."""
    webhook_url = "https://discord.com/api/webhooks/alerts"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_alerts=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier._send_to_discord_content_single(
            "Plain text message", webhook_url
        )

        assert result is True
        mock_post.assert_called_once()

        # Verify content format (not embeds)
        call_args = mock_post.call_args
        assert call_args.args[0] == webhook_url
        assert call_args.kwargs["json"]["content"] == "Plain text message"
        assert "embeds" not in call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_to_discord_content_single_failure(trade_notifier):
    """Test Discord content send failure."""
    webhook_url = "https://discord.com/api/webhooks/alerts"
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
        discord_webhook_alerts=webhook_url,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(side_effect=Exception("Network error"))

    with patch.object(
        trade_notifier._http_client,
        "post",
        new_callable=AsyncMock,
        return_value=mock_response,
    ) as mock_post:
        result = await trade_notifier._send_to_discord_content_single(
            "Plain text message", webhook_url
        )

        assert result is False
        mock_post.assert_called_once()
