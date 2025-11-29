"""Tests for TradeNotifier."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
    """Test TradeNotifier configuration."""
    bot_token = "test_token"
    chat_ids = ["123456", "789012"]

    trade_notifier.configure(
        bot_token=bot_token,
        chat_ids=chat_ids,
        enabled=True,
    )

    assert trade_notifier._bot_token == bot_token
    assert trade_notifier._chat_ids == chat_ids
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

    with patch.object(trade_notifier._http_client, "aclose", new_callable=AsyncMock) as mock_close:
        await trade_notifier.shutdown()
        mock_close.assert_called_once()
        assert trade_notifier._http_client is None


@pytest.mark.unit
def test_format_buy_notification(trade_notifier):
    """Test buy notification formatting."""
    message = trade_notifier._format_buy_notification(
        symbol="BTC",
        korean_name="비트코인",
        order_count=3,
        total_amount=300000.0,
        prices=[100000.0, 101000.0, 102000.0],
        volumes=[0.001, 0.001, 0.001],
        market_type="암호화폐",
    )

    assert "💰 *매수 주문 접수*" in message
    assert "비트코인 (BTC)" in message
    assert "3건" in message
    assert "300,000원" in message
    assert "100,000.00원" in message


@pytest.mark.unit
def test_format_buy_notification_without_details(trade_notifier):
    """Test buy notification formatting without price/volume details."""
    message = trade_notifier._format_buy_notification(
        symbol="BTC",
        korean_name="비트코인",
        order_count=2,
        total_amount=200000.0,
        prices=[],
        volumes=[],
        market_type="암호화폐",
    )

    assert "💰 *매수 주문 접수*" in message
    assert "비트코인 (BTC)" in message
    assert "2건" in message
    assert "200,000원" in message


@pytest.mark.unit
def test_format_sell_notification(trade_notifier):
    """Test sell notification formatting."""
    message = trade_notifier._format_sell_notification(
        symbol="ETH",
        korean_name="이더리움",
        order_count=2,
        total_volume=0.5,
        prices=[2000000.0, 2100000.0],
        volumes=[0.25, 0.25],
        expected_amount=1025000.0,
        market_type="암호화폐",
    )

    assert "💸 *매도 주문 접수*" in message
    assert "이더리움 (ETH)" in message
    assert "2건" in message
    assert "0.5" in message
    assert "1,025,000원" in message


@pytest.mark.unit
def test_format_sell_notification_without_volumes(trade_notifier):
    """Test sell notification formatting with prices but no volumes."""
    message = trade_notifier._format_sell_notification(
        symbol="ETH",
        korean_name="이더리움",
        order_count=2,
        total_volume=0.5,
        prices=[2000000.0, 2100000.0],
        volumes=[],
        expected_amount=1025000.0,
        market_type="암호화폐",
    )

    assert "💸 *매도 주문 접수*" in message
    assert "이더리움 (ETH)" in message
    assert "*매도 가격대:*" in message
    assert "2,000,000.00원" in message


@pytest.mark.unit
def test_format_cancel_notification(trade_notifier):
    """Test cancel notification formatting."""
    message = trade_notifier._format_cancel_notification(
        symbol="XRP",
        korean_name="리플",
        cancel_count=5,
        order_type="매수",
        market_type="암호화폐",
    )

    assert "🚫 *주문 취소*" in message
    assert "리플 (XRP)" in message
    assert "5건" in message
    assert "매수" in message


@pytest.mark.unit
def test_format_analysis_notification(trade_notifier):
    """Test analysis notification formatting."""
    message = trade_notifier._format_analysis_notification(
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

    assert "🟢 *AI 분석 완료*" in message
    assert "비트코인 (BTC)" in message
    assert "매수" in message
    assert "85.5%" in message
    assert "상승 추세 지속" in message


@pytest.mark.unit
def test_format_analysis_notification_hold(trade_notifier):
    """Test analysis notification formatting for hold decision."""
    message = trade_notifier._format_analysis_notification(
        symbol="ETH",
        korean_name="이더리움",
        decision="hold",
        confidence=70.0,
        reasons=["시장 관망"],
        market_type="암호화폐",
    )

    assert "🟡 *AI 분석 완료*" in message
    assert "보유" in message
    assert "70.0%" in message


@pytest.mark.unit
def test_format_analysis_notification_sell(trade_notifier):
    """Test analysis notification formatting for sell decision."""
    message = trade_notifier._format_analysis_notification(
        symbol="XRP",
        korean_name="리플",
        decision="sell",
        confidence=90.0,
        reasons=["하락 전망"],
        market_type="암호화폐",
    )

    assert "🔴 *AI 분석 완료*" in message
    assert "매도" in message


@pytest.mark.unit
def test_format_automation_summary(trade_notifier):
    """Test automation summary notification formatting."""
    message = trade_notifier._format_automation_summary(
        total_coins=10,
        analyzed=10,
        bought=3,
        sold=2,
        errors=0,
        duration_seconds=45.5,
    )

    assert "🤖 *자동 거래 실행 완료*" in message
    assert "10개" in message
    assert "3건" in message
    assert "2건" in message
    assert "45.5초" in message


@pytest.mark.unit
def test_format_automation_summary_with_errors(trade_notifier):
    """Test automation summary notification with errors."""
    message = trade_notifier._format_automation_summary(
        total_coins=5,
        analyzed=5,
        bought=1,
        sold=1,
        errors=2,
        duration_seconds=30.0,
    )

    assert "🤖 *자동 거래 실행 완료*" in message
    assert "⚠️ *오류 발생:* 2건" in message


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
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client, "post", new_callable=AsyncMock, return_value=mock_response
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
        assert "chat_id" in call_args.kwargs["json"]
        assert "text" in call_args.kwargs["json"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_test_connection_success(trade_notifier):
    """Test successful connection test."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456"],
        enabled=True,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client, "post", new_callable=AsyncMock, return_value=mock_response
    ) as mock_post:
        result = await trade_notifier.test_connection()

        assert result is True
        mock_post.assert_called_once()


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
async def test_send_to_multiple_chats(trade_notifier):
    """Test sending notifications to multiple chat IDs."""
    trade_notifier.configure(
        bot_token="test_token",
        chat_ids=["123456", "789012", "345678"],
        enabled=True,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    with patch.object(
        trade_notifier._http_client, "post", new_callable=AsyncMock, return_value=mock_response
    ) as mock_post:
        result = await trade_notifier.notify_buy_order(
            symbol="BTC",
            korean_name="비트코인",
            order_count=1,
            total_amount=100000.0,
            prices=[],
            volumes=[],
        )

        assert result is True
        # Should be called once per chat ID
        assert mock_post.call_count == 3
